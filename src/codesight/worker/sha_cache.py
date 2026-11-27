"""Redis Blob SHA Cache (`file_review:{repo_id}:{blob_sha}:{config_hash}`, 7d TTL, NFR-3)."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final

import redis.asyncio as redis_async
import redis.exceptions
import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from codesight.agents.schemas import VerifiedFinding
from codesight.diff.file_filter import estimate_tokens, normalize_diff_path
from codesight.diff.models import FilterMetrics, ParsedFileDiff, ParsedPullRequestDiff

DEFAULT_SHA_CACHE_TTL_SECONDS: Final[int] = 604_800  # 7 days
SHA_CACHE_KEY_PREFIX: Final[str] = "file_review"
_FULL_BLOB_SHA_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?$")

logger = structlog.get_logger("codesight.worker.sha_cache")


def is_full_blob_sha(raw: str | None) -> bool:
    """Return True if `raw` is a full 40-hex SHA-1 or 64-hex SHA-256 Git blob SHA."""
    if not isinstance(raw, str):
        return False
    return _FULL_BLOB_SHA_RE.fullmatch(raw.strip()) is not None


def compute_patch_fingerprint(parsed_file: ParsedFileDiff) -> str:
    """Compute a zero-code SHA-256 fingerprint over hunk coordinates and diff lines."""
    hasher = hashlib.sha256()
    hasher.update(parsed_file.change_type.encode("utf-8"))
    for hunk in parsed_file.hunks:
        header = f"|{hunk.old_start},{hunk.old_count},{hunk.new_start},{hunk.new_count}\n"
        hasher.update(header.encode("utf-8"))
        for line in hunk.lines:
            row = f"{line.kind}:{line.left_line}:{line.right_line}:{line.content}\n"
            hasher.update(row.encode("utf-8"))
    return hasher.hexdigest()[:32]


def _validate_key_segment(raw: str, *, name: str) -> str:
    cleaned = raw.strip()
    if not cleaned:
        raise ValueError(f"{name} must not be empty.")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 or ch == ":" for ch in cleaned):
        raise ValueError(f"{name} contains invalid cache key characters.")
    return cleaned


def build_sha_cache_key(repo_id: str, blob_sha: str, config_hash: str) -> str:
    """Build deterministic Redis SHA cache key `file_review:{repo_id}:{blob_sha}:{config_hash}`."""
    clean_repo = _validate_key_segment(repo_id, name="repo_id")
    clean_sha = _validate_key_segment(blob_sha, name="blob_sha").lower()
    clean_cfg = _validate_key_segment(config_hash, name="config_hash").lower()
    return f"{SHA_CACHE_KEY_PREFIX}:{clean_repo}:{clean_sha}:{clean_cfg}"


class CachedFileReview(BaseModel):
    """NFR-3 compliant file-level review result cached in Redis (zero raw source/diff)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_path: str = Field(min_length=1)
    blob_sha: str = Field(min_length=1)
    config_hash: str = Field(min_length=1)
    patch_fingerprint: str | None = None
    findings: tuple[VerifiedFinding, ...] = ()
    net_delta_cc: int = 0
    high_cc_count: int = Field(default=0, ge=0)
    cached_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z")
    )


@dataclass(frozen=True, slots=True)
class SHACacheLookupResult:
    """Batch Redis MGET lookup outcome partitioned into cache hits and uncached files."""

    hits: dict[str, CachedFileReview]
    misses: tuple[ParsedFileDiff, ...]
    elapsed_ms: float
    degraded: bool = False

    @property
    def hit_count(self) -> int:
        """Return number of PR files served directly from Redis SHA cache."""
        return len(self.hits)

    @property
    def miss_count(self) -> int:
        """Return number of PR files requiring fresh AI review."""
        return len(self.misses)


def _revalidate_cached_findings(
    parsed_file: ParsedFileDiff,
    cached_findings: Sequence[VerifiedFinding],
) -> tuple[VerifiedFinding, ...]:
    """Re-bind cached findings to current path and strip stale suggestions outside hunks."""
    norm_path = normalize_diff_path(parsed_file.file_path).rstrip("/") or parsed_file.file_path
    reconciled: list[VerifiedFinding] = []
    for finding in cached_findings:
        updates: dict[str, Any] = {}
        if normalize_diff_path(finding.file_path).rstrip("/") != norm_path:
            updates["file_path"] = norm_path
        if finding.suggestion is not None:
            s_start = finding.suggestion.start_line
            s_end = finding.suggestion.end_line
            in_hunk = (
                parsed_file.is_line_commentable(s_end)
                if s_start == s_end
                else parsed_file.is_range_commentable(s_start, s_end)
            )
            if not in_hunk:
                updates["suggestion"] = None
                updates["suggestion_stripped_reason"] = "cached_outside_current_hunk"
                if not finding.suggestion_explanation and finding.suggestion.explanation:
                    updates["suggestion_explanation"] = finding.suggestion.explanation
        reconciled.append(finding.model_copy(update=updates) if updates else finding)
    return tuple(reconciled)


