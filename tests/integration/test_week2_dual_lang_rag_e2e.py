"""Milestone 2 E2E Quality Gate: Dual-Language (FastAPI + React/TSX) AST-RAG & NFR-3 Audit (CA-16).

Verifies end-to-end across live local Docker services (`codesight-postgres` 5433, `codesight-redis`
6381, `codesight-qdrant` 6333/6334):
1. `codesight index` (`run_cli_index`) on realistic multi-layer Python FastAPI & TypeScript/TSX
   repositories with idempotent re-indexing and FR-07 incremental push sync (`PushSyncRunner`).
2. Ground-truth 8-scenario PR evaluation matrix (signature parameter changes, FastAPI `Depends` /
   `Security` DI, breaking symbol renames, JSX component prop changes, incremental push sync, and
   noisy lockfile/decoy/cross-tenant isolation) achieving `Recall@5 >= 85%` and `< 0.85s` latency.
3. Anti-gaming token savings within the realistic `45% - 65%` band on pure code/doc PRs (`<= 3,500`
   auxiliary tokens/batch).
4. Dual-layer `NFR-3` Zero-Code Persistence audit: 100% paginated Qdrant API payload audit (all 7
   keyword indexes + canary check) AND physical on-disk scan of `/qdrant/storage/collections/<col>`
   inside `codesight-qdrant` confirming `0` bytes of raw source code persisted on disk.
5. Opt-in live GitHub + `GEMINI_API_KEY` smoke evaluation when `CODESIGHT_RUN_LIVE_M2_EVAL=1`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import os
import time
from collections.abc import AsyncIterator, Hashable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Literal

import httpx
import pytest
import redis.asyncio as redis_async
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from codesight.ast_engine.registry import ASTParserRegistry
from codesight.cli.commands.index import run_cli_index
from codesight.core.config import Settings
from codesight.db.models import Base, Repository, WebhookEvent
from codesight.diff.smart_diff_parser import SmartDiffParser
from codesight.rag.embedder import GeminiEmbedder
from codesight.rag.live_eval import (
    DEFAULT_LIVE_PY_REPO,
    run_milestone2_live_evaluation,
    validate_m2_kpi_thresholds,
)
from codesight.rag.models import (
    DEFAULT_EMBED_BATCH_SIZE,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL_NAME,
)
from codesight.rag.qdrant_store import QdrantZeroCodeStore
from codesight.rag.retriever import HybridContextRetriever
from codesight.rag.token_packer import TokenBudgetPacker
from codesight.vcs.blob_fetcher import GitHubBlobFetcher
from codesight.vcs.github_auth import GitHubAuthProvider
from codesight.worker.tasks.indexing_tasks import PushSyncRunner

# ---------------------------------------------------------------------------
# Canary secrets planted ONLY in non-persisted code/doc regions (NFR-3 probe)
# ---------------------------------------------------------------------------
CANARY_PY_BODY = "CODESIGHT_NFR3_CANARY_PY_BODY_8f1a9c2e41"
CANARY_PY_DOCSTRING = "CODESIGHT_NFR3_CANARY_PY_DOC_4b7d2a1973"
CANARY_PY_COMMENT = "CODESIGHT_NFR3_CANARY_PY_COMMENT_6e3f10a455"
CANARY_TS_BODY = "CODESIGHT_NFR3_CANARY_TS_BODY_91c4e8b267"
CANARY_TS_JSDOC = "CODESIGHT_NFR3_CANARY_TS_JSDOC_2d8a7f5192"
CANARY_TS_DEFAULT = "CODESIGHT_NFR3_CANARY_TS_DEFAULT_5c9e1b3048"
CANARY_TS_TYPE_ALIAS = "CODESIGHT_NFR3_CANARY_TS_TYPE_7a4c6d0831"
CANARY_MD_EXCERPT = "CODESIGHT_NFR3_CANARY_MD_BODY_3f6b8e4216"

ALL_NFR3_CANARIES: tuple[str, ...] = (
    CANARY_PY_BODY,
    CANARY_PY_DOCSTRING,
    CANARY_PY_COMMENT,
    CANARY_TS_BODY,
    CANARY_TS_JSDOC,
    CANARY_TS_DEFAULT,
    CANARY_TS_TYPE_ALIAS,
    CANARY_MD_EXCERPT,
)

PY_REPO_SLUG = "codesight-eval/fastapi-commerce-api"
TS_REPO_SLUG = "codesight-eval/react-checkout-web"
ROGUE_REPO_SLUG = "rogue-org/tenant-trap"


def _sha1_blob(content: bytes) -> str:
    return hashlib.sha1(
        b"blob " + str(len(content)).encode("ascii") + b"\x00" + content
    ).hexdigest()


def _b64(content: bytes) -> str:
    return base64.b64encode(content).decode("ascii")


def _blob_json(content: bytes, sha: str) -> dict[str, Any]:
    return {
        "sha": sha,
        "size": len(content),
        "encoding": "base64",
        "content": _b64(content),
    }


def _domain_semantic_vec(text: str) -> list[float]:
    """Deterministic 768-dim multi-domain vector separating 7 FastAPI/React topics."""
    lower = text.lower()
    vec = [0.0] * EMBEDDING_DIMENSIONS
    topic_buckets: tuple[tuple[tuple[str, ...], int], ...] = (
        (
            ("jwt", "bearer", "principal", "verify_bearer_principal", "revoked", "jti", "di-guard"),
            0,
        ),
        (
            (
                "authorize_order_payment",
                "idempotency",
                "stripe",
                "ledger",
                "payment_service",
                "charge_stripe_intent",
            ),
            8,
        ),
        (
            (
                "vat",
                "tax",
                "calculate_vat_breakdown",
                "compute_regional_vat",
                "tax_service",
                "duty",
                "regional-vat",
            ),
            16,
        ),
        (
            (
                "inventory",
                "reserve_order_stock",
                "warehouse",
                "fulfillment",
                "confirm_order_fulfillment",
                "sku",
            ),
            24,
        ),
        (
            (
                "usecheckoutsession",
                "usepaymentmethods",
                "state-hooks",
                "hook",
                "cartdrawer",
                "checkoutpage",
            ),
            32,
        ),
        (
            (
                "fetchcartquote",
                "requestvalidatedcartquote",
                "cartapiclient",
                "api-client-contracts",
                "signapirequestheaders",
            ),
            40,
        ),
        (
            (
                "orderstatusbadge",
                "design-system-badges",
                "badge",
                "resolvebadgetone",
                "orderhistorytable",
            ),
            48,
        ),
    )
    for keywords, base_idx in topic_buckets:
        hits = sum(1 for kw in keywords if kw in lower)
        if hits > 0:
            weight = float(hits) * 4.5
            for offset in range(8):
                vec[base_idx + offset] = weight

    # Small deterministic residual across dims 64..79 so vectors are never zero
    digest = hashlib.sha256(lower.encode("utf-8")).digest()
    for i in range(16):
        vec[64 + i] = ((float(digest[i]) / 255.0) - 0.5) * 0.25 + 0.05

    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class _DomainSemanticGenAIClient:
    """Deterministic async google-genai stub simulating realistic embedding latency."""

    def __init__(self, *, simulated_latency_seconds: float = 0.0) -> None:
        self.embed_calls = 0
        self.simulated_latency_seconds = simulated_latency_seconds
        self.aio = SimpleNamespace(models=self)

    async def embed_content(
        self,
        *,
        model: str,
        contents: list[str],
        config: Any = None,
    ) -> Any:
        assert model == EMBEDDING_MODEL_NAME
        assert 1 <= len(contents) <= DEFAULT_EMBED_BATCH_SIZE
        assert config is not None
        assert config.output_dimensionality == EMBEDDING_DIMENSIONS
        assert config.task_type in {"RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"}
        self.embed_calls += 1
        if self.simulated_latency_seconds > 0:
            await asyncio.sleep(self.simulated_latency_seconds)
        return SimpleNamespace(
            embeddings=[SimpleNamespace(values=_domain_semantic_vec(txt)) for txt in contents]
        )


def _py_module_siblings(prefix: str, count: int) -> str:
    """Generate realistic cohesive Python domain helpers (~60-110 LOC per module)."""
    blocks = [
        (
            f"def {prefix}_policy_check_{i}(acct: str, region: str = 'US') -> dict[str, str]:\n"
            f'    """Validate {prefix} policy rule {i} for an account."""\n'
            f"    normalized = acct.strip().lower()\n"
            f"    if not normalized:\n"
            f"        raise ValueError('account_id is required')\n"
            f"    return {{'acct': normalized, 'region': region.upper(), 'id': '{prefix}_{i}'}}\n"
        )
        for i in range(1, count + 1)
    ]
    return "\n\n".join(blocks)


def _ts_module_siblings(prefix: str, count: int) -> str:
    """Generate realistic cohesive TypeScript domain utilities (~60-110 LOC per file)."""
    blocks = [
        (
            f"export function {prefix}FormatField{i}(raw: string, locale = 'en-US'): string {{\n"
            f"  const cleaned = raw.trim();\n"
            f"  if (!cleaned) {{\n"
            f"    return '{prefix}-empty-{i}';\n"
            f"  }}\n"
            f"  return `${{locale}}:${{cleaned}}:${{cleaned.length}}`;\n"
            f"}}\n"
        )
        for i in range(1, count + 1)
    ]
    return "\n".join(blocks)


def _build_fastapi_commerce_repo() -> dict[str, bytes]:
    """Build a realistic 27-file Python FastAPI Clean-Architecture repository in RAM."""
    files: dict[str, str] = {}

    # --- Core & Schemas ---
    files["src/core/config.py"] = (
        '"""FastAPI commerce service runtime settings."""\n\n'
        "from dataclasses import dataclass\n\n"
        "@dataclass(frozen=True)\n"
        "class CommerceSettings:\n"
        "    currency: str = 'USD'\n"
        "    max_retry_attempts: int = 3\n\n"
        "def load_commerce_settings(env_name: str = 'prod') -> CommerceSettings:\n"
        "    return CommerceSettings(currency='USD' if env_name == 'prod' else 'EUR')\n\n"
        + _py_module_siblings("cfg", 3)
        + "\n"
    )
    files["src/core/telemetry.py"] = (
        '"""Structured commerce telemetry counters."""\n\n'
        "def emit_commerce_metric(metric_name: str, amount_cents: int) -> dict[str, object]:\n"
        "    return {'metric': metric_name, 'value': amount_cents}\n\n"
        + _py_module_siblings("telemetry", 3)
        + "\n"
    )
    files["src/schemas/auth_schemas.py"] = (
        '"""Authentication principal schemas."""\n\n'
        "from dataclasses import dataclass\n\n"
        "@dataclass(frozen=True)\n"
        "class Principal:\n"
        "    subject: str\n"
        "    tenant_id: str\n"
        "    scopes: tuple[str, ...]\n\n" + _py_module_siblings("auth_schema", 2) + "\n"
    )
    files["src/schemas/order_schemas.py"] = (
        '"""Order and checkout request/response schemas."""\n\n'
        "from dataclasses import dataclass\n\n"
        "@dataclass(frozen=True)\n"
        "class OrderReceipt:\n"
        "    order_id: str\n"
        "    charged_cents: int\n"
        "    vat_cents: int\n\n" + _py_module_siblings("order_schema", 2) + "\n"
    )

    # --- Clients & Repositories ---
    files["src/clients/jwt_verifier.py"] = (
        '"""Cryptographic JWT decoding and JTI revocation cache."""\n\n'
        "def decode_verified_jwt(raw_token: str) -> dict[str, str]:\n"
        f'    """Decode RS256 bearer token. {CANARY_PY_DOCSTRING}"""\n'
        f"    internal_audit_marker = '{CANARY_PY_BODY}'\n"
        "    if not raw_token or '.' not in raw_token or not internal_audit_marker:\n"
        "        raise ValueError('Malformed bearer token')\n"
        "    sub, jti = raw_token.split('.', 1)\n"
        "    return {'sub': sub, 'jti': jti, 'tenant': 't-1'}\n\n"
        "def check_revoked_jti(jti: str) -> bool:\n"
        '    """Check whether token JTI has been revoked in Redis."""\n'
        "    return jti.startswith('revoked_')\n\n" + _py_module_siblings("jwt_client", 4) + "\n"
    )
    files["src/clients/stripe_gateway.py"] = (
        '"""Stripe payment intent gateway client."""\n\n'
        "def charge_stripe_intent(order_id: str, amount_cents: int) -> str:\n"
        '    """Create and confirm a Stripe payment intent."""\n'
        "    if amount_cents <= 0:\n"
        "        raise ValueError('amount_cents must be positive')\n"
        "    return f'pi_{order_id}_{amount_cents}'\n\n"
        + _py_module_siblings("stripe_gw", 4)
        + "\n"
    )
    files["src/repositories/ledger_repository.py"] = (
        '"""Double-entry payment ledger persistence."""\n\n'
        "def record_ledger_entry(order_id: str, payment_ref: str, amount_cents: int) -> str:\n"
        '    """Persist immutable payment ledger row."""\n'
        "    return f'ldg:{order_id}:{payment_ref}:{amount_cents}'\n\n"
        + _py_module_siblings("ledger_repo", 4)
        + "\n"
    )
    files["src/repositories/order_repository.py"] = (
        '"""Order aggregate persistence and warehouse SKU locks."""\n\n'
        "def save_order_record(order_id: str, tenant: str, cents: int) -> dict[str, object]:\n"
        '    """Upsert order record in PostgreSQL."""\n'
        "    return {'order_id': order_id, 'tenant_id': tenant, 'total_cents': cents}\n\n"
        "def lock_warehouse_sku(sku: str, quantity: int) -> bool:\n"
        '    """Acquire row-level lock on warehouse inventory SKU."""\n'
        "    return bool(sku) and quantity > 0\n\n" + _py_module_siblings("order_repo", 4) + "\n"
    )
    files["src/clients/tax_rate_table.py"] = (
        '"""Regional VAT rate lookup table."""\n\n'
        "def lookup_country_vat_rate(country_code: str) -> float:\n"
        '    """Return VAT rate multiplier for an ISO-3166 country code."""\n'
        "    rates = {'VN': 0.10, 'DE': 0.19, 'SG': 0.09}\n"
        "    return rates.get(country_code.upper(), 0.08)\n\n"
        + _py_module_siblings("tax_table", 4)
        + "\n"
    )

    # --- Services ---
    files["src/services/auth_service.py"] = (
        '"""FastAPI authentication dependency providers."""\n\n'
        "from src.clients.jwt_verifier import check_revoked_jti, decode_verified_jwt\n"
        "from src.schemas.auth_schemas import Principal\n\n"
        f"def verify_bearer_principal(authorization: str) -> Principal:  # {CANARY_PY_COMMENT}\n"
        '    """FastAPI DI guard validating bearer JWT and revocation status."""\n'
        "    claims = decode_verified_jwt(authorization)\n"
        "    if check_revoked_jti(claims['jti']):\n"
        "        raise PermissionError('Bearer token revoked')\n"
        "    return Principal(\n"
        "        subject=claims['sub'], tenant_id=claims['tenant'], scopes=('orders:write',)\n"
        "    )\n\n" + _py_module_siblings("auth_svc", 4) + "\n"
    )
    files["src/services/payment_service.py"] = (
        '"""Payment authorization and idempotent settlement service."""\n\n'
        "from src.clients.stripe_gateway import charge_stripe_intent\n"
        "from src.repositories.ledger_repository import record_ledger_entry\n\n"
        "def authorize_order_payment(order_id: str, amount_cents: int) -> str:\n"
        '    """Authorize payment via Stripe and record an immutable ledger entry."""\n'
        "    payment_ref = charge_stripe_intent(order_id, amount_cents)\n"
        "    return record_ledger_entry(order_id, payment_ref, amount_cents)\n\n"
        + _py_module_siblings("payment_svc", 4)
        + "\n"
    )
    files["src/services/tax_service.py"] = (
        '"""Regional VAT and cross-border duty calculation service."""\n\n'
        "from src.clients.tax_rate_table import lookup_country_vat_rate\n\n"
        "def calculate_vat_breakdown(subtotal_cents: int, country_code: str) -> int:\n"
        '    """Calculate regional VAT in minor currency units."""\n'
        "    rate = lookup_country_vat_rate(country_code)\n"
        "    return int(round(subtotal_cents * rate))\n\n"
        + _py_module_siblings("tax_svc", 4)
        + "\n"
    )
    files["src/services/inventory_service.py"] = (
        '"""Warehouse stock reservation service."""\n\n'
        "from src.repositories.order_repository import lock_warehouse_sku\n\n"
        "def reserve_order_stock(sku: str, quantity: int) -> bool:\n"
        '    """Reserve stock units before confirming order fulfillment."""\n'
        "    if quantity <= 0:\n"
        "        raise ValueError('quantity must be positive')\n"
        "    return lock_warehouse_sku(sku, quantity)\n\n"
        + _py_module_siblings("inventory_svc", 4)
        + "\n"
    )
    files["src/services/order_service.py"] = (
        '"""Order lifecycle orchestration service."""\n\n'
        "from src.repositories.order_repository import save_order_record\n"
        "from src.services.inventory_service import reserve_order_stock\n\n"
        "def legacy_dispatch_order(order_id: str, sku: str) -> dict[str, object]:\n"
        '    """Legacy v1 dispatch replaced in incremental push sync test."""\n'
        "    reserve_order_stock(sku, 1)\n"
        "    return save_order_record(order_id, 't-1', 1000)\n\n"
        + _py_module_siblings("order_svc", 4)
        + "\n"
    )
    files["src/services/notification_service.py"] = (
        '"""Transactional order email notifications."""\n\n'
        "def send_order_receipt_email(recipient: str, order_id: str) -> bool:\n"
        "    return bool(recipient and order_id)\n\n" + _py_module_siblings("notify_svc", 3) + "\n"
    )

    # --- FastAPI Routers (Direct calls + FastAPI Depends & Security DI) ---
    files["src/api/routers/orders_router.py"] = (
        '"""FastAPI orders router using Depends DI and direct service calls."""\n\n'
        "from fastapi import APIRouter, Depends\n"
        "from src.schemas.auth_schemas import Principal\n"
        "from src.services.auth_service import verify_bearer_principal\n"
        "from src.services.inventory_service import reserve_order_stock\n"
        "from src.services.payment_service import authorize_order_payment\n\n"
        "router = APIRouter(prefix='/orders')\n\n"
        "@router.post('/')\n"
        "async def create_order_endpoint(\n"
        "    order_id: str,\n"
        "    sku: str,\n"
        "    amount_cents: int,\n"
        "    principal: Principal = Depends(verify_bearer_principal),\n"
        ") -> dict[str, str]:\n"
        '    """Create an order after reserving stock and authorizing payment."""\n'
        "    reserve_order_stock(sku, 1)\n"
        "    ledger_id = authorize_order_payment(order_id, amount_cents)\n"
        "    return {'order_id': order_id, 'ledger_id': ledger_id, 'sub': principal.subject}\n\n"
        + _py_module_siblings("orders_rt", 4)
        + "\n"
    )
    files["src/api/routers/checkout_router.py"] = (
        '"""FastAPI checkout router orchestrating VAT and payment authorization."""\n\n'
        "from fastapi import APIRouter\n"
        "from src.services.payment_service import authorize_order_payment\n"
        "from src.services.tax_service import calculate_vat_breakdown\n\n"
        "router = APIRouter(prefix='/checkout')\n\n"
        "@router.post('/submit')\n"
        "async def submit_checkout_endpoint(\n"
        "    order_id: str,\n"
        "    subtotal_cents: int,\n"
        "    country_code: str,\n"
        ") -> dict[str, object]:\n"
        '    """Compute regional VAT and authorize total checkout payment."""\n'
        "    vat_cents = calculate_vat_breakdown(subtotal_cents, country_code)\n"
        "    ledger_id = authorize_order_payment(order_id, subtotal_cents + vat_cents)\n"
        "    return {'order_id': order_id, 'vat_cents': vat_cents, 'ledger_id': ledger_id}\n\n"
        + _py_module_siblings("checkout_rt", 4)
        + "\n"
    )
    files["src/api/routers/billing_router.py"] = (
        '"""FastAPI billing router using fastapi.Depends DI and tax previews."""\n\n'
        "import fastapi\n"
        "from src.services import auth_service\n"
        "from src.services.tax_service import calculate_vat_breakdown\n\n"
        "router = fastapi.APIRouter(prefix='/billing')\n\n"
        "@router.get('/invoices')\n"
        "async def list_billing_invoices(\n"
        "    principal = fastapi.Depends(auth_service.verify_bearer_principal),\n"
        ") -> dict[str, str]:\n"
        '    """List invoices scoped to the authenticated principal."""\n'
        "    return {'tenant': principal.tenant_id}\n\n"
        "@router.get('/tax-preview')\n"
        "async def preview_invoice_tax(subtotal_cents: int, country_code: str) -> dict[str, int]:\n"
        '    """Preview regional VAT breakdown prior to invoice issuance."""\n'
        "    vat_cents = calculate_vat_breakdown(subtotal_cents, country_code)\n"
        "    return {'vat_cents': vat_cents}\n\n" + _py_module_siblings("billing_rt", 4) + "\n"
    )
    files["src/api/routers/webhooks_router.py"] = (
        '"""FastAPI Stripe retry webhook handler."""\n\n'
        "from fastapi import APIRouter\n"
        "from src.services.payment_service import authorize_order_payment\n\n"
        "router = APIRouter(prefix='/webhooks')\n\n"
        "@router.post('/stripe-retry')\n"
        "async def handle_stripe_retry(order_id: str, amount_cents: int) -> dict[str, str]:\n"
        '    """Re-run idempotent payment authorization on Stripe webhook retry."""\n'
        "    ledger_id = authorize_order_payment(order_id, amount_cents)\n"
        "    return {'ledger_id': ledger_id}\n\n" + _py_module_siblings("webhook_rt", 4) + "\n"
    )
    files["src/api/routers/admin_router.py"] = (
        '"""FastAPI admin router enforcing Annotated Security DI guards."""\n\n'
        "from typing import Annotated\n"
        "from fastapi import APIRouter, Security\n"
        "from src.schemas.auth_schemas import Principal\n"
        "from src.services.auth_service import verify_bearer_principal\n\n"
        "router = APIRouter(prefix='/admin')\n\n"
        "@router.post('/orders/override')\n"
        "async def override_order_status(\n"
        "    order_id: str,\n"
        "    admin: Annotated[\n"
        "        Principal,\n"
        "        Security(verify_bearer_principal, scopes=['orders:admin']),\n"
        "    ],\n"
        ") -> dict[str, str]:\n"
        '    """Override order status for privileged operators."""\n'
        "    return {'order_id': order_id, 'operator': admin.subject}\n\n"
        + _py_module_siblings("admin_rt", 4)
        + "\n"
    )

    # --- Noise / Ignored Files (must be skipped by FileFilter) ---
    files["uv.lock"] = "version = 1\n" + ("[[package]]\nname = 'fastapi'\n" * 60)
    files["assets/architecture.svg"] = "<svg xmlns='http://www.w3.org/2000/svg'></svg>\n"

    # --- Architecture Markdown Docs (with CANARY_MD_EXCERPT in body) ---
    files["README.md"] = (
        "# FastAPI Commerce API\n\n"
        "## Overview\n"
        "Clean-architecture FastAPI commerce service separating routers and services.\n\n"
        "## Local Development\n"
        "Run pytest against PostgreSQL and Qdrant.\n"
    )
    files["ARCHITECTURE.md"] = (
        "# Commerce Service Architecture\n\n"
        "## Inventory Fulfillment & Warehouse Locking\n"
        "Every order fulfillment transition calling reserve_order_stock MUST lock warehouse SKU\n"
        "rows through lock_warehouse_sku before calling confirm_order_fulfillment.\n"
        f"Internal audit note: {CANARY_MD_EXCERPT}\n\n"
        "## Deployment Topology\n"
        "Stateless FastAPI workers scale behind an HTTP/2 ingress gateway.\n"
        + ("Operational runbook checklist item for cluster rollout stability.\n" * 16)
    )
    files["docs/adr/0001-jwt-di-guard.md"] = (
        "# ADR 0001: FastAPI JWT DI Guard\n\n"
        "## Bearer Principal Verification Policy\n"
        "All protected FastAPI routers MUST inject verify_bearer_principal via Depends/Security.\n"
        "Tokens MUST be decoded by decode_verified_jwt and checked via check_revoked_jti.\n\n"
        "## Key Rotation Schedule\n"
        "JWKS signing keys rotate every 30 days with 24-hour overlap windows.\n"
        + ("Compliance checklist item for JWT audience and issuer pinning.\n" * 16)
    )
    files["docs/adr/0002-order-idempotency.md"] = (
        "# ADR 0002: Order Payment Idempotency\n\n"
        "## Idempotent Stripe Authorization\n"
        "Every caller invoking authorize_order_payment MUST propagate an idempotency key to\n"
        "charge_stripe_intent and record_ledger_entry to prevent duplicate customer charges.\n\n"
        "## Reconciliation Window\n"
        "Ledger entries reconcile nightly against Stripe settlement batches.\n"
        + ("Finance reconciliation audit item for multi-currency settlement.\n" * 16)
    )
    files["docs/adr/0003-regional-vat.md"] = (
        "# ADR 0003: Regional VAT & Cross-Border Duty\n\n"
        "## Tax Calculation Contract\n"
        "Checkout and billing routers computing VAT via calculate_vat_breakdown or\n"
        "compute_regional_vat MUST source rates solely from lookup_country_vat_rate.\n\n"
        "## Audit Trail\n"
        "Store the applied ISO-3166 country code and tax rate snapshot on each invoice.\n"
        + ("Tax compliance note for EU OSS and APAC GST reporting.\n" * 16)
    )

    assert len(files) == 27
    return {path: text.encode("utf-8") for path, text in files.items()}


def _build_react_checkout_repo() -> dict[str, bytes]:
    """Build a realistic 26-file React + TypeScript/TSX checkout repository in RAM."""
    files: dict[str, str] = {}

    # --- Shared Types & API Clients ---
    files["web/src/types/checkoutTypes.ts"] = (
        f"export type InternalSecretMode = '{CANARY_TS_TYPE_ALIAS}';\n\n"
        "export interface CartQuote {\n"
        "  cartId: string;\n"
        "  subtotalCents: number;\n"
        "  currency: string;\n"
        "}\n\n"
        "export interface SavedPaymentMethod {\n"
        "  id: string;\n"
        "  brand: string;\n"
        "  last4: string;\n"
        "}\n\n" + _ts_module_siblings("checkoutTypes", 5)
    )
    files["web/src/api/ordersApiClient.ts"] = (
        "export function signApiRequestHeaders(cartId: string): Record<string, string> {\n"
        "  return { 'X-Cart-Id': cartId, 'X-Client': 'react-checkout-web' };\n"
        "}\n\n"
        "export async function fetchOrderHistoryList(userId: string): Promise<string[]> {\n"
        "  return [`ord_${userId}_1`, `ord_${userId}_2`];\n"
        "}\n\n" + _ts_module_siblings("ordersApi", 7)
    )
    files["web/src/api/cartApiClient.ts"] = (
        "import { signApiRequestHeaders } from './ordersApiClient';\n"
        "import { CartQuote } from '../types/checkoutTypes';\n\n"
        f"/**\n * Fetch validated checkout cart quote.\n * {CANARY_TS_JSDOC}\n */\n"
        "export async function fetchCartQuote(\n"
        "  cartId: string,\n"
        f"  fallbackSecret: string = '{CANARY_TS_DEFAULT}',\n"
        "): Promise<CartQuote> {\n"
        f"  const secretGuard = '{CANARY_TS_BODY}';\n"
        "  const headers = signApiRequestHeaders(cartId);\n"
        "  if (!headers['X-Cart-Id'] || !secretGuard || !fallbackSecret) {\n"
        "    throw new Error('Invalid cart request');\n"
        "  }\n"
        "  return { cartId, subtotalCents: 4500, currency: 'USD' };\n"
        "}\n\n" + _ts_module_siblings("cartApi", 7)
    )
    files["web/src/api/paymentApiClient.ts"] = (
        "import { SavedPaymentMethod } from '../types/checkoutTypes';\n\n"
        "export async function listSavedPaymentMethods("
        "cid: string): Promise<SavedPaymentMethod[]> {\n"
        "  if (!cid) {\n"
        "    return [];\n"
        "  }\n"
        "  return [{ id: 'pm_1', brand: 'visa', last4: '4242' }];\n"
        "}\n\n" + _ts_module_siblings("paymentApi", 7)
    )
    # Unrelated analytics decoy module calling same bare name WITHOUT importing the hook
    files["web/src/analytics/legacyTracker.ts"] = (
        "import { emitTelemetry } from './pixelCore';\n\n"
        "export function trackLegacyPayments(sessionId: string): string {\n"
        "  const summary = usePaymentMethods(sessionId);\n"
        "  return emitTelemetry(String(summary));\n"
        "}\n\n" + _ts_module_siblings("legacyAnalytics", 6)
    )
    files["web/src/analytics/pixelCore.ts"] = (
        "export function emitTelemetry(eventPayload: string): string {\n"
        "  return eventPayload.trim();\n"
        "}\n\n" + _ts_module_siblings("pixelCore", 4)
    )

    # --- Custom React Hooks ---
    files["web/src/hooks/useRegionalCurrency.ts"] = (
        "export function formatRegionalMoney(amountCents: number, currency: string): string {\n"
        "  const whole = (amountCents / 100).toFixed(2);\n"
        "  return `${currency} ${whole}`;\n"
        "}\n\n" + _ts_module_siblings("currencyHook", 7)
    )
    files["web/src/hooks/useCheckoutSession.ts"] = (
        "import { fetchCartQuote } from '../api/cartApiClient';\n"
        "import { formatRegionalMoney } from './useRegionalCurrency';\n\n"
        "export async function useCheckoutSession(cartId: string): Promise<{ total: string }> {\n"
        "  const quote = await fetchCartQuote(cartId);\n"
        "  const total = formatRegionalMoney(quote.subtotalCents, quote.currency);\n"
        "  return { total };\n"
        "}\n\n" + _ts_module_siblings("checkoutSession", 7)
    )
    files["web/src/hooks/usePaymentMethods.ts"] = (
        "import { listSavedPaymentMethods } from '../api/paymentApiClient';\n"
        "import { SavedPaymentMethod } from '../types/checkoutTypes';\n\n"
        "export async function usePaymentMethods(cid: string): Promise<SavedPaymentMethod[]> {\n"
        "  const methods = await listSavedPaymentMethods(cid);\n"
        "  return methods;\n"
        "}\n\n" + _ts_module_siblings("paymentMethodsHook", 7)
    )
    files["web/src/hooks/useOrderHistory.ts"] = (
        "import { fetchOrderHistoryList } from '../api/ordersApiClient';\n\n"
        "export async function useOrderHistory(userId: string): Promise<string[]> {\n"
        "  return await fetchOrderHistoryList(userId);\n"
        "}\n\n" + _ts_module_siblings("orderHistoryHook", 5)
    )

    # --- Shared Components (including React.memo & forwardRef + JSX callers) ---
    files["web/src/components/badgeTheme.ts"] = (
        "export function resolveBadgeTone(status: string): string {\n"
        "  if (status === 'paid') {\n"
        "    return 'emerald';\n"
        "  }\n"
        "  return status === 'failed' ? 'rose' : 'slate';\n"
        "}\n\n" + _ts_module_siblings("badgeTheme", 7)
    )
    files["web/src/components/OrderStatusBadge.tsx"] = (
        "import React from 'react';\n"
        "import { resolveBadgeTone } from './badgeTheme';\n\n"
        "export const OrderStatusBadge = React.forwardRef<HTMLSpanElement, { status: string }>(\n"
        "  (props, ref) => {\n"
        "    const tone = resolveBadgeTone(props.status);\n"
        "    return <span ref={ref} data-tone={tone}>{props.status}</span>;\n"
        "  }\n"
        ");\n\n" + _ts_module_siblings("statusBadge", 7)
    )
    files["web/src/components/CartDrawer.tsx"] = (
        "import React from 'react';\n"
        "import { useCheckoutSession } from '../hooks/useCheckoutSession';\n\n"
        "export const CartDrawer = React.memo((props: { cartId: string }) => {\n"
        "  const session = useCheckoutSession(props.cartId);\n"
        "  return <aside data-ready={Boolean(session)}>{props.cartId}</aside>;\n"
        "});\n\n" + _ts_module_siblings("cartDrawer", 7)
    )
    files["web/src/components/CheckoutSummaryPanel.tsx"] = (
        "import React from 'react';\n"
        "import { useCheckoutSession } from '../hooks/useCheckoutSession';\n\n"
        "export function CheckoutSummaryPanel(props: { cartId: string }) {\n"
        "  const session = useCheckoutSession(props.cartId);\n"
        "  return <section data-session={Boolean(session)}>Summary</section>;\n"
        "}\n\n" + _ts_module_siblings("summaryPanel", 7)
    )
    files["web/src/components/PromoCodeForm.tsx"] = (
        "import React from 'react';\n"
        "import { fetchCartQuote } from '../api/cartApiClient';\n\n"
        "export function PromoCodeForm(props: { cartId: string }) {\n"
        "  const quotePromise = fetchCartQuote(props.cartId);\n"
        "  return <form data-quote={Boolean(quotePromise)}><button>Apply</button></form>;\n"
        "}\n\n" + _ts_module_siblings("promoForm", 7)
    )
    files["web/src/components/OrderHistoryTable.tsx"] = (
        "import React from 'react';\n"
        "import { OrderStatusBadge } from './OrderStatusBadge';\n\n"
        "export function OrderHistoryTable(props: { status: string }) {\n"
        "  return (\n"
        "    <div>\n"
        "      <OrderStatusBadge status={props.status} />\n"
        "    </div>\n"
        "  );\n"
        "}\n\n" + _ts_module_siblings("historyTable", 7)
    )
    files["web/src/components/OrderConfirmationCard.tsx"] = (
        "import React from 'react';\n"
        "import { OrderStatusBadge } from './OrderStatusBadge';\n\n"
        "export function OrderConfirmationCard(props: { orderId: string; status: string }) {\n"
        "  return (\n"
        "    <article>\n"
        "      <h3>{props.orderId}</h3>\n"
        "      <OrderStatusBadge status={props.status} />\n"
        "    </article>\n"
        "  );\n"
        "}\n\n" + _ts_module_siblings("confirmCard", 7)
    )
    files["web/src/components/PaymentMethodSelector.tsx"] = (
        "import React from 'react';\n"
        "import { usePaymentMethods } from '../hooks/usePaymentMethods';\n\n"
        "export function PaymentMethodSelector(props: { customerId: string }) {\n"
        "  const methods = usePaymentMethods(props.customerId);\n"
        "  return <div data-methods={Boolean(methods)}>Select Card</div>;\n"
        "}\n\n" + _ts_module_siblings("methodSelector", 7)
    )

    # --- Pages ---
    files["web/src/pages/CheckoutPage.tsx"] = (
        "import React from 'react';\n"
        "import { useCheckoutSession } from '../hooks/useCheckoutSession';\n"
        "import { CheckoutSummaryPanel } from '../components/CheckoutSummaryPanel';\n\n"
        "export function CheckoutPage(props: { cartId: string }) {\n"
        "  const session = useCheckoutSession(props.cartId);\n"
        "  return (\n"
        "    <main data-ok={Boolean(session)}>\n"
        "      <CheckoutSummaryPanel cartId={props.cartId} />\n"
        "    </main>\n"
        "  );\n"
        "}\n\n" + _ts_module_siblings("checkoutPage", 7)
    )
    files["web/src/pages/PaymentSettingsPage.tsx"] = (
        "import React from 'react';\n"
        "import { usePaymentMethods } from '../hooks/usePaymentMethods';\n"
        "import { PaymentMethodSelector } from '../components/PaymentMethodSelector';\n\n"
        "export function PaymentSettingsPage(props: { customerId: string }) {\n"
        "  const methods = usePaymentMethods(props.customerId);\n"
        "  return (\n"
        "    <main data-has-methods={Boolean(methods)}>\n"
        "      <PaymentMethodSelector customerId={props.customerId} />\n"
        "    </main>\n"
        "  );\n"
        "}\n\n" + _ts_module_siblings("paymentSettings", 7)
    )

    # --- Noise / Ignored Files ---
    files["package-lock.json"] = (
        '{"lockfileVersion": 3, "packages": {'
        + ('"node_modules/react": {"version": "19.0.0"},' * 40)
        + '"": {}}}'
    )
    files["dist/app.min.js"] = "console.log('minified bundle');\n"

    # --- Architecture Markdown Docs ---
    files["README.md"] = (
        "# React Checkout Web\n\n"
        "## Overview\n"
        "TypeScript + React TSX checkout storefront consuming the FastAPI Commerce API.\n"
    )
    files["docs/architecture/state-hooks.md"] = (
        "# Frontend UI Guide\n\n"
        "## Checkout & Payment Hook Boundaries\n"
        "All checkout UI screens (CheckoutPage, CartDrawer, CheckoutSummaryPanel) and payment\n"
        "views MUST consume useCheckoutSession and usePaymentMethods hooks directly.\n\n"
        "## Error Boundaries\n"
        "Unexpected render failures must degrade cleanly inside page-level error boundaries.\n"
        + ("Frontend bundle splitting and hydration runbook item.\n" * 18)
        + "\n## Telemetry & Core Web Vitals\n"
        "Measure LCP and INP budgets on staging before promoting production builds.\n"
        + ("Synthetic browser monitoring checklist line for storefront rollouts.\n" * 16)
    )
    files["docs/adr/0001-api-client-contracts.md"] = (
        "# ADR 0001: Client Contracts\n\n"
        "## Signed Cart Quote Requests\n"
        "Every cart API client (fetchCartQuote / requestValidatedCartQuote) MUST attach\n"
        "signApiRequestHeaders and return strongly typed CartQuote envelopes.\n\n"
        "## Retries & Timeouts\n"
        "Idempotent GET requests cap timeout at 4 seconds with exponential backoff.\n"
        + ("Network resilience and circuit breaker checklist item.\n" * 18)
        + "\n## CORS & Versioning\n"
        "Pin API version headers on browser requests across staging and production.\n"
        + ("HTTP transport audit note for CDN cache keys.\n" * 16)
    )
    files["docs/adr/0002-design-system-badges.md"] = (
        "# ADR 0002: UI Primitives\n\n"
        "## OrderStatusBadge Accessibility & Tone Mapping\n"
        "OrderStatusBadge MUST forward DOM refs via React.forwardRef and map semantic tones\n"
        "strictly through resolveBadgeTone across OrderHistoryTable and OrderConfirmationCard.\n\n"
        "## Color Contrast\n"
        "All visual surface tokens maintain WCAG AA 4.5:1 text contrast.\n"
        + ("Design system token audit checklist item for dark and light themes.\n" * 18)
        + "\n## Iconography\n"
        "Use 16px vector glyphs with explicit aria-hidden attributes.\n"
        + ("Typography and spacing grid verification note.\n" * 16)
    )

    assert len(files) == 26
    return {path: text.encode("utf-8") for path, text in files.items()}


def _build_rogue_cross_tenant_repo() -> dict[str, bytes]:
    """Cross-tenant repository reusing symbol names to verify strict tenant isolation."""
    return {
        "src/rogue_py.py": (
            b"def rogue_py_caller(order_id: str, tok: str) -> None:\n"
            b"    verify_bearer_principal(tok)\n"
            b"    authorize_order_payment(order_id, 100)\n"
            b"    calculate_vat_breakdown(100, 'US')\n"
            b"    reserve_order_stock('SKU-1', 1)\n"
        ),
        "web/src/rogue_ts.tsx": (
            b"export function RogueTsCaller(props: { id: string }) {\n"
            b"  useCheckoutSession(props.id);\n"
            b"  usePaymentMethods(props.id);\n"
            b"  fetchCartQuote(props.id);\n"
            b"  return <OrderStatusBadge status='paid' />;\n"
            b"}\n"
        ),
    }


def _build_tree_and_blobs(
    repo_files: dict[str, bytes],
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    tree: list[dict[str, Any]] = []
    blobs: dict[str, bytes] = {}
    for path, content in repo_files.items():
        sha = _sha1_blob(content)
        blobs[sha] = content
        tree.append(
            {
                "path": path,
                "mode": "100644",
                "type": "blob",
                "sha": sha,
                "size": len(content),
            }
        )
    return tree, blobs


def _compute_recall_at_k[TItem: Hashable](
    retrieved_ordered: Sequence[TItem],
    expected: frozenset[TItem],
    *,
    k: int = 5,
) -> float:
    """Compute strict `Recall@k = |Top-k & GroundTruth| / min(k, |GroundTruth|)`."""
    if not expected:
        return 1.0
    top_k = set(retrieved_ordered[:k])
    denom = min(k, len(expected))
    return len(top_k & expected) / float(denom)


def test_compute_recall_at_k_strict_top_k_window() -> None:
    """[CA-16/F-04] Rank #6 must NEVER leak into Recall@5 when ranks #1..#5 contain duplicates."""
    ranked_files = ["noise.md", "noise.md", "noise.md", "noise.md", "noise.md", "target.md"]
    assert _compute_recall_at_k(ranked_files, frozenset({"target.md"}), k=5) == 0.0
    ranked_pairs = [
        ("src/decoy.py", "submit_order"),
        ("src/real.py", "submit_order"),
    ]
    assert _compute_recall_at_k(
        ranked_pairs, frozenset({("src/real.py", "submit_order")}), k=1
    ) == (0.0)
    assert _compute_recall_at_k(
        ranked_pairs, frozenset({("src/real.py", "submit_order")}), k=2
    ) == (1.0)


