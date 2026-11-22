"""Batched Google Gemini embedder with RPM concurrency guard, retry backoff, and RAM cleanup."""

from __future__ import annotations

import asyncio
import math
import random
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from types import TracebackType
from typing import Any, Final, Literal, Self

import httpx
import structlog
from google import genai
from google.genai import errors, types
from pydantic import SecretStr

from codesight.ast_engine.models import CodeSymbolChunk
from codesight.core.config import Settings, get_settings
from codesight.core.errors import (
    RateLimitTaskError,
    RetryableTaskError,
    parse_retry_after_header,
)
from codesight.rag.models import (
    DEFAULT_EMBED_BATCH_SIZE,
    DEFAULT_RPM_CONCURRENCY,
    EMBEDDING_DIMENSIONS,
    EMBEDDING_MODEL_NAME,
    MAX_EMBED_BATCH_SIZE,
    EmbeddingConfigError,
    EmbeddingRunResult,
    QdrantZeroCodePoint,
    TransientEmbeddingItem,
    normalize_l2_vector,
    validate_positive_int,
    validate_uuid_point_id,
    validate_zero_code_payload,
)

type ExistingPointIdChecker = Callable[[Sequence[str]], Awaitable[AbstractSet[str]]]
type SleepFn = Callable[[float], Awaitable[None]]
type ClockFn = Callable[[], float]
type EmbeddingTaskType = Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]

DEFAULT_MAX_RETRIES: Final[int] = 5
DEFAULT_RETRY_BASE_SECONDS: Final[float] = 1.0
DEFAULT_RETRY_CAP_SECONDS: Final[float] = 65.0
DEFAULT_EMBED_MAX_RETRIES: Final[int] = DEFAULT_MAX_RETRIES
DEFAULT_EMBED_RETRY_CAP_SECONDS: Final[float] = DEFAULT_RETRY_CAP_SECONDS
DEFAULT_TPM_WINDOW_SECONDS: Final[float] = 60.0
_RETRIABLE_CLIENT_STATUS_CODES: Final[frozenset[int]] = frozenset({408, 429})
_TRANSIENT_HTTPX_CLASS_NAMES: Final[frozenset[str]] = frozenset(
    {"TimeoutException", "NetworkError", "RemoteProtocolError", "ProxyError"}
)
_SYSTEM_RNG: Final[random.Random] = random.SystemRandom()

logger = structlog.get_logger("codesight.rag")


def _extract_response_headers(exc: BaseException) -> Mapping[str, str]:
    response = getattr(exc, "response", None)
    raw_headers = getattr(response, "headers", None)
    if isinstance(raw_headers, Mapping):
        return {str(k): str(v) for k, v in raw_headers.items()}
    return {}


def _parse_retry_delay_value(raw_delay: object) -> float | None:
    if isinstance(raw_delay, (int, float)) and not isinstance(raw_delay, bool):
        parsed = float(raw_delay)
    elif isinstance(raw_delay, str):
        cleaned = raw_delay.strip()
        if cleaned.endswith(("s", "S")):
            cleaned = cleaned[:-1].strip()
        if not cleaned:
            return None
        try:
            parsed = float(cleaned)
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(parsed) or parsed <= 0.0:
        return None
    return parsed


def parse_gemini_retry_delay(exc: BaseException, headers: Mapping[str, str]) -> float | None:
    """Extract positive retry delay from `google.rpc.RetryInfo` before `Retry-After` headers."""
    details = getattr(exc, "details", None)
    if isinstance(details, Mapping):
        error_obj = details.get("error")
        nested_details = error_obj.get("details") if isinstance(error_obj, Mapping) else None
        top_details = details.get("details")
        for detail_list in (nested_details, top_details):
            if not isinstance(detail_list, Sequence) or isinstance(detail_list, (str, bytes)):
                continue
            for item in detail_list:
                if not isinstance(item, Mapping):
                    continue
                if "RetryInfo" not in str(item.get("@type", "")):
                    continue
                parsed = _parse_retry_delay_value(item.get("retryDelay"))
                if parsed is not None:
                    return parsed
    return parse_retry_after_header(headers)


