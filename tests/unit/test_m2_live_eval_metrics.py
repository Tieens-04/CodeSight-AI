"""Unit tests for Milestone 2 live evaluation metrics, KPI gates, and harness (CA-16)."""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import math
import sys
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from codesight.core.config import Settings
from codesight.rag.embedder import GeminiEmbedder
from codesight.rag.indexer import InitialIndexReport
from codesight.rag.live_eval import (
    DEFAULT_LIVE_PY_REPO,
    M2LiveEvaluationReport,
    M2RepoEvalSummary,
    compute_recall_at_k,
    discover_live_ts_repo,
    run_milestone2_live_evaluation,
    validate_m2_kpi_thresholds,
)
from codesight.rag.models import EMBEDDING_DIMENSIONS, QdrantStoredPoint
from codesight.rag.retriever import DEFAULT_DOC_SCORE_THRESHOLD


def _sha1_blob(content: bytes) -> str:
    return hashlib.sha1(
        b"blob " + str(len(content)).encode("ascii") + b"\x00" + content
    ).hexdigest()


def _blob_json(content: bytes, sha: str) -> dict[str, Any]:
    return {
        "sha": sha,
        "size": len(content),
        "encoding": "base64",
        "content": base64.b64encode(content).decode("ascii"),
    }


def _unit_vec(primary_idx: int, secondary_idx: int = 1, weight: float = 0.5) -> list[float]:
    vec = [0.0] * EMBEDDING_DIMENSIONS
    vec[primary_idx] = 1.0
    vec[secondary_idx] = weight
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec]


def _make_sample_report(**overrides: Any) -> M2LiveEvaluationReport:
    base: dict[str, Any] = {
        "py_repo": "Tieens-04/CodeSight-AI",
        "ts_repo": "acme/react-dashboard",
        "collection_name": "codesight_ast_index_eval",
        "recall_at_5": 0.95,
        "p50_retrieval_ms": 210.5,
        "tokens_saved_pct": 58.2,
        "zerocode_violations": 0,
        "p95_retrieval_ms": 340.0,
        "cold_retrieval_ms": 510.0,
        "max_packed_aux_tokens": 2400,
        "doc_score_threshold": DEFAULT_DOC_SCORE_THRESHOLD,
        "doc_raw_scores_observed": (0.58, 0.71),
        "disk_violations": (),
    }
    base.update(overrides)
    return M2LiveEvaluationReport(**base)


def _make_repo_summary(
    *,
    repo_full_name: str,
    language: str,
    recall_at_5: float = 0.95,
    p50_retrieval_ms: float = 180.0,
    tokens_saved_pct: float = 55.0,
    cases_evaluated: int = 2,
    max_packed_aux_tokens: int = 2000,
) -> M2RepoEvalSummary:
    return M2RepoEvalSummary(
        repo_full_name=repo_full_name,
        language=language,
        upserted_points=24,
        code_chunks=20,
        doc_chunks=4,
        index_elapsed_ms=420.0,
        cases_evaluated=cases_evaluated,
        recall_at_5=recall_at_5,
        p50_retrieval_ms=p50_retrieval_ms,
        p95_retrieval_ms=p50_retrieval_ms + 30.0,
        cold_retrieval_ms=p50_retrieval_ms + 120.0,
        tokens_saved_pct=tokens_saved_pct,
        max_packed_aux_tokens=max_packed_aux_tokens,
        doc_threshold_used=DEFAULT_DOC_SCORE_THRESHOLD,
        doc_max_raw_score=0.58,
        doc_hits_above_threshold=0,
    )


# ---------------------------------------------------------------------------
# Recall@K & KPI Threshold Validation Tests
# ---------------------------------------------------------------------------


def test_compute_recall_at_k_handles_bounds_duplicates_and_empty_targets() -> None:
    """Strict Recall@K must ignore ranks past K and handle empty/partial targets cleanly."""
    assert compute_recall_at_k(["a", "b", "c"], {"a", "b"}, k=5) == 1.0
    assert compute_recall_at_k(["a", "x", "y", "z"], {"a", "b", "c", "d"}, k=5) == 0.25
    # Rank #6 must never leak into Top-5 even with duplicate entries in slots 1..5
    assert (
        compute_recall_at_k(
            ["noise", "noise", "noise", "noise", "noise", "target"],
            {"target"},
            k=5,
        )
        == 0.0
    )
    # When expected targets exceed k, denominator is capped at k
    assert (
        compute_recall_at_k(
            ["t1", "t2", "t3", "t4", "t5", "t6"],
            {"t1", "t2", "t3", "t4", "t5", "t6", "t7"},
            k=5,
        )
        == 1.0
    )
    assert compute_recall_at_k([], set(), k=5) == 1.0
    assert compute_recall_at_k([], {"t1"}, k=5) == 0.0

    with pytest.raises(ValueError):
        compute_recall_at_k(["a"], {"a"}, k=0)
    with pytest.raises(ValueError):
        compute_recall_at_k(["a"], {"a"}, k=True)


def test_validate_m2_kpi_thresholds_accepts_compliant_report() -> None:
    """A compliant dual-repo evaluation report returns zero KPI violations."""
    report = _make_sample_report(
        recall_at_5=0.85,
        p50_retrieval_ms=849.99,
        tokens_saved_pct=45.0,
        max_packed_aux_tokens=3500,
        py_summary=_make_repo_summary(repo_full_name="Tieens-04/CodeSight-AI", language="python"),
        ts_summary=_make_repo_summary(repo_full_name="acme/react-dashboard", language="typescript"),
    )
    assert validate_m2_kpi_thresholds(report) == []


@pytest.mark.parametrize(
    ("overrides", "expected_fragment"),
    [
        ({"py_repo": "   "}, "py_repo"),
        ({"ts_repo": ""}, "ts_repo"),
        ({"ts_repo": "   "}, "ts_repo"),
        ({"recall_at_5": 0.8499}, "recall_at_5"),
        ({"recall_at_5": float("nan")}, "recall_at_5"),
        ({"p50_retrieval_ms": 850.0}, "p50_retrieval_ms"),
        ({"p50_retrieval_ms": 912.3}, "p50_retrieval_ms"),
        ({"p50_retrieval_ms": float("inf")}, "p50_retrieval_ms"),
        ({"tokens_saved_pct": 44.99}, "tokens_saved_pct"),
        ({"max_packed_aux_tokens": 3501}, "max_packed_aux_tokens"),
        ({"zerocode_violations": 1}, "zerocode_violations"),
        ({"disk_violations": ("NFR-3 disk breach",)}, "disk_violations"),
    ],
)
def test_validate_m2_kpi_thresholds_flags_every_kpi_violation(
    overrides: dict[str, Any],
    expected_fragment: str,
) -> None:
    """Every Milestone 2 KPI breach must be reported with an actionable violation string."""
    report = _make_sample_report(**overrides)
    violations = validate_m2_kpi_thresholds(report)
    assert violations, f"Expected violation containing {expected_fragment!r}"
    assert any(expected_fragment in msg for msg in violations), violations


def test_validate_m2_kpi_thresholds_checks_per_repo_summaries() -> None:
    """A passing global average must not mask a failing Python or TypeScript repo summary."""
    report = _make_sample_report(
        recall_at_5=0.90,
        py_summary=_make_repo_summary(
            repo_full_name="Tieens-04/CodeSight-AI",
            language="python",
            recall_at_5=1.0,
        ),
        ts_summary=_make_repo_summary(
            repo_full_name="acme/react-dashboard",
            language="typescript",
            recall_at_5=0.80,
            p50_retrieval_ms=860.0,
            tokens_saved_pct=42.0,
        ),
    )
    violations = validate_m2_kpi_thresholds(report)
    assert any("acme/react-dashboard" in msg and "recall_at_5" in msg for msg in violations)
    assert any("acme/react-dashboard" in msg and "p50_retrieval_ms" in msg for msg in violations)
    assert any("acme/react-dashboard" in msg and "tokens_saved_pct" in msg for msg in violations)


