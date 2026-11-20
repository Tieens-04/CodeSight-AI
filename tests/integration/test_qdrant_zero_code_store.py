"""Live Docker integration tests for QdrantZeroCodeStore on codesight-qdrant (6333/6334)."""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from qdrant_client import models as qdrant_models

from codesight.ast_engine.python_parser import PythonASTParser
from codesight.ast_engine.typescript_parser import TypeScriptASTParser
from codesight.core.config import Settings
from codesight.rag.embedder import GeminiEmbedder
from codesight.rag.models import (
    EMBEDDING_DIMENSIONS,
    REQUIRED_KEYWORD_INDEX_FIELDS,
    QdrantZeroCodePoint,
    TransientEmbeddingItem,
    ZeroCodeViolationError,
    normalize_l2_vector,
)
from codesight.rag.qdrant_store import QdrantZeroCodeStore

PYTHON_FIXTURE = '''"""Authentication helpers."""
import hmac
import hashlib


def verify_hmac(payload: bytes, sig: str, secret: str) -> bool:
    """Verify GitHub webhook signature in constant time."""
    digest = hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={digest}", sig)


def handle_webhook(body: bytes, sig: str, secret: str) -> bool:
    """Validate incoming webhook delivery before dispatching worker."""
    if not verify_hmac(body, sig, secret):
        return False
    return True
'''

TS_FIXTURE = """import { apiClient } from "./client";

export async function dispatchWebhook(payload: string): Promise<boolean> {
  return verify_hmac(payload) && apiClient.post("/events", payload);
}
"""


