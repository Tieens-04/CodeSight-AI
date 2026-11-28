"""Milestone 3 Full-Pipeline E2E Gate: 10 PRs (5 Py + 5 TS/TSX), Webhook & CLI, SHA Cache,
Commit Suggestion Build/Test Verification, and Opt-in Live Gemini Free Tier + Langfuse Cloud.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections import defaultdict, deque
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal

import httpx
import jwt
import pytest
import redis.asyncio as redis_async
import sqlalchemy as sa
from alembic import command
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from tree_sitter import Parser

from codesight.agents.llm_factory import LLMFactory, StructuredInvocationResult
from codesight.agents.schemas import (
    LLMAuditorDraftOutput,
    LLMCodeSuggestionDraft,
    LLMCriticDraftOutput,
    LLMCriticVerdictDraft,
    LLMFindingDraft,
)
from codesight.api.main import create_app
from codesight.api.routes.webhooks import CeleryWebhookDispatcher
from codesight.ast_engine.registry import ASTParserRegistry
from codesight.ast_engine.suggestion_validator import (
    TreeSitterSuggestionValidator,
    _scan_fenced_blocks,
    splice_source_lines,
)
from codesight.cli.commands.index import run_cli_index
from codesight.cli.commands.review import parse_github_pr_url, run_cli_ai_review
from codesight.core.config import Settings
from codesight.core.ephemeral import ephemeral_workspace, resolve_safe_subpath
from codesight.db.models import PRReview, Repository, ReviewFinding
from codesight.db.session import create_session_factory
from codesight.rag.embedder import GeminiEmbedder
from codesight.rag.models import (
    DEFAULT_EMBED_BATCH_SIZE,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL_NAME,
)
from codesight.rag.qdrant_store import QdrantZeroCodeStore
from codesight.worker.pr_review_runner import Sprint3PRReviewRunner
from tests._helpers import (
    RecordingCelery,
    RecordingSpanExporter,
    b64_encode,
    generate_ephemeral_rsa_pair,
    make_alembic_config,
    sha1_blob,
    sign_github_body,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_WEBHOOK_SECRET = "ca22-m3-webhook-secret"
PY_REPO_SLUG = "codesight-m3/fastapi-commerce-api"
TS_REPO_SLUG = "codesight-m3/react-checkout-web"
PY_GH_ID = 880101
TS_GH_ID = 880202

CANARY_PY_BODY = "CODESIGHT_NFR3_M3_CANARY_PY_BODY_91f2a3b4"
CANARY_PY_DOCSTRING = "CODESIGHT_NFR3_M3_CANARY_PY_DOC_55c1d8e9"
CANARY_PY_COMMENT = "CODESIGHT_NFR3_M3_CANARY_PY_COMMENT_20ab77e1"
CANARY_TS_BODY = "CODESIGHT_NFR3_M3_CANARY_TS_BODY_44fe09c2"
CANARY_TS_JSDOC = "CODESIGHT_NFR3_M3_CANARY_TS_JSDOC_66da12f3"
CANARY_MD_ADR = "CODESIGHT_NFR3_M3_CANARY_MD_ADR_77bc34a5"

ALL_M3_CANARIES: tuple[str, ...] = (
    CANARY_PY_BODY,
    CANARY_PY_DOCSTRING,
    CANARY_PY_COMMENT,
    CANARY_TS_BODY,
    CANARY_TS_JSDOC,
    CANARY_MD_ADR,
)


@asynccontextmanager
async def _isolated_session_factory(dsn: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    await asyncio.to_thread(command.upgrade, make_alembic_config(dsn, REPO_ROOT), "head")
    engine = create_async_engine(dsn, pool_size=5, pool_pre_ping=True)
    factory = create_session_factory(engine)
    try:
        yield factory
    finally:
        await engine.dispose()


def _semantic_vec(text: str) -> list[float]:
    lower = text.lower()
    vec = [0.0] * EMBEDDING_DIMENSIONS
    buckets: tuple[tuple[tuple[str, ...], int], ...] = (
        (("order", "find_orders_by_status", "sql", "order_repo"), 0),
        (("n+1", "get_order_items", "batch_get_order_items", "order_service"), 8),
        (("billing", "layered-architecture", "process_billing_summary", "adr"), 16),
        (("tenant", "verify_tenant_access", "auth_service", "idor"), 24),
        (("receipt", "receiptbanner", "receiptclient", "sanitize"), 32),
        (("cart", "usecartsync", "fetchcartitemsbatch", "fetchcartitemdetail"), 40),
        (("payment", "paymentpanel", "paymentclient", "ui-layer-boundaries"), 48),
        (("checkout", "usecheckoutsession", "abortcontroller", "useeffect"), 56),
    )
    for keywords, base_idx in buckets:
        hits = sum(1 for kw in keywords if kw in lower)
        if hits > 0:
            for off in range(8):
                vec[base_idx + off] = float(hits) * 4.0
    digest = hashlib.sha256(lower.encode("utf-8")).digest()
    for i in range(16):
        vec[64 + i] = ((float(digest[i]) / 255.0) - 0.5) * 0.25 + 0.05
    norm = sum(v * v for v in vec) ** 0.5 or 1.0
    return [v / norm for v in vec]


class _DeterministicGenAIClient:
    def __init__(self) -> None:
        self.embed_calls = 0
        self.aio = SimpleNamespace(models=self)

    async def embed_content(self, *, model: str, contents: list[str], config: Any = None) -> Any:
        assert model == EMBEDDING_MODEL_NAME
        assert 1 <= len(contents) <= DEFAULT_EMBED_BATCH_SIZE
        assert config is not None and config.output_dimensionality == EMBEDDING_DIMENSIONS
        self.embed_calls += 1
        return SimpleNamespace(
            embeddings=[SimpleNamespace(values=_semantic_vec(txt)) for txt in contents]
        )


def _build_python_base_repo() -> dict[str, str]:
    return {
        ".codesight.yaml": "min_debt_score: 75\nreview_action: REQUEST_CHANGES\n",
        "docs/adr/0001-layered-architecture.md": (
            "# ADR 0001: Layered Architecture\n\n"
            "## Router & Repository Boundaries\n\n"
            f"<!-- {CANARY_MD_ADR} -->\n"
            "Routers must delegate all persistence to Services (`order_service`). "
            "Direct `db_session.execute` or SQL queries inside `src/routers/` are forbidden.\n"
        ),
        "src/__init__.py": "",
        "src/repositories/__init__.py": "",
        "src/repositories/order_repo.py": (
            "from __future__ import annotations\n"
            "\n"
            "import sqlite3\n"
            "from collections.abc import Sequence\n"
            "\n"
            "def find_orders_by_status(\n"
            "    conn: sqlite3.Connection, status_filter: str\n"
            ") -> list[tuple[int, int]]:\n"
            f'    """Fetch order ids and totals safely. ({CANARY_PY_DOCSTRING})"""\n'
            f"    canary_marker = '{CANARY_PY_BODY}'\n"
            "    _ = canary_marker\n"
            "    sql = 'SELECT id, total FROM orders WHERE status = ?'\n"
            "    return list(conn.execute(sql, (status_filter,)).fetchall())\n"
            "\n"
            "async def get_order_items(order_id: int) -> list[str]:\n"
            "    return [f'item-{order_id}']\n"
            "\n"
            "async def batch_get_order_items(order_ids: Sequence[int]) -> dict[int, list[str]]:\n"
            "    return {oid: [f'item-{oid}'] for oid in order_ids}\n"
        ),
        "src/services/__init__.py": "",
        "src/services/order_service.py": (
            "from __future__ import annotations\n"
            "\n"
            "import sqlite3\n"
            "from collections.abc import Sequence\n"
            "\n"
            "from src.repositories import order_repo\n"
            "\n"
            "def list_customer_orders(\n"
            "    conn: sqlite3.Connection, status_filter: str\n"
            ") -> list[tuple[int, int]]:\n"
            "    return order_repo.find_orders_by_status(conn, status_filter)\n"
            "\n"
            "async def list_orders_with_items(order_ids: Sequence[int]) -> dict[int, list[str]]:\n"
            "    return await order_repo.batch_get_order_items(order_ids)\n"
            "\n"
            "def process_billing_summary(conn: sqlite3.Connection, tenant_id: str) -> int:\n"
            "    sql = 'SELECT COUNT(*) FROM invoices WHERE tenant_id = ?'\n"
            "    row = conn.execute(sql, (tenant_id,)).fetchone()\n"
            "    return int(row[0]) if row is not None else 0\n"
        ),
        "src/services/auth_service.py": (
            "from __future__ import annotations\n"
            "\n"
            f"# {CANARY_PY_COMMENT}\n"
            "async def verify_tenant_access(current_tenant: str, requested_tenant: str) -> bool:\n"
            "    if current_tenant:\n"
            "        if requested_tenant != current_tenant:\n"
            "            raise PermissionError('Cross-tenant access denied')\n"
            "    return True\n"
        ),
        "src/routers/__init__.py": "",
        "src/routers/orders.py": (
            "from __future__ import annotations\n"
            "\n"
            "import sqlite3\n"
            "from src.services.order_service import list_customer_orders\n"
            "\n"
            "def get_orders_endpoint(\n"
            "    conn: sqlite3.Connection, status_filter: str\n"
            ") -> list[tuple[int, int]]:\n"
            "    return list_customer_orders(conn, status_filter)\n"
        ),
        "src/routers/billing.py": (
            "from __future__ import annotations\n"
            "\n"
            "import sqlite3\n"
            "from src.services import order_service\n"
            "\n"
            "def get_billing_endpoint(conn: sqlite3.Connection, tenant_id: str) -> int:\n"
            "    return order_service.process_billing_summary(conn, tenant_id)\n"
        ),
        "src/services/discount_service.py": (
            "from __future__ import annotations\n"
            "\n"
            "def compute_discount_cents(subtotal_cents: int, pct: int) -> int:\n"
            "    if pct < 0 or pct > 50:\n"
            "        raise ValueError('invalid discount')\n"
            "    return (subtotal_cents * pct) // 100\n"
        ),
        "tests/__init__.py": "",
        "tests/test_commerce.py": (
            "from __future__ import annotations\n"
            "\n"
            "import asyncio\n"
            "import sqlite3\n"
            "import pytest\n"
            "from src.repositories import order_repo\n"
            "from src.routers import billing, orders\n"
            "from src.services import auth_service, discount_service, order_service\n"
            "\n"
            "def _seed_db() -> sqlite3.Connection:\n"
            "    conn = sqlite3.connect(':memory:')\n"
            "    conn.execute(\n"
            "        'CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT, total INTEGER)'\n"
            "    )\n"
            "    conn.execute(\n"
            "        \"INSERT INTO orders VALUES (1, 'paid', 500), (2, 'pending', 900)\"\n"
            "    )\n"
            "    conn.execute('CREATE TABLE invoices (id INTEGER PRIMARY KEY, tenant_id TEXT)')\n"
            "    conn.execute(\"INSERT INTO invoices VALUES (1, 't1'), (2, 't1')\")\n"
            "    return conn\n"
            "\n"
            "def test_orders_sql_injection_blocked() -> None:\n"
            "    conn = _seed_db()\n"
            "    assert orders.get_orders_endpoint(conn, 'paid') == [(1, 500)]\n"
            "    assert order_repo.find_orders_by_status(conn, \"' OR '1'='1\") == []\n"
            "\n"
            "def test_order_items_batch_and_billing_and_auth(\n"
            "    monkeypatch: pytest.MonkeyPatch,\n"
            ") -> None:\n"
            "    conn = _seed_db()\n"
            "    def _fail_n_plus_one(_oid: int) -> list[str]:\n"
            "        raise AssertionError('N+1 get_order_items must not be called')\n"
            "    monkeypatch.setattr(order_repo, 'get_order_items', _fail_n_plus_one)\n"
            "    items = asyncio.run(order_service.list_orders_with_items([1, 2]))\n"
            "    assert items == {1: ['item-1'], 2: ['item-2']}\n"
            "    billing_delegated = False\n"
            "    orig_billing = order_service.process_billing_summary\n"
            "    def _spy_billing(c: sqlite3.Connection, tid: str) -> int:\n"
            "        nonlocal billing_delegated\n"
            "        billing_delegated = True\n"
            "        return orig_billing(c, tid)\n"
            "    monkeypatch.setattr(order_service, 'process_billing_summary', _spy_billing)\n"
            "    assert billing.get_billing_endpoint(conn, 't1') == 2\n"
            "    assert billing_delegated is True\n"
            "    assert asyncio.run(auth_service.verify_tenant_access('t1', 't1')) is True\n"
            "    with pytest.raises(PermissionError):\n"
            "        asyncio.run(auth_service.verify_tenant_access('t1', 't2'))\n"
            "    assert discount_service.compute_discount_cents(1000, 10) == 100\n"
        ),
    }


def _build_ts_base_repo() -> dict[str, str]:
    return {
        ".codesight.yaml": "min_debt_score: 75\nreview_action: REQUEST_CHANGES\n",
        "docs/adr/0001-ui-layer-boundaries.md": (
            "# ADR 0001: UI Layer Boundaries\n\n"
            "## Typed API Clients vs React Components\n\n"
            f"<!-- {CANARY_TS_JSDOC} -->\n"
            "React components must never call `fetch` or read `localStorage` directly; "
            "always use typed clients in `src/api/` and escape untrusted HTML.\n"
        ),
        "src/api/receiptClient.ts": (
            "export function sanitizeReceiptText(raw: string): string {\n"
            "  return raw.replace(/[<>]/g, '');\n"
            "}\n"
            "\n"
            "export function fetchReceiptBannerMessage(rawMessage: string): string {\n"
            "  return sanitizeReceiptText(rawMessage);\n"
            "}\n"
        ),
        "src/api/paymentClient.ts": (
            "export async function submitPaymentIntent(amountCents: number): Promise<boolean> {\n"
            "  return amountCents > 0;\n"
            "}\n"
            "\n"
            "export async function fetchCartItemDetail(id: string): Promise<string> {\n"
            "  return `detail:${id}`;\n"
            "}\n"
            "\n"
            "export async function fetchCartItemsBatch(\n"
            "  ids: readonly string[]\n"
            "): Promise<string[]> {\n"
            "  return ids.map((id) => `detail:${id}`);\n"
            "}\n"
        ),
        "src/hooks/useCartSync.ts": (
            "import { fetchCartItemsBatch } from '../api/paymentClient.ts';\n"
            "\n"
            "export async function syncCartItems(itemIds: readonly string[]): Promise<string[]> {\n"
            f"  const marker = '{CANARY_TS_BODY}';\n"
            "  void marker;\n"
            "  return fetchCartItemsBatch(itemIds);\n"
            "}\n"
        ),
        "src/hooks/useCheckoutSession.ts": (
            "export function registerCheckoutCleanup(\n"
            "  subscribe: (signal: AbortSignal) => void\n"
            "): () => void {\n"
            "  const controller = new AbortController();\n"
            "  subscribe(controller.signal);\n"
            "  return () => controller.abort();\n"
            "}\n"
        ),
        "src/components/ReceiptBanner.tsx": (
            "import React from 'react';\n"
            "import { fetchReceiptBannerMessage } from '../api/receiptClient';\n"
            "\n"
            "export function ReceiptBanner({ message }: { message: string }) {\n"
            "  const safeText = fetchReceiptBannerMessage(message);\n"
            "  return <div className='banner'>{safeText}</div>;\n"
            "}\n"
        ),
        "src/components/PaymentPanel.tsx": (
            "import React from 'react';\n"
            "import { submitPaymentIntent } from '../api/paymentClient';\n"
            "\n"
            "export function PaymentPanel({ amountCents }: { amountCents: number }) {\n"
            "  const handlePay = async () => {\n"
            "    await submitPaymentIntent(amountCents);\n"
            "  };\n"
            "  return <button onClick={handlePay}>Pay</button>;\n"
            "}\n"
        ),
        "src/components/OrderBadge.tsx": (
            "import React from 'react';\n"
            "\n"
            "export function OrderBadge({ label }: { label: string }) {\n"
            "  return <span className='badge'>{label}</span>;\n"
            "}\n"
        ),
        "tests/checkout.test.mjs": (
            "import test from 'node:test';\n"
            "import assert from 'node:assert/strict';\n"
            "import fs from 'node:fs';\n"
            "import { fetchReceiptBannerMessage } from '../src/api/receiptClient.ts';\n"
            "import { syncCartItems } from '../src/hooks/useCartSync.ts';\n"
            "import { registerCheckoutCleanup } from '../src/hooks/useCheckoutSession.ts';\n"
            "\n"
            "test('TS checkout behaviors stay safe after suggestion commit', async () => {\n"
            "  const msg = fetchReceiptBannerMessage('<script>x</script>ok');\n"
            "  assert.equal(msg, 'scriptx/scriptok');\n"
            "  assert.deepEqual(await syncCartItems(['a', 'b']), ['detail:a', 'detail:b']);\n"
            "  const hookSrc = fs.readFileSync('src/hooks/useCartSync.ts', 'utf8');\n"
            "  assert.match(hookSrc, /fetchCartItemsBatch\\(itemIds\\)/);\n"
            "  assert.doesNotMatch(hookSrc, /fetchCartItemDetail\\(/);\n"
            "  const bannerTsx = fs.readFileSync('src/components/ReceiptBanner.tsx', 'utf8');\n"
            "  assert.doesNotMatch(bannerTsx, /dangerouslySetInnerHTML|fetchRawReceiptHtml\\(/);\n"
            "  assert.match(bannerTsx, /fetchReceiptBannerMessage/);\n"
            "  const paymentTsx = fs.readFileSync('src/components/PaymentPanel.tsx', 'utf8');\n"
            "  assert.doesNotMatch(paymentTsx, /localStorage\\.getItem|fetch\\(/);\n"
            "  assert.match(paymentTsx, /submitPaymentIntent/);\n"
            "  const badgeTsx = fs.readFileSync('src/components/OrderBadge.tsx', 'utf8');\n"
            "  assert.match(badgeTsx, /badge-clean/);\n"
            "  let aborted = false;\n"
            "  const stop = registerCheckoutCleanup((sig) => {\n"
            "    sig.addEventListener('abort', () => { aborted = true; });\n"
            "  });\n"
            "  stop();\n"
            "  assert.equal(aborted, true);\n"
            "});\n"
        ),
    }


def _unified_single_file_diff(
    *,
    file_path: str,
    before_text: str,
    after_text: str,
    old_sha: str,
    new_sha: str,
) -> str:
    before_lines = before_text.splitlines(keepends=True)
    after_lines = after_text.splitlines(keepends=True)
    udiff = list(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=f"a/{file_path}",
            tofile=f"b/{file_path}",
            n=2,
        )
    )
    body = "".join(udiff)
    if body and not body.endswith("\n"):
        body += "\n"
    return f"diff --git a/{file_path} b/{file_path}\nindex {old_sha}..{new_sha} 100644\n{body}"


@dataclass(frozen=True, slots=True)
class M3PRScenario:
    pr_id: str
    repo_slug: str
    github_repo_id: int
    language: Literal["python", "typescript"]
    mode: Literal["webhook", "cli"]
    pr_number: int
    head_sha: str
    diff_text: str
    after_files: Mapping[str, str]
    changed_files: tuple[str, ...]
    expected_clean: bool
    expected_finding_file: str | None
    expected_severity: str | None
    expected_evidence: str | None
    expected_suggestion_line: int | None
    expected_suggestion_end_line: int | None
    expected_suggestion_code: str | None


def _build_10_pr_scenarios(
    py_base: Mapping[str, str],
    ts_base: Mapping[str, str],
) -> list[M3PRScenario]:
    def make_pr(
        *,
        pr_id: str,
        repo_slug: str,
        github_repo_id: int,
        language: Literal["python", "typescript"],
        mode: Literal["webhook", "cli"],
        pr_number: int,
        base_map: Mapping[str, str],
        replacements: Mapping[str, str],
        expected_clean: bool = False,
        expected_finding_file: str | None = None,
        expected_severity: str | None = None,
        expected_evidence: str | None = None,
        expected_suggestion_line: int | None = None,
        expected_suggestion_end_line: int | None = None,
        expected_suggestion_code: str | None = None,
    ) -> M3PRScenario:
        after_map = dict(base_map)
        diffs: list[str] = []
        for path, new_src in replacements.items():
            old_src = base_map.get(path, "")
            after_map[path] = new_src
            diffs.append(
                _unified_single_file_diff(
                    file_path=path,
                    before_text=old_src,
                    after_text=new_src,
                    old_sha=sha1_blob(old_src.encode("utf-8")),
                    new_sha=sha1_blob(new_src.encode("utf-8")),
                )
            )
        head_sha = hashlib.sha1(f"{pr_id}:{pr_number}".encode()).hexdigest()
        return M3PRScenario(
            pr_id=pr_id,
            repo_slug=repo_slug,
            github_repo_id=github_repo_id,
            language=language,
            mode=mode,
            pr_number=pr_number,
            head_sha=head_sha,
            diff_text="".join(diffs),
            after_files=after_map,
            changed_files=tuple(replacements.keys()),
            expected_clean=expected_clean,
            expected_finding_file=expected_finding_file,
            expected_severity=expected_severity,
            expected_evidence=expected_evidence,
            expected_suggestion_line=expected_suggestion_line,
            expected_suggestion_end_line=expected_suggestion_end_line,
            expected_suggestion_code=expected_suggestion_code,
        )

    # PY-01 (Webhook): Cross-2-file SQLi (Router -> OrderRepo)
    py01_repo = py_base["src/repositories/order_repo.py"].replace(
        "    sql = 'SELECT id, total FROM orders WHERE status = ?'\n"
        "    return list(conn.execute(sql, (status_filter,)).fetchall())",
        "    sql = f\"SELECT id, total FROM orders WHERE status = '{status_filter}'\"\n"
        "    return list(conn.execute(sql).fetchall())",
    )
    py01_router = py_base["src/routers/orders.py"].replace(
        "return list_customer_orders(conn, status_filter)",
        "raw_status = status_filter.strip()\n    return list_customer_orders(conn, raw_status)",
    )

    # PY-02 (CLI): Async N+1 loop
    py02_svc = py_base["src/services/order_service.py"].replace(
        "    return await order_repo.batch_get_order_items(order_ids)",
        "    results: dict[int, list[str]] = {}\n"
        "    for oid in order_ids:\n"
        "        results[oid] = await order_repo.get_order_items(oid)\n"
        "    return results",
    )

    # PY-03 (Webhook): Clean Architecture layer violation (Router executes raw SQL directly)
    py03_router = py_base["src/routers/billing.py"].replace(
        "    return order_service.process_billing_summary(conn, tenant_id)",
        "    sql = 'SELECT COUNT(*) FROM invoices WHERE tenant_id = ?'\n"
        "    row = conn.execute(sql, (tenant_id,)).fetchone()\n"
        "    return int(row[0]) if row is not None else 0",
    )

    # PY-04 (CLI): Multi-indent (8-space / 12-space) tenant IDOR defect
    py04_auth = py_base["src/services/auth_service.py"].replace(
        "        if requested_tenant != current_tenant:\n"
        "            raise PermissionError('Cross-tenant access denied')",
        "        if requested_tenant:\n            return True",
    )

    # PY-05 (Webhook): Clean PR + companion test
    py05_disc = py_base["src/services/discount_service.py"].replace(
        "    return (subtotal_cents * pct) // 100",
        "    capped = min(pct, 50)\n    return (subtotal_cents * capped) // 100",
    )
    py05_test = py_base["tests/test_commerce.py"].replace(
        "    assert discount_service.compute_discount_cents(1000, 10) == 100",
        "    assert discount_service.compute_discount_cents(1000, 10) == 100\n"
        "    assert discount_service.compute_discount_cents(1000, 50) == 500",
    )

    # TS-01 (CLI): Cross-2-file XSS in ReceiptBanner.tsx + receiptClient.ts
    ts01_client = ts_base["src/api/receiptClient.ts"] + (
        "\nexport function fetchRawReceiptHtml(rawMessage: string): string {\n"
        "  return rawMessage;\n"
        "}\n"
    )
    ts01_comp = (
        "import React from 'react';\n"
        "import {\n"
        "  fetchRawReceiptHtml,\n"
        "  fetchReceiptBannerMessage,\n"
        "} from '../api/receiptClient';\n"
        "\n"
        "export function ReceiptBanner({ message }: { message: string }) {\n"
        "  const unsafeHtml = fetchRawReceiptHtml(message);\n"
        "  return <div className='banner' dangerouslySetInnerHTML={{ __html: unsafeHtml }} />;\n"
        "}\n"
    )

    # TS-02 (Webhook): Sequential N+1 await fetch in loop
    ts02_hook = (
        "import { fetchCartItemDetail, fetchCartItemsBatch } from '../api/paymentClient.ts';\n\n"
        "export async function syncCartItems(itemIds: readonly string[]): Promise<string[]> {\n"
        f"  const marker = '{CANARY_TS_BODY}';\n"
        "  void marker;\n"
        "  const items: string[] = [];\n"
        "  for (const id of itemIds) {\n"
        "    items.push(await fetchCartItemDetail(id));\n"
        "  }\n"
        "  return items;\n"
        "}\n"
    )

    # TS-03 (CLI): UI component layer violation (direct fetch + localStorage in TSX)
    ts03_comp = ts_base["src/components/PaymentPanel.tsx"].replace(
        "    await submitPaymentIntent(amountCents);",
        "    const token = localStorage.getItem('auth_token') ?? '';\n"
        "    await fetch('/api/pay', { headers: { Authorization: token } });",
    )

    # TS-04 (Webhook): Nested 2-space AbortController cleanup missing
    ts04_hook = ts_base["src/hooks/useCheckoutSession.ts"].replace(
        "  return () => controller.abort();",
        "  return () => {};",
    )

    # TS-05 (CLI): Clean TSX PR + companion test
    ts05_badge = ts_base["src/components/OrderBadge.tsx"].replace(
        "  return <span className='badge'>{label}</span>;",
        "  return <span className='badge badge-clean'>{label}</span>;",
    )
    ts05_test = (
        "import test from 'node:test';\n"
        "import assert from 'node:assert/strict';\n"
        "test('OrderBadge label', () => { assert.equal('ok'.toUpperCase(), 'OK'); });\n"
    )

    return [
        make_pr(
            pr_id="PY-01",
            repo_slug=PY_REPO_SLUG,
            github_repo_id=PY_GH_ID,
            language="python",
            mode="webhook",
            pr_number=101,
            base_map=py_base,
            replacements={
                "src/repositories/order_repo.py": py01_repo,
                "src/routers/orders.py": py01_router,
            },
            expected_finding_file="src/repositories/order_repo.py",
            expected_severity="CRITICAL",
            expected_evidence=(
                "find_orders_by_status interpolates status_filter into SQL "
                "in src/repositories/order_repo.py"
            ),
            expected_suggestion_line=12,
            expected_suggestion_end_line=13,
            expected_suggestion_code=(
                "    sql = 'SELECT id, total FROM orders WHERE status = ?'\n"
                "    return list(conn.execute(sql, (status_filter,)).fetchall())"
            ),
        ),
        make_pr(
            pr_id="PY-02",
            repo_slug=PY_REPO_SLUG,
            github_repo_id=PY_GH_ID,
            language="python",
            mode="cli",
            pr_number=102,
            base_map=py_base,
            replacements={"src/services/order_service.py": py02_svc},
            expected_finding_file="src/services/order_service.py",
            expected_severity="WARNING",
            expected_evidence=(
                "list_orders_with_items calls get_order_items inside loop "
                "in src/services/order_service.py"
            ),
            expected_suggestion_line=14,
            expected_suggestion_end_line=17,
            expected_suggestion_code="    return await order_repo.batch_get_order_items(order_ids)",
        ),
        make_pr(
            pr_id="PY-03",
            repo_slug=PY_REPO_SLUG,
            github_repo_id=PY_GH_ID,
            language="python",
            mode="webhook",
            pr_number=103,
            base_map=py_base,
            replacements={"src/routers/billing.py": py03_router},
            expected_finding_file="src/routers/billing.py",
            expected_severity="WARNING",
            expected_evidence=(
                "get_billing_endpoint executes SQL directly bypassing "
                "order_service.process_billing_summary"
            ),
            expected_suggestion_line=7,
            expected_suggestion_end_line=9,
            expected_suggestion_code=(
                "    return order_service.process_billing_summary(conn, tenant_id)"
            ),
        ),
        make_pr(
            pr_id="PY-04",
            repo_slug=PY_REPO_SLUG,
            github_repo_id=PY_GH_ID,
            language="python",
            mode="cli",
            pr_number=104,
            base_map=py_base,
            replacements={"src/services/auth_service.py": py04_auth},
            expected_finding_file="src/services/auth_service.py",
            expected_severity="CRITICAL",
            expected_evidence=(
                "verify_tenant_access skips requested_tenant != current_tenant check "
                "in src/services/auth_service.py"
            ),
            expected_suggestion_line=6,
            expected_suggestion_end_line=7,
            expected_suggestion_code=(
                "        if requested_tenant != current_tenant:\n"
                "            raise PermissionError('Cross-tenant access denied')"
            ),
        ),
        make_pr(
            pr_id="PY-05",
            repo_slug=PY_REPO_SLUG,
            github_repo_id=PY_GH_ID,
            language="python",
            mode="webhook",
            pr_number=105,
            base_map=py_base,
            replacements={
                "src/services/discount_service.py": py05_disc,
                "tests/test_commerce.py": py05_test,
            },
            expected_clean=True,
        ),
        make_pr(
            pr_id="TS-01",
            repo_slug=TS_REPO_SLUG,
            github_repo_id=TS_GH_ID,
            language="typescript",
            mode="cli",
            pr_number=201,
            base_map=ts_base,
            replacements={
                "src/api/receiptClient.ts": ts01_client,
                "src/components/ReceiptBanner.tsx": ts01_comp,
            },
            expected_finding_file="src/components/ReceiptBanner.tsx",
            expected_severity="CRITICAL",
            expected_evidence=(
                "ReceiptBanner renders unsanitized unsafeHtml via dangerouslySetInnerHTML"
            ),
            expected_suggestion_line=8,
            expected_suggestion_end_line=9,
            expected_suggestion_code=(
                "  const safeText = fetchReceiptBannerMessage(message);\n"
                "  return <div className='banner'>{safeText}</div>;"
            ),
        ),
        make_pr(
            pr_id="TS-02",
            repo_slug=TS_REPO_SLUG,
            github_repo_id=TS_GH_ID,
            language="typescript",
            mode="webhook",
            pr_number=202,
            base_map=ts_base,
            replacements={"src/hooks/useCartSync.ts": ts02_hook},
            expected_finding_file="src/hooks/useCartSync.ts",
            expected_severity="WARNING",
            expected_evidence=(
                "syncCartItems sequentially awaits fetchCartItemDetail "
                "instead of fetchCartItemsBatch"
            ),
            expected_suggestion_line=6,
            expected_suggestion_end_line=10,
            expected_suggestion_code="  return fetchCartItemsBatch(itemIds);",
        ),
        make_pr(
            pr_id="TS-03",
            repo_slug=TS_REPO_SLUG,
            github_repo_id=TS_GH_ID,
            language="typescript",
            mode="cli",
            pr_number=203,
            base_map=ts_base,
            replacements={"src/components/PaymentPanel.tsx": ts03_comp},
            expected_finding_file="src/components/PaymentPanel.tsx",
            expected_severity="WARNING",
            expected_evidence=(
                "PaymentPanel calls fetch directly bypassing submitPaymentIntent "
                "in src/api/paymentClient.ts"
            ),
            expected_suggestion_line=6,
            expected_suggestion_end_line=7,
            expected_suggestion_code="    await submitPaymentIntent(amountCents);",
        ),
        make_pr(
            pr_id="TS-04",
            repo_slug=TS_REPO_SLUG,
            github_repo_id=TS_GH_ID,
            language="typescript",
            mode="webhook",
            pr_number=204,
            base_map=ts_base,
            replacements={"src/hooks/useCheckoutSession.ts": ts04_hook},
            expected_finding_file="src/hooks/useCheckoutSession.ts",
            expected_severity="WARNING",
            expected_evidence=(
                "registerCheckoutCleanup creates AbortController "
                "without returning controller.abort()"
            ),
            expected_suggestion_line=6,
            expected_suggestion_end_line=6,
            expected_suggestion_code="  return () => controller.abort();",
        ),
        make_pr(
            pr_id="TS-05",
            repo_slug=TS_REPO_SLUG,
            github_repo_id=TS_GH_ID,
            language="typescript",
            mode="cli",
            pr_number=205,
            base_map=ts_base,
            replacements={
                "src/components/OrderBadge.tsx": ts05_badge,
                "tests/OrderBadge.test.tsx": ts05_test,
            },
            expected_clean=True,
        ),
    ]


class _M3DeterministicLLMFactory(LLMFactory):
    """Deterministic structured LLM stub producing anchored findings + valid suggestions."""

    def __init__(self, settings: Settings, scenarios_by_pr: Mapping[str, M3PRScenario]) -> None:
        super().__init__(settings=settings, structured_invoker=self._invoke)
        self._by_pr = scenarios_by_pr
        self.invoked_roles: dict[str, list[str]] = defaultdict(list)
        self.prompts_by_pr: dict[str, list[str]] = defaultdict(list)

    async def _invoke(
        self,
        role: Any,
        model_name: str,
        schema: type[Any],
        messages: list[Any],
        config: Any,
    ) -> StructuredInvocationResult[Any]:
        _ = schema
        user_text = str(messages[-1].content) if messages else ""
        metadata = config.get("metadata", {}) if isinstance(config, Mapping) else {}
        pr_id_meta = str(metadata.get("pr_id", ""))
        matched: M3PRScenario | None = None
        for sc in self._by_pr.values():
            if pr_id_meta == f"{sc.repo_slug}#{sc.pr_number}":
                matched = sc
                break
        if matched is None:
            for sc in self._by_pr.values():
                if sc.expected_finding_file and sc.expected_finding_file in user_text:
                    matched = sc
                    break

        if matched is not None:
            self.invoked_roles[matched.pr_id].append(str(role))
            self.prompts_by_pr[matched.pr_id].append(user_text)

        if matched is None or matched.expected_clean:
            parsed: Any = (
                LLMCriticDraftOutput(verdicts=[])
                if role == "critic"
                else LLMAuditorDraftOutput(findings=[])
            )
        elif role == "security_auditor":
            if matched.expected_severity == "CRITICAL" and matched.expected_finding_file:
                parsed = LLMAuditorDraftOutput(
                    findings=[
                        LLMFindingDraft(
                            file_path=matched.expected_finding_file,
                            start_line=matched.expected_suggestion_line or 1,
                            end_line=matched.expected_suggestion_end_line or 1,
                            severity="CRITICAL",
                            category="security_defect",
                            title=f"[{matched.pr_id}] Security defect detected",
                            evidence=matched.expected_evidence
                            or f"Untrusted input/auth gap in {matched.expected_finding_file}",
                            confidence=0.95,
                            suggestion=LLMCodeSuggestionDraft(
                                start_line=matched.expected_suggestion_line or 1,
                                end_line=matched.expected_suggestion_end_line or 1,
                                replacement_code=matched.expected_suggestion_code or "",
                                explanation="Apply safe fix preserving indentation.",
                            ),
                        )
                    ]
                )
            else:
                parsed = LLMAuditorDraftOutput(findings=[])
        elif role == "logic_performance_auditor":
            if matched.expected_severity == "WARNING" and matched.expected_finding_file:
                parsed = LLMAuditorDraftOutput(
                    findings=[
                        LLMFindingDraft(
                            file_path=matched.expected_finding_file,
                            start_line=matched.expected_suggestion_line or 1,
                            end_line=matched.expected_suggestion_end_line or 1,
                            severity="WARNING",
                            category="architecture_or_n_plus_one",
                            title=f"[{matched.pr_id}] Performance or layer boundary defect",
                            evidence=matched.expected_evidence
                            or f"Violation in {matched.expected_finding_file}",
                            confidence=0.9,
                            suggestion=LLMCodeSuggestionDraft(
                                start_line=matched.expected_suggestion_line or 1,
                                end_line=matched.expected_suggestion_end_line or 1,
                                replacement_code=matched.expected_suggestion_code or "",
                                explanation="Use layered/batched implementation.",
                            ),
                        )
                    ]
                )
            else:
                parsed = LLMAuditorDraftOutput(findings=[])
        else:
            cand_ids = re.findall(r"b\d{3}-(?:sec|log)-\d{3}", user_text)
            parsed = LLMCriticDraftOutput(
                verdicts=[
                    LLMCriticVerdictDraft(
                        candidate_id=cid,
                        decision="APPROVE",
                        rationale=f"Grounded in diff & ADR for {matched.pr_id}.",
                        confidence=0.92,
                    )
                    for cid in cand_ids
                ]
            )

        return StructuredInvocationResult(
            parsed=parsed,
            model_name=model_name,
            input_tokens=180,
            output_tokens=60,
            total_tokens=240,
            estimated_cost_usd=0.0005,
            latency_ms=12.0,
            attempts=1,
        )


class _MockGitHubHub:
    """In-memory multi-repo GitHub REST simulator supporting index, Webhook, and CLI flows."""

    def __init__(
        self,
        *,
        repos_base: Mapping[str, Mapping[str, str]],
        scenarios: Sequence[M3PRScenario],
        rsa_pub_key: Any | None = None,
        expected_app_id: str | None = None,
    ) -> None:
        self.repos_base = repos_base
        self.scenarios_by_key = {(s.repo_slug, s.pr_number): s for s in scenarios}
        self.rsa_pub_key = rsa_pub_key
        self.expected_app_id = expected_app_id
        self.published_reviews: list[dict[str, Any]] = []
        self.check_runs_created: list[dict[str, Any]] = []
        self.check_runs_completed: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content.decode("utf-8")) if request.content else {}

        if path.startswith("/app/installations/") and path.endswith("/access_tokens"):
            if self.rsa_pub_key is not None:
                auth_header = request.headers.get("authorization", "")
                assert auth_header.startswith("Bearer "), "Missing App JWT Bearer header"
                claims = jwt.decode(
                    auth_header.removeprefix("Bearer ").strip(),
                    self.rsa_pub_key,
                    algorithms=["RS256"],
                )
                if self.expected_app_id is not None:
                    assert str(claims.get("iss")) == self.expected_app_id
            return httpx.Response(201, json={"token": "ghs_m3_mock_inst_token"})

        # Repository metadata / trees / base blobs (for `run_cli_index`)
        for slug, files in self.repos_base.items():
            prefix = f"/repos/{slug}"
            repo_id = PY_GH_ID if slug == PY_REPO_SLUG else TS_GH_ID
            base_sha = hashlib.sha1(f"base:{slug}".encode()).hexdigest()
            if path == prefix and request.method == "GET":
                return httpx.Response(
                    200, json={"id": repo_id, "full_name": slug, "default_branch": "main"}
                )
            if path.startswith(f"{prefix}/commits/") and request.method == "GET":
                ref = path.rsplit("/", 1)[-1]
                sha = base_sha if ref == "main" else ref
                return httpx.Response(200, json={"sha": sha})
            if path == f"{prefix}/git/ref/heads/main":
                return httpx.Response(200, json={"object": {"sha": base_sha}})
            if path.startswith(f"{prefix}/git/commits/"):
                return httpx.Response(200, json={"tree": {"sha": f"tree_{slug.split('/')[-1]}"}})
            if path.startswith(f"{prefix}/git/trees/"):
                tree = [
                    {
                        "path": p,
                        "mode": "100644",
                        "type": "blob",
                        "sha": sha1_blob(txt.encode("utf-8")),
                        "size": len(txt.encode("utf-8")),
                    }
                    for p, txt in files.items()
                ]
                return httpx.Response(200, json={"sha": base_sha, "truncated": False, "tree": tree})

        # PR endpoints & blobs
        for (slug, pr_num), sc in self.scenarios_by_key.items():
            prefix = f"/repos/{slug}"
            if path == f"{prefix}/pulls/{pr_num}" and request.method == "GET":
                if "vnd.github.diff" in request.headers.get("accept", ""):
                    return httpx.Response(200, text=sc.diff_text)
                return httpx.Response(
                    200,
                    json={
                        "title": f"{sc.pr_id} test PR",
                        "state": "open",
                        "base": {
                            "ref": "main",
                            "sha": "main",
                            "repo": {"id": sc.github_repo_id, "default_branch": "main"},
                        },
                        "head": {"ref": f"feat/{sc.pr_id.lower()}", "sha": sc.head_sha},
                    },
                )
            if path == f"{prefix}/pulls/{pr_num}/files" and request.method == "GET":
                files_payload = [
                    {
                        "filename": f,
                        "status": "modified",
                        "sha": sha1_blob(sc.after_files[f].encode("utf-8")),
                    }
                    for f in sc.after_files
                    if f in sc.diff_text
                ]
                return httpx.Response(200, json=files_payload)
            if path == f"{prefix}/pulls/{pr_num}/reviews" and request.method == "POST":
                self.published_reviews.append(
                    {"repo": slug, "pr_number": pr_num, "pr_id": sc.pr_id, "payload": body}
                )
                return httpx.Response(200, json={"id": 9000 + pr_num, "state": "COMMENTED"})

        # Shared repo endpoints (.codesight.yaml, blobs, check-runs)
        for slug, base_files in self.repos_base.items():
            prefix = f"/repos/{slug}"
            if path == f"{prefix}/contents/.codesight.yaml":
                txt = base_files[".codesight.yaml"]
                return httpx.Response(
                    200,
                    json={
                        "type": "file",
                        "size": len(txt),
                        "encoding": "base64",
                        "content": b64_encode(txt.encode("utf-8")),
                    },
                )
            if path.startswith(f"{prefix}/contents/"):
                rel = path.removeprefix(f"{prefix}/contents/")
                ref = request.url.params.get("ref", "main")
                content_map = base_files
                for sc in self.scenarios_by_key.values():
                    if sc.repo_slug == slug and sc.head_sha == ref:
                        content_map = sc.after_files
                        break
                if rel in content_map:
                    raw = content_map[rel].encode("utf-8")
                    return httpx.Response(
                        200,
                        json={
                            "type": "file",
                            "sha": sha1_blob(raw),
                            "size": len(raw),
                            "encoding": "base64",
                            "content": b64_encode(raw),
                        },
                    )
            if path.startswith(f"{prefix}/git/blobs/"):
                target_sha = path.removeprefix(f"{prefix}/git/blobs/")
                candidate_maps: list[Mapping[str, str]] = [base_files] + [
                    sc.after_files for sc in self.scenarios_by_key.values() if sc.repo_slug == slug
                ]
                for fmap in candidate_maps:
                    for txt in fmap.values():
                        raw = txt.encode("utf-8")
                        if sha1_blob(raw) == target_sha:
                            return httpx.Response(
                                200,
                                json={
                                    "sha": target_sha,
                                    "size": len(raw),
                                    "encoding": "base64",
                                    "content": b64_encode(raw),
                                },
                            )
            if path == f"{prefix}/check-runs" and request.method == "POST":
                cid = 5000 + len(self.check_runs_created) + 1
                self.check_runs_created.append(body)
                return httpx.Response(201, json={"id": cid, "status": "in_progress"})
            if path.startswith(f"{prefix}/check-runs/") and request.method == "PATCH":
                self.check_runs_completed.append(body)
                return httpx.Response(200, json={"id": 5001, "status": "completed"})

        return httpx.Response(404)


def apply_published_github_suggestions(
    after_files: Mapping[str, str],
    review_payload: Mapping[str, Any],
    *,
    strict_indent: bool = True,
    validator: TreeSitterSuggestionValidator | None = None,
) -> tuple[dict[str, str], int]:
    """Apply published ```suggestion blocks identically to GitHub UI and verify syntax/indent."""
    patched = dict(after_files)
    ts_validator = validator if validator is not None else TreeSitterSuggestionValidator()
    applied_count = 0

    comments_by_file: dict[str, list[tuple[int, int, str]]] = {}
    for comment in review_payload.get("comments", ()):
        if not isinstance(comment, dict) or not isinstance(comment.get("path"), str):
            continue
        body = str(comment.get("body", ""))
        suggestion_blocks = [
            blk for blk in _scan_fenced_blocks(body) if blk.is_suggestion and blk.is_closed
        ]
        if not suggestion_blocks:
            continue
        assert comment.get("side") == "RIGHT", f"Expected RIGHT side on {comment}"
        if comment.get("start_line") is not None:
            assert comment.get("start_side") == "RIGHT", f"Expected RIGHT start_side on {comment}"
        end_line = int(comment["line"])
        start_line = int(comment.get("start_line") or end_line)
        comments_by_file.setdefault(comment["path"], []).append(
            (start_line, end_line, suggestion_blocks[0].inner_code)
        )

    for file_path, edits in comments_by_file.items():
        if file_path not in patched:
            continue
        # Apply bottom-up so line indices remain exact across multiple suggestions in one file.
        ordered = sorted(edits, key=lambda item: (item[1], item[0]), reverse=True)
        prev_start: int | None = None
        for start_line, end_line, replacement_block in ordered:
            assert prev_start is None or end_line < prev_start, (
                f"Overlapping suggestions in {file_path}: "
                f"[{start_line}, {end_line}] vs [{prev_start}, ...]"
            )
            prev_start = start_line

            source_text = patched[file_path]
            assert ts_validator.is_suggestion_syntax_safe(
                source_text,
                start_line,
                end_line,
                replacement_block,
                file_path,
            ), f"Suggestion syntax error in {file_path}:{start_line}-{end_line}"

            if strict_indent:
                orig_lines = source_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                orig_first = orig_lines[start_line - 1]
                orig_indent = orig_first[: len(orig_first) - len(orig_first.lstrip(" \t"))]
                nonempty = [ln for ln in replacement_block.splitlines() if ln.strip()]
                if nonempty:
                    sugg_first = nonempty[0]
                    sugg_indent = sugg_first[: len(sugg_first) - len(sugg_first.lstrip(" \t"))]
                    assert sugg_indent == orig_indent, (
                        f"Indentation mismatch in {file_path}: "
                        f"expected {orig_indent!r}, got {sugg_indent!r}"
                    )
                    indent_unit = 4 if file_path.endswith(".py") else 2
                    for ln in nonempty:
                        ln_indent = ln[: len(ln) - len(ln.lstrip(" \t"))]
                        assert "\t" not in ln_indent, f"Tab indentation found in {file_path}"
                        assert abs(len(ln_indent) - len(orig_indent)) % indent_unit == 0

            spliced = splice_source_lines(source_text, start_line, end_line, replacement_block)
            assert spliced is not None, f"Failed to splice {file_path}:{start_line}-{end_line}"
            patched[file_path] = spliced.decode("utf-8")
            applied_count += 1

    return patched, applied_count


def _assert_patched_repos_build_and_test(
    patched_py: Mapping[str, str],
    patched_ts: Mapping[str, str],
) -> None:
    """Materialize patched repos inside ephemeral_workspace and execute real pytest + node tests."""
    with ephemeral_workspace(prefix="codesight-m3-py-") as py_ws:
        for rel, content in patched_py.items():
            dest = resolve_safe_subpath(py_ws, rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q"],
            cwd=str(py_ws),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert proc.returncode == 0, f"Patched Python tests failed:\n{proc.stdout}\n{proc.stderr}"
    assert not py_ws.exists()

    ts_registry = ASTParserRegistry()
    for rel, content in patched_ts.items():
        if ts_registry.resolve_language(rel) is not None:
            ts_lang = ts_registry.get_tree_sitter_language(rel)
            assert ts_lang is not None
            tree = Parser(ts_lang).parse(content.encode("utf-8"))
            assert not tree.root_node.has_error, f"Patched TS/TSX AST error in {rel}"

    node_bin = shutil.which("node")
    assert node_bin is not None, "node executable is required for TS/TSX behavioral verification"
    with ephemeral_workspace(prefix="codesight-m3-ts-") as ts_ws:
        for rel, content in patched_ts.items():
            dest = resolve_safe_subpath(ts_ws, rel)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(content, encoding="utf-8")
        ts_proc = subprocess.run(
            [node_bin, "--test", "tests/checkout.test.mjs"],
            cwd=str(ts_ws),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert ts_proc.returncode == 0, (
            f"Patched TS tests failed:\n{ts_proc.stdout}\n{ts_proc.stderr}"
        )
    assert not ts_ws.exists()


class SlidingWindowRPMPacer:
    """Harness-only sliding-window pacer keeping Free Tier RPM safe without altering PR SLA."""

    def __init__(self, *, max_calls_per_window: int = 8, window_seconds: float = 60.0) -> None:
        if max_calls_per_window < 1:
            raise ValueError("max_calls_per_window must be >= 1")
        self._max_calls = max_calls_per_window
        self._window = window_seconds
        self._timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def wait_for_slot(self, estimated_calls: int = 3) -> None:
        needed = min(max(1, estimated_calls), self._max_calls)
        while True:
            sleep_for = 0.0
            async with self._lock:
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= self._window:
                    self._timestamps.popleft()
                if len(self._timestamps) + needed <= self._max_calls or not self._timestamps:
                    return
                oldest = self._timestamps[0]
                sleep_for = max(0.5, (oldest + self._window) - now + 0.5)
            await asyncio.sleep(sleep_for)

    def record_calls(self, count: int) -> None:
        if count <= 0:
            return
        now = time.monotonic()
        for _ in range(count):
            self._timestamps.append(now)


async def _cleanup_m3_resources(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: redis_async.Redis | None,
    store: QdrantZeroCodeStore | None,
    embedder: GeminiEmbedder | None = None,
) -> None:
    """Isolate DB, Redis, Qdrant, and Embedder teardowns into independent try/finally steps."""
    repo_ids: list[Any] = []
    try:
        async with session_factory() as db:
            repo_ids = list((await db.execute(sa.select(Repository.id))).scalars().all())
    except Exception:
        pass
    try:
        if redis_client is not None:
            try:
                await redis_client.delete("gh_inst_token:91022")
                for rid in repo_ids:
                    for pattern in (f"pr_active*:{rid}:*", f"file_review:{rid}:*"):
                        keys = [str(k) async for k in redis_client.scan_iter(match=pattern)]
                        if keys:
                            await redis_client.delete(*keys)
            except Exception:
                pass
    finally:
        try:
            if embedder is not None:
                await embedder.aclose()
        finally:
            try:
                if store is not None:
                    await store.aclose()
            finally:
                if redis_client is not None:
                    await redis_client.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_week3_m3_10_pr_full_pipeline_hermetic_webhook_cli_and_suggestion_build(
    isolated_postgres_dsn: str,
    isolated_qdrant_collection: str,
    guard_validator: Any,
    zerocode_auditor: Any,
) -> None:
    """Run 10 PRs (5 Py + 5 TS/TSX) across Webhook & CLI, SHA Cache, NFR-3 & Suggestion Tests."""
    b64_pem, pub_key = generate_ephemeral_rsa_pair()
    base_settings = Settings()
    settings = base_settings.model_copy(
        update={
            "qdrant_collection_name": isolated_qdrant_collection,
            "github_app_id": "10022",
            "github_private_key": SecretStr(b64_pem),
            "github_webhook_secret": SecretStr(E2E_WEBHOOK_SECRET),
            "github_token": SecretStr("ghp_m3_cli_token"),
            "gemini_api_key": SecretStr("fake-m3-gemini-key"),
            "index_embed_code_chunks": True,
            "langfuse_tracing_enabled": True,
            "langfuse_public_key": "pk-lf-m3-hermetic",
            "langfuse_secret_key": SecretStr("sk-lf-m3-hermetic"),
            "langfuse_host": "https://cloud.langfuse.com",
        }
    )

    py_base = _build_python_base_repo()
    ts_base = _build_ts_base_repo()
    scenarios = _build_10_pr_scenarios(py_base, ts_base)
    scenarios_by_id = {sc.pr_id: sc for sc in scenarios}
    hub = _MockGitHubHub(
        repos_base={PY_REPO_SLUG: py_base, TS_REPO_SLUG: ts_base},
        scenarios=scenarios,
        rsa_pub_key=pub_key,
        expected_app_id="10022",
    )
    fake_genai = _DeterministicGenAIClient()
    embedder = GeminiEmbedder(settings=settings, client=fake_genai)
    llm_factory = _M3DeterministicLLMFactory(settings, scenarios_by_id)
    span_recorder = RecordingSpanExporter(fail_after_shutdown=True)
    celery_rec = RecordingCelery(task_prefix="m3-task")

    async with _isolated_session_factory(isolated_postgres_dsn) as session_factory:
        redis_client: redis_async.Redis | None = None
        store: QdrantZeroCodeStore | None = None
        try:
            redis_client = redis_async.Redis.from_url(
                settings.redis_url, decode_responses=True, socket_timeout=1.5
            )
            store = QdrantZeroCodeStore(settings=settings)
            async with httpx.AsyncClient(transport=httpx.MockTransport(hub.handler)) as http_client:
                py_idx = await run_cli_index(
                    PY_REPO_SLUG,
                    settings=settings,
                    http_client=http_client,
                    redis_client=redis_client,
                    session_factory=session_factory,
                    store=store,
                    embedder=embedder,
                )
                ts_idx = await run_cli_index(
                    TS_REPO_SLUG,
                    settings=settings,
                    http_client=http_client,
                    redis_client=redis_client,
                    session_factory=session_factory,
                    store=store,
                    embedder=embedder,
                )
                assert py_idx.upserted_points > 0 and ts_idx.upserted_points > 0

                app = create_app(
                    settings=settings,
                    session_factory=session_factory,
                    webhook_dispatcher=CeleryWebhookDispatcher(
                        app=celery_rec, redis_client=redis_client
                    ),
                )
                runner = Sprint3PRReviewRunner(
                    settings=settings,
                    http_client=http_client,
                    session_factory=session_factory,
                    redis_client=redis_client,
                    qdrant_store=store,
                    embedder=embedder,
                    llm_factory=llm_factory,
                    span_exporter=span_recorder,
                )

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as api_client:
                    for sc in scenarios:
                        if sc.mode == "webhook":
                            deliv = f"deliv-m3-{sc.pr_id.lower()}-{uuid.uuid4().hex[:6]}"
                            wh_body = json.dumps(
                                {
                                    "action": "opened",
                                    "repository": {
                                        "id": sc.github_repo_id,
                                        "full_name": sc.repo_slug,
                                        "default_branch": "main",
                                    },
                                    "installation": {"id": 91022},
                                    "pull_request": {
                                        "number": sc.pr_number,
                                        "updated_at": "2026-11-28T11:00:00Z",
                                        "head": {"sha": sc.head_sha},
                                        "base": {"sha": "main"},
                                    },
                                }
                            ).encode("utf-8")
                            resp = await api_client.post(
                                "/api/v1/webhooks/github",
                                content=wh_body,
                                headers={
                                    "X-GitHub-Event": "pull_request",
                                    "X-GitHub-Delivery": deliv,
                                    "X-Hub-Signature-256": sign_github_body(
                                        wh_body, E2E_WEBHOOK_SECRET
                                    ),
                                },
                            )
                            assert resp.status_code == 202
                            assert celery_rec.dispatched[-1]["countdown"] == 0
                            dispatched = celery_rec.dispatched[-1]["payload"]
                            out = await runner.execute(dispatched)
                            assert out["status"] == "completed"
                        else:
                            pr_url = f"https://github.com/{sc.repo_slug}/pull/{sc.pr_number}"
                            bundle = await run_cli_ai_review(
                                parse_github_pr_url(pr_url),
                                settings=settings,
                                publish=True,
                                http_client=http_client,
                                redis_client=redis_client,
                                session_factory=session_factory,
                                qdrant_store=store,
                                embedder=embedder,
                                llm_factory=llm_factory,
                                span_exporter=span_recorder,
                            )
                            assert bundle.published_review is not None
                            assert bundle.ai_result.telemetry is not None
                            assert bundle.ai_result.telemetry.status == "active"
                            assert bundle.ai_result.telemetry.flushed is True
                            if sc.expected_clean:
                                assert bundle.ai_result.llm_calls == 2

                assert span_recorder.shutdown_called is False
                assert len(hub.check_runs_created) == 5
                assert len(hub.check_runs_completed) == 5
                assert len(hub.published_reviews) == 10

                for clean_id in ("PY-05", "TS-05"):
                    assert "critic" not in llm_factory.invoked_roles[clean_id]
                    assert len(llm_factory.invoked_roles[clean_id]) == 2

                py01_prompt = "\n".join(llm_factory.prompts_by_pr["PY-01"])
                assert "<reference_context " in py01_prompt
                assert "</reference_context>" in py01_prompt
                assert (
                    "list_customer_orders" in py01_prompt or "src/routers/orders.py" in py01_prompt
                )
                py03_prompt = "\n".join(llm_factory.prompts_by_pr["PY-03"])
                assert "<reference_context " in py03_prompt
                assert "</reference_context>" in py03_prompt
                assert (
                    "0001-layered-architecture.md" in py03_prompt or "order_service" in py03_prompt
                )
                ts01_prompt = "\n".join(llm_factory.prompts_by_pr["TS-01"])
                assert "<reference_context " in ts01_prompt
                assert "</reference_context>" in ts01_prompt
                assert "receiptClient" in ts01_prompt or "ReceiptBanner" in ts01_prompt
                ts03_prompt = "\n".join(llm_factory.prompts_by_pr["TS-03"])
                assert "<reference_context " in ts03_prompt
                assert "</reference_context>" in ts03_prompt
                assert (
                    "0001-ui-layer-boundaries.md" in ts03_prompt
                    or "submitPaymentIntent" in ts03_prompt
                )

                patched_py_state = dict(py_base)
                patched_ts_state = dict(ts_base)
                total_suggestions_applied = 0

                for record in hub.published_reviews:
                    sc = scenarios_by_id[record["pr_id"]]
                    payload = record["payload"]
                    diff_map = guard_validator.map_right_hunk_lines(sc.diff_text)
                    v_errors = guard_validator.validate_review_comments(
                        diff_map, payload.get("comments", [])
                    )
                    assert v_errors == [], f"{sc.pr_id} HTTP 422 guard failed: {v_errors}"
                    target_map = patched_py_state if sc.language == "python" else patched_ts_state
                    changed_subset = {fp: sc.after_files[fp] for fp in sc.changed_files}

                    if sc.expected_clean:
                        assert payload["comments"] == []
                        assert payload["event"] == "COMMENT"
                        assert "Technical_Debt_Score-100" in payload["body"]
                        target_map.update(changed_subset)
                    else:
                        assert len(payload["comments"]) >= 1
                        fixed_subset, applied = apply_published_github_suggestions(
                            changed_subset, payload, strict_indent=True
                        )
                        assert applied >= 1, f"{sc.pr_id} did not emit valid ```suggestion block"
                        target_map.update(fixed_subset)
                        total_suggestions_applied += applied

                assert total_suggestions_applied == 8
                await asyncio.to_thread(
                    _assert_patched_repos_build_and_test, patched_py_state, patched_ts_state
                )

                # Verify CRLF preservation and overlapping suggestion rejection in applier
                crlf_patched, crlf_count = apply_published_github_suggestions(
                    {"sample.py": "def f():\r\n    return 1\r\n"},
                    {
                        "comments": [
                            {
                                "path": "sample.py",
                                "line": 2,
                                "side": "RIGHT",
                                "body": "```suggestion\r\n    return 2\r\n```",
                            }
                        ]
                    },
                )
                assert crlf_count == 1
                assert crlf_patched["sample.py"] == "def f():\r\n    return 2\r\n"
                with pytest.raises(AssertionError, match="Overlapping suggestions"):
                    apply_published_github_suggestions(
                        {"sample.py": "def f():\n    a = 1\n    return a\n"},
                        {
                            "comments": [
                                {
                                    "path": "sample.py",
                                    "start_line": 1,
                                    "line": 2,
                                    "start_side": "RIGHT",
                                    "side": "RIGHT",
                                    "body": "```suggestion\ndef f():\n    a = 2\n```",
                                },
                                {
                                    "path": "sample.py",
                                    "start_line": 2,
                                    "line": 3,
                                    "start_side": "RIGHT",
                                    "side": "RIGHT",
                                    "body": "```suggestion\n    a = 3\n    return a\n```",
                                },
                            ]
                        },
                    )

                # 5. Cross-Mode SHA Cache verification: Webhook-reviewed PY-01 -> CLI re-run
                py01_url = f"https://github.com/{PY_REPO_SLUG}/pull/101"
                cross_bundle = await run_cli_ai_review(
                    parse_github_pr_url(py01_url),
                    settings=settings,
                    publish=False,
                    http_client=http_client,
                    redis_client=redis_client,
                    session_factory=session_factory,
                    qdrant_store=store,
                    embedder=embedder,
                    llm_factory=llm_factory,
                    span_exporter=span_recorder,
                )
                assert cross_bundle.ai_result.cache_hits == 2
                assert cross_bundle.ai_result.llm_calls == 0
                assert cross_bundle.ai_result.telemetry is None

                async with session_factory() as db:
                    reviews = (await db.execute(sa.select(PRReview))).scalars().all()
                    findings = (await db.execute(sa.select(ReviewFinding))).scalars().all()
                    assert len(reviews) == 11
                    assert len(findings) >= 8

                assert (
                    await asyncio.to_thread(
                        zerocode_auditor.audit_live_collection,
                        settings.qdrant_http_url,
                        isolated_qdrant_collection,
                        scan_all=True,
                        strict_indexes=True,
                        canary_substrings=ALL_M3_CANARIES,
                    )
                    == 0
                )
                qdrant_container = (
                    settings.codesight_qdrant_container or ""
                ).strip() or "codesight-qdrant"
                assert (
                    await asyncio.to_thread(
                        zerocode_auditor.scan_container_storage_for_canaries,
                        qdrant_container,
                        isolated_qdrant_collection,
                        ALL_M3_CANARIES,
                    )
                    == []
                )
                assert len(span_recorder.spans) > 0
                span_records = [
                    {
                        "name": sp.name,
                        "status": getattr(sp.status, "description", None),
                        "attributes": dict(sp.attributes or {}),
                        "events": [
                            {"name": ev.name, "attributes": dict(ev.attributes or {})}
                            for ev in (sp.events or ())
                        ],
                    }
                    for sp in span_recorder.spans
                ]
                span_blob = json.dumps(span_records, default=str)
                for secret_or_canary in (
                    *ALL_M3_CANARIES,
                    E2E_WEBHOOK_SECRET,
                    "ghp_m3_cli_token",
                    "fake-m3-gemini-key",
                    "sk-lf-m3-hermetic",
                ):
                    assert secret_or_canary not in span_blob, (
                        f"NFR-3 canary/secret leaked into OTel span: {secret_or_canary}"
                    )
        finally:
            await _cleanup_m3_resources(
                session_factory=session_factory,
                redis_client=redis_client,
                store=store,
                embedder=embedder,
            )