class _AsyncTPMLimiter:
    """Sliding-window 60-second TPM quota pacer for Gemini embedding batches."""

    def __init__(
        self,
        tpm_budget: int | None,
        sleep_fn: SleepFn = asyncio.sleep,
        clock_fn: ClockFn = time.monotonic,
        *,
        window_seconds: float = DEFAULT_TPM_WINDOW_SECONDS,
    ) -> None:
        if tpm_budget is not None:
            validate_positive_int(tpm_budget, name="tpm_budget")
        if window_seconds <= 0.0:
            raise ValueError("window_seconds must be > 0.")
        self._tpm_budget = tpm_budget
        self._sleep_fn = sleep_fn
        self._clock_fn = clock_fn
        self._window_seconds = window_seconds
        self._lock = asyncio.Lock()
        self._events: deque[tuple[float, int]] = deque()
        self._used_tokens = 0
        self._virtual_offset = 0.0

    @staticmethod
    def estimate_tokens(texts: Sequence[str]) -> int:
        """Estimate batch tokens conservatively using `chars // 4`."""
        if not texts:
            return 0
        total_chars = sum(len(text) for text in texts)
        return max(1, total_chars // 4) if total_chars > 0 else 0

    def _now(self) -> float:
        return self._clock_fn() + self._virtual_offset

    def _prune(self, now: float) -> None:
        cutoff = now - self._window_seconds
        while self._events and self._events[0][0] <= cutoff:
            _, expired = self._events.popleft()
            self._used_tokens -= expired

    async def acquire(self, texts_or_tokens: Sequence[str] | int) -> int:
        """Wait until `texts_or_tokens` fits in the 60s TPM window, then reserve its budget."""
        if isinstance(texts_or_tokens, bool):
            raise TypeError("texts_or_tokens must be a Sequence[str] or int, not bool.")
        if isinstance(texts_or_tokens, int):
            tokens = max(0, texts_or_tokens)
        else:
            tokens = self.estimate_tokens(texts_or_tokens)
        if self._tpm_budget is None or tokens <= 0:
            return tokens

        async with self._lock:
            while True:
                now = self._now()
                self._prune(now)
                if self._used_tokens == 0 or (self._used_tokens + tokens) <= self._tpm_budget:
                    self._events.append((now, tokens))
                    self._used_tokens += tokens
                    return tokens
                oldest_ts, _ = self._events[0]
                delay = max(0.01, (oldest_ts + self._window_seconds) - now)
                before_sleep = self._clock_fn()
                await self._sleep_fn(delay)
                elapsed = max(0.0, self._clock_fn() - before_sleep)
                if elapsed < delay * 0.5:
                    # Support non-advancing sleep_fn test stubs without spinning.
                    self._virtual_offset += delay - elapsed


def _is_transient_transport_error(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
            httpx.ProxyError,
            TimeoutError,
            ConnectionError,
        ),
    ):
        return True
    return any(
        cls.__module__.startswith("httpx2") and cls.__name__ in _TRANSIENT_HTTPX_CLASS_NAMES
        for cls in type(exc).__mro__
    )