class RedisBlobSHACache:
    """Fail-open Redis cache keyed by `(repo_id, blob_sha, config_hash)` with 7-day TTL."""

    def __init__(
        self,
        redis_client: redis_async.Redis,
        *,
        ttl_seconds: int = DEFAULT_SHA_CACHE_TTL_SECONDS,
    ) -> None:
        if isinstance(ttl_seconds, bool) or ttl_seconds < 1:
            raise ValueError("ttl_seconds must be a positive integer.")
        self._redis = redis_client
        self._ttl_seconds = ttl_seconds

    async def get_many(
        self,
        *,
        repo_id: str,
        config_hash: str,
        files: Sequence[ParsedFileDiff],
    ) -> SHACacheLookupResult:
        """Fetch cached file reviews in a single Redis MGET round-trip (< 10ms typical)."""
        started = time.perf_counter()
        if not files:
            return SHACacheLookupResult(hits={}, misses=(), elapsed_ms=0.0)

        lookup_targets: list[tuple[int, ParsedFileDiff, str]] = []
        for idx, pf in enumerate(files):
            if (
                pf.change_type == "deleted"
                or not pf.hunks
                or not pf.annotated_diff.strip()
                or not is_full_blob_sha(pf.blob_sha)
            ):
                continue
            try:
                assert pf.blob_sha is not None
                key = build_sha_cache_key(repo_id, pf.blob_sha, config_hash)
            except ValueError:
                continue
            lookup_targets.append((idx, pf, key))

        if not lookup_targets:
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
            return SHACacheLookupResult(hits={}, misses=tuple(files), elapsed_ms=elapsed_ms)

        try:
            raw_values = await self._redis.mget([key for _, _, key in lookup_targets])
        except (redis.exceptions.RedisError, TimeoutError, OSError) as exc:
            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
            logger.warning(
                "sha_cache_read_degraded",
                repo_id=repo_id,
                error_type=type(exc).__name__,
                elapsed_ms=elapsed_ms,
            )
            return SHACacheLookupResult(
                hits={},
                misses=tuple(files),
                elapsed_ms=elapsed_ms,
                degraded=True,
            )

        hits_by_idx: dict[int, CachedFileReview] = {}
        for (idx, pf, _), raw_val in zip(lookup_targets, raw_values, strict=True):
            if raw_val is None:
                continue
            raw_text = (
                raw_val.decode("utf-8", errors="replace")
                if isinstance(raw_val, bytes)
                else str(raw_val)
            )
            try:
                entry = CachedFileReview.model_validate_json(raw_text)
            except (ValidationError, ValueError) as exc:
                logger.warning(
                    "sha_cache_corrupt_entry_ignored",
                    repo_id=repo_id,
                    file_path=pf.file_path,
                    error_type=type(exc).__name__,
                )
                continue

            expected_sha = (pf.blob_sha or "").strip().lower()
            if (
                entry.blob_sha.strip().lower() != expected_sha
                or entry.config_hash.lower() != config_hash.strip().lower()
            ):
                continue
            if entry.patch_fingerprint is not None and (
                entry.patch_fingerprint != compute_patch_fingerprint(pf)
            ):
                continue

            norm_path = normalize_diff_path(pf.file_path).rstrip("/") or pf.file_path
            revalidated_findings = _revalidate_cached_findings(pf, entry.findings)
            if entry.file_path != norm_path or revalidated_findings != entry.findings:
                entry = entry.model_copy(
                    update={"file_path": norm_path, "findings": revalidated_findings}
                )
            hits_by_idx[idx] = entry

        hits: dict[str, CachedFileReview] = {}
        misses: list[ParsedFileDiff] = []
        for idx, pf in enumerate(files):
            hit = hits_by_idx.get(idx)
            if hit is not None:
                norm_path = normalize_diff_path(pf.file_path).rstrip("/") or pf.file_path
                hits[norm_path] = hit
            else:
                misses.append(pf)

        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
        logger.info(
            "sha_cache_lookup",
            repo_id=repo_id,
            hit_count=len(hits),
            miss_count=len(misses),
            elapsed_ms=elapsed_ms,
        )
        return SHACacheLookupResult(
            hits=hits,
            misses=tuple(misses),
            elapsed_ms=elapsed_ms,
        )

    async def set_many(
        self,
        *,
        repo_id: str,
        config_hash: str,
        entries: Sequence[CachedFileReview],
    ) -> int:
        """Persist file-level review caches via a single Redis pipeline SETEX batch."""
        if not entries:
            return 0

        pairs: list[tuple[str, str]] = []
        for entry in entries:
            if not is_full_blob_sha(entry.blob_sha):
                continue
            try:
                key = build_sha_cache_key(repo_id, entry.blob_sha, config_hash)
            except ValueError:
                continue
            pairs.append((key, entry.model_dump_json()))

        if not pairs:
            return 0

        try:
            async with self._redis.pipeline(transaction=False) as pipe:
                for key, payload_json in pairs:
                    pipe.setex(key, self._ttl_seconds, payload_json)
                await pipe.execute()
            return len(pairs)
        except (redis.exceptions.RedisError, TimeoutError, OSError) as exc:
            logger.warning(
                "sha_cache_write_degraded",
                repo_id=repo_id,
                entry_count=len(pairs),
                error_type=type(exc).__name__,
            )
            return 0