def _deterministic_vector(text: str, dim: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """Produce a deterministic non-zero 768-dim vector keyed by semantic topic."""
    vec = [0.01] * dim
    lower = text.lower()
    if "zero-code" in lower or "architecture" in lower or "qdrant" in lower:
        vec[0] = 10.0
        vec[1] = 8.0
    else:
        vec[2] = 10.0
        vec[3 + (sum(ord(ch) for ch in text) % 20)] = 4.0
    return vec


class _DeterministicGenAIClient:
    def __init__(self) -> None:
        self.calls: int = 0
        self.aio = SimpleNamespace(models=self)

    async def embed_content(
        self,
        *,
        model: str,
        contents: list[str],
        config: Any = None,
    ) -> Any:
        self.calls += 1
        return SimpleNamespace(
            embeddings=[SimpleNamespace(values=_deterministic_vector(txt)) for txt in contents]
        )


@contextlib.contextmanager
def _pause_coverage_tracer() -> Iterator[None]:
    """Pause coverage.py Python frame tracing while measuring sub-30ms gRPC latency."""
    try:
        import coverage
    except ImportError:
        yield
        return

    active_cov = coverage.Coverage.current()
    if active_cov is None:
        yield
        return

    active_cov.stop()
    try:
        yield
    finally:
        active_cov.start()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_qdrant_zero_code_store_indexes_lookup_under_30ms_and_audits_clean(
    isolated_qdrant_collection: str,
    zerocode_auditor: Any,
) -> None:
    settings = Settings()
    py_parsed = PythonASTParser().parse(PYTHON_FIXTURE, file_path="src/codesight/auth.py")
    ts_parsed = TypeScriptASTParser().parse(TS_FIXTURE, file_path="web/src/webhook.ts")

    repo_a = "Tieens-04/CodeSight-AI"
    repo_a_id = str(uuid.uuid4())
    repo_b = "Tieens-04/Other-Repo"
    repo_b_id = str(uuid.uuid4())

    items: list[TransientEmbeddingItem] = [
        TransientEmbeddingItem.from_code_chunk(
            chunk,
            repo_full_name=repo_a,
            blob_sha="blob-py-001",
            repo_id=repo_a_id,
            transient_snippet=PYTHON_FIXTURE,
            indexed_at="2026-11-25T08:00:00Z",
        )
        for chunk in py_parsed.chunks
    ]
    items.extend(
        TransientEmbeddingItem.from_code_chunk(
            chunk,
            repo_full_name=repo_a,
            blob_sha="blob-ts-001",
            repo_id=repo_a_id,
            transient_snippet=TS_FIXTURE,
            indexed_at="2026-11-25T08:00:00Z",
        )
        for chunk in ts_parsed.chunks
    )
    items.append(
        TransientEmbeddingItem.from_architecture_doc(
            repo_full_name=repo_a,
            blob_sha="blob-doc-001",
            file_path="docs/ADR/001-zero-code.md",
            section_title="Zero-Code Persistence Architecture",
            start_line=1,
            end_line=25,
            transient_snippet=(
                "Zero-Code Persistence forbids storing raw source code inside Qdrant payloads."
            ),
            repo_id=repo_a_id,
            indexed_at="2026-11-25T08:00:00Z",
        )
    )
    # Add a second repo point on the same file_path to prove tenant-scoped deletion
    items.append(
        TransientEmbeddingItem.from_code_chunk(
            py_parsed.chunks[0],
            repo_full_name=repo_b,
            blob_sha="blob-py-001",
            repo_id=repo_b_id,
            indexed_at="2026-11-25T08:00:00Z",
        )
    )

    fake_client = _DeterministicGenAIClient()
    embedder = GeminiEmbedder(client=fake_client)  # type: ignore[arg-type]

    async with QdrantZeroCodeStore(
        settings=settings,
        collection_name=isolated_qdrant_collection,
    ) as store:
        # 0. Cold-start before collection exists returns empty/no-op cleanly over live gRPC 6334
        probe_vec = normalize_l2_vector(_deterministic_vector("cold-start"))
        assert await store.get_existing_point_ids([items[0].point_id]) == set()
        assert await store.find_by_called_symbols(["verify_hmac"], repo_full_name=repo_a) == ()
        assert await store.search_similar(probe_vec, repo_full_name=repo_a) == ()
        await store.delete_by_file_paths(["src/codesight/auth.py"], repo_full_name=repo_a)

        # 1. Idempotent collection + all 7 keyword indexes with verified KEYWORD data_type
        await store.ensure_collection()
        await store.ensure_collection()

        info = await store.client.get_collection(isolated_qdrant_collection)
        payload_schema = info.payload_schema or {}
        assert set(REQUIRED_KEYWORD_INDEX_FIELDS) <= set(payload_schema.keys())
        for field_name in REQUIRED_KEYWORD_INDEX_FIELDS:
            assert payload_schema[field_name].data_type == qdrant_models.PayloadSchemaType.KEYWORD

        # 2. Initial embed + upsert via gRPC
        first_run = await embedder.embed_items(
            items,
            existing_id_checker=store.get_existing_point_ids,
        )
        assert first_run.embedded_count == len(items)
        assert first_run.skipped_existing == 0
        upserted = await store.upsert_points(first_run.points)
        assert upserted == len(items)

        # 3. Re-running embed_items with Qdrant dedup skips 100% of existing chunks (0 API calls)
        second_items = [
            TransientEmbeddingItem.from_code_chunk(
                chunk,
                repo_full_name=repo_a,
                blob_sha="blob-py-001",
                repo_id=repo_a_id,
            )
            for chunk in py_parsed.chunks
        ]
        calls_before = fake_client.calls
        second_run = await embedder.embed_items(
            second_items,
            existing_id_checker=store.get_existing_point_ids,
        )
        assert second_run.skipped_existing == len(py_parsed.chunks)
        assert second_run.embedded_count == 0
        assert fake_client.calls == calls_before

        # 4. All-or-nothing invariant guard blocks mixed [clean, raw_code] batch with 0 count delta
        count_before = (await store.client.count(isolated_qdrant_collection, exact=True)).count
        clean_extra = QdrantZeroCodePoint(
            point_id=str(uuid.uuid4()),
            vector=normalize_l2_vector(_deterministic_vector("clean-extra")),
            payload={
                "repo_id": repo_a_id,
                "repo_full_name": repo_a,
                "file_path": "src/clean.py",
                "blob_sha": "sha-clean",
                "chunk_type": "code",
                "language": "python",
                "symbol_name": "clean_helper",
                "symbol_kind": "function",
                "start_line": 1,
                "end_line": 5,
                "signature": "def clean_helper() -> None",
                "called_symbols": [],
                "imports": [],
                "cyclomatic_complexity": 1,
            },
        )
        leaky_point = QdrantZeroCodePoint(
            point_id=str(uuid.uuid4()),
            vector=normalize_l2_vector(_deterministic_vector("leak")),
            payload={
                "repo_id": repo_a_id,
                "repo_full_name": repo_a,
                "file_path": "src/leak.py",
                "blob_sha": "sha-leak",
                "chunk_type": "code",
                "language": "python",
                "symbol_name": "leak",
                "symbol_kind": "function",
                "start_line": 1,
                "end_line": 2,
                "signature": "def leak() -> None",
                "called_symbols": [],
                "imports": [],
                "cyclomatic_complexity": 1,
                "raw_code": "def leak(): return 'secret'",
            },
        )
        with pytest.raises(ZeroCodeViolationError):
            await store.upsert_points([clean_extra, leaky_point], batch_size=1)
        count_after = (await store.client.count(isolated_qdrant_collection, exact=True)).count
        assert count_after == count_before == len(items)

        # 5. Warm-up + latency measurement (< 30ms) for indexed called_symbols lookup
        _ = await store.find_by_called_symbols(["verify_hmac"], repo_full_name=repo_a)
        with _pause_coverage_tracer():
            t0 = time.perf_counter()
            callers = await store.find_by_called_symbols(["verify_hmac"], repo_full_name=repo_a)
            elapsed_ms = (time.perf_counter() - t0) * 1000.0

        caller_names = {pt.payload["symbol_name"] for pt in callers}
        assert caller_names == {"handle_webhook", "dispatchWebhook"}
        assert elapsed_ms < 30.0, f"called_symbols lookup took {elapsed_ms:.2f}ms (>= 30ms)"

        # 6. Exact symbol_name lookup (Callees)
        callees = await store.find_by_symbol_names(["verify_hmac"], repo_full_name=repo_a)
        assert len(callees) == 1
        assert callees[0].payload["file_path"] == "src/codesight/auth.py"

        # 7. Dense similarity search for architecture_doc (CA-15 readiness)
        query_vec = await embedder.embed_query("Zero-Code Persistence architecture in Qdrant")
        doc_hits = await store.search_similar(
            query_vec,
            repo_full_name=repo_a,
            chunk_type="architecture_doc",
            score_threshold=0.65,
            limit=5,
        )
        assert len(doc_hits) == 1
        assert doc_hits[0].payload["file_path"] == "docs/ADR/001-zero-code.md"
        assert doc_hits[0].score is not None and doc_hits[0].score >= 0.65

        # 8. Live NFR-3 disk audit using zerocode-ast-rag mechanical script
        audit_rc = zerocode_auditor.audit_live_collection(
            settings.qdrant_http_url,
            isolated_qdrant_collection,
            200,
        )
        assert audit_rc == 0

        # 9. Tenant-scoped delete_by_file_paths removes only repo_a's file, keeps repo_b intact
        await store.delete_by_file_paths(
            ["src/codesight/auth.py"],
            repo_full_name=repo_a,
        )
        remaining_a = await store.find_by_symbol_names(["verify_hmac"], repo_full_name=repo_a)
        remaining_b = await store.find_by_symbol_names(["verify_hmac"], repo_full_name=repo_b)
        assert remaining_a == ()
        assert len(remaining_b) == 1

        # 10. Tenant-scoped delete_by_repo wipes repo_b cleanly
        await store.delete_by_repo(repo_id=repo_b_id)
        assert await store.find_by_symbol_names(["verify_hmac"], repo_id=repo_b_id) == ()