# ---------------------------------------------------------------------------
# GitHub TypeScript/TSX Auto-Discovery Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_discover_live_ts_repo_selects_typescript_or_tsx_repo() -> None:
    """Auto-discovery queries /user/repos?sort=updated&per_page=50 and picks TS/TSX repos."""
    requested_urls: list[str] = []

    async def _handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        assert request.headers.get("Authorization") == "Bearer ghp_test_pat"
        return httpx.Response(
            200,
            json=[
                {"full_name": "Tieens-04/CodeSight-AI", "language": "Python", "archived": False},
                {"full_name": "Tieens-04/old-ts", "language": "TypeScript", "archived": True},
                {"full_name": "Tieens-04/active-web", "language": "TSX", "archived": False},
            ],
        )

    settings = Settings(github_token=SecretStr("ghp_test_pat"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        selected = await discover_live_ts_repo(settings=settings, http_client=client)

    assert selected == "Tieens-04/active-web"
    assert requested_urls == ["https://api.github.com/user/repos?sort=updated&per_page=50"]


@pytest.mark.asyncio
async def test_discover_live_ts_repo_fails_loudly_when_unavailable() -> None:
    """Missing credentials, empty TS repos, or HTTP failures must raise a clear ValueError."""
    no_creds = Settings(github_token=None, github_app_id=None, github_private_key=None)
    with pytest.raises(ValueError, match="CODESIGHT_LIVE_TS_REPO"):
        await discover_live_ts_repo(settings=no_creds)

    async def _only_python(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[{"full_name": "Tieens-04/CodeSight-AI", "language": "Python"}],
        )

    settings = Settings(github_token=SecretStr("ghp_test_pat"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(_only_python)) as client:
        with pytest.raises(ValueError, match="CODESIGHT_LIVE_TS_REPO"):
            await discover_live_ts_repo(settings=settings, http_client=client)

    async def _server_error(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_server_error)) as client:
        with pytest.raises(ValueError, match="CODESIGHT_LIVE_TS_REPO"):
            await discover_live_ts_repo(settings=settings, http_client=client)


# ---------------------------------------------------------------------------
# End-to-End Synthetic Live Evaluation Harness Tests
# ---------------------------------------------------------------------------


@dataclass
class _InMemoryEvalStore:
    """In-memory QdrantZeroCodeStore test double supporting scroll + vector search."""

    collection_name: str
    points: dict[str, dict[str, Any]] = field(default_factory=dict)
    vectors: dict[str, list[float]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.client = self

    def add_point(
        self,
        *,
        payload: dict[str, Any],
        vector: list[float],
        point_id: str | None = None,
    ) -> str:
        pid = point_id or str(uuid.uuid4())
        self.points[pid] = dict(payload)
        self.vectors[pid] = list(vector)
        return pid

    @staticmethod
    def _matches_filter(payload: Mapping[str, Any], scroll_filter: Any) -> bool:
        if scroll_filter is None:
            return True
        for cond in getattr(scroll_filter, "must", None) or ():
            key = getattr(cond, "key", None)
            match = getattr(cond, "match", None)
            if not key or match is None:
                continue
            val = getattr(match, "value", None)
            any_vals = getattr(match, "any", None)
            field_val = payload.get(key)
            if val is not None and field_val != val:
                return False
            if any_vals is not None:
                if isinstance(field_val, list):
                    if not any(item in any_vals for item in field_val):
                        return False
                elif field_val not in any_vals:
                    return False
        return True

    async def scroll(
        self,
        *,
        collection_name: str,
        scroll_filter: Any = None,
        limit: int = 256,
        offset: Any = None,
        with_payload: bool = True,
        with_vectors: bool = False,
    ) -> tuple[list[SimpleNamespace], Any]:
        del collection_name, with_payload, with_vectors
        matching = [
            SimpleNamespace(id=pid, payload=dict(payload))
            for pid, payload in self.points.items()
            if self._matches_filter(payload, scroll_filter)
        ]
        start = int(offset) if isinstance(offset, int) else 0
        batch = matching[start : start + limit]
        next_offset = start + limit if start + limit < len(matching) else None
        return batch, next_offset

    async def find_by_called_symbols(
        self,
        called_symbols: Sequence[str],
        *,
        repo_full_name: str | None = None,
        repo_id: str | None = None,
        limit: int = 50,
    ) -> tuple[QdrantStoredPoint, ...]:
        del repo_id
        targets = set(called_symbols)
        out: list[QdrantStoredPoint] = []
        for pid, payload in self.points.items():
            if repo_full_name and payload.get("repo_full_name") != repo_full_name:
                continue
            if payload.get("chunk_type") != "code":
                continue
            called = payload.get("called_symbols") or []
            if any(sym in targets for sym in called):
                out.append(QdrantStoredPoint(point_id=pid, payload=dict(payload), score=None))
        return tuple(out[:limit])

    async def find_by_symbol_names(
        self,
        symbol_names: Sequence[str],
        *,
        repo_full_name: str | None = None,
        repo_id: str | None = None,
        limit: int = 50,
    ) -> tuple[QdrantStoredPoint, ...]:
        del repo_id
        targets = set(symbol_names)
        out: list[QdrantStoredPoint] = []
        for pid, payload in self.points.items():
            if repo_full_name and payload.get("repo_full_name") != repo_full_name:
                continue
            if payload.get("chunk_type") != "code":
                continue
            if payload.get("symbol_name") in targets:
                out.append(QdrantStoredPoint(point_id=pid, payload=dict(payload), score=None))
        return tuple(out[:limit])

    async def search_similar(
        self,
        query_vector: Sequence[float],
        *,
        repo_full_name: str | None = None,
        repo_id: str | None = None,
        chunk_type: str | None = None,
        score_threshold: float | None = None,
        limit: int = 10,
    ) -> tuple[QdrantStoredPoint, ...]:
        del repo_id
        hits: list[QdrantStoredPoint] = []
        for pid, payload in self.points.items():
            if repo_full_name and payload.get("repo_full_name") != repo_full_name:
                continue
            if chunk_type and payload.get("chunk_type") != chunk_type:
                continue
            vec = self.vectors[pid]
            score = sum(a * b for a, b in zip(query_vector, vec, strict=True))
            if score_threshold is not None and score < score_threshold:
                continue
            hits.append(QdrantStoredPoint(point_id=pid, payload=dict(payload), score=score))
        hits.sort(key=lambda pt: -(pt.score or 0.0))
        return tuple(hits[:limit])


class _FakeEvalGenAIClient:
    """Simulates realistic query vectors where architecture docs cluster around ~0.58 cosine."""

    def __init__(self) -> None:
        self.aio = SimpleNamespace(models=self)

    async def embed_content(
        self,
        *,
        model: str,
        contents: list[str],
        config: Any = None,
    ) -> Any:
        del model, config
        # Query vector yields ~0.58 cosine similarity against _unit_vec(0, 1, 1.4)
        # Wait: dot([1, 0], [cos(theta), sin(theta)]) == cos(theta); 1 / sqrt(1 + 1.4^2) = 0.5812!
        del contents
        return SimpleNamespace(
            embeddings=[SimpleNamespace(values=_unit_vec(0, 2, 0.0)) for _ in range(1)]
        )


def _build_synthetic_repo_blobs() -> tuple[dict[str, dict[str, bytes]], dict[str, bytes]]:
    py_service = (
        b'"""Payment domain helpers."""\n\n'
        b"from src.clients.gateway import dispatch_charge\n\n\n"
        b"def compute_invoice_total(subtotal_cents: int, fee_bps: int) -> int:\n"
        b'    """Compute total cents including gateway processing fees."""\n'
        b"    normalized_fee = (subtotal_cents * fee_bps) // 10_000\n"
        b"    return subtotal_cents + normalized_fee\n\n\n"
        + b"\n\n".join(
            f"def aux_py_helper_{i}(val: int) -> int:\n"
            f"    step_one = val + {i}\n"
            f"    step_two = step_one * 2\n"
            f"    return step_two\n".encode()
            for i in range(1, 14)
        )
    )
    py_router = (
        b'"""Orders HTTP router."""\n\n'
        b"from src.services.billing import compute_invoice_total\n\n\n"
        b"def submit_order_route(order_id: str, subtotal_cents: int) -> dict[str, int]:\n"
        b'    """Submit order and compute invoice total."""\n'
        b"    total = compute_invoice_total(subtotal_cents, 250)\n"
        b"    return {'order': len(order_id), 'total': total}\n\n\n"
        + b"\n\n".join(
            f"def aux_router_helper_{i}(flag: bool) -> int:\n"
            f"    base_val = {i} if flag else 0\n"
            f"    return base_val + 1\n".encode()
            for i in range(1, 14)
        )
    )
    py_worker = (
        b'"""Settlement worker."""\n\n'
        b"from src.services.billing import compute_invoice_total\n\n\n"
        b"def settle_daily_batch(subtotal_cents: int) -> int:\n"
        b'    """Settle nightly merchant payouts."""\n'
        b"    return compute_invoice_total(subtotal_cents, 150)\n\n\n"
        + b"\n\n".join(
            f"def aux_worker_helper_{i}(count: int) -> int:\n"
            f"    adjusted = count + {i}\n"
            f"    return adjusted * 3\n".encode()
            for i in range(1, 14)
        )
    )
    py_readme = b"# Billing Architecture\n\n## Invoice Fees\nAlways use compute_invoice_total.\n"

    ts_hook = (
        b"import { formatCurrencyValue } from './moneyFormat';\n\n"
        b"export function buildCheckoutSummary(cents: number, taxBps: number): string {\n"
        b"  const tax = Math.floor((cents * taxBps) / 10000);\n"
        b"  return formatCurrencyValue(cents + tax);\n"
        b"}\n\n"
        + b"\n".join(
            f"export function auxTsHelper{i}(x: number): number {{\n"
            f"  const temp = x + {i};\n"
            f"  return temp * 2;\n"
            f"}}\n".encode()
            for i in range(1, 14)
        )
    )
    ts_page = (
        b"import { buildCheckoutSummary } from './checkoutSummary';\n\n"
        b"export function CheckoutScreen(props: { cents: number }): string {\n"
        b"  const renderedSummaryText = buildCheckoutSummary(props.cents, 800);\n"
        b"  return `<div>${renderedSummaryText}</div>`;\n"
        b"}\n\n"
        + b"\n".join(
            f"export function auxPageHelper{i}(label: string): string {{\n"
            f"  const cleanedLabel = label.trim();\n"
            f"  return `${{cleanedLabel}}-${i}`;\n"
            f"}}\n".encode()
            for i in range(1, 14)
        )
    )
    ts_drawer = (
        b"import { buildCheckoutSummary } from './checkoutSummary';\n\n"
        b"export function CartPreviewDrawer(cents: number): string {\n"
        b"  const summaryText = buildCheckoutSummary(cents, 500);\n"
        b"  return summaryText.toUpperCase();\n"
        b"}\n\n"
        + b"\n".join(
            f"export function auxDrawerHelper{i}(n: number): number {{\n"
            f"  return n + {i};\n"
            f"}}\n".encode()
            for i in range(1, 14)
        )
    )
    ts_readme = b"# Web Checkout\n\n## Summary Rendering\nUse buildCheckoutSummary.\n"

    repo_files = {
        "Tieens-04/CodeSight-AI": {
            "src/services/billing.py": py_service,
            "src/routers/orders.py": py_router,
            "src/workers/settlement.py": py_worker,
            "README.md": py_readme,
        },
        "acme/react-dashboard": {
            "src/hooks/checkoutSummary.ts": ts_hook,
            "src/pages/CheckoutScreen.tsx": ts_page,
            "src/components/CartPreviewDrawer.tsx": ts_drawer,
            "README.md": ts_readme,
        },
    }
    blobs_by_sha: dict[str, bytes] = {}
    for files in repo_files.values():
        for raw in files.values():
            blobs_by_sha[_sha1_blob(raw)] = raw
    return repo_files, blobs_by_sha


@pytest.mark.asyncio
async def test_run_milestone2_live_evaluation_auto_discovers_ts_and_captures_raw_doc_scores() -> (
    None
):
    """Offline E2E harness test verifying TS discovery, warm telemetry, and doc score logging."""
    repo_files, blobs_by_sha = _build_synthetic_repo_blobs()
    store = _InMemoryEvalStore(collection_name="eval_test_col")
    indexed_calls: list[tuple[str, bool, bool | None]] = []

    async def _fake_index_fn(
        repo_full_name: str,
        *,
        settings: Settings,
        http_client: httpx.AsyncClient,
        redis_client: Any,
        session_factory: Any,
        store: _InMemoryEvalStore,
        embedder: GeminiEmbedder,
        embed_code_chunks: bool,
    ) -> InitialIndexReport:
        del http_client, redis_client, session_factory, embedder
        indexed_calls.append((repo_full_name, settings.index_embed_code_chunks, embed_code_chunks))
        files = repo_files[repo_full_name]
        if repo_full_name == DEFAULT_LIVE_PY_REPO:
            store.add_point(
                payload={
                    "repo_full_name": repo_full_name,
                    "file_path": "src/services/billing.py",
                    "blob_sha": _sha1_blob(files["src/services/billing.py"]),
                    "chunk_type": "code",
                    "language": "python",
                    "symbol_name": "compute_invoice_total",
                    "symbol_kind": "function",
                    "start_line": 6,
                    "end_line": 9,
                    "signature": (
                        "def compute_invoice_total(subtotal_cents: int, fee_bps: int) -> int"
                    ),
                    "called_symbols": [],
                    "imports": ["src.clients.gateway.dispatch_charge"],
                    "cyclomatic_complexity": 1,
                },
                vector=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
            )
            store.add_point(
                payload={
                    "repo_full_name": repo_full_name,
                    "file_path": "src/services/billing.py",
                    "blob_sha": _sha1_blob(files["src/services/billing.py"]),
                    "chunk_type": "code",
                    "language": "python",
                    "symbol_name": "aux_py_helper_1",
                    "symbol_kind": "function",
                    "start_line": 12,
                    "end_line": 15,
                    "signature": "def aux_py_helper_1(val: int) -> int",
                    "called_symbols": [],
                    "imports": [],
                    "cyclomatic_complexity": 1,
                },
                vector=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
            )
            store.add_point(
                payload={
                    "repo_full_name": repo_full_name,
                    "file_path": "src/routers/orders.py",
                    "blob_sha": _sha1_blob(files["src/routers/orders.py"]),
                    "chunk_type": "code",
                    "language": "python",
                    "symbol_name": "submit_order_route",
                    "symbol_kind": "function",
                    "start_line": 6,
                    "end_line": 9,
                    "signature": (
                        "def submit_order_route(order_id: str, subtotal_cents: int) "
                        "-> dict[str, int]"
                    ),
                    "called_symbols": ["compute_invoice_total"],
                    "imports": ["src.services.billing.compute_invoice_total"],
                    "cyclomatic_complexity": 1,
                },
                vector=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
            )
            store.add_point(
                payload={
                    "repo_full_name": repo_full_name,
                    "file_path": "src/workers/settlement.py",
                    "blob_sha": _sha1_blob(files["src/workers/settlement.py"]),
                    "chunk_type": "code",
                    "language": "python",
                    "symbol_name": "settle_daily_batch",
                    "symbol_kind": "function",
                    "start_line": 6,
                    "end_line": 8,
                    "signature": "def settle_daily_batch(subtotal_cents: int) -> int",
                    "called_symbols": ["compute_invoice_total"],
                    "imports": ["src.services.billing.compute_invoice_total"],
                    "cyclomatic_complexity": 1,
                },
                vector=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
            )
            store.add_point(
                payload={
                    "repo_full_name": repo_full_name,
                    "file_path": "README.md",
                    "blob_sha": _sha1_blob(files["README.md"]),
                    "chunk_type": "architecture_doc",
                    "language": "markdown",
                    "symbol_name": "Billing Architecture > Invoice Fees",
                    "symbol_kind": "section",
                    "start_line": 1,
                    "end_line": 4,
                    "signature": "## Invoice Fees",
                    "called_symbols": [],
                    "imports": [],
                    "cyclomatic_complexity": 0,
                },
                # Dot product against _unit_vec(0, 2, 0.0) == 1 / sqrt(1 + 1.4^2) ~= 0.5812 (< 0.65)
                vector=_unit_vec(0, 1, 1.4),
            )
        else:
            store.add_point(
                payload={
                    "repo_full_name": repo_full_name,
                    "file_path": "src/hooks/checkoutSummary.ts",
                    "blob_sha": _sha1_blob(files["src/hooks/checkoutSummary.ts"]),
                    "chunk_type": "code",
                    "language": "typescript",
                    "symbol_name": "buildCheckoutSummary",
                    "symbol_kind": "function",
                    "start_line": 3,
                    "end_line": 6,
                    "signature": (
                        "export function buildCheckoutSummary(cents: number, taxBps: number): "
                        "string"
                    ),
                    "called_symbols": ["formatCurrencyValue"],
                    "imports": ["./moneyFormat.formatCurrencyValue"],
                    "cyclomatic_complexity": 1,
                },
                vector=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
            )
            store.add_point(
                payload={
                    "repo_full_name": repo_full_name,
                    "file_path": "src/hooks/checkoutSummary.ts",
                    "blob_sha": _sha1_blob(files["src/hooks/checkoutSummary.ts"]),
                    "chunk_type": "code",
                    "language": "typescript",
                    "symbol_name": "auxTsHelper1",
                    "symbol_kind": "function",
                    "start_line": 8,
                    "end_line": 11,
                    "signature": "export function auxTsHelper1(x: number): number",
                    "called_symbols": [],
                    "imports": [],
                    "cyclomatic_complexity": 1,
                },
                vector=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
            )
            store.add_point(
                payload={
                    "repo_full_name": repo_full_name,
                    "file_path": "src/pages/CheckoutScreen.tsx",
                    "blob_sha": _sha1_blob(files["src/pages/CheckoutScreen.tsx"]),
                    "chunk_type": "code",
                    "language": "tsx",
                    "symbol_name": "CheckoutScreen",
                    "symbol_kind": "function",
                    "start_line": 3,
                    "end_line": 6,
                    "signature": "export function CheckoutScreen(props: { cents: number }): string",
                    "called_symbols": ["buildCheckoutSummary"],
                    "imports": ["./checkoutSummary.buildCheckoutSummary"],
                    "cyclomatic_complexity": 1,
                },
                vector=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
            )
            store.add_point(
                payload={
                    "repo_full_name": repo_full_name,
                    "file_path": "src/components/CartPreviewDrawer.tsx",
                    "blob_sha": _sha1_blob(files["src/components/CartPreviewDrawer.tsx"]),
                    "chunk_type": "code",
                    "language": "tsx",
                    "symbol_name": "CartPreviewDrawer",
                    "symbol_kind": "function",
                    "start_line": 3,
                    "end_line": 6,
                    "signature": "export function CartPreviewDrawer(cents: number): string",
                    "called_symbols": ["buildCheckoutSummary"],
                    "imports": ["./checkoutSummary.buildCheckoutSummary"],
                    "cyclomatic_complexity": 1,
                },
                vector=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
            )
            store.add_point(
                payload={
                    "repo_full_name": repo_full_name,
                    "file_path": "README.md",
                    "blob_sha": _sha1_blob(files["README.md"]),
                    "chunk_type": "architecture_doc",
                    "language": "markdown",
                    "symbol_name": "Web Checkout > Summary Rendering",
                    "symbol_kind": "section",
                    "start_line": 1,
                    "end_line": 4,
                    "signature": "## Summary Rendering",
                    "called_symbols": [],
                    "imports": [],
                    "cyclomatic_complexity": 0,
                },
                vector=_unit_vec(0, 1, 1.4),
            )

        return InitialIndexReport(
            repo_full_name=repo_full_name,
            repo_id=str(uuid.uuid4()),
            default_branch="main",
            indexed_branch="main",
            commit_sha="a" * 40,
            tree_entries=len(files),
            eligible_files=len(files),
            unchanged_files_skipped=0,
            fetched_files=len(files),
            skipped_files=0,
            code_chunks=4,
            doc_chunks=1,
            embedded_points=1,
            upserted_points=5,
            stale_points_deleted=0,
            persisted_cursor=True,
            truncated_tree=False,
            elapsed_ms=25.0,
        )

    async def _gh_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/user/repos":
            return httpx.Response(
                200,
                json=[{"full_name": "acme/react-dashboard", "language": "TypeScript"}],
            )
        if "/git/blobs/" in path:
            sha = path.rsplit("/", 1)[-1]
            raw = blobs_by_sha.get(sha)
            if raw is not None:
                return httpx.Response(200, json=_blob_json(raw, sha))
        return httpx.Response(404, json={"message": "Not Found"})

    audit_calls: list[tuple[str, str, bool, bool]] = []
    disk_calls: list[tuple[str, str, tuple[str, ...]]] = []

    def _fake_audit(
        base_url: str,
        collection: str,
        sample_limit: int = 200,
        *,
        scan_all: bool = False,
        strict_indexes: bool = False,
        canary_substrings: Sequence[str] = (),
    ) -> int:
        del sample_limit, canary_substrings
        audit_calls.append((base_url, collection, scan_all, strict_indexes))
        return 0

    def _fake_disk_scan(
        container_name: str,
        collection: str,
        canary_substrings: Sequence[str],
    ) -> list[str]:
        markers = tuple(canary_substrings)
        disk_calls.append((container_name, collection, markers))
        if markers == (DEFAULT_LIVE_PY_REPO,):
            return ["NFR-3 disk breach — raw-code canary matched on Qdrant disk file: seg0"]
        return []

    settings = Settings(
        qdrant_collection_name="eval_test_col",
        github_token=SecretStr("ghp_eval_mock"),
        gemini_api_key=SecretStr("gem_eval_mock"),
        index_embed_code_chunks=True,  # Must be forced to False during evaluation
    )
    fake_session_factory: Any = object()
    fake_redis: Any = SimpleNamespace()
    fake_genai = _FakeEvalGenAIClient()

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(_gh_handler)) as http_client,
        GeminiEmbedder(client=fake_genai) as embedder,  # type: ignore[arg-type]
    ):
        report = await run_milestone2_live_evaluation(
            settings=settings,
            py_repo=DEFAULT_LIVE_PY_REPO,
            ts_repo="",  # Triggers auto-discovery via /user/repos
            collection_name="eval_test_col",
            session_factory=fake_session_factory,
            http_client=http_client,
            redis_client=fake_redis,
            store=store,  # type: ignore[arg-type]
            embedder=embedder,
            index_fn=_fake_index_fn,
            auditor_fn=_fake_audit,
            disk_scanner_fn=_fake_disk_scan,
        )

    assert report.py_repo == DEFAULT_LIVE_PY_REPO
    assert report.ts_repo == "acme/react-dashboard"
    assert report.doc_score_threshold == DEFAULT_DOC_SCORE_THRESHOLD
    assert indexed_calls == [
        (DEFAULT_LIVE_PY_REPO, False, False),
        ("acme/react-dashboard", False, False),
    ]
    assert report.recall_at_5 == 1.0
    assert report.p50_retrieval_ms < 850.0
    assert report.tokens_saved_pct >= 45.0
    assert report.zerocode_violations == 0
    assert report.disk_violations == ()
    assert validate_m2_kpi_thresholds(report) == []

    # Diagnostic raw cosine similarity (~0.5812) captured below DEFAULT_DOC_SCORE_THRESHOLD=0.65
    assert report.py_summary is not None
    assert report.ts_summary is not None
    assert report.py_summary.doc_hits_above_threshold == 0
    assert report.py_summary.doc_max_raw_score is not None
    assert 0.55 <= report.py_summary.doc_max_raw_score <= 0.62
    assert any(0.55 <= s <= 0.62 for s in report.doc_raw_scores_observed)

    # Audit & container /qdrant/storage disk checks ran with strict parameters
    assert audit_calls == [(settings.qdrant_http_url, "eval_test_col", True, True)]
    assert len(disk_calls) == 2
    assert disk_calls[0][2] == (DEFAULT_LIVE_PY_REPO,)
    assert len(disk_calls[1][2]) >= 1  # Extracted real interior body lines verified absent on disk

    # Serializes cleanly to JSON
    serialized = json.dumps(report.to_dict())
    assert '"ts_repo": "acme/react-dashboard"' in serialized


@pytest.mark.asyncio
async def test_run_milestone2_live_evaluation_raises_when_repo_has_no_cross_file_callers() -> None:
    """Evaluation must fail loudly if an indexed repository has zero cross-file callers."""
    store = _InMemoryEvalStore(collection_name="empty_graph_col")

    async def _empty_index_fn(*args: Any, **kwargs: Any) -> InitialIndexReport:
        del args, kwargs
        return InitialIndexReport(
            repo_full_name=DEFAULT_LIVE_PY_REPO,
            repo_id="r1",
            default_branch="main",
            indexed_branch="main",
            commit_sha="b" * 40,
            tree_entries=1,
            eligible_files=1,
            unchanged_files_skipped=0,
            fetched_files=1,
            skipped_files=0,
            code_chunks=0,
            doc_chunks=0,
            embedded_points=0,
            upserted_points=0,
            stale_points_deleted=0,
            persisted_cursor=True,
            truncated_tree=False,
            elapsed_ms=5.0,
        )

    settings = Settings(
        qdrant_collection_name="empty_graph_col",
        github_token=SecretStr("ghp_eval_mock"),
        gemini_api_key=SecretStr("gem_eval_mock"),
    )
    fake_genai = _FakeEvalGenAIClient()

    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(404))
        ) as http_client,
        GeminiEmbedder(client=fake_genai) as embedder,  # type: ignore[arg-type]
    ):
        with pytest.raises(ValueError, match="cross-file"):
            await run_milestone2_live_evaluation(
                settings=settings,
                py_repo=DEFAULT_LIVE_PY_REPO,
                ts_repo="acme/react-dashboard",
                collection_name="empty_graph_col",
                session_factory=object(),  # type: ignore[arg-type]
                http_client=http_client,
                redis_client=SimpleNamespace(),  # type: ignore[arg-type]
                store=store,  # type: ignore[arg-type]
                embedder=embedder,
                index_fn=_empty_index_fn,
            )


