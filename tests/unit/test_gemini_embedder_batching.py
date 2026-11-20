"""Unit tests for GeminiEmbedder batching, retries, deduplication, and NFR-3 guards."""

from __future__ import annotations

import asyncio
import math
import random
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from types import SimpleNamespace
from typing import Any

import httpx
import httpx2
import pytest
from google.genai import errors, types
from pydantic import SecretStr
from qdrant_client import models as qdrant_models
from qdrant_client.http.exceptions import UnexpectedResponse

from codesight.ast_engine.models import CodeSymbolChunk
from codesight.ast_engine.python_parser import PythonASTParser
from codesight.ast_engine.typescript_parser import TypeScriptASTParser
from codesight.core.config import Settings
from codesight.core.errors import RateLimitTaskError, RetryableTaskError
from codesight.rag.embedder import GeminiEmbedder
from codesight.rag.models import (
    ALLOWED_PAYLOAD_KEYS,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL_NAME,
    FORBIDDEN_PAYLOAD_KEYS,
    MAX_TRANSIENT_SNIPPET_CHARS,
    EmbeddingConfigError,
    QdrantSchemaMismatchError,
    QdrantZeroCodePoint,
    TransientEmbeddingItem,
    ZeroCodeViolationError,
    build_deterministic_point_id,
    normalize_l2_vector,
    validate_zero_code_payload,
    validate_zero_code_point,
)
from codesight.rag.qdrant_store import QdrantZeroCodeStore


def _make_chunk(
    idx: int,
    *,
    file_path: str = "src/codesight/auth.py",
    docstring: str | None = "Verify request HMAC digest safely.",
    called_symbols: tuple[str, ...] = ("compare_digest", "new"),
) -> CodeSymbolChunk:
    start = idx * 10 + 1
    return CodeSymbolChunk(
        file_path=file_path,
        language="python",
        symbol_name=f"verify_token_{idx}",
        qualified_name=f"AuthService.verify_token_{idx}",
        symbol_kind="method",
        start_line=start,
        end_line=start + 8,
        signature=f"def verify_token_{idx}(self, token: str) -> bool",
        docstring=docstring,
        decorators=("@staticmethod",),
        imports=("hmac", "hashlib"),
        called_symbols=called_symbols,
        cyclomatic_complexity=2,
    )


def _make_unit_vector(seed: int, dim: int = EMBEDDING_DIMENSIONS) -> list[float]:
    raw = [0.0] * dim
    raw[seed % dim] = 3.0
    raw[(seed + 1) % dim] = 4.0
    return raw


