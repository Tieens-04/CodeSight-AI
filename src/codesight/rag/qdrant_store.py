"""Qdrant gRPC Zero-Code Persistence store enforcing NFR-3 invariants and keyword indexes."""

from __future__ import annotations

import time
from collections.abc import Sequence
from types import TracebackType
from typing import Any, Final, Self

import structlog
from qdrant_client import AsyncQdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from codesight.core.config import Settings, get_settings
from codesight.diff.file_filter import normalize_diff_path
from codesight.rag.models import (
    ALLOWED_CHUNK_TYPES,
    EMBEDDING_DIMENSIONS,
    REQUIRED_KEYWORD_INDEX_FIELDS,
    ChunkType,
    QdrantSchemaMismatchError,
    QdrantStoredPoint,
    QdrantZeroCodePoint,
    normalize_l2_vector,
    validate_positive_int,
    validate_uuid_point_id,
    validate_zero_code_point,
)

DEFAULT_QDRANT_TIMEOUT_SECONDS: Final[int] = 15
DEFAULT_UPSERT_BATCH_SIZE: Final[int] = 256
DEFAULT_RETRIEVE_BATCH_SIZE: Final[int] = 512
DEFAULT_SCROLL_PAGE_SIZE: Final[int] = 256

logger = structlog.get_logger("codesight.rag")


def _extract_enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw).strip().lower()


def _is_missing_collection_error(exc: BaseException) -> bool:
    """Return True when Qdrant HTTP (404) or gRPC (NOT_FOUND) signals a missing collection."""
    if isinstance(exc, UnexpectedResponse) and exc.status_code == 404:
        return True
    code_fn = getattr(exc, "code", None)
    if callable(code_fn):
        rpc_code = code_fn()
        code_name = getattr(rpc_code, "name", str(rpc_code))
        if str(code_name).upper() == "NOT_FOUND":
            return True
    return False