@pytest.mark.asyncio
async def test_run_milestone2_live_evaluation_reports_disk_positive_control_failure() -> None:
    """If container disk positive control fails, disk_violations captures the failure."""
    repo_files, blobs_by_sha = _build_synthetic_repo_blobs()
    store = _InMemoryEvalStore(collection_name="disk_fail_col")

    async def _seed_index_fn(
        repo_full_name: str,
        **kwargs: Any,
    ) -> InitialIndexReport:
        del kwargs
        files = repo_files[repo_full_name]
        if repo_full_name == DEFAULT_LIVE_PY_REPO:
            store.add_point(
                payload={
                    "repo_full_name": repo_full_name,
                    "file_path": "src/services/billing.py",
                    "blob_sha": _sha1_blob(files["src/services/billing.py"]),
                    "chunk_type": "code",
                    "language": "python",
                    "symbol_name": "compute_invoice_total",
                    "symbol_kind": "function",
                    "start_line": 6,
                    "end_line": 9,
                    "signature": (
                        "def compute_invoice_total(subtotal_cents: int, fee_bps: int) -> int"
                    ),
                    "called_symbols": [],
                    "imports": [],
                    "cyclomatic_complexity": 1,
                },
                vector=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
            )
            store.add_point(
                payload={
                    "repo_full_name": repo_full_name,
                    "file_path": "src/routers/orders.py",
                    "blob_sha": _sha1_blob(files["src/routers/orders.py"]),
                    "chunk_type": "code",
                    "language": "python",
                    "symbol_name": "submit_order_route",
                    "symbol_kind": "function",
                    "start_line": 6,
                    "end_line": 9,
                    "signature": (
                        "def submit_order_route(order_id: str, subtotal_cents: int) "
                        "-> dict[str, int]"
                    ),
                    "called_symbols": ["compute_invoice_total"],
                    "imports": ["src.services.billing"],
                    "cyclomatic_complexity": 1,
                },
                vector=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
            )
        else:
            store.add_point(
                payload={
                    "repo_full_name": repo_full_name,
                    "file_path": "src/hooks/checkoutSummary.ts",
                    "blob_sha": _sha1_blob(files["src/hooks/checkoutSummary.ts"]),
                    "chunk_type": "code",
                    "language": "typescript",
                    "symbol_name": "buildCheckoutSummary",
                    "symbol_kind": "function",
                    "start_line": 3,
                    "end_line": 6,
                    "signature": (
                        "export function buildCheckoutSummary(cents: number, taxBps: number): "
                        "string"
                    ),
                    "called_symbols": [],
                    "imports": [],
                    "cyclomatic_complexity": 1,
                },
                vector=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
            )
            store.add_point(
                payload={
                    "repo_full_name": repo_full_name,
                    "file_path": "src/pages/CheckoutScreen.tsx",
                    "blob_sha": _sha1_blob(files["src/pages/CheckoutScreen.tsx"]),
                    "chunk_type": "code",
                    "language": "tsx",
                    "symbol_name": "CheckoutScreen",
                    "symbol_kind": "function",
                    "start_line": 3,
                    "end_line": 6,
                    "signature": "export function CheckoutScreen(props: { cents: number }): string",
                    "called_symbols": ["buildCheckoutSummary"],
                    "imports": ["./checkoutSummary"],
                    "cyclomatic_complexity": 1,
                },
                vector=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
            )
        return InitialIndexReport(
            repo_full_name=repo_full_name,
            repo_id="r",
            default_branch="main",
            indexed_branch="main",
            commit_sha="c" * 40,
            tree_entries=2,
            eligible_files=2,
            unchanged_files_skipped=0,
            fetched_files=2,
            skipped_files=0,
            code_chunks=2,
            doc_chunks=0,
            embedded_points=0,
            upserted_points=2,
            stale_points_deleted=0,
            persisted_cursor=True,
            truncated_tree=False,
            elapsed_ms=10.0,
        )

    async def _gh_handler(request: httpx.Request) -> httpx.Response:
        if "/git/blobs/" in request.url.path:
            sha = request.url.path.rsplit("/", 1)[-1]
            raw = blobs_by_sha.get(sha)
            if raw is not None:
                return httpx.Response(200, json=_blob_json(raw, sha))
        return httpx.Response(404)

    settings = Settings(
        qdrant_collection_name="disk_fail_col",
        github_token=SecretStr("ghp_eval_mock"),
        gemini_api_key=SecretStr("gem_eval_mock"),
    )
    fake_genai = _FakeEvalGenAIClient()

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(_gh_handler)) as http_client,
        GeminiEmbedder(client=fake_genai) as embedder,  # type: ignore[arg-type]
    ):
        report = await run_milestone2_live_evaluation(
            settings=settings,
            py_repo=DEFAULT_LIVE_PY_REPO,
            ts_repo="acme/react-dashboard",
            collection_name="disk_fail_col",
            session_factory=object(),  # type: ignore[arg-type]
            http_client=http_client,
            redis_client=SimpleNamespace(),  # type: ignore[arg-type]
            store=store,  # type: ignore[arg-type]
            embedder=embedder,
            index_fn=_seed_index_fn,
            auditor_fn=lambda *_a, **_kw: 1,
            disk_scanner_fn=lambda *_a, **_kw: [],
        )

    assert report.zerocode_violations == 1
    assert any("Positive control failed" in msg for msg in report.disk_violations)
    assert validate_m2_kpi_thresholds(report)