def build_reduced_parsed_diff(
    parsed_diff: ParsedPullRequestDiff,
    uncached_files: Sequence[ParsedFileDiff],
) -> ParsedPullRequestDiff:
    """Build a reduced ParsedPullRequestDiff containing only uncached files for LLM review."""
    total_kept_annotated = sum(
        estimate_tokens(pf.annotated_diff) for pf in parsed_diff.files if pf.annotated_diff
    )
    uncached_annotated = sum(
        estimate_tokens(pf.annotated_diff) for pf in uncached_files if pf.annotated_diff
    )
    kept_tokens = (
        round(
            parsed_diff.metrics.kept_estimated_tokens * (uncached_annotated / total_kept_annotated)
        )
        if total_kept_annotated > 0
        else 0
    )
    annotated_diff = "\n\n".join(pf.annotated_diff for pf in uncached_files if pf.annotated_diff)
    raw_tokens = parsed_diff.metrics.raw_estimated_tokens
    saved_tokens = max(0, raw_tokens - kept_tokens)
    tokens_saved_pct = round((saved_tokens / raw_tokens) * 100.0, 2) if raw_tokens > 0 else 0.0
    reduced_metrics = FilterMetrics(
        total_files=parsed_diff.metrics.total_files,
        kept_files=len(uncached_files),
        skipped_files=max(0, parsed_diff.metrics.total_files - len(uncached_files)),
        skipped_by_reason=dict(parsed_diff.metrics.skipped_by_reason),
        raw_lines=parsed_diff.metrics.raw_lines,
        kept_lines=parsed_diff.metrics.kept_lines,
        skipped_lines=parsed_diff.metrics.skipped_lines,
        raw_bytes=parsed_diff.metrics.raw_bytes,
        kept_bytes=parsed_diff.metrics.kept_bytes,
        skipped_bytes=parsed_diff.metrics.skipped_bytes,
        raw_estimated_tokens=raw_tokens,
        kept_estimated_tokens=kept_tokens,
        saved_estimated_tokens=saved_tokens,
        tokens_saved_pct=tokens_saved_pct,
    )
    return ParsedPullRequestDiff(
        files=tuple(uncached_files),
        skipped_files=parsed_diff.skipped_files,
        metrics=reduced_metrics,
        annotated_diff=annotated_diff,
    )


def compute_combined_tokens_saved_pct(
    parsed_diff: ParsedPullRequestDiff,
    *,
    cached_files: Sequence[ParsedFileDiff],
) -> float:
    """Compute combined token reduction across noise filtering and Redis SHA cache hits."""
    raw_tokens = parsed_diff.metrics.raw_estimated_tokens
    if raw_tokens <= 0:
        return 0.0
    total_kept_annotated = sum(
        estimate_tokens(pf.annotated_diff) for pf in parsed_diff.files if pf.annotated_diff
    )
    cached_annotated = sum(
        estimate_tokens(pf.annotated_diff) for pf in cached_files if pf.annotated_diff
    )
    cached_raw_share = (
        parsed_diff.metrics.kept_estimated_tokens * (cached_annotated / total_kept_annotated)
        if total_kept_annotated > 0
        else 0.0
    )
    total_saved = min(
        float(raw_tokens),
        float(parsed_diff.metrics.saved_estimated_tokens) + cached_raw_share,
    )
    return round((total_saved / raw_tokens) * 100.0, 2)