class _FakeAsyncModels:
    def __init__(
        self,
        handler: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._handler = handler

    async def embed_content(
        self,
        *,
        model: str,
        contents: list[str],
        config: types.EmbedContentConfig | None = None,
    ) -> Any:
        call_info = {"model": model, "contents": list(contents), "config": config}
        self.calls.append(call_info)
        if self._handler is not None:
            return await self._handler(model=model, contents=contents, config=config)
        embeddings = [
            SimpleNamespace(values=_make_unit_vector(i + len(self.calls) * 100))
            for i in range(len(contents))
        ]
        return SimpleNamespace(embeddings=embeddings)


def _make_fake_genai_client(
    handler: Callable[..., Awaitable[Any]] | None = None,
) -> tuple[Any, _FakeAsyncModels]:
    models_stub = _FakeAsyncModels(handler=handler)
    client = SimpleNamespace(aio=SimpleNamespace(models=models_stub))
    return client, models_stub


class TestTransientEmbeddingItem:
    def test_from_code_chunk_matches_point_id_truncates_snippet_and_audits_clean(
        self,
        zerocode_auditor: Any,
    ) -> None:
        chunk = _make_chunk(1)
        oversized_snippet = "x" * (MAX_TRANSIENT_SNIPPET_CHARS + 500)
        item = TransientEmbeddingItem.from_code_chunk(
            chunk,
            repo_full_name="Tieens-04/CodeSight-AI",
            blob_sha="deadbeef1234",
            repo_id="repo-uuid-1",
            transient_snippet=oversized_snippet,
            indexed_at="2026-11-25T00:00:00Z",
        )

        expected_id = chunk.build_point_id("Tieens-04/CodeSight-AI", "deadbeef1234")
        assert item.point_id == expected_id
        assert item.chunk_type == "code"
        assert set(item.payload.keys()) <= ALLOWED_PAYLOAD_KEYS
        assert set(item.payload.keys()).isdisjoint(FORBIDDEN_PAYLOAD_KEYS)
        assert "docstring" not in item.payload
        assert (
            zerocode_auditor.audit_payloads([{"id": item.point_id, "payload": item.payload}]) == []
        )

        text = item.peek_embedding_text()
        assert text is not None
        assert "src/codesight/auth.py" in text
        assert "AuthService.verify_token_1" in text
        assert "Verify request HMAC digest safely." in text
        assert "compare_digest" in text
        # Transient snippet was included up to the strict cap (not full 2500 chars)
        assert ("x" * MAX_TRANSIENT_SNIPPET_CHARS) in text
        assert ("x" * (MAX_TRANSIENT_SNIPPET_CHARS + 1)) not in text

        item.clear_transient_text()
        assert item.peek_embedding_text() is None

    def test_from_architecture_doc_builds_zero_code_payload_and_ram_text(
        self,
        zerocode_auditor: Any,
    ) -> None:
        item = TransientEmbeddingItem.from_architecture_doc(
            repo_full_name="Tieens-04/CodeSight-AI",
            blob_sha="abc9876",
            file_path="docs/ADR/001-zero-code.md",
            section_title="Zero-Code Persistence Invariant",
            start_line=10,
            end_line=42,
            transient_snippet="Never persist customer raw source code in Qdrant payloads.",
            repo_id="repo-uuid-1",
            indexed_at="2026-11-25T00:00:00Z",
        )

        expected_id = build_deterministic_point_id(
            "Tieens-04/CodeSight-AI",
            "docs/ADR/001-zero-code.md",
            "abc9876",
            "Zero-Code Persistence Invariant",
            10,
        )
        assert item.point_id == expected_id
        assert item.payload["chunk_type"] == "architecture_doc"
        assert item.payload["language"] == "markdown"
        assert item.payload["symbol_kind"] == "section"
        assert item.payload["called_symbols"] == []
        assert item.payload["imports"] == []
        assert item.payload["cyclomatic_complexity"] == 0
        assert (
            zerocode_auditor.audit_payloads([{"id": item.point_id, "payload": item.payload}]) == []
        )

        text = item.peek_embedding_text()
        assert text is not None
        assert "Never persist customer raw source code" in text
        assert "Never persist customer raw source code" not in str(item.payload)


class TestGeminiEmbedderBatchingAndDeduplication:
    @pytest.mark.asyncio
    async def test_batches_150_items_into_64_64_22_normalizes_and_clears_ram(self) -> None:
        client, models_stub = _make_fake_genai_client()
        embedder = GeminiEmbedder(client=client)

        items = [
            TransientEmbeddingItem.from_code_chunk(
                _make_chunk(i),
                repo_full_name="org/repo",
                blob_sha="sha-batch-1",
                transient_snippet=f"secret_body_{i}()",
            )
            for i in range(150)
        ]

        result = await embedder.embed_items(items)

        assert result.total_items == 150
        assert result.deduped_in_batch == 0
        assert result.skipped_existing == 0
        assert result.embedded_count == 150
        assert result.batch_calls == 3
        assert len(result.points) == 150

        batch_sizes = sorted(len(call["contents"]) for call in models_stub.calls)
        assert batch_sizes == [22, 64, 64]
        for call in models_stub.calls:
            assert call["model"] == EMBEDDING_MODEL_NAME
            cfg = call["config"]
            assert isinstance(cfg, types.EmbedContentConfig)
            assert cfg.task_type == "RETRIEVAL_DOCUMENT"
            assert cfg.output_dimensionality == EMBEDDING_DIMENSIONS

        # Order preserved and vectors L2-normalized to unit length (768 dims)
        assert [pt.point_id for pt in result.points] == [it.point_id for it in items]
        for pt in result.points:
            assert len(pt.vector) == EMBEDDING_DIMENSIONS
            norm = math.sqrt(sum(v * v for v in pt.vector))
            assert math.isclose(norm, 1.0, rel_tol=1e-6)
            assert "secret_body_" not in str(pt.payload)

        # Transient RAM text cleared on every item
        assert all(item.peek_embedding_text() is None for item in items)

    @pytest.mark.asyncio
    async def test_semaphore_caps_concurrency_at_3_and_preserves_order(self) -> None:
        in_flight = 0
        max_in_flight = 0

        async def _slow_embed(
            *,
            model: str,
            contents: list[str],
            config: types.EmbedContentConfig | None = None,
        ) -> Any:
            nonlocal in_flight, max_in_flight
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            try:
                await asyncio.sleep(0.015)
                # Encode first line of each text into vector slot so order is verifiable
                embeddings = []
                for idx, text in enumerate(contents):
                    marker = sum(ord(ch) for ch in text) % EMBEDDING_DIMENSIONS
                    embeddings.append(SimpleNamespace(values=_make_unit_vector(marker + idx)))
                return SimpleNamespace(embeddings=embeddings)
            finally:
                in_flight -= 1

        client, _ = _make_fake_genai_client(handler=_slow_embed)
        embedder = GeminiEmbedder(client=client, batch_size=64, max_concurrency=3)

        items = [
            TransientEmbeddingItem.from_code_chunk(
                _make_chunk(i),
                repo_full_name="org/repo",
                blob_sha="sha-conc",
            )
            for i in range(300)
        ]
        result = await embedder.embed_items(items)

        assert result.batch_calls == 5
        assert result.embedded_count == 300
        assert max_in_flight <= 3
        assert max_in_flight >= 2
        assert [pt.point_id for pt in result.points] == [it.point_id for it in items]

    @pytest.mark.asyncio
    async def test_deduplicates_within_batch_and_skips_existing_qdrant_point_ids(self) -> None:
        client, models_stub = _make_fake_genai_client()
        embedder = GeminiEmbedder(client=client)

        base_items = [
            TransientEmbeddingItem.from_code_chunk(
                _make_chunk(i),
                repo_full_name="org/repo",
                blob_sha="sha-dedup",
                transient_snippet=f"body_{i}",
            )
            for i in range(10)
        ]
        duplicate_of_0 = TransientEmbeddingItem.from_code_chunk(
            _make_chunk(0),
            repo_full_name="org/repo",
            blob_sha="sha-dedup",
            transient_snippet="body_0_dup",
        )
        all_items = [*base_items, duplicate_of_0]
        already_in_qdrant = {base_items[i].point_id for i in range(4)}

        checked_batches: list[list[str]] = []

        async def _checker(ids: Sequence[str]) -> set[str]:
            checked_batches.append(list(ids))
            return {pid for pid in ids if pid in already_in_qdrant}

        result = await embedder.embed_items(all_items, existing_id_checker=_checker)

        assert result.total_items == 11
        assert result.deduped_in_batch == 1
        assert result.skipped_existing == 4
        assert result.embedded_count == 6
        assert result.batch_calls == 1
        assert len(models_stub.calls) == 1
        assert len(models_stub.calls[0]["contents"]) == 6
        assert [pt.point_id for pt in result.points] == [
            base_items[i].point_id for i in range(4, 10)
        ]
        assert checked_batches == [[it.point_id for it in base_items]]
        assert all(it.peek_embedding_text() is None for it in all_items)

    @pytest.mark.asyncio
    async def test_skips_api_call_entirely_when_all_items_already_exist(self) -> None:
        client, models_stub = _make_fake_genai_client()
        embedder = GeminiEmbedder(client=client)

        items = [
            TransientEmbeddingItem.from_code_chunk(
                _make_chunk(i),
                repo_full_name="org/repo",
                blob_sha="sha-cached",
            )
            for i in range(3)
        ]

        async def _all_exist(ids: Sequence[str]) -> set[str]:
            return set(ids)

        result = await embedder.embed_items(items, existing_id_checker=_all_exist)
        assert result.points == ()
        assert result.skipped_existing == 3
        assert result.embedded_count == 0
        assert result.batch_calls == 0
        assert models_stub.calls == []
        assert all(it.peek_embedding_text() is None for it in items)

    @pytest.mark.asyncio
    async def test_embed_code_chunks_convenience_helper(self) -> None:
        client, _ = _make_fake_genai_client()
        embedder = GeminiEmbedder(client=client)
        chunks = [_make_chunk(0), _make_chunk(1)]

        result = await embedder.embed_code_chunks(
            chunks,
            repo_full_name="org/repo",
            blob_sha="sha-helper",
            repo_id="repo-1",
        )
        assert result.embedded_count == 2
        assert result.points[0].payload["repo_id"] == "repo-1"

    @pytest.mark.asyncio
    async def test_embed_query_uses_retrieval_query_task_type_and_768_dim(self) -> None:
        client, models_stub = _make_fake_genai_client()
        embedder = GeminiEmbedder(client=client)

        vector = await embedder.embed_query("zero-code persistence architecture invariant")

        assert len(vector) == EMBEDDING_DIMENSIONS
        assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, rel_tol=1e-6)
        assert len(models_stub.calls) == 1
        cfg = models_stub.calls[0]["config"]
        assert cfg.task_type == "RETRIEVAL_QUERY"
        assert cfg.output_dimensionality == EMBEDDING_DIMENSIONS

        with pytest.raises(ValueError, match="query_text"):
            await embedder.embed_query("   ")