def _load_eval_cli_module() -> Any:
    repo_root = Path(__file__).resolve().parents[2]
    script_path = repo_root / "evals" / "run_m2_eval.py"
    spec = importlib.util.spec_from_file_location("run_m2_eval", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Failed to load {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_m2_eval"] = module
    spec.loader.exec_module(module)
    return module


def test_eval_cli_exits_2_without_required_credentials(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Thin CLI `evals/run_m2_eval.py` exits cleanly with code 2 when API keys are absent."""
    cli_mod = _load_eval_cli_module()
    monkeypatch.setattr(
        cli_mod,
        "Settings",
        lambda **_kw: Settings(
            gemini_api_key=None,
            github_token=None,
            github_app_id=None,
            github_private_key=None,
        ),
    )
    rc = cli_mod.main(["--py-repo", "Tieens-04/CodeSight-AI", "--ts-repo", "acme/web"])
    assert rc == 2
    assert "GEMINI_API_KEY" in capsys.readouterr().err

    monkeypatch.setattr(
        cli_mod,
        "Settings",
        lambda **_kw: Settings(
            gemini_api_key=SecretStr("gem_ok"),
            github_token=None,
            github_app_id=None,
            github_private_key=None,
        ),
    )
    rc_gh = cli_mod.main(["--py-repo", "Tieens-04/CodeSight-AI", "--ts-repo", "acme/web"])
    assert rc_gh == 2
    assert "GITHUB_TOKEN" in capsys.readouterr().err


def test_eval_cli_returns_0_on_pass_and_1_on_kpi_violation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Thin CLI `evals/run_m2_eval.py` emits JSON report and maps KPI failures to exit 1."""
    cli_mod = _load_eval_cli_module()
    monkeypatch.setattr(
        cli_mod,
        "Settings",
        lambda **_kw: Settings(
            gemini_api_key=SecretStr("gem_ok"),
            github_token=SecretStr("ghp_ok"),
        ),
    )

    class _FakeConn:
        async def run_sync(self, fn: Any) -> None:
            del fn

        async def __aenter__(self) -> _FakeConn:
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

    class _FakeEngine:
        def begin(self) -> _FakeConn:
            return _FakeConn()

        async def dispose(self) -> None:
            return None

    class _FakeCleanupClient:
        async def collection_exists(self, _name: str) -> bool:
            return True

        async def delete_collection(self, _name: str) -> None:
            return None

        async def close(self) -> None:
            return None

    monkeypatch.setattr(cli_mod, "create_db_engine", lambda _s: _FakeEngine())
    monkeypatch.setattr(cli_mod, "create_session_factory", lambda _e: object())
    monkeypatch.setattr(cli_mod, "AsyncQdrantClient", lambda **_kw: _FakeCleanupClient())

    async def _passing_eval(**_kw: Any) -> M2LiveEvaluationReport:
        return _make_sample_report()

    monkeypatch.setattr(cli_mod, "run_milestone2_live_evaluation", _passing_eval)
    rc_ok = cli_mod.main(
        ["--py-repo", "Tieens-04/CodeSight-AI", "--ts-repo", "acme/web", "--collection", "eval_c"]
    )
    assert rc_ok == 0
    captured_ok = capsys.readouterr()
    parsed = json.loads(captured_ok.out)
    assert parsed["ts_repo"] == "acme/react-dashboard"

    async def _failing_eval(**_kw: Any) -> M2LiveEvaluationReport:
        return _make_sample_report(recall_at_5=0.5)

    monkeypatch.setattr(cli_mod, "run_milestone2_live_evaluation", _failing_eval)
    rc_fail = cli_mod.main(
        ["--py-repo", "Tieens-04/CodeSight-AI", "--ts-repo", "acme/web", "--keep-collection"]
    )
    assert rc_fail == 1
    assert "recall_at_5" in capsys.readouterr().err


def test_validate_m2_kpi_thresholds_flags_zero_cases_and_aux_budget_on_repo_summary() -> None:
    """Repo summary checks flag 0 evaluated cases and auxiliary token budget overflow."""
    zero_cases = _make_repo_summary(
        repo_full_name="Tieens-04/CodeSight-AI",
        language="python",
        cases_evaluated=0,
    )
    over_aux = _make_repo_summary(
        repo_full_name="acme/react-dashboard",
        language="typescript",
        max_packed_aux_tokens=3600,
    )
    violations = validate_m2_kpi_thresholds(
        _make_sample_report(py_summary=zero_cases, ts_summary=over_aux)
    )
    assert any("0 cross-file probe cases" in v for v in violations)
    assert any("max_packed_aux_tokens 3600 > 3500" in v for v in violations)


@pytest.mark.asyncio
async def test_discover_live_ts_repo_edge_errors_and_archived_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Auto-discovery handles transport errors, bad JSON, malformed rows, and archived TS."""

    async def _transport_error(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network unreachable")

    settings = Settings(github_token=SecretStr("ghp_test_pat"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(_transport_error)) as client:
        with pytest.raises(ValueError, match="request failed"):
            await discover_live_ts_repo(settings=settings, http_client=client)

    async def _invalid_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{bad json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(_invalid_json)) as client:
        with pytest.raises(ValueError, match="invalid JSON"):
            await discover_live_ts_repo(settings=settings, http_client=client)

    async def _non_list_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"items": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(_non_list_json)) as client:
        with pytest.raises(ValueError, match="response format"):
            await discover_live_ts_repo(settings=settings, http_client=client)

    async def _mixed_archived(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                "not-a-dict",
                {"full_name": "", "language": "TypeScript"},
                {"full_name": "invalid slug spaces", "language": "TypeScript"},
                {"full_name": "acme/archived-ts", "language": "TypeScript", "archived": True},
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_mixed_archived)) as client:
        selected = await discover_live_ts_repo(settings=settings, http_client=client)
    assert selected == "acme/archived-ts"

    # Also cover CODESIGHT_LIVE_TS_REPO env fallback and invalid slug ValueError conversion
    from codesight.rag import live_eval

    monkeypatch.setenv("CODESIGHT_LIVE_TS_REPO", "acme/env-ts-repo")
    resolved = await live_eval._resolve_ts_repo(
        "",
        settings=settings,
        http_client=None,
        redis_client=None,
        api_base_url="https://api.github.com",
    )
    assert resolved == "acme/env-ts-repo"
    with pytest.raises(ValueError, match="Invalid ts_repo"):
        await live_eval._resolve_ts_repo(
            "invalid/slug/extra",
            settings=settings,
            http_client=None,
            redis_client=None,
            api_base_url="https://api.github.com",
        )


def test_probe_selection_helpers_callees_test_paths_and_fallback_ast() -> None:
    """Test helper branches: _is_test_path, callee extraction, backfill, and fallback AST."""
    from codesight.ast_engine.registry import ASTParserRegistry
    from codesight.diff.smart_diff_parser import SmartDiffParser
    from codesight.rag.live_eval import (
        _build_probe_diff,
        _extract_body_canaries,
        _is_test_path,
        _load_zerocode_auditor_module,
        _resolve_probe_chunk_and_ast,
        _select_cross_file_probes,
    )

    assert _is_test_path("tests/unit/test_a.py") is True
    assert _is_test_path("pkg/tests/helpers.py") is True
    assert _is_test_path("src/app.test.tsx") is True
    assert _is_test_path("src/services/billing.py") is False

    pts = [
        # Malformed point ignored
        QdrantStoredPoint(point_id="0", payload={"chunk_type": "code", "file_path": ""}),
        # Target 1 & 2 in same file_a (multi-chunk)
        QdrantStoredPoint(
            point_id="1",
            payload={
                "file_path": "src/a.py",
                "blob_sha": "a" * 40,
                "chunk_type": "code",
                "language": "python",
                "symbol_name": "alpha_fn",
                "symbol_kind": "function",
                "start_line": 2,
                "end_line": 6,
                "signature": "def alpha_fn() -> None",
                "called_symbols": ["beta_fn", "svc.beta_fn", "list.append", "get"],
                "imports": [],
                "cyclomatic_complexity": 1,
            },
        ),
        QdrantStoredPoint(
            point_id="2",
            payload={
                "file_path": "src/a.py",
                "blob_sha": "a" * 40,
                "chunk_type": "code",
                "language": "python",
                "symbol_name": "gamma_fn",
                "symbol_kind": "function",
                "start_line": 8,
                "end_line": 12,
                "signature": "def gamma_fn() -> None",
                "called_symbols": [],
                "imports": [],
                "cyclomatic_complexity": 1,
            },
        ),
        # Callee & caller in src/b.py
        QdrantStoredPoint(
            point_id="3",
            payload={
                "file_path": "src/b.py",
                "blob_sha": "b" * 40,
                "chunk_type": "code",
                "language": "python",
                "symbol_name": "beta_fn",
                "symbol_kind": "function",
                "start_line": 2,
                "end_line": 6,
                "signature": "def beta_fn() -> None",
                "called_symbols": ["alpha_fn", "gamma_fn"],
                "imports": [],
                "cyclomatic_complexity": 1,
            },
        ),
    ]
    # max_probes=3 exercises both distinct-file pass (a.py, b.py) and backfill (a.py::gamma_fn)
    probes, code_cnt, doc_cnt = _select_cross_file_probes(
        pts,
        repo_full_name="org/repo",
        max_probes=3,
    )
    assert code_cnt == 4
    assert doc_cnt == 0
    assert len(probes) == 3

    probes_one, _, _ = _select_cross_file_probes(pts, repo_full_name="org/repo", max_probes=1)
    assert len(probes_one) == 1
    alpha_probe = next(p for p in probes if p.symbol_name == "alpha_fn")
    assert alpha_probe.expected_callees == ("src/b.py::beta_fn",)

    # Fallback chunk resolution when file extension is unsupported by ASTParserRegistry
    unknown_probe = alpha_probe
    registry = ASTParserRegistry()
    # Matching by symbol name when start_line shifted
    _ast, matched_by_name = _resolve_probe_chunk_and_ast(
        unknown_probe,
        source_text="\n\n\ndef alpha_fn() -> None:\n    return None\n",
        registry=registry,
    )
    assert matched_by_name.symbol_name == "alpha_fn"
    # Fallback to payload chunk when source has no AST chunks
    _ast_none, fallback_chunk = _resolve_probe_chunk_and_ast(
        unknown_probe,
        source_text="",
        registry=registry,
    )
    assert fallback_chunk.symbol_name == "alpha_fn"

    empty_diff = _build_probe_diff(
        unknown_probe,
        source_text="",
        target_line=1,
        diff_parser=SmartDiffParser(),
    )
    assert empty_diff.files == ()

    # Body canary extraction filters short/existing/over-cap lines cleanly
    canaries: list[str] = []
    _extract_body_canaries(
        "def f():\n    x = 1\n    secret_interior_line_for_disk_scan = 42\n",
        start_line=1,
        end_line=3,
        payload_json_blobs=['{"signature": "def f()"}'],
        collected=canaries,
    )
    assert canaries == ["secret_interior_line_for_disk_scan = 42"]

    auditor = _load_zerocode_auditor_module()
    assert callable(auditor.audit_live_collection)
    assert callable(auditor.scan_container_storage_for_canaries)


@pytest.mark.asyncio
async def test_diagnostic_doc_scores_and_blob_skip_and_config_guards() -> None:
    """Cover diagnostic doc error isolation, ZeroCodeViolationError pass-through, and guards."""
    from codesight.rag.live_eval import _measure_diagnostic_doc_scores
    from codesight.rag.models import EmbeddingConfigError, ZeroCodeViolationError

    store = _InMemoryEvalStore(collection_name="guard_col")

    class _ErrEmbedder:
        async def embed_query(self, _text: str) -> list[float]:
            raise RuntimeError("transient error")

    scores, max_score = await _measure_diagnostic_doc_scores(
        embedder=_ErrEmbedder(),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        repo_full_name="org/repo",
        query_text="hello",
        has_docs=True,
    )
    assert scores == ()
    assert max_score is None

    class _Nfr3Embedder:
        async def embed_query(self, _text: str) -> list[float]:
            raise ZeroCodeViolationError("leaked")

    with pytest.raises(ZeroCodeViolationError):
        await _measure_diagnostic_doc_scores(
            embedder=_Nfr3Embedder(),  # type: ignore[arg-type]
            store=store,  # type: ignore[arg-type]
            repo_full_name="org/repo",
            query_text="hello",
            has_docs=True,
        )

    # Guard: blank collection_name -> ValueError
    with pytest.raises(ValueError, match="collection_name"):
        await run_milestone2_live_evaluation(
            settings=Settings(qdrant_collection_name=""),
            py_repo=DEFAULT_LIVE_PY_REPO,
            ts_repo="acme/web",
            collection_name="",
            session_factory=object(),  # type: ignore[arg-type]
        )

    # Guard: missing GEMINI_API_KEY when embedder=None -> EmbeddingConfigError
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(404))
    ) as unauthed_http:
        with pytest.raises(EmbeddingConfigError, match="GEMINI_API_KEY"):
            await run_milestone2_live_evaluation(
                settings=Settings(
                    qdrant_collection_name="col",
                    gemini_api_key=None,
                    github_token=SecretStr("ghp_ok"),
                ),
                py_repo=DEFAULT_LIVE_PY_REPO,
                ts_repo="acme/web",
                collection_name="col",
                session_factory=object(),  # type: ignore[arg-type]
                http_client=unauthed_http,
                redis_client=SimpleNamespace(),  # type: ignore[arg-type]
            )

    # Guard: all probe blobs returning 404 fails loudly with ValueError
    async def _seed_unfetchable(
        repo_full_name: str,
        **kwargs: Any,
    ) -> InitialIndexReport:
        del kwargs
        store.add_point(
            payload={
                "repo_full_name": repo_full_name,
                "file_path": "src/missing.py",
                "blob_sha": "f" * 40,
                "chunk_type": "code",
                "language": "python",
                "symbol_name": "missing_fn",
                "symbol_kind": "function",
                "start_line": 1,
                "end_line": 3,
                "signature": "def missing_fn() -> None",
                "called_symbols": [],
                "imports": [],
                "cyclomatic_complexity": 1,
            },
            vector=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
        )
        store.add_point(
            payload={
                "repo_full_name": repo_full_name,
                "file_path": "src/caller.py",
                "blob_sha": "e" * 40,
                "chunk_type": "code",
                "language": "python",
                "symbol_name": "caller_fn",
                "symbol_kind": "function",
                "start_line": 1,
                "end_line": 3,
                "signature": "def caller_fn() -> None",
                "called_symbols": ["missing_fn"],
                "imports": [],
                "cyclomatic_complexity": 1,
            },
            vector=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
        )
        return InitialIndexReport(
            repo_full_name=repo_full_name,
            repo_id="r",
            default_branch="main",
            indexed_branch="main",
            commit_sha="d" * 40,
            tree_entries=2,
            eligible_files=2,
            unchanged_files_skipped=0,
            fetched_files=2,
            skipped_files=0,
            code_chunks=2,
            doc_chunks=0,
            embedded_points=0,
            upserted_points=2,
            stale_points_deleted=0,
            persisted_cursor=True,
            truncated_tree=False,
            elapsed_ms=5.0,
        )

    fake_genai = _FakeEvalGenAIClient()
    async with (
        httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _r: httpx.Response(404))
        ) as http_client,
        GeminiEmbedder(client=fake_genai) as embedder,  # type: ignore[arg-type]
    ):
        with pytest.raises(ValueError, match="0 fetchable cross-file probe blobs"):
            await run_milestone2_live_evaluation(
                settings=Settings(
                    qdrant_collection_name="guard_col",
                    github_token=SecretStr("ghp_ok"),
                    gemini_api_key=SecretStr("gem_ok"),
                ),
                py_repo=DEFAULT_LIVE_PY_REPO,
                ts_repo="acme/web",
                collection_name="guard_col",
                session_factory=object(),  # type: ignore[arg-type]
                http_client=http_client,
                redis_client=SimpleNamespace(),  # type: ignore[arg-type]
                store=store,  # type: ignore[arg-type]
                embedder=embedder,
                index_fn=_seed_unfetchable,
            )


@pytest.mark.asyncio
async def test_live_eval_uses_settings_dotenv_overrides_canonical_slugs_and_per_repo_canaries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify .env Settings overrides, canonical casing resolution, and TS disk canary budget."""
    for key in ("CODESIGHT_LIVE_PY_REPO", "CODESIGHT_LIVE_TS_REPO", "CODESIGHT_QDRANT_CONTAINER"):
        monkeypatch.delenv(key, raising=False)

    # Python probe has >= 8 interior body lines (> 6 per-repo cap) so TS canaries must not starve.
    py_service_long = (
        b'"""Long billing module."""\n\n'
        b"def compute_invoice_total(subtotal_cents: int, fee_bps: int) -> int:\n"
        b'    """Compute total cents with audit trail steps."""\n'
        b"    py_canary_body_line_one = subtotal_cents + 1001\n"
        b"    py_canary_body_line_two = py_canary_body_line_one + 1002\n"
        b"    py_canary_body_line_three = py_canary_body_line_two + 1003\n"
        b"    py_canary_body_line_four = py_canary_body_line_three + 1004\n"
        b"    py_canary_body_line_five = py_canary_body_line_four + 1005\n"
        b"    py_canary_body_line_six = py_canary_body_line_five + 1006\n"
        b"    py_canary_body_line_seven = py_canary_body_line_six + 1007\n"
        b"    py_canary_body_line_eight = py_canary_body_line_seven + fee_bps\n"
        b"    return py_canary_body_line_eight\n\n\n"
        + b"\n\n".join(
            f"def aux_billing_{i}(v: int) -> int:\n    return v + {i}\n".encode()
            for i in range(1, 16)
        )
    )
    py_router_long = (
        b"from src.services.billing import compute_invoice_total\n\n"
        b"def submit_order_route(subtotal_cents: int) -> int:\n"
        b"    return compute_invoice_total(subtotal_cents, 250)\n\n"
        + b"\n\n".join(
            f"def aux_route_{i}(v: int) -> int:\n    return v + {i}\n".encode()
            for i in range(1, 16)
        )
    )
    ts_hook_long = (
        b"export function buildCheckoutSummary(cents: number, taxBps: number): string {\n"
        b"  const tsCanaryBodyLineOne = Math.floor((cents * taxBps) / 10000);\n"
        b"  const tsCanaryBodyLineTwo = cents + tsCanaryBodyLineOne;\n"
        b"  return `total=${tsCanaryBodyLineTwo}`;\n"
        b"}\n\n"
        + b"\n".join(
            f"export function auxTsFn{i}(n: number): number {{ return n + {i}; }}\n".encode()
            for i in range(1, 16)
        )
    )
    ts_page_long = (
        b"import { buildCheckoutSummary } from './checkoutSummary';\n\n"
        b"export function CheckoutScreen(cents: number): string {\n"
        b"  return buildCheckoutSummary(cents, 800);\n"
        b"}\n\n"
        + b"\n".join(
            f"export function auxPageFn{i}(n: number): number {{ return n + {i}; }}\n".encode()
            for i in range(1, 16)
        )
    )

    canonical_py = "Tieens-04/CodeSight-AI"
    canonical_ts = "Acme/React-Dashboard"
    input_py_lower = "tieens-04/codesight-ai"
    input_ts_lower = "acme/react-dashboard"

    blobs_by_sha = {
        _sha1_blob(raw): raw
        for raw in (py_service_long, py_router_long, ts_hook_long, ts_page_long)
    }
    store = _InMemoryEvalStore(collection_name="canon_col")
    seen_index_inputs: list[str] = []

    async def _canonicalizing_index_fn(
        repo_full_name: str,
        **kwargs: Any,
    ) -> InitialIndexReport:
        del kwargs
        seen_index_inputs.append(repo_full_name)
        if repo_full_name == input_py_lower:
            canonical = canonical_py
            store.add_point(
                payload={
                    "repo_full_name": canonical,
                    "file_path": "src/services/billing.py",
                    "blob_sha": _sha1_blob(py_service_long),
                    "chunk_type": "code",
                    "language": "python",
                    "symbol_name": "compute_invoice_total",
                    "symbol_kind": "function",
                    "start_line": 3,
                    "end_line": 13,
                    "signature": (
                        "def compute_invoice_total(subtotal_cents: int, fee_bps: int) -> int"
                    ),
                    "called_symbols": [],
                    "imports": [],
                    "cyclomatic_complexity": 1,
                },
                vector=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
            )
            store.add_point(
                payload={
                    "repo_full_name": canonical,
                    "file_path": "src/routers/orders.py",
                    "blob_sha": _sha1_blob(py_router_long),
                    "chunk_type": "code",
                    "language": "python",
                    "symbol_name": "submit_order_route",
                    "symbol_kind": "function",
                    "start_line": 3,
                    "end_line": 4,
                    "signature": "def submit_order_route(subtotal_cents: int) -> int",
                    "called_symbols": ["compute_invoice_total"],
                    "imports": ["src.services.billing"],
                    "cyclomatic_complexity": 1,
                },
                vector=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
            )
        else:
            canonical = canonical_ts
            store.add_point(
                payload={
                    "repo_full_name": canonical,
                    "file_path": "src/hooks/checkoutSummary.ts",
                    "blob_sha": _sha1_blob(ts_hook_long),
                    "chunk_type": "code",
                    "language": "typescript",
                    "symbol_name": "buildCheckoutSummary",
                    "symbol_kind": "function",
                    "start_line": 1,
                    "end_line": 5,
                    "signature": (
                        "export function buildCheckoutSummary(cents: number, taxBps: number): "
                        "string"
                    ),
                    "called_symbols": [],
                    "imports": [],
                    "cyclomatic_complexity": 1,
                },
                vector=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
            )
            store.add_point(
                payload={
                    "repo_full_name": canonical,
                    "file_path": "src/pages/CheckoutScreen.tsx",
                    "blob_sha": _sha1_blob(ts_page_long),
                    "chunk_type": "code",
                    "language": "tsx",
                    "symbol_name": "CheckoutScreen",
                    "symbol_kind": "function",
                    "start_line": 3,
                    "end_line": 5,
                    "signature": "export function CheckoutScreen(cents: number): string",
                    "called_symbols": ["buildCheckoutSummary"],
                    "imports": ["./checkoutSummary"],
                    "cyclomatic_complexity": 1,
                },
                vector=[1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1),
            )
        return InitialIndexReport(
            repo_full_name=canonical,
            repo_id="r",
            default_branch="main",
            indexed_branch="main",
            commit_sha="e" * 40,
            tree_entries=2,
            eligible_files=2,
            unchanged_files_skipped=0,
            fetched_files=2,
            skipped_files=0,
            code_chunks=2,
            doc_chunks=0,
            embedded_points=0,
            upserted_points=2,
            stale_points_deleted=0,
            persisted_cursor=True,
            truncated_tree=False,
            elapsed_ms=5.0,
        )

    async def _gh_handler(request: httpx.Request) -> httpx.Response:
        if "/git/blobs/" in request.url.path:
            sha = request.url.path.rsplit("/", 1)[-1]
            raw = blobs_by_sha.get(sha)
            if raw is not None:
                return httpx.Response(200, json=_blob_json(raw, sha))
        return httpx.Response(404)

    disk_calls: list[tuple[str, str, tuple[str, ...]]] = []

    def _disk_scan(
        container_name: str,
        collection: str,
        canary_substrings: Sequence[str],
    ) -> list[str]:
        markers = tuple(canary_substrings)
        disk_calls.append((container_name, collection, markers))
        if markers == (canonical_py,):
            return ["NFR-3 disk breach — raw-code canary matched on Qdrant disk file: seg0"]
        return []

    settings = Settings(
        _env_file=None,
        qdrant_collection_name="canon_col",
        github_token=SecretStr("ghp_ok"),
        gemini_api_key=SecretStr("gem_ok"),
        codesight_live_py_repo=input_py_lower,
        codesight_live_ts_repo=input_ts_lower,
        codesight_qdrant_container="codesight-qdrant-env",
    )
    fake_genai = _FakeEvalGenAIClient()

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(_gh_handler)) as http_client,
        GeminiEmbedder(client=fake_genai) as embedder,  # type: ignore[arg-type]
    ):
        report = await run_milestone2_live_evaluation(
            settings=settings,
            py_repo=None,
            ts_repo=None,
            collection_name="canon_col",
            session_factory=object(),  # type: ignore[arg-type]
            http_client=http_client,
            redis_client=SimpleNamespace(),  # type: ignore[arg-type]
            store=store,  # type: ignore[arg-type]
            embedder=embedder,
            index_fn=_canonicalizing_index_fn,
            auditor_fn=lambda *_a, **_kw: 0,
            disk_scanner_fn=_disk_scan,
        )

    # Resolved from Settings (.env) without os.environ
    assert seen_index_inputs == [input_py_lower, input_ts_lower]
    # Canonicalized GitHub full_name used for Qdrant scroll, report, and disk positive control
    assert report.py_repo == canonical_py
    assert report.ts_repo == canonical_ts
    assert report.recall_at_5 == 1.0
    assert report.disk_violations == ()
    assert disk_calls[0] == ("codesight-qdrant-env", "canon_col", (canonical_py,))

    # Partitioned per-repo canary budget guarantees both Python (capped == 6) and TS/TSX lines
    scanned_markers = disk_calls[1][2]
    assert len(scanned_markers) == 9
    py_hits = [
        m for m in scanned_markers if "py_canary_body_line_" in m or "Compute total cents" in m
    ]
    ts_hits = [m for m in scanned_markers if "tsCanaryBodyLine" in m]
    assert len(py_hits) == 6
    assert len(ts_hits) == 3


def test_eval_cli_passes_settings_dotenv_repos_when_cli_flags_omitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Thin CLI `evals/run_m2_eval.py` resolves py/ts repos from Settings when omitted."""
    for key in ("CODESIGHT_LIVE_PY_REPO", "CODESIGHT_LIVE_TS_REPO"):
        monkeypatch.delenv(key, raising=False)

    cli_mod = _load_eval_cli_module()
    monkeypatch.setattr(
        cli_mod,
        "Settings",
        lambda **_kw: Settings(
            _env_file=None,
            gemini_api_key=SecretStr("gem_ok"),
            github_token=SecretStr("ghp_ok"),
            codesight_live_py_repo="org/dotenv-py",
            codesight_live_ts_repo="org/dotenv-ts",
        ),
    )
    captured_args: dict[str, Any] = {}

    async def _fake_run_async(
        args: Any,
        settings: Settings,
        *,
        py_repo: str,
        ts_repo: str,
    ) -> int:
        del args, settings
        captured_args["py_repo"] = py_repo
        captured_args["ts_repo"] = ts_repo
        return 0

    monkeypatch.setattr(cli_mod, "_run_async", _fake_run_async)
    rc = cli_mod.main(["--collection", "env_test_col"])
    assert rc == 0
    assert captured_args == {"py_repo": "org/dotenv-py", "ts_repo": "org/dotenv-ts"}