def _build_hermetic_settings(collection_name: str) -> Settings:
    """Preserve `.env` Docker host/port overrides while isolating hermetic mock credentials."""
    return Settings().model_copy(
        update={
            "qdrant_collection_name": collection_name,
            "github_app_id": None,
            "github_private_key": None,
            "github_token": SecretStr("ghp_m2_integration_mock"),
            "gemini_api_key": SecretStr("gem_m2_integration_mock"),
        }
    )


@asynccontextmanager
async def _isolated_session_factory(
    dsn: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Initialize isolated schema and guarantee SQLAlchemy async engine disposal on any error."""
    engine = create_async_engine(dsn, echo=False)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    finally:
        await engine.dispose()


@asynccontextmanager
async def _isolated_redis_client(redis_url: str) -> AsyncIterator[Any]:
    """Open async Redis client and guarantee cleanup independent of other resources."""
    client = redis_async.from_url(redis_url, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()


@dataclass(frozen=True, slots=True)
class _PRScenario:
    scenario_id: str
    language_group: Literal["python", "typescript"]
    repo_slug: str
    file_path: str
    diff_text: str
    after_source: bytes
    before_source: bytes
    expected_callers: frozenset[tuple[str, str]]
    expected_callees: frozenset[tuple[str, str]]
    expected_docs: frozenset[tuple[str, str]]
    has_noise_files: bool = False


def _replace_once(source_bytes: bytes, old: str, new: str) -> bytes:
    text = source_bytes.decode("utf-8")
    assert old in text, f"Expected snippet not found: {old!r}"
    return text.replace(old, new, 1).encode("utf-8")


def _single_line_diff(file_path: str, source_bytes: bytes, old_line: str, new_line: str) -> str:
    lines = source_bytes.decode("utf-8").splitlines()
    idx = next(i for i, ln in enumerate(lines) if ln == old_line)
    ctx_before = lines[idx - 1] if idx > 0 else None
    ctx_after = lines[idx + 1] if idx + 1 < len(lines) else None
    start = idx if ctx_before is not None else idx + 1
    body: list[str] = []
    if ctx_before is not None:
        body.append(f" {ctx_before}")
    body.append(f"-{old_line}")
    body.append(f"+{new_line}")
    if ctx_after is not None:
        body.append(f" {ctx_after}")
    count = len(body) - 1
    return (
        f"diff --git a/{file_path} b/{file_path}\n"
        f"--- a/{file_path}\n"
        f"+++ b/{file_path}\n"
        f"@@ -{start},{count} +{start},{count} @@ {old_line}\n" + "\n".join(body) + "\n"
    )


def _build_github_mock_handler(
    *,
    repos: dict[str, dict[str, Any]],
    all_blobs: dict[str, bytes],
    compare_routes: dict[tuple[str, str, str], dict[str, Any]],
    blob_latency_ref: list[float],
) -> Any:
    async def _handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for slug, meta in repos.items():
            if path == f"/repos/{slug}":
                return httpx.Response(
                    200,
                    json={
                        "id": meta["github_repo_id"],
                        "full_name": slug,
                        "default_branch": "main",
                    },
                )
            if path.startswith(f"/repos/{slug}/commits/"):
                ref = path.rsplit("/", 1)[-1]
                sha = meta["head_sha"] if ref == "main" else ref
                return httpx.Response(200, json={"sha": sha})
            if path.startswith(f"/repos/{slug}/contents/.codesight."):
                return httpx.Response(404)
            if path.startswith(f"/repos/{slug}/git/trees/"):
                return httpx.Response(
                    200,
                    json={"sha": meta["head_sha"], "truncated": False, "tree": meta["tree"]},
                )
            if path.startswith(f"/repos/{slug}/compare/"):
                pair = path.rsplit("/", 1)[-1]
                if "..." in pair:
                    base_sha, head_sha = pair.split("...", 1)
                    payload = compare_routes.get((slug, base_sha, head_sha))
                    if payload is not None:
                        return httpx.Response(200, json=payload)
                return httpx.Response(404)
            if path.startswith(f"/repos/{slug}/git/blobs/"):
                sha = path.rsplit("/", 1)[-1]
                if blob_latency_ref[0] > 0:
                    await asyncio.sleep(blob_latency_ref[0])
                raw = all_blobs.get(sha)
                if raw is not None:
                    return httpx.Response(200, json=_blob_json(raw, sha))
                return httpx.Response(404)
        return httpx.Response(404, json={"message": "Not Found"})

    return _handler


@pytest.mark.integration
@pytest.mark.asyncio
async def test_week2_dual_repo_cli_index_recall_latency_and_token_band(
    isolated_postgres_dsn: str,
    isolated_qdrant_collection: str,
    zerocode_auditor: Any,
) -> None:
    """Milestone 2 Gate: CLI index 2 repos, FR-07 sync, 8-PR Recall@5>=85%, <0.85s, 45-65%."""
    py_files = _build_fastapi_commerce_repo()
    ts_files = _build_react_checkout_repo()
    rogue_files = _build_rogue_cross_tenant_repo()

    py_tree, py_blobs = _build_tree_and_blobs(py_files)
    ts_tree, ts_blobs = _build_tree_and_blobs(ts_files)
    rogue_tree, rogue_blobs = _build_tree_and_blobs(rogue_files)

    all_blobs: dict[str, bytes] = {**py_blobs, **ts_blobs, **rogue_blobs}

    py_commit_v1 = "1" * 40
    py_commit_v2 = "2" * 40
    ts_commit_v1 = "3" * 40
    rogue_commit_v1 = "9" * 40

    # Prepare incremental v2 update for PY-4: order_service replaces legacy_dispatch_order
    # with confirm_order_fulfillment calling reserve_order_stock.
    py_order_svc_v2 = (
        '"""Order lifecycle orchestration service (v2 fulfillment sync)."""\n\n'
        "from src.repositories.order_repository import save_order_record\n"
        "from src.services.inventory_service import reserve_order_stock\n\n"
        "def confirm_order_fulfillment(order_id: str, sku: str, qty: int) -> dict[str, object]:\n"
        '    """Reserve warehouse stock and persist confirmed fulfillment."""\n'
        "    reserve_order_stock(sku, qty)\n"
        "    return save_order_record(order_id, 't-1', qty * 1000)\n\n"
        + _py_module_siblings("order_svc", 4)
        + "\n"
    ).encode("utf-8")
    py_order_svc_v2_sha = _sha1_blob(py_order_svc_v2)
    all_blobs[py_order_svc_v2_sha] = py_order_svc_v2

    repos: dict[str, dict[str, Any]] = {
        PY_REPO_SLUG: {"github_repo_id": 20016, "head_sha": py_commit_v1, "tree": py_tree},
        TS_REPO_SLUG: {"github_repo_id": 20017, "head_sha": ts_commit_v1, "tree": ts_tree},
        ROGUE_REPO_SLUG: {"github_repo_id": 20099, "head_sha": rogue_commit_v1, "tree": rogue_tree},
    }
    compare_routes = {
        (PY_REPO_SLUG, py_commit_v1, py_commit_v2): {
            "status": "ahead",
            "total_commits": 1,
            "files": [
                {
                    "filename": "src/services/order_service.py",
                    "status": "modified",
                    "sha": py_order_svc_v2_sha,
                }
            ],
        }
    }
    blob_latency_ref = [0.0]

    base_settings = _build_hermetic_settings(isolated_qdrant_collection)
    fake_genai = _DomainSemanticGenAIClient()
    registry = ASTParserRegistry()
    diff_parser = SmartDiffParser()
    auth = GitHubAuthProvider(base_settings)
    packer = TokenBudgetPacker(budget_tokens=3500)
    handler = _build_github_mock_handler(
        repos=repos,
        all_blobs=all_blobs,
        compare_routes=compare_routes,
        blob_latency_ref=blob_latency_ref,
    )

    async with _isolated_session_factory(isolated_postgres_dsn) as session_factory:
        async with (
            _isolated_redis_client(base_settings.redis_url) as redis_client,
            httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client,
            QdrantZeroCodeStore(
                settings=base_settings,
                collection_name=isolated_qdrant_collection,
            ) as store,
            GeminiEmbedder(client=fake_genai) as embedder,  # type: ignore[arg-type]
        ):
            # 1. Run `codesight index` (`run_cli_index`) on Python FastAPI, TS/TSX & rogue repos
            py_report = await run_cli_index(
                PY_REPO_SLUG,
                settings=base_settings,
                http_client=http_client,
                session_factory=session_factory,
                store=store,
                embedder=embedder,
            )
            assert py_report.tree_entries == 27
            assert py_report.eligible_files == 25  # uv.lock + svg skipped
            assert py_report.skipped_files == 2
            assert py_report.code_chunks > 80
            assert py_report.doc_chunks >= 5
            assert py_report.persisted_cursor is True

            # Idempotent re-index skips 100% unchanged files with 0 fetches/embeds
            py_replay = await run_cli_index(
                PY_REPO_SLUG,
                settings=base_settings,
                http_client=http_client,
                session_factory=session_factory,
                store=store,
                embedder=embedder,
            )
            assert py_replay.unchanged_files_skipped == py_report.eligible_files
            assert py_replay.fetched_files == 0
            assert py_replay.embedded_points == 0

            ts_report = await run_cli_index(
                TS_REPO_SLUG,
                settings=base_settings,
                http_client=http_client,
                session_factory=session_factory,
                store=store,
                embedder=embedder,
            )
            assert ts_report.tree_entries == 26
            assert ts_report.eligible_files == 24  # package-lock.json + dist/app.min.js skipped
            assert ts_report.skipped_files == 2
            assert ts_report.code_chunks > 100
            assert ts_report.doc_chunks >= 6
            assert ts_report.persisted_cursor is True

            rogue_report = await run_cli_index(
                ROGUE_REPO_SLUG,
                settings=base_settings,
                http_client=http_client,
                session_factory=session_factory,
                store=store,
                embedder=embedder,
            )
            assert rogue_report.code_chunks == 2

            # Verify PostgreSQL persisted last_indexed_sha for both real repos
            async with session_factory() as session:
                py_row = (
                    await session.execute(
                        sa.select(Repository).where(Repository.full_name == PY_REPO_SLUG)
                    )
                ).scalar_one()
                ts_row = (
                    await session.execute(
                        sa.select(Repository).where(Repository.full_name == TS_REPO_SLUG)
                    )
                ).scalar_one()
                assert py_row.last_indexed_sha == py_commit_v1
                assert ts_row.last_indexed_sha == ts_commit_v1
                py_repo_id = str(py_row.id)
                ts_repo_id = str(ts_row.id)

                session.add(
                    WebhookEvent(
                        delivery_id="ca16-push-py-v2",
                        event_type="push",
                        action=None,
                        status="queued",
                    )
                )
                await session.commit()

            # 2. Execute live FR-07 incremental push sync on Python repo before PR matrix
            repos[PY_REPO_SLUG]["head_sha"] = py_commit_v2
            push_runner = PushSyncRunner(
                settings=base_settings,
                http_client=http_client,
                session_factory=session_factory,
                redis_client=redis_client,
                store=store,
                embedder=embedder,
            )
            t_sync = time.perf_counter()
            sync_res = await push_runner.execute(
                {
                    "delivery_id": "ca16-push-py-v2",
                    "repo_id": py_repo_id,
                    "github_repo_id": 20016,
                    "repo_full_name": PY_REPO_SLUG,
                    "default_branch": "main",
                    "installation_id": None,
                    "ref": "refs/heads/main",
                    "before_sha": py_commit_v1,
                    "after_sha": py_commit_v2,
                }
            )
            sync_elapsed = time.perf_counter() - t_sync
            assert sync_elapsed < 5.0
            assert sync_res["status"] == "completed"
            assert sync_res["mode"] == "incremental_compare"
            assert (
                await store.find_by_symbol_names(
                    ["legacy_dispatch_order"],
                    repo_full_name=PY_REPO_SLUG,
                    repo_id=py_repo_id,
                )
                == ()
            )
            assert await redis_client.get(f"repo_index_lock:{py_repo_id}") is None

            async with session_factory() as session:
                refreshed_py = (
                    await session.execute(
                        sa.select(Repository).where(Repository.full_name == PY_REPO_SLUG)
                    )
                ).scalar_one()
                assert refreshed_py.last_indexed_sha == py_commit_v2

            # 3. Build 8-scenario ground-truth PR evaluation matrix (4 Python + 4 TS/TSX)
            py1_before = py_files["src/services/payment_service.py"]
            py1_old = "def authorize_order_payment(order_id: str, amount_cents: int) -> str:"
            py1_new = (
                "def authorize_order_payment("
                "order_id: str, amount_cents: int, idempotency_key: str) -> str:"
            )
            py1_after = _replace_once(py1_before, py1_old, py1_new)
            py1_diff = _single_line_diff(
                "src/services/payment_service.py", py1_before, py1_old, py1_new
            )

            py2_before = py_files["src/services/auth_service.py"]
            py2_old = (
                f"def verify_bearer_principal(authorization: str) -> Principal:  "
                f"# {CANARY_PY_COMMENT}"
            )
            py2_new = (
                "def verify_bearer_principal("
                "authorization: str, required_tenant: str) -> Principal:"
            )
            py2_after = _replace_once(py2_before, py2_old, py2_new)
            py2_diff = _single_line_diff(
                "src/services/auth_service.py", py2_before, py2_old, py2_new
            )

            py3_before = py_files["src/services/tax_service.py"]
            py3_old = "def calculate_vat_breakdown(subtotal_cents: int, country_code: str) -> int:"
            py3_new = (
                "def compute_regional_vat("
                "subtotal_cents: int, country_code: str, include_duty: bool) -> int:"
            )
            py3_after = _replace_once(py3_before, py3_old, py3_new)
            py3_diff = _single_line_diff(
                "src/services/tax_service.py", py3_before, py3_old, py3_new
            )

            py4_before = py_files["src/services/inventory_service.py"]
            py4_old = "def reserve_order_stock(sku: str, quantity: int) -> bool:"
            py4_new = "def reserve_order_stock(sku: str, quantity: int, warehouse_id: str) -> bool:"
            py4_after = _replace_once(py4_before, py4_old, py4_new)
            py4_diff = _single_line_diff(
                "src/services/inventory_service.py", py4_before, py4_old, py4_new
            )

            ts1_before = ts_files["web/src/hooks/useCheckoutSession.ts"]
            ts1_old = (
                "export async function useCheckoutSession(cartId: string): "
                "Promise<{ total: string }> {"
            )
            ts1_new = (
                "export async function useCheckoutSession("
                "cartId: string, currency: string): Promise<{ total: string; currency: string }> {"
            )
            ts1_after = _replace_once(ts1_before, ts1_old, ts1_new)
            ts1_diff = _single_line_diff(
                "web/src/hooks/useCheckoutSession.ts", ts1_before, ts1_old, ts1_new
            )

            ts2_before = ts_files["web/src/api/cartApiClient.ts"]
            ts2_old = "export async function fetchCartQuote("
            ts2_new = "export async function requestValidatedCartQuote("
            ts2_after = _replace_once(ts2_before, ts2_old, ts2_new)
            ts2_diff = _single_line_diff(
                "web/src/api/cartApiClient.ts", ts2_before, ts2_old, ts2_new
            )

            ts3_before = ts_files["web/src/components/OrderStatusBadge.tsx"]
            ts3_old = (
                "export const OrderStatusBadge = React.forwardRef<"
                "HTMLSpanElement, { status: string }>("
            )
            ts3_new = (
                "export const OrderStatusBadge = React.forwardRef<"
                "HTMLSpanElement, { status: string; size: 'sm' | 'md' }>("
            )
            ts3_after = _replace_once(ts3_before, ts3_old, ts3_new)
            ts3_diff = _single_line_diff(
                "web/src/components/OrderStatusBadge.tsx", ts3_before, ts3_old, ts3_new
            )

            ts4_before = ts_files["web/src/hooks/usePaymentMethods.ts"]
            ts4_old = (
                "export async function usePaymentMethods(cid: string): "
                "Promise<SavedPaymentMethod[]> {"
            )
            ts4_new = (
                "export async function usePaymentMethods("
                "cid: string, includeExpired: boolean): Promise<SavedPaymentMethod[]> {"
            )
            ts4_after = _replace_once(ts4_before, ts4_old, ts4_new)
            ts4_diff = (
                _single_line_diff(
                    "web/src/hooks/usePaymentMethods.ts", ts4_before, ts4_old, ts4_new
                )
                + "diff --git a/package-lock.json b/package-lock.json\n"
                "--- a/package-lock.json\n"
                "+++ b/package-lock.json\n"
                "@@ -1,1 +1,1 @@\n"
                '-{"lockfileVersion": 3, "name": "old"}\n'
                '+{"lockfileVersion": 3, "name": "new", "packages": {"a": 1, "b": 2}}\n'
            )

            scenarios: tuple[_PRScenario, ...] = (
                _PRScenario(
                    scenario_id="PY-1",
                    language_group="python",
                    repo_slug=PY_REPO_SLUG,
                    file_path="src/services/payment_service.py",
                    diff_text=py1_diff,
                    after_source=py1_after,
                    before_source=py1_before,
                    expected_callers=frozenset(
                        {
                            ("src/api/routers/orders_router.py", "create_order_endpoint"),
                            ("src/api/routers/checkout_router.py", "submit_checkout_endpoint"),
                            ("src/api/routers/webhooks_router.py", "handle_stripe_retry"),
                        }
                    ),
                    expected_callees=frozenset(
                        {
                            ("src/clients/stripe_gateway.py", "charge_stripe_intent"),
                            ("src/repositories/ledger_repository.py", "record_ledger_entry"),
                        }
                    ),
                    expected_docs=frozenset(
                        {
                            (
                                "docs/adr/0002-order-idempotency.md",
                                "ADR 0002: Order Payment Idempotency > "
                                "Idempotent Stripe Authorization",
                            )
                        }
                    ),
                ),
                _PRScenario(
                    scenario_id="PY-2",
                    language_group="python",
                    repo_slug=PY_REPO_SLUG,
                    file_path="src/services/auth_service.py",
                    diff_text=py2_diff,
                    after_source=py2_after,
                    before_source=py2_before,
                    expected_callers=frozenset(
                        {
                            ("src/api/routers/orders_router.py", "create_order_endpoint"),
                            ("src/api/routers/billing_router.py", "list_billing_invoices"),
                            ("src/api/routers/admin_router.py", "override_order_status"),
                        }
                    ),
                    expected_callees=frozenset(
                        {
                            ("src/clients/jwt_verifier.py", "decode_verified_jwt"),
                            ("src/clients/jwt_verifier.py", "check_revoked_jti"),
                        }
                    ),
                    expected_docs=frozenset(
                        {
                            (
                                "docs/adr/0001-jwt-di-guard.md",
                                "ADR 0001: FastAPI JWT DI Guard > "
                                "Bearer Principal Verification Policy",
                            )
                        }
                    ),
                ),
                _PRScenario(
                    scenario_id="PY-3",
                    language_group="python",
                    repo_slug=PY_REPO_SLUG,
                    file_path="src/services/tax_service.py",
                    diff_text=py3_diff,
                    after_source=py3_after,
                    before_source=py3_before,
                    expected_callers=frozenset(
                        {
                            ("src/api/routers/checkout_router.py", "submit_checkout_endpoint"),
                            ("src/api/routers/billing_router.py", "preview_invoice_tax"),
                        }
                    ),
                    expected_callees=frozenset(
                        {("src/clients/tax_rate_table.py", "lookup_country_vat_rate")}
                    ),
                    expected_docs=frozenset(
                        {
                            (
                                "docs/adr/0003-regional-vat.md",
                                "ADR 0003: Regional VAT & Cross-Border Duty > "
                                "Tax Calculation Contract",
                            )
                        }
                    ),
                ),
                _PRScenario(
                    scenario_id="PY-4",
                    language_group="python",
                    repo_slug=PY_REPO_SLUG,
                    file_path="src/services/inventory_service.py",
                    diff_text=py4_diff,
                    after_source=py4_after,
                    before_source=py4_before,
                    expected_callers=frozenset(
                        {
                            ("src/services/order_service.py", "confirm_order_fulfillment"),
                            ("src/api/routers/orders_router.py", "create_order_endpoint"),
                        }
                    ),
                    expected_callees=frozenset(
                        {("src/repositories/order_repository.py", "lock_warehouse_sku")}
                    ),
                    expected_docs=frozenset(
                        {
                            (
                                "ARCHITECTURE.md",
                                "Commerce Service Architecture > "
                                "Inventory Fulfillment & Warehouse Locking",
                            )
                        }
                    ),
                ),
                _PRScenario(
                    scenario_id="TS-1",
                    language_group="typescript",
                    repo_slug=TS_REPO_SLUG,
                    file_path="web/src/hooks/useCheckoutSession.ts",
                    diff_text=ts1_diff,
                    after_source=ts1_after,
                    before_source=ts1_before,
                    expected_callers=frozenset(
                        {
                            ("web/src/pages/CheckoutPage.tsx", "CheckoutPage"),
                            ("web/src/components/CartDrawer.tsx", "CartDrawer"),
                            ("web/src/components/CheckoutSummaryPanel.tsx", "CheckoutSummaryPanel"),
                        }
                    ),
                    expected_callees=frozenset(
                        {
                            ("web/src/api/cartApiClient.ts", "fetchCartQuote"),
                            ("web/src/hooks/useRegionalCurrency.ts", "formatRegionalMoney"),
                        }
                    ),
                    expected_docs=frozenset(
                        {
                            (
                                "docs/architecture/state-hooks.md",
                                "Frontend UI Guide > Checkout & Payment Hook Boundaries",
                            )
                        }
                    ),
                ),
                _PRScenario(
                    scenario_id="TS-2",
                    language_group="typescript",
                    repo_slug=TS_REPO_SLUG,
                    file_path="web/src/api/cartApiClient.ts",
                    diff_text=ts2_diff,
                    after_source=ts2_after,
                    before_source=ts2_before,
                    expected_callers=frozenset(
                        {
                            ("web/src/hooks/useCheckoutSession.ts", "useCheckoutSession"),
                            ("web/src/components/PromoCodeForm.tsx", "PromoCodeForm"),
                        }
                    ),
                    expected_callees=frozenset(
                        {("web/src/api/ordersApiClient.ts", "signApiRequestHeaders")}
                    ),
                    expected_docs=frozenset(
                        {
                            (
                                "docs/adr/0001-api-client-contracts.md",
                                "ADR 0001: Client Contracts > Signed Cart Quote Requests",
                            )
                        }
                    ),
                ),
                _PRScenario(
                    scenario_id="TS-3",
                    language_group="typescript",
                    repo_slug=TS_REPO_SLUG,
                    file_path="web/src/components/OrderStatusBadge.tsx",
                    diff_text=ts3_diff,
                    after_source=ts3_after,
                    before_source=ts3_before,
                    expected_callers=frozenset(
                        {
                            ("web/src/components/OrderHistoryTable.tsx", "OrderHistoryTable"),
                            (
                                "web/src/components/OrderConfirmationCard.tsx",
                                "OrderConfirmationCard",
                            ),
                        }
                    ),
                    expected_callees=frozenset(
                        {("web/src/components/badgeTheme.ts", "resolveBadgeTone")}
                    ),
                    expected_docs=frozenset(
                        {
                            (
                                "docs/adr/0002-design-system-badges.md",
                                "ADR 0002: UI Primitives > "
                                "OrderStatusBadge Accessibility & Tone Mapping",
                            )
                        }
                    ),
                ),
                _PRScenario(
                    scenario_id="TS-4",
                    language_group="typescript",
                    repo_slug=TS_REPO_SLUG,
                    file_path="web/src/hooks/usePaymentMethods.ts",
                    diff_text=ts4_diff,
                    after_source=ts4_after,
                    before_source=ts4_before,
                    expected_callers=frozenset(
                        {
                            (
                                "web/src/components/PaymentMethodSelector.tsx",
                                "PaymentMethodSelector",
                            ),
                            ("web/src/pages/PaymentSettingsPage.tsx", "PaymentSettingsPage"),
                        }
                    ),
                    expected_callees=frozenset(
                        {("web/src/api/paymentApiClient.ts", "listSavedPaymentMethods")}
                    ),
                    expected_docs=frozenset(
                        {
                            (
                                "docs/architecture/state-hooks.md",
                                "Frontend UI Guide > Checkout & Payment Hook Boundaries",
                            )
                        }
                    ),
                    has_noise_files=True,
                ),
            )

            # Simulate realistic live network RTT during PR retrieval:
            # ~70ms Gemini query embed + ~220ms concurrent HTTP/2 GitHub blob RTT
            fake_genai.simulated_latency_seconds = 0.07
            blob_latency_ref[0] = 0.22

            retriever = HybridContextRetriever(store=store, embedder=embedder)
            fetcher = GitHubBlobFetcher(auth, http_client=http_client)

            repo_id_by_slug = {PY_REPO_SLUG: py_repo_id, TS_REPO_SLUG: ts_repo_id}
            recall_callers: dict[str, list[float]] = {"python": [], "typescript": []}
            recall_callees: dict[str, list[float]] = {"python": [], "typescript": []}
            recall_docs: dict[str, list[float]] = {"python": [], "typescript": []}
            pure_token_savings: list[float] = []

            for case in scenarios:
                parsed_diff = diff_parser.parse_diff(case.diff_text)
                if case.has_noise_files:
                    assert parsed_diff.metrics.skipped_files == 1
                    assert parsed_diff.metrics.saved_estimated_tokens > 0
                else:
                    assert parsed_diff.metrics.skipped_files == 0

                file_diff = next(f for f in parsed_diff.files if f.file_path == case.file_path)
                file_ctx = registry.resolve_pr_file(
                    file_diff,
                    after_source=case.after_source,
                    before_source=case.before_source,
                )

                embed_before = fake_genai.embed_calls
                assembled = await retriever.retrieve_and_pack(
                    [file_ctx],
                    repo_full_name=case.repo_slug,
                    repo_id=repo_id_by_slug[case.repo_slug],
                    blob_fetcher=fetcher,
                    packer=packer,
                    parsed_diff=parsed_diff,
                    in_memory_sources={case.file_path: case.after_source},
                )

                # Single batched query embedding call per PR
                assert fake_genai.embed_calls - embed_before == 1, case.scenario_id

                # Strict cross-tenant isolation across every retrieved tier
                all_cands = (
                    *assembled.retrieval.callers,
                    *assembled.retrieval.callees,
                    *assembled.retrieval.architecture_docs,
                )
                assert all(c.repo_full_name == case.repo_slug for c in all_cands), case.scenario_id
                assert not any(
                    c.symbol_name in {"rogue_py_caller", "RogueTsCaller"} for c in all_cands
                ), f"Cross-tenant leak in {case.scenario_id}"

                # Strict Recall@5 over exact (file_path, symbol_name) tuples without pre-dedup
                got_callers = [(c.file_path, c.symbol_name) for c in assembled.retrieval.callers]
                got_callees = [(c.file_path, c.symbol_name) for c in assembled.retrieval.callees]
                got_docs = [
                    (d.file_path, d.symbol_name) for d in assembled.retrieval.architecture_docs
                ]

                c_rec = _compute_recall_at_k(got_callers, case.expected_callers, k=5)
                e_rec = _compute_recall_at_k(got_callees, case.expected_callees, k=5)
                d_rec = _compute_recall_at_k(got_docs, case.expected_docs, k=5)
                recall_callers[case.language_group].append(c_rec)
                recall_callees[case.language_group].append(e_rec)
                recall_docs[case.language_group].append(d_rec)

                # Decoy ranking guard on TS-4: true imported callers rank ahead of unrelated decoy
                decoy_key = ("web/src/analytics/legacyTracker.ts", "trackLegacyPayments")
                if case.scenario_id == "TS-4" and decoy_key in got_callers:
                    decoy_pos = got_callers.index(decoy_key)
                    assert (
                        got_callers.index(
                            (
                                "web/src/components/PaymentMethodSelector.tsx",
                                "PaymentMethodSelector",
                            )
                        )
                        < decoy_pos
                    )
                    assert (
                        got_callers.index(
                            ("web/src/pages/PaymentSettingsPage.tsx", "PaymentSettingsPage")
                        )
                        < decoy_pos
                    )

                # AC-1 latency SLA (< 0.85s) & hard auxiliary token budget (<= 3,500)
                assert assembled.total_ms < 850.0, (
                    f"{case.scenario_id} exceeded 0.85s SLA: {assembled.total_ms:.1f}ms "
                    f"(qdrant={assembled.qdrant_ms}ms, blob={assembled.blob_fetch_ms}ms)"
                )
                metrics = assembled.packed_bundle.metrics
                assert metrics.packed_aux_tokens <= 3500, case.scenario_id
                assert metrics.tokens_saved_pct >= 45.0, (
                    f"{case.scenario_id} token savings {metrics.tokens_saved_pct}% < 45.0%"
                )
                if case.has_noise_files:
                    assert metrics.skipped_noise_tokens > 0, case.scenario_id
                    assert metrics.aux_tokens_saved_pct >= 45.0, (
                        f"{case.scenario_id} aux_tokens_saved_pct "
                        f"{metrics.aux_tokens_saved_pct}% < 45.0%"
                    )
                else:
                    assert metrics.skipped_noise_tokens == 0, case.scenario_id
                    pure_token_savings.append(metrics.tokens_saved_pct)

            # Assert Recall@5 >= 85% across BOTH Python and TypeScript for Callers, Callees, Docs
            for lang in ("python", "typescript"):
                mean_callers = sum(recall_callers[lang]) / len(recall_callers[lang])
                mean_callees = sum(recall_callees[lang]) / len(recall_callees[lang])
                mean_docs = sum(recall_docs[lang]) / len(recall_docs[lang])
                assert mean_callers >= 0.85, f"{lang} Callers Recall@5={mean_callers:.2%} < 85%"
                assert mean_callees >= 0.85, f"{lang} Callees Recall@5={mean_callees:.2%} < 85%"
                assert mean_docs >= 0.85, f"{lang} ArchDocs Recall@5={mean_docs:.2%} < 85%"

            # AC-2 anti-gaming band: pure code/doc AST-RAG mean token savings within [45%, 65%]
            mean_pure_savings = sum(pure_token_savings) / len(pure_token_savings)
            assert 45.0 <= mean_pure_savings <= 65.0, (
                f"Expected authentic pure AST-RAG token reduction in [45%, 65%], "
                f"got {mean_pure_savings:.2f}% (per-PR: {pure_token_savings})"
            )

            # 4. Dual-layer NFR-3 Zero-Code Persistence audit over BOTH initial CLI index &
            # incremental PushSyncRunner writes (100% paginated API + physical Qdrant disk scan)
            expected_total_points = (
                py_report.upserted_points + ts_report.upserted_points + rogue_report.upserted_points
            )
            assert expected_total_points > 200
            scrolled = zerocode_auditor.scroll_all_points(
                base_settings.qdrant_http_url,
                isolated_qdrant_collection,
                page_size=64,
                max_points=None,
            )
            assert len(scrolled) == expected_total_points
            assert (
                zerocode_auditor.audit_live_collection(
                    base_settings.qdrant_http_url,
                    isolated_qdrant_collection,
                    scan_all=True,
                    strict_indexes=True,
                    canary_substrings=ALL_NFR3_CANARIES,
                )
                == 0
            )

            # Layer 2: Physical container disk scan over `/qdrant/storage/collections/<collection>`
            qdrant_container = (
                (base_settings.codesight_qdrant_container or "").strip()
                or os.getenv("CODESIGHT_QDRANT_CONTAINER", "").strip()
                or "codesight-qdrant"
            )
            control_hits = zerocode_auditor.scan_container_storage_for_canaries(
                qdrant_container,
                isolated_qdrant_collection,
                ("fastapi-commerce-api",),
            )
            assert control_hits and all(
                hit.startswith("NFR-3 disk breach") for hit in control_hits
            ), (
                "Positive control failed: expected Qdrant WAL/segment files on disk to match "
                f"indexed metadata slug 'fastapi-commerce-api', got: {control_hits!r}"
            )

            disk_leaks = zerocode_auditor.scan_container_storage_for_canaries(
                qdrant_container,
                isolated_qdrant_collection,
                ALL_NFR3_CANARIES,
            )
            assert disk_leaks == [], f"NFR-3 disk persistence violations: {disk_leaks}"


@pytest.mark.skipif(
    os.getenv("CODESIGHT_RUN_LIVE_M2_EVAL") != "1",
    reason=(
        "Opt-in live GitHub + Gemini evaluation disabled "
        "(set CODESIGHT_RUN_LIVE_M2_EVAL=1, GEMINI_API_KEY, and GITHUB_TOKEN in .env to run)."
    ),
)
@pytest.mark.integration
@pytest.mark.asyncio
async def test_week2_opt_in_live_github_and_gemini_rag_smoke(
    isolated_postgres_dsn: str,
    isolated_qdrant_collection: str,
    zerocode_auditor: Any,
) -> None:
    """Opt-in live Milestone 2 gate indexing & evaluating real Python + TS/TSX GitHub repos."""
    settings = Settings(qdrant_collection_name=isolated_qdrant_collection)
    if settings.gemini_api_key is None or not settings.gemini_api_key.get_secret_value().strip():
        pytest.skip("GEMINI_API_KEY is not configured in .env for live Milestone 2 evaluation.")
    if not settings.has_github_credentials:
        pytest.skip(
            "GitHub credentials (GITHUB_TOKEN or GitHub App) are required for live Milestone 2 "
            "evaluation to avoid GitHub's 60 req/hr unauthenticated rate limit."
        )

    live_py_repo = (
        (settings.codesight_live_py_repo or "").strip()
        or os.getenv("CODESIGHT_LIVE_PY_REPO", "").strip()
        or DEFAULT_LIVE_PY_REPO
    )
    live_ts_repo = (settings.codesight_live_ts_repo or "").strip() or os.getenv(
        "CODESIGHT_LIVE_TS_REPO", ""
    ).strip()

    async with _isolated_session_factory(isolated_postgres_dsn) as session_factory:
        report = await run_milestone2_live_evaluation(
            settings=settings,
            py_repo=live_py_repo,
            ts_repo=live_ts_repo,
            collection_name=isolated_qdrant_collection,
            session_factory=session_factory,
            auditor_module=zerocode_auditor,
        )

    print(json.dumps(report.to_dict(), indent=2))
    assert report.py_repo
    assert report.ts_repo
    assert report.recall_at_5 >= 0.85
    assert report.p50_retrieval_ms < 850.0
    assert report.tokens_saved_pct >= 45.0
    assert report.zerocode_violations == 0
    assert report.disk_violations == ()
    assert validate_m2_kpi_thresholds(report) == []