class TestGeminiEmbedderRetriesAndErrors:
    @pytest.mark.asyncio
    async def test_retries_429_honoring_retry_after_and_succeeds(self) -> None:
        attempts = 0
        sleeps: list[float] = []

        async def _flaky_429(
            *,
            model: str,
            contents: list[str],
            config: types.EmbedContentConfig | None = None,
        ) -> Any:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                resp = SimpleNamespace(headers={"Retry-After": "4"})
                raise errors.ClientError(429, {"error": {"message": "Quota exceeded"}}, resp)
            return SimpleNamespace(
                embeddings=[
                    SimpleNamespace(values=_make_unit_vector(i)) for i in range(len(contents))
                ]
            )

        async def _fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        client, _ = _make_fake_genai_client(handler=_flaky_429)
        embedder = GeminiEmbedder(client=client, sleep_fn=_fake_sleep, max_retries=3)
        items = [
            TransientEmbeddingItem.from_code_chunk(
                _make_chunk(0),
                repo_full_name="org/repo",
                blob_sha="sha-429",
            )
        ]

        result = await embedder.embed_items(items)
        assert result.embedded_count == 1
        assert attempts == 2
        assert sleeps == [4.0]

    @pytest.mark.asyncio
    async def test_exhausted_429_raises_rate_limit_task_error_and_clears_ram(self) -> None:
        sleeps: list[float] = []

        async def _always_429(**_: Any) -> Any:
            resp = SimpleNamespace(headers={"Retry-After": "7"})
            raise errors.ClientError(429, {"error": {"message": "RESOURCE_EXHAUSTED"}}, resp)

        async def _fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        client, _ = _make_fake_genai_client(handler=_always_429)
        embedder = GeminiEmbedder(client=client, sleep_fn=_fake_sleep, max_retries=2)
        item = TransientEmbeddingItem.from_code_chunk(
            _make_chunk(0),
            repo_full_name="org/repo",
            blob_sha="sha-429-fail",
            transient_snippet="sensitive_secret_logic()",
        )

        with pytest.raises(RateLimitTaskError) as exc_info:
            await embedder.embed_items([item])

        assert exc_info.value.retry_after_seconds == 7.0
        assert len(sleeps) == 2
        assert item.peek_embedding_text() is None

    @pytest.mark.asyncio
    async def test_retries_503_and_transport_error_then_raises_retryable_task_error(self) -> None:
        sleeps: list[float] = []
        calls = 0

        async def _flaky_server(**_: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise errors.ServerError(503, {"error": {"message": "Service unavailable"}})
            raise httpx.ConnectTimeout("timed out")

        async def _fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        client, _ = _make_fake_genai_client(handler=_flaky_server)
        embedder = GeminiEmbedder(
            client=client,
            sleep_fn=_fake_sleep,
            max_retries=2,
            retry_base_seconds=1.0,
            retry_cap_seconds=5.0,
        )
        item = TransientEmbeddingItem.from_code_chunk(
            _make_chunk(0),
            repo_full_name="org/repo",
            blob_sha="sha-503",
        )

        with pytest.raises(RetryableTaskError):
            await embedder.embed_items([item])

        assert calls == 3
        assert len(sleeps) == 2
        assert all(0.0 <= d <= 5.0 for d in sleeps)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("status_code", [400, 401, 403, 404])
    async def test_fail_fast_on_non_retriable_4xx(self, status_code: int) -> None:
        calls = 0
        sleeps: list[float] = []

        async def _bad_client(**_: Any) -> Any:
            nonlocal calls
            calls += 1
            raise errors.ClientError(status_code, {"error": {"message": "Invalid API key"}})

        async def _fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        client, _ = _make_fake_genai_client(handler=_bad_client)
        embedder = GeminiEmbedder(client=client, sleep_fn=_fake_sleep, max_retries=4)
        item = TransientEmbeddingItem.from_code_chunk(
            _make_chunk(0),
            repo_full_name="org/repo",
            blob_sha="sha-4xx",
        )

        with pytest.raises(EmbeddingConfigError):
            await embedder.embed_items([item])

        assert calls == 1
        assert sleeps == []

    def test_missing_api_key_raises_embedding_config_error(self) -> None:
        settings = Settings(gemini_api_key=None)
        with pytest.raises(EmbeddingConfigError, match="GEMINI_API_KEY"):
            GeminiEmbedder(settings=settings)

        blank_settings = Settings(gemini_api_key=SecretStr("   "))
        with pytest.raises(EmbeddingConfigError, match="GEMINI_API_KEY"):
            GeminiEmbedder(settings=blank_settings)

    @pytest.mark.asyncio
    async def test_wrong_dimension_or_count_from_api_raises_error(self) -> None:
        async def _wrong_dim(**_: Any) -> Any:
            return SimpleNamespace(embeddings=[SimpleNamespace(values=[0.1] * 3072)])

        client, _ = _make_fake_genai_client(handler=_wrong_dim)
        embedder = GeminiEmbedder(client=client)
        item = TransientEmbeddingItem.from_code_chunk(
            _make_chunk(0),
            repo_full_name="org/repo",
            blob_sha="sha-dim",
        )

        with pytest.raises(ValueError, match="768"):
            await embedder.embed_items([item])

    @pytest.mark.asyncio
    async def test_embedder_constructor_and_empty_inputs_and_408_retry(self) -> None:
        client, models_stub = _make_fake_genai_client()
        embedder = GeminiEmbedder(client=client)

        empty_res = await embedder.embed_items([])
        assert empty_res.points == ()
        assert empty_res.total_items == 0
        assert empty_res.batch_calls == 0
        assert models_stub.calls == []

        with pytest.raises(ValueError, match="output_dimensionality"):
            GeminiEmbedder(client=client, output_dimensionality=3072)
        with pytest.raises(ValueError, match="batch_size"):
            GeminiEmbedder(client=client, batch_size=0)
        with pytest.raises(ValueError, match="batch_size"):
            GeminiEmbedder(client=client, batch_size=101)
        with pytest.raises(ValueError, match="max_retries"):
            GeminiEmbedder(client=client, max_retries=-1)
        with pytest.raises(ValueError, match="retry_base_seconds"):
            GeminiEmbedder(client=client, retry_base_seconds=5.0, retry_cap_seconds=1.0)

        # Raw API key initializes SDK client cleanly without network call
        key_embedder = GeminiEmbedder(api_key="test-fake-key-for-init-only")
        assert key_embedder is not None

        # HTTP 408 timeout retries and surfaces RetryableTaskError (not RateLimitTaskError)
        sleeps: list[float] = []

        async def _always_408(**_: Any) -> Any:
            raise errors.ClientError(408, {"error": {"message": "Request Timeout"}})

        async def _fake_sleep(delay: float) -> None:
            sleeps.append(delay)

        c408, _ = _make_fake_genai_client(handler=_always_408)
        e408 = GeminiEmbedder(client=c408, sleep_fn=_fake_sleep, max_retries=1)
        with pytest.raises(RetryableTaskError) as exc_info:
            await e408.embed_query("check timeout mapping")
        assert not isinstance(exc_info.value, RateLimitTaskError)
        assert len(sleeps) == 1

        # Count mismatch & missing values raise ValueError
        async def _short_response(**_: Any) -> Any:
            return SimpleNamespace(embeddings=[])

        c_short, _ = _make_fake_genai_client(handler=_short_response)
        with pytest.raises(ValueError, match="returned 0 embeddings"):
            await GeminiEmbedder(client=c_short).embed_query("short")

        async def _missing_values(**_: Any) -> Any:
            return SimpleNamespace(embeddings=[SimpleNamespace(values=None)])

        c_missing, _ = _make_fake_genai_client(handler=_missing_values)
        with pytest.raises(ValueError, match="missing numeric values"):
            await GeminiEmbedder(client=c_missing).embed_query("missing")


class TestZeroCodeInvariantGuardAndStoreUnit:
    def _valid_payload(self) -> dict[str, Any]:
        return {
            "repo_id": "repo-1",
            "repo_full_name": "Tieens-04/CodeSight-AI",
            "file_path": "src/codesight/auth.py",
            "blob_sha": "abc1234",
            "chunk_type": "code",
            "language": "python",
            "symbol_name": "verify_hmac",
            "symbol_kind": "function",
            "start_line": 12,
            "end_line": 28,
            "signature": "def verify_hmac(payload: bytes, sig: str, secret: str) -> bool",
            "called_symbols": ["compare_digest"],
            "imports": ["hmac"],
            "cyclomatic_complexity": 3,
        }

    @pytest.mark.parametrize("forbidden_key", sorted(FORBIDDEN_PAYLOAD_KEYS))
    def test_rejects_every_forbidden_key_and_case_variants(self, forbidden_key: str) -> None:
        payload = self._valid_payload()
        payload[forbidden_key] = "def secret(): return 42"
        with pytest.raises(ZeroCodeViolationError, match="NFR-3"):
            validate_zero_code_payload(payload)

        upper_payload = self._valid_payload()
        upper_payload[forbidden_key.upper()] = "def secret(): return 42"
        with pytest.raises(ZeroCodeViolationError, match="NFR-3"):
            validate_zero_code_payload(upper_payload)

    def test_rejects_nested_forbidden_key_and_unknown_top_level_key(self) -> None:
        nested = self._valid_payload()
        nested["called_symbols"] = [{"raw_code": "return secret"}]
        with pytest.raises(ZeroCodeViolationError, match="NFR-3"):
            validate_zero_code_payload(nested)

        unknown = self._valid_payload()
        unknown["extra_unapproved_field"] = "hello"
        with pytest.raises(ZeroCodeViolationError, match="unapproved"):
            validate_zero_code_payload(unknown)

    def test_rejects_leaky_or_oversized_signature(self) -> None:
        oversized = self._valid_payload()
        oversized["signature"] = "def f(" + "a" * 305 + ")"
        with pytest.raises(ZeroCodeViolationError, match="300"):
            validate_zero_code_payload(oversized)

        multiline = self._valid_payload()
        multiline["signature"] = "def f():\n    return secret_key"
        with pytest.raises(ZeroCodeViolationError, match="signature"):
            validate_zero_code_payload(multiline)

    def test_rejects_non_uuid_and_bad_vectors(self) -> None:
        valid_vec = normalize_l2_vector(_make_unit_vector(0))
        bad_id_pt = QdrantZeroCodePoint(
            point_id="not-a-uuid",
            vector=valid_vec,
            payload=self._valid_payload(),
        )
        with pytest.raises(ZeroCodeViolationError, match="UUID"):
            validate_zero_code_point(bad_id_pt)

        bad_dim_pt = QdrantZeroCodePoint(
            point_id=str(uuid.uuid4()),
            vector=(0.1, 0.2),
            payload=self._valid_payload(),
        )
        with pytest.raises(ZeroCodeViolationError, match="768"):
            validate_zero_code_point(bad_dim_pt)

        nan_vec = (float("nan"), *valid_vec[1:])
        nan_pt = QdrantZeroCodePoint(
            point_id=str(uuid.uuid4()),
            vector=nan_vec,
            payload=self._valid_payload(),
        )
        with pytest.raises(ZeroCodeViolationError, match="finite"):
            validate_zero_code_point(nan_pt)

    @pytest.mark.asyncio
    async def test_store_upsert_preflight_blocks_all_batches_before_network_call(self) -> None:
        upsert_called = False

        class _StubQdrantClient:
            async def upsert(self, **_: Any) -> Any:
                nonlocal upsert_called
                upsert_called = True

        store = QdrantZeroCodeStore(client=_StubQdrantClient(), collection_name="test_col")  # type: ignore[arg-type]
        valid_vec = normalize_l2_vector(_make_unit_vector(1))
        good_point = QdrantZeroCodePoint(
            point_id=str(uuid.uuid4()),
            vector=valid_vec,
            payload=self._valid_payload(),
        )
        leaky_payload = self._valid_payload()
        leaky_payload["raw_code"] = "print('leak')"
        bad_point = QdrantZeroCodePoint(
            point_id=str(uuid.uuid4()),
            vector=valid_vec,
            payload=leaky_payload,
        )

        with pytest.raises(ZeroCodeViolationError):
            await store.upsert_points([good_point, bad_point], batch_size=1)

        assert upsert_called is False

    @pytest.mark.asyncio
    async def test_store_requires_repo_scope_for_delete_and_detects_schema_mismatch(self) -> None:
        class _MismatchedQdrantClient:
            async def collection_exists(self, collection_name: str) -> bool:
                return True

            async def get_collection(self, collection_name: str) -> Any:
                return SimpleNamespace(
                    config=SimpleNamespace(
                        params=SimpleNamespace(
                            vectors=SimpleNamespace(size=3072, distance="Cosine")
                        )
                    )
                )

        store = QdrantZeroCodeStore(
            client=_MismatchedQdrantClient(),  # type: ignore[arg-type]
            collection_name="bad_schema",
        )
        with pytest.raises(QdrantSchemaMismatchError, match="768"):
            await store.ensure_collection()

        with pytest.raises(ValueError, match=r"repo_full_name|repo_id"):
            await store.delete_by_file_paths(["src/a.py"])

        with pytest.raises(ValueError, match=r"repo_full_name|repo_id"):
            await store.delete_by_repo()

    @pytest.mark.asyncio
    async def test_models_and_store_additional_validation_edge_cases(self) -> None:
        with pytest.raises(ValueError, match="start_line"):
            build_deterministic_point_id("org/repo", "a.py", "sha", "fn", 0)

        with pytest.raises(ZeroCodeViolationError, match="positive L2 norm"):
            normalize_l2_vector([0.0] * EMBEDDING_DIMENSIONS)

        with pytest.raises(ZeroCodeViolationError, match="finite numbers"):
            normalize_l2_vector([True] + [0.0] * (EMBEDDING_DIMENSIONS - 1))

        with pytest.raises(ValueError, match="section_title"):
            TransientEmbeddingItem.from_architecture_doc(
                repo_full_name="org/repo",
                blob_sha="sha",
                file_path="README.md",
                section_title="   ",
                start_line=1,
                end_line=5,
                transient_snippet="docs",
            )

        item = TransientEmbeddingItem.from_code_chunk(
            _make_chunk(0),
            repo_full_name="org/repo",
            blob_sha="sha-once",
        )
        assert "verify_token_0" in item.consume_embedding_text()
        with pytest.raises(ValueError, match="already cleared"):
            item.consume_embedding_text()

        # Payload validation branches: missing required key, bad chunk_type/language/lines/list
        missing = self._valid_payload()
        del missing["blob_sha"]
        with pytest.raises(ZeroCodeViolationError, match="missing required"):
            validate_zero_code_payload(missing)

        bad_type = self._valid_payload()
        bad_type["chunk_type"] = "raw_dump"
        with pytest.raises(ZeroCodeViolationError, match="chunk_type"):
            validate_zero_code_payload(bad_type)

        bad_lang = self._valid_payload()
        bad_lang["language"] = "rust"
        with pytest.raises(ZeroCodeViolationError, match="language"):
            validate_zero_code_payload(bad_lang)

        bad_lines = self._valid_payload()
        bad_lines["start_line"] = 20
        bad_lines["end_line"] = 10
        with pytest.raises(ZeroCodeViolationError, match="start_line"):
            validate_zero_code_payload(bad_lines)

        bad_cc = self._valid_payload()
        bad_cc["cyclomatic_complexity"] = -1
        with pytest.raises(ZeroCodeViolationError, match="cyclomatic_complexity"):
            validate_zero_code_payload(bad_cc)

        bad_list = self._valid_payload()
        bad_list["called_symbols"] = "not-a-list"
        with pytest.raises(ZeroCodeViolationError, match="list"):
            validate_zero_code_payload(bad_list)

        # Store race-safe ensure_collection + empty no-op guards
        exists_calls = 0

        class _RaceQdrantClient:
            def __init__(self) -> None:
                self.deleted = False

            async def collection_exists(self, collection_name: str) -> bool:
                nonlocal exists_calls
                exists_calls += 1
                return exists_calls >= 2

            async def create_collection(self, **_: Any) -> None:
                raise RuntimeError("already created concurrently")

            async def get_collection(self, collection_name: str) -> Any:
                return SimpleNamespace(
                    config=SimpleNamespace(
                        params=SimpleNamespace(
                            vectors=SimpleNamespace(size=EMBEDDING_DIMENSIONS, distance="Cosine")
                        )
                    ),
                    payload_schema={
                        "repo_id": SimpleNamespace(
                            data_type=qdrant_models.PayloadSchemaType.KEYWORD
                        ),
                        "repo_full_name": SimpleNamespace(
                            data_type=qdrant_models.PayloadSchemaType.KEYWORD
                        ),
                        "file_path": SimpleNamespace(
                            data_type=qdrant_models.PayloadSchemaType.KEYWORD
                        ),
                        "symbol_name": SimpleNamespace(
                            data_type=qdrant_models.PayloadSchemaType.KEYWORD
                        ),
                        "called_symbols": SimpleNamespace(
                            data_type=qdrant_models.PayloadSchemaType.KEYWORD
                        ),
                        "blob_sha": SimpleNamespace(
                            data_type=qdrant_models.PayloadSchemaType.KEYWORD
                        ),
                        "chunk_type": SimpleNamespace(
                            data_type=qdrant_models.PayloadSchemaType.KEYWORD
                        ),
                    },
                )

            async def delete(self, **_: Any) -> None:
                self.deleted = True

        race_client = _RaceQdrantClient()
        race_store = QdrantZeroCodeStore(client=race_client, collection_name="race_col")  # type: ignore[arg-type]
        await race_store.ensure_collection()
        assert await race_store.upsert_points([]) == 0
        assert await race_store.get_existing_point_ids([]) == set()
        assert await race_store.find_by_called_symbols([], repo_full_name="org/repo") == ()
        await race_store.delete_by_file_paths(["   "], repo_full_name="org/repo")
        assert race_client.deleted is False


class TestReviewFixRegressions:
    """TDD regression suite pinning all verified findings [F-01]..[F-09] from review-01.md."""

    def _valid_payload(self) -> dict[str, Any]:
        return {
            "repo_id": "repo-1",
            "repo_full_name": "Tieens-04/CodeSight-AI",
            "file_path": "src/codesight/auth.py",
            "blob_sha": "abc1234",
            "chunk_type": "code",
            "language": "python",
            "symbol_name": "verify_hmac",
            "symbol_kind": "function",
            "start_line": 12,
            "end_line": 28,
            "signature": "def verify_hmac(payload: bytes, sig: str, secret: str) -> bool",
            "called_symbols": ["compare_digest"],
            "imports": ["hmac"],
            "cyclomatic_complexity": 3,
        }

    @pytest.mark.asyncio
    async def test_f01_multi_batch_failure_cancels_sibling_tasks_and_clears_ram(self) -> None:
        batch1_started = asyncio.Event()
        batch1_cancelled = False
        invocations = 0

        async def _handler(
            *,
            model: str,
            contents: list[str],
            config: types.EmbedContentConfig | None = None,
        ) -> Any:
            nonlocal invocations, batch1_cancelled
            call_num = invocations
            invocations += 1
            if call_num == 0:
                await batch1_started.wait()
                raise errors.ClientError(401, {"error": {"message": "Unauthorized"}})
            if call_num == 1:
                batch1_started.set()
                try:
                    await asyncio.sleep(5.0)
                except asyncio.CancelledError:
                    batch1_cancelled = True
                    raise
            raise AssertionError("Queued batch 2 must never execute after batch 0 fails fast")

        client, _ = _make_fake_genai_client(handler=_handler)
        embedder = GeminiEmbedder(client=client, batch_size=2, max_concurrency=2)
        items = [
            TransientEmbeddingItem.from_code_chunk(
                _make_chunk(i),
                repo_full_name="org/repo",
                blob_sha="sha-f01",
                transient_snippet=f"secret_fn_body_{i}()",
            )
            for i in range(6)
        ]

        with pytest.raises(EmbeddingConfigError):
            await embedder.embed_items(items)

        assert batch1_cancelled is True
        assert invocations == 2
        assert all(item.peek_embedding_text() is None for item in items)

    def test_f02_rejects_collapsed_and_single_line_body_leaks_and_allows_valid_ts(self) -> None:
        leaky_chunk = CodeSymbolChunk(
            file_path="src/codesight/auth.py",
            language="python",
            symbol_name="get_secret",
            qualified_name="get_secret",
            symbol_kind="function",
            start_line=1,
            end_line=3,
            signature="def get_secret():\n    return 'ghp_live_secret'",
        )
        with pytest.raises(ZeroCodeViolationError, match="signature"):
            TransientEmbeddingItem.from_code_chunk(
                leaky_chunk,
                repo_full_name="org/repo",
                blob_sha="sha-f02",
            )

        with pytest.raises(ZeroCodeViolationError, match=r"section_title|signature"):
            TransientEmbeddingItem.from_architecture_doc(
                repo_full_name="org/repo",
                blob_sha="sha-f02",
                file_path="docs/adr.md",
                section_title="Architecture\n    return secret_key",
                start_line=1,
                end_line=5,
                transient_snippet="context",
            )

        for bad_sig in (
            "def f(): return secret_key",
            "def f(x: int) -> int: raise ValueError('secret')",
            "function auth() { const k = 'live'; return k; }",
            "const f = () => return secret",
        ):
            payload = self._valid_payload()
            payload["signature"] = bad_sig
            with pytest.raises(ZeroCodeViolationError, match="signature"):
                validate_zero_code_payload(payload)

        # Single-line metadata fields reject embedded newlines
        bad_symbol = self._valid_payload()
        bad_symbol["symbol_name"] = "ok\n    return secret"
        with pytest.raises(ZeroCodeViolationError):
            validate_zero_code_payload(bad_symbol)

        bad_calls = self._valid_payload()
        bad_calls["called_symbols"] = ["safe_fn", "leak\n    return 1"]
        with pytest.raises(ZeroCodeViolationError):
            validate_zero_code_payload(bad_calls)

        # Anti-false-positive checks: valid TS {...}, destructuring, Python keyword-prefixed names,
        # and Markdown architecture_doc titles pass cleanly.
        for valid_sig in (
            "export async function fetchUser(opts: {...}): Promise<{...}>",
            "export function Button({ title, onClick }: Props): JSX.Element",
            "export type Handler = (req: Request) => Promise<Response>",
            "def f(return_code: int, if_match: str) -> Awaitable[bool]",
        ):
            ok_payload = self._valid_payload()
            ok_payload["signature"] = valid_sig
            cleaned = validate_zero_code_payload(ok_payload)
            assert cleaned["signature"] == valid_sig

        doc_item = TransientEmbeddingItem.from_architecture_doc(
            repo_full_name="org/repo",
            blob_sha="sha-doc",
            file_path="docs/errors.md",
            section_title="Error Handling: raise RetryableTaskError",
            start_line=1,
            end_line=10,
            transient_snippet="Always propagate RetryableTaskError.",
        )
        assert doc_item.payload["signature"] == "Error Handling: raise RetryableTaskError"

    @pytest.mark.asyncio
    async def test_f03_store_requires_tenant_scope_on_reads_and_rejects_blank_scope(self) -> None:
        class _NeverCalledClient:
            async def scroll(self, **_: Any) -> Any:
                raise AssertionError("scroll must not be called without tenant scope")

            async def query_points(self, **_: Any) -> Any:
                raise AssertionError("query_points must not be called without tenant scope")

            async def delete(self, **_: Any) -> Any:
                raise AssertionError("delete must not be called without tenant scope")

        store = QdrantZeroCodeStore(client=_NeverCalledClient(), collection_name="scoped")  # type: ignore[arg-type]
        valid_vec = normalize_l2_vector(_make_unit_vector(0))

        with pytest.raises(ValueError, match=r"repo_full_name|repo_id"):
            await store.find_by_called_symbols(["verify_hmac"])
        with pytest.raises(ValueError, match="repo_full_name"):
            await store.find_by_called_symbols(["verify_hmac"], repo_full_name="   ")
        with pytest.raises(ValueError, match=r"repo_full_name|repo_id"):
            await store.find_by_symbol_names(["verify_hmac"])
        with pytest.raises(ValueError, match=r"repo_full_name|repo_id"):
            await store.search_similar(valid_vec)
        with pytest.raises(ValueError, match="repo_id"):
            await store.search_similar(valid_vec, repo_id="")
        with pytest.raises(ValueError, match="repo_full_name"):
            await store.delete_by_repo(repo_full_name="  ")

    @pytest.mark.asyncio
    async def test_f04_lazy_nonblocking_client_init_and_aclose_closes_both_pools(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        created: list[dict[str, Any]] = []
        aio_closed = False
        sync_closed = False

        class _FakeOwnedClient:
            def __init__(self, *, api_key: str, http_options: Any) -> None:
                created.append({"api_key": api_key, "http_options": http_options})
                self.aio = SimpleNamespace(
                    models=_FakeAsyncModels(),
                    aclose=self._aclose,
                )

            async def _aclose(self) -> None:
                nonlocal aio_closed
                aio_closed = True

            def close(self) -> None:
                nonlocal sync_closed
                sync_closed = True

        monkeypatch.setattr("codesight.rag.embedder.genai.Client", _FakeOwnedClient)

        t0 = time.perf_counter()
        embedder = GeminiEmbedder(api_key="test-lazy-key")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert elapsed_ms < 50.0
        assert created == []

        async with embedder:
            vec = await embedder.embed_query("lazy init test")
            assert len(vec) == EMBEDDING_DIMENSIONS
            assert len(created) == 1

        assert aio_closed is True
        assert sync_closed is True

        with pytest.raises(RuntimeError, match="closed"):
            await embedder.embed_query("after close")

    @pytest.mark.asyncio
    async def test_f05_cold_start_missing_collection_returns_empty_without_exists_rpc(self) -> None:
        class _FakeGrpcNotFound(Exception):
            def code(self) -> Any:
                return SimpleNamespace(name="NOT_FOUND")

        class _MissingCollectionClient:
            def __init__(self, exc_factory: Callable[[], Exception]) -> None:
                self._exc_factory = exc_factory

            async def collection_exists(self, collection_name: str) -> bool:
                raise AssertionError("get_existing_point_ids must not call collection_exists")

            async def retrieve(self, **_: Any) -> Any:
                raise self._exc_factory()

            async def scroll(self, **_: Any) -> Any:
                raise self._exc_factory()

            async def query_points(self, **_: Any) -> Any:
                raise self._exc_factory()

            async def delete(self, **_: Any) -> Any:
                raise self._exc_factory()

        valid_vec = normalize_l2_vector(_make_unit_vector(0))
        http_404 = lambda: UnexpectedResponse(404, "Not Found", b"", httpx.Headers())  # noqa: E731
        for factory in (_FakeGrpcNotFound, http_404):
            store = QdrantZeroCodeStore(
                client=_MissingCollectionClient(factory),  # type: ignore[arg-type]
                collection_name="uninitialized_col",
            )
            assert await store.get_existing_point_ids([str(uuid.uuid4())]) == set()
            assert await store.find_by_called_symbols(["fn"], repo_full_name="org/repo") == ()
            assert await store.find_by_symbol_names(["fn"], repo_full_name="org/repo") == ()
            assert await store.search_similar(valid_vec, repo_full_name="org/repo") == ()
            await store.delete_by_file_paths(["src/a.py"], repo_full_name="org/repo")
            await store.delete_by_repo(repo_full_name="org/repo")

    @pytest.mark.asyncio
    async def test_f06_repr_redacts_text_uuid_preflight_path_norm_and_zero_vector(self) -> None:
        item = TransientEmbeddingItem.from_code_chunk(
            _make_chunk(0),
            repo_full_name="org/repo",
            blob_sha="sha-f06",
            transient_snippet="TOP_SECRET_CUSTOMER_LOGIC_DO_NOT_LOG",
        )
        assert "TOP_SECRET_CUSTOMER_LOGIC_DO_NOT_LOG" not in repr(item)

        # Preflight UUID validation blocks bad ID before spending Gemini API quota
        client, models_stub = _make_fake_genai_client()
        embedder = GeminiEmbedder(client=client)
        bad_id_item = TransientEmbeddingItem(
            point_id="not-a-valid-uuid",
            chunk_type="code",
            payload=self._valid_payload(),
            _embedding_text="some text",
        )
        with pytest.raises(ZeroCodeViolationError, match="UUID"):
            await embedder.embed_items([bad_id_item])
        assert models_stub.calls == []
        assert bad_id_item.peek_embedding_text() is None

        # Uppercase UUID matches lowercase existing_id_checker set and skips Gemini call
        raw_uuid = str(uuid.uuid4())
        upper_item = TransientEmbeddingItem(
            point_id=raw_uuid.upper(),
            chunk_type="code",
            payload=self._valid_payload(),
            _embedding_text="canonical uuid dedup",
        )

        async def _lowercase_checker(ids: Sequence[str]) -> set[str]:
            assert ids == [raw_uuid.lower()]
            return {raw_uuid.lower()}

        res = await embedder.embed_items([upper_item], existing_id_checker=_lowercase_checker)
        assert res.skipped_existing == 1
        assert res.embedded_count == 0
        assert models_stub.calls == []

        # Path normalization in validate_zero_code_payload & "./" rejection
        unnorm = self._valid_payload()
        unnorm["file_path"] = "./src\\codesight\\auth.py"
        cleaned = validate_zero_code_payload(unnorm)
        assert cleaned["file_path"] == "src/codesight/auth.py"

        dot_path = self._valid_payload()
        dot_path["file_path"] = "./"
        with pytest.raises(ZeroCodeViolationError, match="file_path"):
            validate_zero_code_payload(dot_path)

        # Zero-norm vector rejected at both normalize_l2_vector and validate_zero_code_point
        zero_pt = QdrantZeroCodePoint(
            point_id=str(uuid.uuid4()),
            vector=tuple([0.0] * EMBEDDING_DIMENSIONS),
            payload=self._valid_payload(),
        )
        with pytest.raises(ZeroCodeViolationError, match="positive L2 norm"):
            validate_zero_code_point(zero_pt)

    @pytest.mark.asyncio
    async def test_f07_httpx2_transient_retried_and_permanent_errors_fail_fast(self) -> None:
        async def _probe_exc(
            exc: Exception,
            *,
            max_retries: int,
            expected_exc_type: type[BaseException],
        ) -> tuple[int, list[float]]:
            calls = 0
            sleeps: list[float] = []

            async def _raise_exc(**_: Any) -> Any:
                nonlocal calls
                calls += 1
                raise exc

            async def _record_sleep(delay: float) -> None:
                sleeps.append(delay)

            client, _ = _make_fake_genai_client(handler=_raise_exc)
            embedder = GeminiEmbedder(
                client=client, sleep_fn=_record_sleep, max_retries=max_retries
            )
            with pytest.raises(expected_exc_type):
                await embedder.embed_query("transport classification test")
            return calls, sleeps

        # Transient httpx2.ConnectTimeout and httpx.PoolTimeout are retried
        for transient_exc in (
            httpx2.ConnectTimeout("connect timeout"),
            httpx.PoolTimeout("pool exhausted"),
        ):
            calls, sleeps = await _probe_exc(
                transient_exc,
                max_retries=1,
                expected_exc_type=RetryableTaskError,
            )
            assert calls == 2
            assert len(sleeps) == 1

        # Permanent httpx2 client errors must NOT be retried
        for perm_exc in (
            httpx2.InvalidURL("bad url"),
            httpx2.LocalProtocolError("illegal header newline"),
        ):
            calls, sleeps = await _probe_exc(
                perm_exc,
                max_retries=4,
                expected_exc_type=type(perm_exc),
            )
            assert calls == 1
            assert sleeps == []

    @pytest.mark.asyncio
    async def test_f08_scroll_pagination_index_type_mismatch_and_store_unit_ops(self) -> None:
        class _BadIndexTypeClient:
            async def collection_exists(self, collection_name: str) -> bool:
                return True

            async def get_collection(self, collection_name: str) -> Any:
                return SimpleNamespace(
                    config=SimpleNamespace(
                        params=SimpleNamespace(
                            vectors=SimpleNamespace(size=EMBEDDING_DIMENSIONS, distance="Cosine")
                        )
                    ),
                    payload_schema={
                        "repo_id": SimpleNamespace(
                            data_type=qdrant_models.PayloadSchemaType.INTEGER
                        ),
                    },
                )

        bad_idx_store = QdrantZeroCodeStore(
            client=_BadIndexTypeClient(),  # type: ignore[arg-type]
            collection_name="bad_idx",
        )
        with pytest.raises(QdrantSchemaMismatchError, match="keyword"):
            await bad_idx_store.ensure_collection()

        # Multi-page scroll + search_similar + delete_by_file_paths (normalized) + delete_by_repo
        scroll_offsets: list[Any] = []
        delete_selectors: list[Any] = []
        query_kwargs: list[dict[str, Any]] = []

        class _PagedClient:
            async def scroll(self, **kwargs: Any) -> tuple[list[Any], Any]:
                scroll_offsets.append(kwargs.get("offset"))
                if kwargs.get("offset") is None:
                    return [
                        SimpleNamespace(id="id-1", payload={"symbol_name": "a"}),
                        SimpleNamespace(id="id-2", payload={"symbol_name": "b"}),
                    ], "cursor-2"
                return [
                    SimpleNamespace(id="id-3", payload={"symbol_name": "c"}),
                    SimpleNamespace(id="id-4", payload={"symbol_name": "d"}),
                ], None

            async def query_points(self, **kwargs: Any) -> Any:
                query_kwargs.append(kwargs)
                return SimpleNamespace(
                    points=[
                        SimpleNamespace(
                            id="doc-1",
                            payload={"file_path": "docs/adr.md"},
                            score=0.91,
                        )
                    ]
                )

            async def delete(self, **kwargs: Any) -> None:
                delete_selectors.append(kwargs["points_selector"])

        store = QdrantZeroCodeStore(client=_PagedClient(), collection_name="paged_col")  # type: ignore[arg-type]
        pts = await store.find_by_symbol_names(["a", "b", "c"], repo_full_name="org/repo", limit=3)
        assert [p.point_id for p in pts] == ["id-1", "id-2", "id-3"]
        assert scroll_offsets == [None, "cursor-2"]

        valid_vec = normalize_l2_vector(_make_unit_vector(0))
        hits = await store.search_similar(
            valid_vec,
            repo_id="repo-1",
            chunk_type="architecture_doc",
            score_threshold=0.7,
            limit=5,
        )
        assert len(hits) == 1
        assert hits[0].score == pytest.approx(0.91)
        assert query_kwargs[0]["score_threshold"] == 0.7

        with pytest.raises(ValueError, match="Unsupported chunk_type"):
            await store.search_similar(
                valid_vec,
                repo_full_name="org/repo",
                chunk_type="invalid_type",  # type: ignore[arg-type]
            )

        await store.delete_by_file_paths(["./", "   "], repo_full_name="org/repo")
        assert delete_selectors == []

        await store.delete_by_file_paths(["./src\\auth.py"], repo_full_name="org/repo")
        await store.delete_by_repo(repo_id="repo-1")
        assert len(delete_selectors) == 2

    def test_f09_backoff_exponential_growth_cap_and_non_int_start_line(self) -> None:
        with pytest.raises(ValueError, match="start_line"):
            build_deterministic_point_id("org/repo", "a.py", "sha", "fn", 1.5)  # type: ignore[arg-type]

        class _MaxRng(random.Random):
            def uniform(self, a: float, b: float) -> float:
                return b

        client, _ = _make_fake_genai_client()
        embedder = GeminiEmbedder(
            client=client,
            retry_base_seconds=1.0,
            retry_cap_seconds=5.0,
            rng=_MaxRng(),
        )
        assert embedder._compute_backoff_delay(0) == 1.0
        assert embedder._compute_backoff_delay(1) == 2.0
        assert embedder._compute_backoff_delay(2) == 4.0
        assert embedder._compute_backoff_delay(3) == 5.0
        assert embedder._compute_backoff_delay(4) == 5.0
        assert embedder._compute_backoff_delay(0, retry_after=9.0) == 9.0

    def test_new01_allows_valid_tsx_destructuring_and_inline_object_type_signatures(
        self,
        zerocode_auditor: Any,
    ) -> None:
        tsx_source = """export function ErrorBoundary({ raise, fallback }: Props) {
  return fallback;
}

export function Label({ for: htmlFor, if: show }: LabelProps) {
  return show ? <label htmlFor={htmlFor} /> : null;
}

export function login({ password: pass = "" }: Creds) {
  return Boolean(pass);
}

export function Nav({ return: returnUrl }: NavProps) {
  return <a href={returnUrl}>Back</a>;
}

export function render(props: { for?: string; raise?: boolean; pass: boolean }): void {
  void props;
}
"""
        parsed = TypeScriptASTParser().parse(tsx_source, file_path="src/components/Form.tsx")
        assert len(parsed.chunks) == 5

        for chunk in parsed.chunks:
            item = TransientEmbeddingItem.from_code_chunk(
                chunk,
                repo_full_name="org/repo",
                blob_sha="sha-new01",
            )
            assert (
                zerocode_auditor.audit_payloads([{"id": item.point_id, "payload": item.payload}])
                == []
            )

        # Single-line code body leaks remain strictly rejected
        for bad_sig in (
            "def f(): pass",
            "def f(): pass; x = 1",
            "def f(): raise",
            "def f(): return secret_key",
            "function f() { return; }",
            "function auth() { const k = 'live'; return k; }",
            "const f = async () => await fetch(secret)",
        ):
            payload = self._valid_payload()
            payload["signature"] = bad_sig
            with pytest.raises(ZeroCodeViolationError, match="signature"):
                validate_zero_code_payload(payload)

    @pytest.mark.asyncio
    async def test_new02_cancellation_and_concurrent_aclose_during_cold_init_closes_pools(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Scenario A: Task cancelled during cold `to_thread` init inside `async with embedder:`
        init_entered_a = threading.Event()
        release_init_a = threading.Event()
        aio_closed_a = False
        sync_closed_a = False

        class _SlowOwnedClientA:
            def __init__(self, *, api_key: str, http_options: Any) -> None:
                init_entered_a.set()
                release_init_a.wait(timeout=5.0)
                self.aio = SimpleNamespace(
                    models=_FakeAsyncModels(),
                    aclose=self._aclose,
                )

            async def _aclose(self) -> None:
                nonlocal aio_closed_a
                aio_closed_a = True

            def close(self) -> None:
                nonlocal sync_closed_a
                sync_closed_a = True

        monkeypatch.setattr("codesight.rag.embedder.genai.Client", _SlowOwnedClientA)

        async def _run_and_cancel() -> None:
            async with GeminiEmbedder(api_key="test-cancel-init-key") as embedder:
                await embedder.embed_query("cancel during cold init")

        task_a = asyncio.create_task(_run_and_cancel())
        await asyncio.to_thread(init_entered_a.wait, 5.0)
        assert init_entered_a.is_set()
        task_a.cancel()
        # Let __aexit__/aclose enter while thread init is still running, then release thread
        await asyncio.sleep(0.02)
        release_init_a.set()
        with pytest.raises(asyncio.CancelledError):
            await task_a

        assert aio_closed_a is True
        assert sync_closed_a is True

        # Scenario B: Concurrent `aclose()` while another coroutine awaits cold init
        init_entered_b = threading.Event()
        release_init_b = threading.Event()
        aio_closed_b = False
        sync_closed_b = False
        models_stub_b = _FakeAsyncModels()

        class _SlowOwnedClientB:
            def __init__(self, *, api_key: str, http_options: Any) -> None:
                init_entered_b.set()
                release_init_b.wait(timeout=5.0)
                self.aio = SimpleNamespace(
                    models=models_stub_b,
                    aclose=self._aclose,
                )

            async def _aclose(self) -> None:
                nonlocal aio_closed_b
                aio_closed_b = True

            def close(self) -> None:
                nonlocal sync_closed_b
                sync_closed_b = True

        monkeypatch.setattr("codesight.rag.embedder.genai.Client", _SlowOwnedClientB)

        embedder_b = GeminiEmbedder(api_key="test-concurrent-aclose-key")
        query_task = asyncio.create_task(embedder_b.embed_query("race with aclose"))
        await asyncio.to_thread(init_entered_b.wait, 5.0)
        assert init_entered_b.is_set()

        close_task = asyncio.create_task(embedder_b.aclose())
        await asyncio.sleep(0.02)
        release_init_b.set()

        with pytest.raises(RuntimeError, match="closed"):
            await query_task
        await close_task

        assert models_stub_b.calls == []
        assert aio_closed_b is True
        assert sync_closed_b is True

    def test_r2_01_language_aware_signature_guard_allows_py_ts_types_and_blocks_asi_return(
        self,
        zerocode_auditor: Any,
    ) -> None:
        py_source = """def configure(cfg: const.Config, item: var[int], flag: var = 1) -> bool:
    return True

def handle(x: let, err: throw, s = {var for var in items}) -> None:
    pass
"""
        py_parsed = PythonASTParser().parse(py_source, file_path="src/codesight/config_loader.py")
        assert len(py_parsed.chunks) == 2
        for chunk in py_parsed.chunks:
            item = TransientEmbeddingItem.from_code_chunk(
                chunk,
                repo_full_name="org/repo",
                blob_sha="sha-r2-01-py",
            )
            assert (
                zerocode_auditor.audit_payloads([{"id": item.point_id, "payload": item.payload}])
                == []
            )

        ts_source = """export function check(status: pass, err: raise): boolean {
  return Boolean(status && err);
}

export function SingleDestruct({ return }: NavProps) {
  return null;
}

export type Validator = (input: string) => pass;
export type ErrorMapper = (err: unknown) => raise<Error>;
export type Cond<T> = T extends string ? pass : raise;
"""
        ts_parsed = TypeScriptASTParser().parse(ts_source, file_path="src/types/guards.tsx")
        assert len(ts_parsed.chunks) == 5
        for chunk in ts_parsed.chunks:
            item = TransientEmbeddingItem.from_code_chunk(
                chunk,
                repo_full_name="org/repo",
                blob_sha="sha-r2-01-ts",
            )
            assert (
                zerocode_auditor.audit_payloads([{"id": item.point_id, "payload": item.payload}])
                == []
            )

        # Semicolon-less JS/TS single-line body leaks (`{ return }`, `{ throw err }`) must fail
        for bad_ts_sig in (
            "function f() { return }",
            "const f = () => { return }",
            "function f() { throw err }",
            "const f = () => throw new Error('secret')",
            "function auth() { const { a } = secret; }",
        ):
            ts_payload = self._valid_payload()
            ts_payload["language"] = "typescript"
            ts_payload["signature"] = bad_ts_sig
            with pytest.raises(ZeroCodeViolationError, match="signature"):
                validate_zero_code_payload(ts_payload)

    @pytest.mark.asyncio
    async def test_r2_02_aclose_timeout_retry_and_multibatch_cold_init_failure_clean(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Scenario A: aclose() times out during cold init; subsequent aclose() still closes pools
        entered = threading.Event()
        release = threading.Event()
        aio_closed = False
        sync_closed = False

        class _SlowClient:
            def __init__(self, *, api_key: str, http_options: Any) -> None:
                entered.set()
                release.wait(timeout=5.0)
                self.aio = SimpleNamespace(
                    models=_FakeAsyncModels(),
                    aclose=self._aclose,
                )

            async def _aclose(self) -> None:
                nonlocal aio_closed
                aio_closed = True

            def close(self) -> None:
                nonlocal sync_closed
                sync_closed = True

        monkeypatch.setattr("codesight.rag.embedder.genai.Client", _SlowClient)

        emb = GeminiEmbedder(api_key="test-r2-02-timeout-key")
        q_task = asyncio.create_task(emb.embed_query("hello"))
        await asyncio.to_thread(entered.wait, 5.0)
        assert entered.is_set()

        q_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await q_task

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(emb.aclose(), timeout=0.02)

        release.set()
        await asyncio.sleep(0.03)
        await emb.aclose()

        assert aio_closed is True
        assert sync_closed is True

        # Scenario B: Multi-batch cold-init failure builds once with zero orphan futures
        builds = 0
        unhandled_contexts: list[dict[str, Any]] = []
        loop = asyncio.get_running_loop()
        prev_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, ctx: unhandled_contexts.append(ctx))
        try:

            class _FailingClient:
                def __init__(self, *, api_key: str, http_options: Any) -> None:
                    nonlocal builds
                    builds += 1
                    raise RuntimeError("SSL cert bundle load failed")

            monkeypatch.setattr("codesight.rag.embedder.genai.Client", _FailingClient)
            emb_fail = GeminiEmbedder(
                api_key="test-r2-02-fail-key",
                batch_size=2,
                max_concurrency=2,
            )
            items = [
                TransientEmbeddingItem.from_code_chunk(
                    _make_chunk(i),
                    repo_full_name="org/repo",
                    blob_sha="sha-r2-02",
                    transient_snippet=f"secret_{i}()",
                )
                for i in range(6)
            ]

            with pytest.raises(RuntimeError, match="SSL cert bundle"):
                await emb_fail.embed_items(items)

            await asyncio.sleep(0.05)
            assert builds == 1
            assert unhandled_contexts == []
            assert all(it.peek_embedding_text() is None for it in items)
        finally:
            loop.set_exception_handler(prev_handler)