class GeminiEmbedder:
    """Async batched embedder for `gemini-embedding-001` protecting Google AI Studio quotas."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        api_key: str | SecretStr | None = None,
        client: genai.Client | None = None,
        model: str = EMBEDDING_MODEL_NAME,
        output_dimensionality: int = EMBEDDING_DIMENSIONS,
        batch_size: int = DEFAULT_EMBED_BATCH_SIZE,
        max_concurrency: int | None = None,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
        retry_cap_seconds: float = DEFAULT_RETRY_CAP_SECONDS,
        tpm_budget: int | None = None,
        sleep_fn: SleepFn = asyncio.sleep,
        clock_fn: ClockFn = time.monotonic,
        rng: random.Random = _SYSTEM_RNG,
    ) -> None:
        if output_dimensionality != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"output_dimensionality must equal {EMBEDDING_DIMENSIONS} for Qdrant compatibility."
            )
        self._batch_size = validate_positive_int(
            batch_size,
            name="batch_size",
            maximum=MAX_EMBED_BATCH_SIZE,
        )
        if max_concurrency is not None:
            resolved_concurrency = validate_positive_int(max_concurrency, name="max_concurrency")
        elif client is None and settings is not None:
            resolved_concurrency = validate_positive_int(
                settings.gemini_embed_concurrency,
                name="max_concurrency",
            )
        else:
            resolved_concurrency = DEFAULT_RPM_CONCURRENCY
        self._max_concurrency = resolved_concurrency

        if tpm_budget is not None:
            resolved_tpm_budget: int | None = validate_positive_int(tpm_budget, name="tpm_budget")
        elif client is None and settings is not None:
            resolved_tpm_budget = validate_positive_int(
                settings.gemini_embed_tpm_budget,
                name="tpm_budget",
            )
        else:
            resolved_tpm_budget = None

        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a non-negative integer.")
        if retry_base_seconds <= 0.0 or retry_cap_seconds < retry_base_seconds:
            raise ValueError("retry_base_seconds must be > 0 and <= retry_cap_seconds.")

        self._model = model
        self._output_dimensionality = output_dimensionality
        self._max_retries = max_retries
        self._retry_base_seconds = retry_base_seconds
        self._retry_cap_seconds = retry_cap_seconds
        self._sleep_fn = sleep_fn
        self._rng = rng
        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        self._tpm_limiter: _AsyncTPMLimiter | None = (
            _AsyncTPMLimiter(
                resolved_tpm_budget,
                sleep_fn=sleep_fn,
                clock_fn=clock_fn,
            )
            if resolved_tpm_budget is not None
            else None
        )
        self._client_lock = asyncio.Lock()
        self._client_future: asyncio.Future[Any] | None = None
        self._closed = False

        if client is not None:
            self._client: Any = client
            self._owns_client = False
            self._resolved_api_key: SecretStr | None = None
        else:
            resolved_key = self._resolve_api_key(api_key=api_key, settings=settings)
            self._client = None
            self._owns_client = True
            self._resolved_api_key = SecretStr(resolved_key)

    @staticmethod
    def _build_genai_client(api_key: str) -> genai.Client:
        return genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )

    @staticmethod
    async def _close_client_instance(client: Any) -> None:
        aio_client = getattr(client, "aio", None)
        aclose_fn = getattr(aio_client, "aclose", None)
        try:
            if callable(aclose_fn):
                await aclose_fn()
        finally:
            close_fn = getattr(client, "close", None)
            if callable(close_fn):
                close_fn()

    async def _ensure_client(self) -> Any:
        if self._closed:
            raise RuntimeError("GeminiEmbedder is closed.")
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._closed:
                raise RuntimeError("GeminiEmbedder is closed.")
            if self._client is None:
                if self._client_future is None:
                    if self._resolved_api_key is None:
                        raise EmbeddingConfigError(
                            "GEMINI_API_KEY is required to initialize GeminiEmbedder."
                        )
                    raw_key = self._resolved_api_key.get_secret_value()
                    loop = asyncio.get_running_loop()
                    fut = loop.run_in_executor(
                        None,
                        self._build_genai_client,
                        raw_key,
                    )
                    fut.add_done_callback(lambda f: None if f.cancelled() else f.exception())
                    self._client_future = fut
                pending = self._client_future
                try:
                    built = await asyncio.shield(pending)
                finally:
                    if pending.done() and self._client_future is pending:
                        self._client_future = None
                self._resolved_api_key = None
                if self._closed:
                    self._client = built
                    await self._close_client_instance(built)
                    self._client = None
                    raise RuntimeError("GeminiEmbedder is closed.")
                self._client = built
        return self._client

    async def aclose(self) -> None:
        """Close owned async and sync google-genai HTTP client pools cleanly."""
        self._closed = True
        self._resolved_api_key = None
        if not self._owns_client:
            return
        async with self._client_lock:
            pending = self._client_future
            if self._client is None and pending is not None:
                try:
                    self._client = await asyncio.shield(pending)
                except Exception:
                    self._client = None
                finally:
                    if pending.done() and self._client_future is pending:
                        self._client_future = None
            if self._client is not None:
                client_to_close = self._client
                await self._close_client_instance(client_to_close)
                self._client = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    @staticmethod
    def _resolve_api_key(
        *,
        api_key: str | SecretStr | None,
        settings: Settings | None,
    ) -> str:
        raw_secret: str | SecretStr | None = api_key
        if raw_secret is None:
            cfg = settings if settings is not None else get_settings()
            raw_secret = cfg.gemini_api_key
        if isinstance(raw_secret, SecretStr):
            resolved = raw_secret.get_secret_value().strip()
        elif isinstance(raw_secret, str):
            resolved = raw_secret.strip()
        else:
            resolved = ""
        if not resolved:
            raise EmbeddingConfigError("GEMINI_API_KEY is required to initialize GeminiEmbedder.")
        return resolved

    def _compute_backoff_delay(
        self,
        attempt: int,
        *,
        retry_after: float | None = None,
    ) -> float:
        if retry_after is not None:
            return min(self._retry_cap_seconds, max(1.0, retry_after))
        ceiling = min(self._retry_cap_seconds, self._retry_base_seconds * float(2**attempt))
        return self._rng.uniform(0.0, ceiling)

    async def _embed_batch_with_retry(
        self,
        texts: list[str] | None = None,
        *,
        consume_texts: Callable[[], list[str]] | None = None,
        task_type: EmbeddingTaskType,
        abort_event: asyncio.Event | None = None,
    ) -> list[tuple[float, ...]]:
        config = types.EmbedContentConfig(
            task_type=task_type,
            output_dimensionality=self._output_dimensionality,
        )
        client = await self._ensure_client()
        materialized: list[str] | None = list(texts) if texts is not None else None

        try:
            for attempt in range(self._max_retries + 1):
                if abort_event is not None and abort_event.is_set():
                    raise asyncio.CancelledError("Embedding run aborted after sibling failure.")
                try:
                    async with self._semaphore:
                        if abort_event is not None and abort_event.is_set():
                            raise asyncio.CancelledError(
                                "Embedding run aborted after sibling failure."
                            )
                        if materialized is None:
                            if consume_texts is None:
                                raise ValueError("Either texts or consume_texts must be provided.")
                            materialized = consume_texts()
                        if self._tpm_limiter is not None:
                            await self._tpm_limiter.acquire(materialized)
                        if abort_event is not None and abort_event.is_set():
                            raise asyncio.CancelledError(
                                "Embedding run aborted after sibling failure."
                            )
                        response = await client.aio.models.embed_content(
                            model=self._model,
                            contents=materialized,
                            config=config,
                        )
                    return self._parse_and_normalize_response(
                        response,
                        expected_count=len(materialized),
                    )
                except errors.ClientError as exc:
                    status_code = getattr(exc, "code", None)
                    if status_code not in _RETRIABLE_CLIENT_STATUS_CODES:
                        raise EmbeddingConfigError(
                            f"Gemini embedding rejected with non-retriable HTTP {status_code}."
                        ) from exc
                    headers = _extract_response_headers(exc)
                    retry_after = parse_gemini_retry_delay(exc, headers)
                    if attempt >= self._max_retries:
                        if status_code == 429:
                            raise RateLimitTaskError(
                                "Gemini embedding rate limit exhausted after retries.",
                                retry_after_seconds=retry_after,
                            ) from exc
                        raise RetryableTaskError(
                            f"Gemini embedding request timed out (HTTP {status_code}) "
                            "after retries."
                        ) from exc
                    delay = self._compute_backoff_delay(attempt, retry_after=retry_after)
                    logger.warning(
                        "gemini_embed_batch_retry",
                        attempt=attempt + 1,
                        status_code=status_code,
                        delay_seconds=round(delay, 3),
                        batch_len=len(materialized) if materialized is not None else 0,
                    )
                    await self._sleep_fn(delay)
                except errors.ServerError as exc:
                    status_code = getattr(exc, "code", 500)
                    headers = _extract_response_headers(exc)
                    retry_after = parse_gemini_retry_delay(exc, headers)
                    if attempt >= self._max_retries:
                        raise RetryableTaskError(
                            f"Gemini embedding server error (HTTP {status_code}) after retries."
                        ) from exc
                    delay = self._compute_backoff_delay(attempt, retry_after=retry_after)
                    logger.warning(
                        "gemini_embed_batch_retry",
                        attempt=attempt + 1,
                        status_code=status_code,
                        delay_seconds=round(delay, 3),
                        batch_len=len(materialized) if materialized is not None else 0,
                    )
                    await self._sleep_fn(delay)
                except Exception as exc:
                    if not _is_transient_transport_error(exc):
                        raise
                    if attempt >= self._max_retries:
                        raise RetryableTaskError(
                            f"Gemini embedding transport failure ({type(exc).__name__}) "
                            "after retries."
                        ) from exc
                    delay = self._compute_backoff_delay(attempt)
                    logger.warning(
                        "gemini_embed_batch_retry",
                        attempt=attempt + 1,
                        error_type=type(exc).__name__,
                        delay_seconds=round(delay, 3),
                        batch_len=len(materialized) if materialized is not None else 0,
                    )
                    await self._sleep_fn(delay)
        finally:
            if materialized is not None:
                materialized.clear()

        raise RetryableTaskError("Gemini embedding failed unexpectedly after exhausting retries.")

    def _parse_and_normalize_response(
        self,
        response: Any,
        *,
        expected_count: int,
    ) -> list[tuple[float, ...]]:
        embeddings = getattr(response, "embeddings", None)
        if not isinstance(embeddings, Sequence) or len(embeddings) != expected_count:
            actual = len(embeddings) if isinstance(embeddings, Sequence) else 0
            raise ValueError(
                f"Gemini embed_content returned {actual} embeddings for {expected_count} inputs."
            )
        normalized: list[tuple[float, ...]] = []
        for emb in embeddings:
            values = getattr(emb, "values", None)
            if not isinstance(values, Sequence):
                raise ValueError("Gemini embedding response item is missing numeric values.")
            normalized.append(normalize_l2_vector(values, expected_dim=self._output_dimensionality))
        return normalized

    async def embed_items(
        self,
        items: Sequence[TransientEmbeddingItem],
        *,
        existing_id_checker: ExistingPointIdChecker | None = None,
    ) -> EmbeddingRunResult:
        """Deduplicate, batch-embed, normalize, and release transient RAM text for all items."""
        started = time.perf_counter()
        total_items = len(items)
        if total_items == 0:
            return EmbeddingRunResult(
                points=(),
                total_items=0,
                deduped_in_batch=0,
                skipped_existing=0,
                embedded_count=0,
                batch_calls=0,
            )

        try:
            # 1. Preflight UUID + NFR-3 payload validation and in-batch deduplication
            unique_entries: list[tuple[str, dict[str, Any], TransientEmbeddingItem]] = []
            seen_ids: set[str] = set()
            deduped_in_batch = 0
            for item in items:
                canonical_id = validate_uuid_point_id(item.point_id)
                clean_payload = validate_zero_code_payload(item.payload, point_id=canonical_id)
                if canonical_id in seen_ids:
                    deduped_in_batch += 1
                    item.clear_transient_text()
                    continue
                seen_ids.add(canonical_id)
                unique_entries.append((canonical_id, clean_payload, item))

            # 2. Skip chunks whose deterministic UUIDv5 (blob_sha + symbol) already exists in Qdrant
            existing_ids: AbstractSet[str] = frozenset()
            if existing_id_checker is not None:
                raw_existing = await existing_id_checker([cid for cid, _, _ in unique_entries])
                existing_ids = {eid.strip().lower() for eid in raw_existing}

            missing_entries: list[tuple[str, dict[str, Any], TransientEmbeddingItem]] = []
            skipped_existing = 0
            for cid, clean_payload, item in unique_entries:
                if cid in existing_ids:
                    skipped_existing += 1
                    item.clear_transient_text()
                else:
                    missing_entries.append((cid, clean_payload, item))

            if not missing_entries:
                elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
                logger.info(
                    "gemini_embed_completed",
                    total_items=total_items,
                    deduped_in_batch=deduped_in_batch,
                    skipped_existing=skipped_existing,
                    embedded_count=0,
                    batch_calls=0,
                    latency_ms=elapsed_ms,
                )
                return EmbeddingRunResult(
                    points=(),
                    total_items=total_items,
                    deduped_in_batch=deduped_in_batch,
                    skipped_existing=skipped_existing,
                    embedded_count=0,
                    batch_calls=0,
                )

            # 3. Ensure client once before partitioning into batches bounded by Semaphore(3)
            await self._ensure_client()
            batches: list[list[tuple[str, dict[str, Any], TransientEmbeddingItem]]] = [
                missing_entries[idx : idx + self._batch_size]
                for idx in range(0, len(missing_entries), self._batch_size)
            ]

            abort_event = asyncio.Event()

            async def _run_batch(
                batch_entries: list[tuple[str, dict[str, Any], TransientEmbeddingItem]],
            ) -> list[QdrantZeroCodePoint]:
                try:
                    vectors = await self._embed_batch_with_retry(
                        consume_texts=lambda: [
                            entry_item.consume_embedding_text()
                            for _, _, entry_item in batch_entries
                        ],
                        task_type="RETRIEVAL_DOCUMENT",
                        abort_event=abort_event,
                    )
                except BaseException:
                    abort_event.set()
                    raise
                return [
                    QdrantZeroCodePoint(
                        point_id=cid,
                        vector=vec,
                        payload=clean_payload,
                    )
                    for (cid, clean_payload, _), vec in zip(batch_entries, vectors, strict=True)
                ]

            tasks = [asyncio.create_task(_run_batch(batch)) for batch in batches]
            try:
                batch_results = await asyncio.gather(*tasks)
            except BaseException:
                abort_event.set()
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

            ordered_points = tuple(pt for batch_pts in batch_results for pt in batch_pts)

            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
            logger.info(
                "gemini_embed_completed",
                total_items=total_items,
                deduped_in_batch=deduped_in_batch,
                skipped_existing=skipped_existing,
                embedded_count=len(ordered_points),
                batch_calls=len(batches),
                latency_ms=elapsed_ms,
            )
            return EmbeddingRunResult(
                points=ordered_points,
                total_items=total_items,
                deduped_in_batch=deduped_in_batch,
                skipped_existing=skipped_existing,
                embedded_count=len(ordered_points),
                batch_calls=len(batches),
            )
        finally:
            # Guarantee zero transient source text survives even on partial failure/cancellation
            for item in items:
                item.clear_transient_text()

    async def embed_code_chunks(
        self,
        chunks: Sequence[CodeSymbolChunk],
        *,
        repo_full_name: str,
        blob_sha: str,
        repo_id: str | None = None,
        indexed_at: str | None = None,
        existing_id_checker: ExistingPointIdChecker | None = None,
    ) -> EmbeddingRunResult:
        """Convenience wrapper embedding AST CodeSymbolChunk units directly."""
        items = [
            TransientEmbeddingItem.from_code_chunk(
                chunk,
                repo_full_name=repo_full_name,
                blob_sha=blob_sha,
                repo_id=repo_id,
                indexed_at=indexed_at,
            )
            for chunk in chunks
        ]
        return await self.embed_items(items, existing_id_checker=existing_id_checker)

    async def embed_query(self, query_text: str) -> tuple[float, ...]:
        """Embed a single retrieval query with `RETRIEVAL_QUERY` and 768-dim L2 normalization."""
        cleaned = query_text.strip()
        if not cleaned:
            raise ValueError("query_text must be a non-empty string.")
        vectors = await self._embed_batch_with_retry([cleaned], task_type="RETRIEVAL_QUERY")
        return vectors[0]
