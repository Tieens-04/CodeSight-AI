"""Live Qdrant gRPC integration tests for HybridContextRetriever, BlobFetcher & Packer (CA-15)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import math
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from codesight.ast_engine.registry import ASTParserRegistry
from codesight.core.config import Settings
from codesight.diff.smart_diff_parser import SmartDiffParser
from codesight.rag.doc_chunker import MarkdownDocChunker
from codesight.rag.embedder import GeminiEmbedder
from codesight.rag.models import EMBEDDING_DIMENSIONS, TransientEmbeddingItem
from codesight.rag.qdrant_store import QdrantZeroCodeStore
from codesight.rag.retriever import HybridContextRetriever
from codesight.rag.token_packer import TokenBudgetPacker
from codesight.vcs.blob_fetcher import GitHubBlobFetcher
from codesight.vcs.github_auth import GitHubAuthProvider


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


def _topic_embed_vec(text: str) -> list[float]:
    """Deterministic 768-dim topic vector yielding high cosine (>=0.85) on matching domain docs."""
    lower = text.lower()
    vec = [0.0] * EMBEDDING_DIMENSIONS
    topic_groups = (
        (("auth", "jwt", "session", "verify_session", "token", "revocation"), 0),
        (("billing", "invoice", "stripe", "refund", "ledger"), 4),
        (("useauthsession", "react", "tsx", "dashboard", "navbar", "frontend"), 8),
    )
    for keywords, base_idx in topic_groups:
        hits = sum(1 for kw in keywords if kw in lower)
        if hits:
            val = float(hits) * 4.0
            for offset in range(4):
                vec[base_idx + offset] = val

    # Deterministic small tie-break residual across dims 16..31
    digest = hashlib.sha256(lower.encode("utf-8")).digest()
    for i in range(16):
        vec[16 + i] = (float(digest[i]) / 255.0) * 0.15

    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class _TopicGenAIClient:
    def __init__(self, *, simulated_latency_seconds: float = 0.0) -> None:
        self.embed_calls = 0
        self._simulated_latency_seconds = simulated_latency_seconds
        self.aio = SimpleNamespace(models=self)

    async def embed_content(
        self,
        *,
        model: str,
        contents: list[str],
        config: Any = None,
    ) -> Any:
        self.embed_calls += 1
        if self._simulated_latency_seconds > 0:
            await asyncio.sleep(self._simulated_latency_seconds)
        return SimpleNamespace(
            embeddings=[SimpleNamespace(values=_topic_embed_vec(txt)) for txt in contents]
        )


def _pad_py_helpers(prefix: str, count: int) -> str:
    blocks = [
        (
            f"def _{prefix}_support_{i:02d}(payload: dict[str, str]) -> str:\n"
            f'    """Internal helper {i:02d} keeping realistic module size for token KPI."""\n'
            f"    raw = payload.get('key_{i:02d}', 'default_value_{i:02d}')\n"
            f"    return f'{prefix}_{i:02d}:{{raw.strip().lower()}}'\n"
        )
        for i in range(count)
    ]
    return "\n\n".join(blocks)


def _pad_ts_helpers(prefix: str, count: int) -> str:
    blocks = [
        (
            f"export function {prefix}Helper{i:02d}(input: string): string {{\n"
            f"  const cleaned = input.trim().toLowerCase();\n"
            f"  return `${{cleaned}}-{prefix}-{i:02d}`;\n"
            f"}}\n"
        )
        for i in range(count)
    ]
    return "\n".join(blocks)


def _build_dual_language_repo() -> dict[str, bytes]:
    """Build a realistic multi-file Python + TypeScript/TSX + ADR repository fixture."""
    jwt_helper_py = (
        '"""JWT decoding and revocation helpers."""\n\n'
        "def decode_jwt_claims(raw_token: str) -> dict[str, str]:\n"
        '    """Decode and validate JWT signature claims."""\n'
        "    if not raw_token or '.' not in raw_token:\n"
        "        raise ValueError('Malformed JWT token')\n"
        "    return {'sub': raw_token.split('.')[0], 'scope': 'user'}\n\n"
        "def is_token_revoked(subject: str) -> bool:\n"
        '    """Check revocation deny-list for subject."""\n'
        "    return subject.startswith('revoked_')\n\n" + _pad_py_helpers("jwt", 22) + "\n"
    ).encode("utf-8")

    auth_service_py = (
        '"""Core authentication session verification service."""\n\n'
        "from src.crypto.jwt_helper import decode_jwt_claims, is_token_revoked\n\n"
        + _pad_py_helpers("auth_pre", 18)
        + "\n\n"
        "def verify_session(raw_token: str) -> dict[str, str]:\n"
        '    """Verify active user session JWT and return validated claims."""\n'
        "    claims = decode_jwt_claims(raw_token)\n"
        "    if is_token_revoked(claims['sub']):\n"
        "        raise PermissionError('Session token revoked')\n"
        "    return claims\n\n" + _pad_py_helpers("auth_post", 18) + "\n"
    ).encode("utf-8")

    users_router_py = (
        '"""FastAPI users router calling verify_session."""\n\n'
        "from src.services.auth_service import verify_session\n\n"
        + _pad_py_helpers("users_pre", 20)
        + "\n\n"
        "def get_current_profile(auth_header: str) -> dict[str, str]:\n"
        '    """Return authenticated user profile."""\n'
        "    claims = verify_session(auth_header)\n"
        "    return {'user': claims['sub'], 'role': claims['scope']}\n\n"
        "def delete_current_account(auth_header: str) -> dict[str, bool]:\n"
        '    """Delete authenticated user account after session check."""\n'
        "    claims = verify_session(auth_header)\n"
        "    return {'deleted': bool(claims['sub'])}\n\n" + _pad_py_helpers("users_post", 16) + "\n"
    ).encode("utf-8")

    unrelated_billing_py = (
        '"""Billing ledger utilities (unrelated to auth)."""\n\n'
        "def reconcile_invoice(invoice_id: str) -> int:\n"
        "    return len(invoice_id) * 100\n\n" + _pad_py_helpers("billing", 20) + "\n"
    ).encode("utf-8")

    ts_api_client = (
        "export async function fetchSessionClaims(token: string): Promise<{ sub: string }> {\n"
        "  if (!token) {\n"
        "    throw new Error('Missing bearer token');\n"
        "  }\n"
        "  return { sub: token.slice(0, 8) };\n"
        "}\n\n" + _pad_ts_helpers("apiClient", 24)
    ).encode("utf-8")

    ts_use_auth_hook = (
        "import { fetchSessionClaims } from '../api/sessionClient';\n\n"
        + _pad_ts_helpers("hookPre", 20)
        + "\n"
        "export async function useAuthSession(token: string): Promise<string> {\n"
        "  const claims = await fetchSessionClaims(token);\n"
        "  return claims.sub;\n"
        "}\n\n" + _pad_ts_helpers("hookPost", 20)
    ).encode("utf-8")

    tsx_dashboard = (
        "import { useAuthSession } from '../hooks/useAuthSession';\n\n"
        + _pad_ts_helpers("dashPre", 22)
        + "\n"
        "export async function DashboardPanel(props: { token: string }): Promise<string> {\n"
        "  const subject = await useAuthSession(props.token);\n"
        "  return `<div>${subject}</div>`;\n"
        "}\n\n" + _pad_ts_helpers("dashPost", 20)
    ).encode("utf-8")

    auth_adr_md = (
        "# ADR 0001: Auth Session & JWT Revocation\n\n"
        "## Session Token Verification Policy\n"
        "Every backend router calling verify_session MUST enforce JWT signature validation\n"
        "and check token revocation state before returning user profile data.\n\n"
        + ("Additional security audit guidelines and historical rollout context.\n" * 45)
    ).encode("utf-8")

    frontend_arch_md = (
        "# Frontend Auth Architecture\n\n"
        "## React TSX Session Hook Rules\n"
        "All React TSX Dashboard and Navbar components MUST read authenticated identity via\n"
        "useAuthSession instead of decoding raw tokens directly in UI components.\n\n"
        + ("Additional frontend state-management notes and migration logs.\n" * 45)
    ).encode("utf-8")

    billing_adr_md = (
        "# ADR 0002: Billing Stripe Retries\n\n"
        "## Invoice Idempotency\n"
        "Stripe payment webhook retries must deduplicate by invoice ledger key.\n\n"
        + ("Additional billing reconciliation procedures.\n" * 45)
    ).encode("utf-8")

    return {
        "src/crypto/jwt_helper.py": jwt_helper_py,
        "src/services/auth_service.py": auth_service_py,
        "src/api/users_router.py": users_router_py,
        "src/billing/ledger.py": unrelated_billing_py,
        "web/src/api/sessionClient.ts": ts_api_client,
        "web/src/hooks/useAuthSession.ts": ts_use_auth_hook,
        "web/src/components/DashboardPanel.tsx": tsx_dashboard,
        "docs/adr/0001-auth-security.md": auth_adr_md,
        "docs/architecture/frontend-auth.md": frontend_arch_md,
        "docs/adr/0002-billing-retries.md": billing_adr_md,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_hybrid_retriever_dual_lang_live_qdrant_latency_and_token_savings(
    isolated_qdrant_collection: str,
    zerocode_auditor: Any,
) -> None:
    """Live Qdrant gRPC + concurrent RAM BlobFetcher + TokenPacker meets <0.85s, >=45%, NFR-3."""
    repo_slug = "Tieens-04/codesight-sample-dual"
    other_repo = "other-org/isolated-tenant"
    repo_files = _build_dual_language_repo()

    registry = ASTParserRegistry()
    doc_chunker = MarkdownDocChunker()
    fake_genai = _TopicGenAIClient()
    embedder = GeminiEmbedder(client=fake_genai)  # type: ignore[arg-type]

    blobs_by_sha: dict[str, bytes] = {}
    items: list[TransientEmbeddingItem] = []

    for file_path, content in repo_files.items():
        sha = _sha1_blob(content)
        blobs_by_sha[sha] = content
        if file_path.endswith(".md"):
            for section in doc_chunker.chunk_markdown(content, file_path=file_path):
                items.append(
                    section.to_transient_embedding_item(
                        repo_full_name=repo_slug,
                        blob_sha=sha,
                    )
                )
        else:
            parsed = registry.parse_file(content, file_path=file_path)
            assert parsed is not None
            for chunk in parsed.chunks:
                items.append(
                    TransientEmbeddingItem.from_code_chunk(
                        chunk,
                        repo_full_name=repo_slug,
                        blob_sha=sha,
                    )
                )

    # Add a cross-tenant sentinel with colliding symbol_name & called_symbols in the same collection
    sentinel_src = b"def rogue_caller():\n    return verify_session('leak')\n"
    sentinel_sha = _sha1_blob(sentinel_src)
    sentinel_parsed = registry.parse_file(sentinel_src, file_path="src/rogue.py")
    assert sentinel_parsed is not None
    items.append(
        TransientEmbeddingItem.from_code_chunk(
            sentinel_parsed.chunks[0],
            repo_full_name=other_repo,
            blob_sha=sentinel_sha,
        )
    )

    settings = Settings(github_token=SecretStr("ghp_integration_test"))
    async with QdrantZeroCodeStore(
        settings=settings,
        collection_name=isolated_qdrant_collection,
    ) as store:
        await store.ensure_collection()
        embed_res = await embedder.embed_items(items)
        await store.upsert_points(embed_res.points)

        # Simulate realistic network RTT during PR retrieval: ~80ms query embed + ~250ms/blob GET
        fake_genai._simulated_latency_seconds = 0.08
        blob_http_calls: list[str] = []

        async def mock_github(request: httpx.Request) -> httpx.Response:
            blob_http_calls.append(request.url.path)
            await asyncio.sleep(0.25)
            sha = request.url.path.rsplit("/", 1)[-1]
            if sha in blobs_by_sha:
                return httpx.Response(200, json=_blob_json(blobs_by_sha[sha], sha))
            return httpx.Response(404)

        retriever = HybridContextRetriever(store=store, embedder=embedder)
        packer = TokenBudgetPacker(budget_tokens=3500)
        auth = GitHubAuthProvider(settings)

        # --- Case 1: Python PR modifying verify_session in src/services/auth_service.py ---
        py_src = repo_files["src/services/auth_service.py"].decode("utf-8")
        py_lines = py_src.splitlines()
        target_line = next(
            idx for idx, line in enumerate(py_lines, start=1) if "if is_token_revoked" in line
        )
        py_diff_text = (
            "diff --git a/src/services/auth_service.py b/src/services/auth_service.py\n"
            "--- a/src/services/auth_service.py\n"
            "+++ b/src/services/auth_service.py\n"
            f"@@ -{target_line - 1},3 +{target_line - 1},4 @@ def verify_session\n"
            f"     claims = decode_jwt_claims(raw_token)\n"
            f"+    if not claims.get('sub'): raise PermissionError('missing sub')\n"
            f"     if is_token_revoked(claims['sub']):\n"
            f"         raise PermissionError('Session token revoked')\n"
        )
        py_parsed_diff = SmartDiffParser().parse_diff(py_diff_text)
        py_file_ctx = registry.resolve_pr_file(
            py_parsed_diff.files[0],
            after_source=repo_files["src/services/auth_service.py"],
        )

        embed_calls_before = fake_genai.embed_calls
        async with httpx.AsyncClient(transport=httpx.MockTransport(mock_github)) as http_client:
            fetcher = GitHubBlobFetcher(auth, http_client=http_client)
            py_assembled = await retriever.retrieve_and_pack(
                [py_file_ctx],
                repo_full_name=repo_slug,
                blob_fetcher=fetcher,
                packer=packer,
                parsed_diff=py_parsed_diff,
                in_memory_sources={
                    "src/services/auth_service.py": repo_files["src/services/auth_service.py"]
                },
            )

        # Single batch query embedding call
        assert fake_genai.embed_calls - embed_calls_before == 1
        # Callers from users_router.py (2 endpoints, 1 blob), Callees from jwt_helper.py, Auth ADR
        caller_names = {c.symbol_name for c in py_assembled.retrieval.callers}
        callee_names = {c.symbol_name for c in py_assembled.retrieval.callees}
        doc_paths = {d.file_path for d in py_assembled.retrieval.architecture_docs}

        assert {"get_current_profile", "delete_current_account"} <= caller_names
        assert "rogue_caller" not in caller_names  # strict tenant isolation
        assert {"decode_jwt_claims", "is_token_revoked"} <= callee_names
        assert "docs/adr/0001-auth-security.md" in doc_paths
        assert "docs/adr/0002-billing-retries.md" not in doc_paths

        # Deduplication: users_router.py (2 callers) + jwt_helper.py (2 callees) + 1 ADR = 3 blobs
        assert py_assembled.blob_report.unique_blobs_fetched <= 4
        assert len(blob_http_calls) == py_assembled.blob_report.unique_blobs_requested

        # AC-1 (< 0.85s) & AC-2 (tokens_saved_pct >= 45%, packed <= 3500)
        assert py_assembled.total_ms < 850.0, f"Exceeded 0.85s SLA: {py_assembled.total_ms}ms"
        assert py_assembled.packed_bundle.metrics.packed_aux_tokens <= 3500
        assert py_assembled.packed_bundle.metrics.tokens_saved_pct >= 45.0

        # --- Case 2: TypeScript PR modifying useAuthSession in web/src/hooks/useAuthSession.ts ---
        blob_http_calls.clear()
        ts_src = repo_files["web/src/hooks/useAuthSession.ts"].decode("utf-8")
        ts_lines = ts_src.splitlines()
        ts_target = next(
            idx
            for idx, line in enumerate(ts_lines, start=1)
            if "fetchSessionClaims" in line and "await" in line
        )
        ts_diff_text = (
            "diff --git a/web/src/hooks/useAuthSession.ts b/web/src/hooks/useAuthSession.ts\n"
            "--- a/web/src/hooks/useAuthSession.ts\n"
            "+++ b/web/src/hooks/useAuthSession.ts\n"
            f"@@ -{ts_target - 1},3 +{ts_target - 1},4 @@ export async function useAuthSession\n"
            " export async function useAuthSession(token: string): Promise<string> {\n"
            "+  if (!token.trim()) { throw new Error('empty'); }\n"
            "   const claims = await fetchSessionClaims(token);\n"
            "   return claims.sub;\n"
        )
        ts_parsed_diff = SmartDiffParser().parse_diff(ts_diff_text)
        ts_file_ctx = registry.resolve_pr_file(
            ts_parsed_diff.files[0],
            after_source=repo_files["web/src/hooks/useAuthSession.ts"],
        )

        async with httpx.AsyncClient(transport=httpx.MockTransport(mock_github)) as http_client:
            fetcher = GitHubBlobFetcher(auth, http_client=http_client)
            ts_assembled = await retriever.retrieve_and_pack(
                [ts_file_ctx],
                repo_full_name=repo_slug,
                blob_fetcher=fetcher,
                packer=packer,
                parsed_diff=ts_parsed_diff,
                in_memory_sources={
                    "web/src/hooks/useAuthSession.ts": repo_files["web/src/hooks/useAuthSession.ts"]
                },
            )

        assert "DashboardPanel" in {c.symbol_name for c in ts_assembled.retrieval.callers}
        assert "fetchSessionClaims" in {c.symbol_name for c in ts_assembled.retrieval.callees}
        assert "docs/architecture/frontend-auth.md" in {
            d.file_path for d in ts_assembled.retrieval.architecture_docs
        }
        assert ts_assembled.total_ms < 850.0, f"Exceeded 0.85s SLA: {ts_assembled.total_ms}ms"
        assert ts_assembled.packed_bundle.metrics.packed_aux_tokens <= 3500
        assert ts_assembled.packed_bundle.metrics.tokens_saved_pct >= 45.0

        # Live NFR-3 Zero-Code Persistence audit on codesight-qdrant
        audit_rc = zerocode_auditor.audit_live_collection(
            settings.qdrant_http_url,
            isolated_qdrant_collection,
            500,
        )
        assert audit_rc == 0