def _has_live_m3_credentials() -> bool:
    if os.getenv("CODESIGHT_RUN_LIVE_M3_EVAL") != "1":
        return False
    try:
        cfg = Settings()
    except Exception:
        return False
    return cfg.has_gemini_api_key and cfg.has_langfuse_credentials and cfg.langfuse_tracing_enabled


async def _fetch_langfuse_trace_with_retry(
    *,
    host: str,
    public_key: str,
    secret_key: str,
    trace_id: str,
) -> httpx.Response:
    url = f"{host.rstrip('/')}/api/public/v2/observations"
    async with httpx.AsyncClient(timeout=10.0) as client:
        last_resp: httpx.Response | None = None
        for _ in range(6):
            last_resp = await client.get(
                url,
                params={"traceId": trace_id, "fields": "core,basic,usage,io"},
                auth=(public_key, secret_key),
            )
            if last_resp.status_code == 200 and len(last_resp.json().get("data", [])) > 0:
                return last_resp
            await asyncio.sleep(2.0)
        assert last_resp is not None
        return last_resp


@pytest.mark.skipif(
    not _has_live_m3_credentials(),
    reason=(
        "Opt-in live M3 evaluation disabled "
        "(set CODESIGHT_RUN_LIVE_M3_EVAL=1 with GEMINI_API_KEY and LANGFUSE_* keys)."
    ),
)
@pytest.mark.integration
@pytest.mark.asyncio
async def test_week3_m3_opt_in_live_gemini_10_prs_rpm_paced_and_langfuse_cloud(
    isolated_postgres_dsn: str,
    isolated_qdrant_collection: str,
    guard_validator: Any,
) -> None:
    """Live Gemini Free Tier 10-PR suite paced <=8 calls/min with Langfuse Cloud REST proof."""
    b64_pem, _ = generate_ephemeral_rsa_pair()
    live_settings = Settings().model_copy(
        update={
            "qdrant_collection_name": isolated_qdrant_collection,
            "github_app_id": "10022",
            "github_private_key": SecretStr(b64_pem),
            "github_webhook_secret": SecretStr(E2E_WEBHOOK_SECRET),
            "github_token": SecretStr("ghp_m3_live_mock_pub"),
            "index_embed_code_chunks": True,
        }
    )
    py_base = _build_python_base_repo()
    ts_base = _build_ts_base_repo()
    scenarios = _build_10_pr_scenarios(py_base, ts_base)
    scenarios_by_id = {sc.pr_id: sc for sc in scenarios}
    hub = _MockGitHubHub(
        repos_base={PY_REPO_SLUG: py_base, TS_REPO_SLUG: ts_base},
        scenarios=scenarios,
    )
    pacer = SlidingWindowRPMPacer(max_calls_per_window=8, window_seconds=60.0)
    live_llm = LLMFactory(settings=live_settings)
    live_embedder = GeminiEmbedder(settings=live_settings)

    async with _isolated_session_factory(isolated_postgres_dsn) as session_factory:
        redis_client: redis_async.Redis | None = None
        store: QdrantZeroCodeStore | None = None
        try:
            redis_client = redis_async.Redis.from_url(
                live_settings.redis_url, decode_responses=True, socket_timeout=1.5
            )
            store = QdrantZeroCodeStore(settings=live_settings)
            async with httpx.AsyncClient(transport=httpx.MockTransport(hub.handler)) as http_client:
                await run_cli_index(
                    PY_REPO_SLUG,
                    settings=live_settings,
                    http_client=http_client,
                    redis_client=redis_client,
                    session_factory=session_factory,
                    store=store,
                    embedder=live_embedder,
                )
                await run_cli_index(
                    TS_REPO_SLUG,
                    settings=live_settings,
                    http_client=http_client,
                    redis_client=redis_client,
                    session_factory=session_factory,
                    store=store,
                    embedder=live_embedder,
                )

                trace_ids: list[str] = []
                llm_calls_by_pr: dict[str, int] = {}
                for sc in scenarios:
                    await pacer.wait_for_slot(estimated_calls=3)
                    t0 = time.perf_counter()
                    pr_url = f"https://github.com/{sc.repo_slug}/pull/{sc.pr_number}"
                    bundle = await run_cli_ai_review(
                        parse_github_pr_url(pr_url),
                        settings=live_settings,
                        publish=True,
                        http_client=http_client,
                        redis_client=redis_client,
                        session_factory=session_factory,
                        qdrant_store=store,
                        embedder=live_embedder,
                        llm_factory=live_llm,
                    )
                    elapsed_s = time.perf_counter() - t0
                    assert elapsed_s < 25.0, f"{sc.pr_id} exceeded 25s SLA: {elapsed_s:.2f}s"
                    if bundle.ai_result.llm_calls > 0:
                        pacer.record_calls(bundle.ai_result.llm_calls)
                    llm_calls_by_pr[sc.pr_id] = bundle.ai_result.llm_calls
                    assert bundle.ai_result.telemetry is not None
                    assert bundle.ai_result.telemetry.status == "active"
                    assert bundle.ai_result.telemetry.flushed is True
                    if bundle.ai_result.telemetry.trace_id:
                        trace_ids.append(bundle.ai_result.telemetry.trace_id)

                assert len(trace_ids) == 10
                assert len(hub.published_reviews) == 10
                patched_py_state = dict(py_base)
                patched_ts_state = dict(ts_base)

                for record in hub.published_reviews:
                    sc = scenarios_by_id[record["pr_id"]]
                    payload = record["payload"]
                    diff_map = guard_validator.map_right_hunk_lines(sc.diff_text)
                    v_errors = guard_validator.validate_review_comments(
                        diff_map, payload.get("comments", [])
                    )
                    assert v_errors == []
                    target_map = patched_py_state if sc.language == "python" else patched_ts_state
                    changed_subset = {fp: sc.after_files[fp] for fp in sc.changed_files}

                    if sc.expected_clean:
                        assert payload["comments"] == []
                        assert "Technical_Debt_Score-100" in payload["body"]
                        assert llm_calls_by_pr[sc.pr_id] == 2
                        target_map.update(changed_subset)
                    else:
                        assert len(payload.get("comments", [])) >= 1, (
                            f"{sc.pr_id} produced 0 inline comments in live run"
                        )
                        fixed_subset, applied = apply_published_github_suggestions(
                            changed_subset, payload, strict_indent=False
                        )
                        assert applied >= 1, f"{sc.pr_id} emitted 0 applicable suggestions"
                        target_map.update(fixed_subset)

                await asyncio.to_thread(
                    _assert_patched_repos_build_and_test, patched_py_state, patched_ts_state
                )

                assert live_settings.langfuse_public_key and live_settings.langfuse_secret_key
                trace_resp = await _fetch_langfuse_trace_with_retry(
                    host=live_settings.langfuse_host,
                    public_key=live_settings.langfuse_public_key,
                    secret_key=live_settings.langfuse_secret_key.get_secret_value(),
                    trace_id=trace_ids[-1],
                )
                assert trace_resp.status_code == 200, (
                    f"Langfuse Cloud trace {trace_ids[-1]} lookup failed: {trace_resp.status_code}"
                )
                assert len(trace_resp.json().get("data", [])) > 0
                for canary in ALL_M3_CANARIES:
                    assert canary not in trace_resp.text, (
                        f"NFR-3 canary leaked into Langfuse Cloud trace: {canary}"
                    )
        finally:
            await _cleanup_m3_resources(
                session_factory=session_factory,
                redis_client=redis_client,
                store=store,
                embedder=live_embedder,
            )
