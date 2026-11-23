"""Qdrant gRPC Zero-Code Persistence store enforcing NFR-3 invariants and keyword indexes."""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class QdrantPointRef:
    """Lightweight NFR-3 metadata reference for repository reconciliation (zero vectors/code)."""

    point_id: str
    file_path: str
    blob_sha: str
    repo_full_name: str | None = None
    repo_id: str | None = None


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
        self._created_collection = False

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

    @property
    def created_collection(self) -> bool:
        """Return True iff `ensure_collection()` created this collection on the server."""
        return self._created_collection

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
                self._created_collection = True
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
        repo_full_name: str | None = None,
        repo_id: str | None = None,
        batch_size: int = DEFAULT_RETRIEVE_BATCH_SIZE,
    ) -> set[str]:
        """Batch-check which UUIDv5 point IDs already exist in Qdrant (optionally tenant-scoped)."""
        validate_positive_int(batch_size, name="batch_size")
        target_name: str | None = None
        if repo_full_name is not None:
            target_name = repo_full_name.strip()
            if not target_name:
                raise ValueError("repo_full_name must be a non-empty string when provided.")
        target_id: str | None = None
        if repo_id is not None:
            target_id = repo_id.strip()
            if not target_id:
                raise ValueError("repo_id must be a non-empty string when provided.")
        if not point_ids:
            return set()

        canonical_ids: list[str] = []
        seen: set[str] = set()
        for raw_id in point_ids:
            cid = validate_uuid_point_id(raw_id)
            if cid not in seen:
                seen.add(cid)
                canonical_ids.append(cid)

        check_tenant = target_name is not None or target_id is not None
        with_payload: Any = ["repo_full_name", "repo_id"] if check_tenant else False
        existing: set[str] = set()
        for idx in range(0, len(canonical_ids), batch_size):
            batch = canonical_ids[idx : idx + batch_size]
            try:
                records = await self._client.retrieve(
                    collection_name=self._collection_name,
                    ids=batch,
                    with_payload=with_payload,
                    with_vectors=False,
                )
            except Exception as exc:
                if _is_missing_collection_error(exc):
                    return set()
                raise
            for rec in records:
                if check_tenant:
                    payload = rec.payload or {}
                    if target_name is not None and payload.get("repo_full_name") != target_name:
                        continue
                    if target_id is not None and payload.get("repo_id") != target_id:
                        continue
                existing.add(str(rec.id))
        return existing

    async def upsert_points(
        self,
        points: Sequence[QdrantZeroCodePoint],
        *,
        batch_size: int = DEFAULT_UPSERT_BATCH_SIZE,
        wait: bool = True,
    ) -> int:
        """Validate 100% of points against NFR-3 preflight, then upsert via file-aligned batches."""
        validate_positive_int(batch_size, name="batch_size")
        if not points:
            return 0

        # All-or-nothing preflight guard: reject before any partial write reaches Qdrant
        grouped_by_file: dict[str, list[models.PointStruct]] = {}
        total_structs = 0
        for point in points:
            canonical_id, vec_list, clean_payload = validate_zero_code_point(point)
            file_key = str(clean_payload["file_path"])
            grouped_by_file.setdefault(file_key, []).append(
                models.PointStruct(
                    id=canonical_id,
                    vector=vec_list,
                    payload=clean_payload,
                )
            )
            total_structs += 1

        batches: list[list[models.PointStruct]] = []
        current_batch: list[models.PointStruct] = []
        for file_group in grouped_by_file.values():
            if len(file_group) > batch_size:
                if current_batch:
                    batches.append(current_batch)
                    current_batch = []
                for idx in range(0, len(file_group), batch_size):
                    batches.append(file_group[idx : idx + batch_size])
                continue
            if current_batch and len(current_batch) + len(file_group) > batch_size:
                batches.append(current_batch)
                current_batch = []
            current_batch.extend(file_group)
        if current_batch:
            batches.append(current_batch)

        started = time.perf_counter()
        for batch in batches:
            await self._client.upsert(
                collection_name=self._collection_name,
                points=batch,
                wait=wait,
            )

        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        logger.info(
            "qdrant_points_upserted",
            collection=self._collection_name,
            count=total_structs,
            latency_ms=elapsed_ms,
        )
        return total_structs

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

    @classmethod
    def _build_scope_filter(
        cls,
        *,
        repo_full_name: str | None = None,
        repo_id: str | None = None,
        match_any_scope: bool = False,
    ) -> models.Filter:
        conditions = cls._build_scope_conditions(
            repo_full_name=repo_full_name,
            repo_id=repo_id,
        )
        if match_any_scope and len(conditions) > 1:
            return models.Filter(should=conditions)
        return models.Filter(must=conditions)

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

    async def list_repo_point_refs(
        self,
        *,
        repo_full_name: str | None = None,
        repo_id: str | None = None,
        match_any_scope: bool = False,
        page_size: int = DEFAULT_SCROLL_PAGE_SIZE,
    ) -> tuple[QdrantPointRef, ...]:
        """Scroll lightweight `(point_id, file_path, blob_sha)` refs scoped to one repository."""
        validate_positive_int(page_size, name="page_size")
        scroll_filter = self._build_scope_filter(
            repo_full_name=repo_full_name,
            repo_id=repo_id,
            match_any_scope=match_any_scope,
        )
        refs: list[QdrantPointRef] = []
        next_offset: Any = None

        while True:
            try:
                records, next_offset = await self._client.scroll(
                    collection_name=self._collection_name,
                    scroll_filter=scroll_filter,
                    limit=page_size,
                    offset=next_offset,
                    with_payload=["file_path", "blob_sha", "repo_full_name", "repo_id"],
                    with_vectors=False,
                )
            except Exception as exc:
                if _is_missing_collection_error(exc):
                    return ()
                raise
            if not records:
                break
            for rec in records:
                payload = rec.payload or {}
                raw_path = payload.get("file_path")
                raw_sha = payload.get("blob_sha")
                raw_repo_name = payload.get("repo_full_name")
                raw_repo_id = payload.get("repo_id")
                if isinstance(raw_path, str) and isinstance(raw_sha, str):
                    refs.append(
                        QdrantPointRef(
                            point_id=str(rec.id),
                            file_path=normalize_diff_path(raw_path).rstrip("/"),
                            blob_sha=raw_sha.strip(),
                            repo_full_name=(
                                raw_repo_name.strip() if isinstance(raw_repo_name, str) else None
                            ),
                            repo_id=raw_repo_id.strip() if isinstance(raw_repo_id, str) else None,
                        )
                    )
            if next_offset is None:
                break

        return tuple(refs)

    async def has_legacy_slug_points(
        self,
        *,
        repo_id: str,
        expected_repo_full_name: str,
    ) -> bool:
        """Return True when Qdrant holds points for `repo_id` with a different repo_full_name."""
        cleaned_id = repo_id.strip()
        cleaned_name = expected_repo_full_name.strip()
        if not cleaned_id or not cleaned_name:
            return False
        scroll_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="repo_id",
                    match=models.MatchValue(value=cleaned_id),
                )
            ],
            must_not=[
                models.FieldCondition(
                    key="repo_full_name",
                    match=models.MatchValue(value=cleaned_name),
                )
            ],
        )
        try:
            records, _ = await self._client.scroll(
                collection_name=self._collection_name,
                scroll_filter=scroll_filter,
                limit=1,
                with_payload=False,
                with_vectors=False,
            )
        except Exception as exc:
            if _is_missing_collection_error(exc):
                return False
            raise
        return bool(records)

    async def delete_points_by_ids(
        self,
        point_ids: Sequence[str],
        *,
        repo_full_name: str | None = None,
        repo_id: str | None = None,
        match_any_scope: bool = False,
        batch_size: int = DEFAULT_RETRIEVE_BATCH_SIZE,
        wait: bool = True,
    ) -> int:
        """Delete specific UUID points scoped strictly to one repository tenant."""
        validate_positive_int(batch_size, name="batch_size")
        scope_conditions = self._build_scope_conditions(
            repo_full_name=repo_full_name,
            repo_id=repo_id,
        )
        scope_clauses: list[models.Condition] = (
            [models.Filter(should=scope_conditions)]
            if match_any_scope and len(scope_conditions) > 1
            else scope_conditions
        )
        canonical_ids: list[str] = []
        seen: set[str] = set()
        for raw_id in point_ids:
            cid = validate_uuid_point_id(raw_id)
            if cid not in seen:
                seen.add(cid)
                canonical_ids.append(cid)
        if not canonical_ids:
            return 0

        for idx in range(0, len(canonical_ids), batch_size):
            batch = canonical_ids[idx : idx + batch_size]
            selector = models.FilterSelector(
                filter=models.Filter(
                    must=[
                        *scope_clauses,
                        models.HasIdCondition(has_id=list(batch)),
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
                    return 0
                raise

        return len(canonical_ids)

    async def delete_by_file_paths(
        self,
        file_paths: Sequence[str],
        *,
        repo_full_name: str | None = None,
        repo_id: str | None = None,
        exclude_point_ids: Sequence[str] = (),
        wait: bool = True,
    ) -> None:
        """Delete indexed points for `file_paths` in one repo, optionally keeping specific IDs."""
        scope_conditions = self._build_scope_conditions(
            repo_full_name=repo_full_name,
            repo_id=repo_id,
        )
        norm_paths = _dedupe_non_empty_strings(
            [normalize_diff_path(path).rstrip("/") for path in file_paths]
        )
        if not norm_paths:
            return

        must_not: list[models.Condition] | None = None
        if exclude_point_ids:
            excluded_ids: list[str] = []
            seen_excluded: set[str] = set()
            for raw_id in exclude_point_ids:
                cid = validate_uuid_point_id(raw_id)
                if cid not in seen_excluded:
                    seen_excluded.add(cid)
                    excluded_ids.append(cid)
            if excluded_ids:
                must_not = [models.HasIdCondition(has_id=list(excluded_ids))]

        selector = models.FilterSelector(
            filter=models.Filter(
                must=[
                    *scope_conditions,
                    models.FieldCondition(
                        key="file_path",
                        match=models.MatchAny(any=norm_paths),
                    ),
                ],
                must_not=must_not,
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
        match_any_scope: bool = False,
        wait: bool = True,
    ) -> None:
        """Delete all indexed points belonging to a single repository."""
        scope_filter = self._build_scope_filter(
            repo_full_name=repo_full_name,
            repo_id=repo_id,
            match_any_scope=match_any_scope,
        )
        selector = models.FilterSelector(filter=scope_filter)
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