def _dedupe_non_empty_strings(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in values:
        item = raw.strip()
        if item and item not in seen:
            seen.add(item)
            cleaned.append(item)
    return cleaned


class QdrantZeroCodeStore:
    """Async Qdrant gRPC repository strictly enforcing Zero-Code Persistence (`NFR-3`)."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        client: AsyncQdrantClient | None = None,
        collection_name: str | None = None,
        timeout_seconds: int = DEFAULT_QDRANT_TIMEOUT_SECONDS,
    ) -> None:
        cfg = settings if settings is not None else get_settings()
        self._collection_name = (
            collection_name.strip()
            if isinstance(collection_name, str) and collection_name.strip()
            else cfg.qdrant_collection_name
        )
        validate_positive_int(timeout_seconds, name="timeout_seconds")

        if client is not None:
            self._client = client
            self._owns_client = False
        else:
            self._client = AsyncQdrantClient(
                host=cfg.qdrant_host,
                port=cfg.qdrant_http_port,
                grpc_port=cfg.qdrant_grpc_port,
                prefer_grpc=True,
                timeout=timeout_seconds,
            )
            self._owns_client = True

    @property
    def client(self) -> AsyncQdrantClient:
        """Expose underlying AsyncQdrantClient for inspection or lifecycle management."""
        return self._client

    @property
    def collection_name(self) -> str:
        """Return target Qdrant collection name."""
        return self._collection_name

    async def aclose(self) -> None:
        """Close the underlying gRPC/HTTP client when owned by this store instance."""
        if self._owns_client:
            await self._client.close()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def _verify_collection_schema(self, collection_info: Any) -> None:
        config = getattr(collection_info, "config", None)
        params = getattr(config, "params", None)
        vectors = getattr(params, "vectors", None)
        actual_size = getattr(vectors, "size", None)
        actual_distance = _extract_enum_value(getattr(vectors, "distance", None))

        if actual_size != EMBEDDING_DIMENSIONS or actual_distance != "cosine":
            raise QdrantSchemaMismatchError(
                f"Qdrant collection '{self._collection_name}' has incompatible vector config "
                f"(size={actual_size}, distance={actual_distance}); expected "
                f"(size={EMBEDDING_DIMENSIONS}, distance=cosine)."
            )

    async def ensure_collection(self) -> None:
        """Idempotently create `codesight_ast_index` (768 Cosine) and all 7 keyword indexes."""
        exists = await self._client.collection_exists(self._collection_name)
        if not exists:
            try:
                await self._client.create_collection(
                    collection_name=self._collection_name,
                    vectors_config=models.VectorParams(
                        size=EMBEDDING_DIMENSIONS,
                        distance=models.Distance.COSINE,
                    ),
                )
            except Exception:
                # Handle benign concurrent creation race across workers
                if not await self._client.collection_exists(self._collection_name):
                    raise

        info = await self._client.get_collection(self._collection_name)
        self._verify_collection_schema(info)

        existing_schema = getattr(info, "payload_schema", None) or {}
        created_count = 0
        for field_name in REQUIRED_KEYWORD_INDEX_FIELDS:
            field_info = existing_schema.get(field_name)
            if field_info is None:
                await self._client.create_payload_index(
                    collection_name=self._collection_name,
                    field_name=field_name,
                    field_schema=models.PayloadSchemaType.KEYWORD,
                    wait=True,
                )
                created_count += 1
                continue
            actual_type = _extract_enum_value(getattr(field_info, "data_type", None))
            if actual_type != "keyword":
                raise QdrantSchemaMismatchError(
                    f"Qdrant collection '{self._collection_name}' index '{field_name}' has "
                    f"data_type={actual_type!r}; expected 'keyword'."
                )

        logger.info(
            "qdrant_collection_ensured",
            collection=self._collection_name,
            indexes_total=len(REQUIRED_KEYWORD_INDEX_FIELDS),
            indexes_created=created_count,
        )

    async def get_existing_point_ids(
        self,
        point_ids: Sequence[str],
        *,
        batch_size: int = DEFAULT_RETRIEVE_BATCH_SIZE,
    ) -> set[str]:
        """Batch-check which UUIDv5 point IDs already exist in Qdrant (zero payload transfer)."""
        validate_positive_int(batch_size, name="batch_size")
        if not point_ids:
            return set()

        canonical_ids: list[str] = []
        seen: set[str] = set()
        for raw_id in point_ids:
            cid = validate_uuid_point_id(raw_id)
            if cid not in seen:
                seen.add(cid)
                canonical_ids.append(cid)

        existing: set[str] = set()
        for idx in range(0, len(canonical_ids), batch_size):
            batch = canonical_ids[idx : idx + batch_size]
            try:
                records = await self._client.retrieve(
                    collection_name=self._collection_name,
                    ids=batch,
                    with_payload=False,
                    with_vectors=False,
                )
            except Exception as exc:
                if _is_missing_collection_error(exc):
                    return set()
                raise
            for rec in records:
                existing.add(str(rec.id))
        return existing

    async def upsert_points(
        self,
        points: Sequence[QdrantZeroCodePoint],
        *,
        batch_size: int = DEFAULT_UPSERT_BATCH_SIZE,
        wait: bool = True,
    ) -> int:
        """Validate 100% of points against NFR-3 preflight, then upsert via gRPC batches."""
        validate_positive_int(batch_size, name="batch_size")
        if not points:
            return 0

        # All-or-nothing preflight guard: reject before any partial write reaches Qdrant
        validated_structs: list[models.PointStruct] = []
        for point in points:
            canonical_id, vec_list, clean_payload = validate_zero_code_point(point)
            validated_structs.append(
                models.PointStruct(
                    id=canonical_id,
                    vector=vec_list,
                    payload=clean_payload,
                )
            )

        started = time.perf_counter()
        for idx in range(0, len(validated_structs), batch_size):
            batch = validated_structs[idx : idx + batch_size]
            await self._client.upsert(
                collection_name=self._collection_name,
                points=batch,
                wait=wait,
            )

        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        logger.info(
            "qdrant_points_upserted",
            collection=self._collection_name,
            count=len(validated_structs),
            latency_ms=elapsed_ms,
        )
        return len(validated_structs)

    @staticmethod
    def _build_scope_conditions(
        *,
        repo_full_name: str | None = None,
        repo_id: str | None = None,
    ) -> list[models.Condition]:
        conditions: list[models.Condition] = []
        if repo_full_name is not None:
            cleaned_name = repo_full_name.strip()
            if not cleaned_name:
                raise ValueError("repo_full_name must be a non-empty string when provided.")
            conditions.append(
                models.FieldCondition(
                    key="repo_full_name",
                    match=models.MatchValue(value=cleaned_name),
                )
            )
        if repo_id is not None:
            cleaned_id = repo_id.strip()
            if not cleaned_id:
                raise ValueError("repo_id must be a non-empty string when provided.")
            conditions.append(
                models.FieldCondition(
                    key="repo_id",
                    match=models.MatchValue(value=cleaned_id),
                )
            )
        if not conditions:
            raise ValueError(
                "At least one tenant scope filter ('repo_full_name' or 'repo_id') is required."
            )
        return conditions

    async def _scroll_code_by_keyword(
        self,
        *,
        field_name: str,
        values: Sequence[str],
        repo_full_name: str | None = None,
        repo_id: str | None = None,
        limit: int = 50,
    ) -> tuple[QdrantStoredPoint, ...]:
        validate_positive_int(limit, name="limit")
        scope_conditions = self._build_scope_conditions(
            repo_full_name=repo_full_name,
            repo_id=repo_id,
        )
        cleaned = _dedupe_non_empty_strings(values)
        if not cleaned:
            return ()

        scroll_filter = models.Filter(
            must=[
                *scope_conditions,
                models.FieldCondition(
                    key="chunk_type",
                    match=models.MatchValue(value="code"),
                ),
                models.FieldCondition(
                    key=field_name,
                    match=models.MatchAny(any=cleaned),
                ),
            ]
        )

        collected: list[QdrantStoredPoint] = []
        next_offset: Any = None
        while len(collected) < limit:
            page_limit = min(limit - len(collected), DEFAULT_SCROLL_PAGE_SIZE)
            try:
                records, next_offset = await self._client.scroll(
                    collection_name=self._collection_name,
                    scroll_filter=scroll_filter,
                    limit=page_limit,
                    offset=next_offset,
                    with_payload=True,
                    with_vectors=False,
                )
            except Exception as exc:
                if _is_missing_collection_error(exc):
                    return ()
                raise
            if not records:
                break
            for rec in records:
                collected.append(
                    QdrantStoredPoint(
                        point_id=str(rec.id),
                        payload=dict(rec.payload or {}),
                        score=None,
                    )
                )
                if len(collected) >= limit:
                    break
            if next_offset is None:
                break
        return tuple(collected)

    async def find_by_called_symbols(
        self,
        called_symbols: Sequence[str],
        *,
        repo_full_name: str | None = None,
        repo_id: str | None = None,
        limit: int = 50,
    ) -> tuple[QdrantStoredPoint, ...]:
        """Exact keyword lookup returning code chunks that call any of `called_symbols`."""
        return await self._scroll_code_by_keyword(
            field_name="called_symbols",
            values=called_symbols,
            repo_full_name=repo_full_name,
            repo_id=repo_id,
            limit=limit,
        )

    async def find_by_symbol_names(
        self,
        symbol_names: Sequence[str],
        *,
        repo_full_name: str | None = None,
        repo_id: str | None = None,
        limit: int = 50,
    ) -> tuple[QdrantStoredPoint, ...]:
        """Exact keyword lookup returning definitions matching any of `symbol_names`."""
        return await self._scroll_code_by_keyword(
            field_name="symbol_name",
            values=symbol_names,
            repo_full_name=repo_full_name,
            repo_id=repo_id,
            limit=limit,
        )

    async def search_similar(
        self,
        query_vector: Sequence[float],
        *,
        repo_full_name: str | None = None,
        repo_id: str | None = None,
        chunk_type: ChunkType | None = None,
        score_threshold: float | None = None,
        limit: int = 10,
    ) -> tuple[QdrantStoredPoint, ...]:
        """Dense cosine similarity search over 768-dim vectors scoped strictly to a repository."""
        validate_positive_int(limit, name="limit")
        must_conditions = self._build_scope_conditions(
            repo_full_name=repo_full_name,
            repo_id=repo_id,
        )
        normalized_vec = normalize_l2_vector(query_vector, expected_dim=EMBEDDING_DIMENSIONS)

        if chunk_type is not None:
            if chunk_type not in ALLOWED_CHUNK_TYPES:
                raise ValueError(f"Unsupported chunk_type '{chunk_type}'.")
            must_conditions.append(
                models.FieldCondition(
                    key="chunk_type",
                    match=models.MatchValue(value=chunk_type),
                )
            )

        query_filter = models.Filter(must=must_conditions)
        try:
            response = await self._client.query_points(
                collection_name=self._collection_name,
                query=list(normalized_vec),
                query_filter=query_filter,
                score_threshold=score_threshold,
                limit=limit,
                with_payload=True,
                with_vectors=False,
            )
        except Exception as exc:
            if _is_missing_collection_error(exc):
                return ()
            raise
        return tuple(
            QdrantStoredPoint(
                point_id=str(pt.id),
                payload=dict(pt.payload or {}),
                score=float(pt.score),
            )
            for pt in response.points
        )

    async def delete_by_file_paths(
        self,
        file_paths: Sequence[str],
        *,
        repo_full_name: str | None = None,
        repo_id: str | None = None,
        wait: bool = True,
    ) -> None:
        """Delete all indexed points for `file_paths` scoped strictly to one repository."""
        scope_conditions = self._build_scope_conditions(
            repo_full_name=repo_full_name,
            repo_id=repo_id,
        )
        norm_paths = _dedupe_non_empty_strings(
            [normalize_diff_path(path).rstrip("/") for path in file_paths]
        )
        if not norm_paths:
            return

        selector = models.FilterSelector(
            filter=models.Filter(
                must=[
                    *scope_conditions,
                    models.FieldCondition(
                        key="file_path",
                        match=models.MatchAny(any=norm_paths),
                    ),
                ]
            )
        )
        try:
            await self._client.delete(
                collection_name=self._collection_name,
                points_selector=selector,
                wait=wait,
            )
        except Exception as exc:
            if _is_missing_collection_error(exc):
                return
            raise

    async def delete_by_repo(
        self,
        *,
        repo_full_name: str | None = None,
        repo_id: str | None = None,
        wait: bool = True,
    ) -> None:
        """Delete all indexed points belonging to a single repository."""
        scope_conditions = self._build_scope_conditions(
            repo_full_name=repo_full_name,
            repo_id=repo_id,
        )
        selector = models.FilterSelector(filter=models.Filter(must=scope_conditions))
        try:
            await self._client.delete(
                collection_name=self._collection_name,
                points_selector=selector,
                wait=wait,
            )
        except Exception as exc:
            if _is_missing_collection_error(exc):
                return
            raise
