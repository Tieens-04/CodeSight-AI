"""Concurrent RAM-only GitHub Blob fetcher with deduplication and NFR-3 buffer cleanup."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal, Self
from urllib.parse import quote

import httpx
import structlog

from codesight import __version__
from codesight.core.errors import (
    RateLimitTaskError,
    RetryableTaskError,
    is_rate_limited_403,
    parse_retry_after_header,
)
from codesight.diff.file_filter import estimate_tokens, normalize_diff_path
from codesight.vcs._http import (
    BoundedStreamResult,
    stream_same_origin_bounded,
    validate_positive_int,
)
from codesight.vcs.github_auth import (
    GITHUB_ACCEPT_HEADER,
    GITHUB_API_VERSION,
    GitHubAuthError,
    GitHubAuthProvider,
    validate_repo_full_name,
)

MAX_BLOB_FETCH_BYTES: Final[int] = 512 * 1024  # 512 KiB per blob ceiling
DEFAULT_BLOB_FETCH_CONCURRENCY: Final[int] = 16
_SAFE_BLOB_SHA_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

type SliceStatus = Literal["ok", "not_found", "oversize", "binary", "empty"]
type BlobDecodeStatus = Literal["ok", "oversize", "binary", "invalid"]

logger = structlog.get_logger("codesight.vcs.blob_fetcher")


def validate_safe_repo_path(raw_path: str) -> str:
    """Normalize a repository-relative POSIX path and block control chars or dot-segments."""
    if not isinstance(raw_path, str):
        raise ValueError("file_path must be a non-empty string.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw_path):
        raise ValueError("file_path must not contain NUL or control characters.")
    norm_path = normalize_diff_path(raw_path).rstrip("/")
    if not norm_path:
        raise ValueError("file_path must normalize to a non-empty POSIX path.")
    if any(seg in {"", ".", ".."} for seg in norm_path.split("/")):
        raise ValueError("file_path must not contain dot-segments ('.' or '..').")
    return norm_path


def validate_safe_blob_sha(raw_sha: str) -> str:
    """Validate that a Git blob SHA / locator token cannot inject URL path segments."""
    if not isinstance(raw_sha, str):
        raise ValueError("blob_sha must be a string.")
    cleaned = raw_sha.strip()
    if not _SAFE_BLOB_SHA_RE.fullmatch(cleaned):
        raise ValueError("blob_sha contains unsafe characters for GitHub API path.")
    return cleaned


def _is_github_too_large_response(status_code: int, body_bytes: bytes | None) -> bool:
    """Return True when GitHub Contents/Blobs API signals a >1 MiB 'too_large' error."""
    if status_code not in {403, 422} or not body_bytes:
        return False
    try:
        payload = json.loads(body_bytes)
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    errors = payload.get("errors")
    if isinstance(errors, list):
        for item in errors:
            if (
                isinstance(item, dict)
                and str(item.get("code") or "").strip().lower() == "too_large"
            ):
                return True
    message = payload.get("message")
    if isinstance(message, str):
        lower_msg = message.lower()
        if "too large" in lower_msg or "blobs up to 1 mb" in lower_msg:
            return True
    return False


class GitHubBlobFetchError(RuntimeError):
    """Raised when a GitHub Blob/Contents fetch encounters a permanent protocol error."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _make_blob_fetch_error(message: str, status_code: int | None) -> GitHubBlobFetchError:
    return GitHubBlobFetchError(message, status_code=status_code)


def _public_github_headers() -> dict[str, str]:
    return {
        "Accept": GITHUB_ACCEPT_HEADER,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": f"codesight-ai/{__version__}",
    }


def _raise_for_github_status(
    status_code: int,
    headers: httpx.Headers,
    *,
    slug: str,
    context: str,
) -> None:
    if status_code == 200:
        return
    if status_code == 429 or (status_code == 403 and is_rate_limited_403(headers)):
        delay = parse_retry_after_header(headers)
        logger.warning(
            "github_blob_fetcher_rate_limited",
            repo=slug,
            status_code=status_code,
            retry_after_seconds=delay,
        )
        raise RateLimitTaskError(
            f"GitHub rate limit ({status_code}) while fetching {context}.",
            retry_after_seconds=delay,
        )
    if status_code in {401, 403}:
        logger.error("github_blob_fetcher_auth_error", repo=slug, status_code=status_code)
        raise GitHubAuthError(
            f"GitHub API returned HTTP {status_code} while fetching {context}.",
            status_code=status_code,
        )
    if 500 <= status_code < 600:
        logger.warning("github_blob_fetcher_server_error", repo=slug, status_code=status_code)
        raise RetryableTaskError(
            f"GitHub API returned HTTP {status_code} while fetching {context}."
        )
    logger.error("github_blob_fetcher_http_error", repo=slug, status_code=status_code)
    raise GitHubBlobFetchError(
        f"GitHub API rejected request for {context} with HTTP {status_code}.",
        status_code=status_code,
    )


def decode_github_base64_blob(
    body_bytes: bytes,
    *,
    max_bytes: int = MAX_BLOB_FETCH_BYTES,
) -> tuple[bytes | None, BlobDecodeStatus, str | None]:
    """Decode a GitHub Git Blobs or Contents API JSON envelope in RAM with byte/binary guards."""
    try:
        payload = json.loads(body_bytes)
    except ValueError:
        return None, "invalid", None
    if not isinstance(payload, dict):
        return None, "invalid", None

    raw_type = payload.get("type")
    if raw_type is not None and raw_type != "file":
        return None, "invalid", None

    raw_sha = payload.get("sha")
    resolved_sha = raw_sha.strip() if isinstance(raw_sha, str) and raw_sha.strip() else None

    declared_size = payload.get("size")
    if (
        isinstance(declared_size, int)
        and not isinstance(declared_size, bool)
        and declared_size > max_bytes
    ):
        return None, "oversize", resolved_sha

    encoding = payload.get("encoding")
    raw_content = payload.get("content")
    if encoding != "base64" or not isinstance(raw_content, str):
        return None, "invalid", resolved_sha

    compact = "".join(raw_content.split())
    try:
        decoded = base64.b64decode(compact.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        return None, "invalid", resolved_sha

    if len(decoded) > max_bytes:
        return None, "oversize", resolved_sha
    if b"\x00" in decoded:
        return None, "binary", resolved_sha
    return decoded, "ok", resolved_sha


def _split_lines_strict(decoded_bytes: bytes) -> list[str]:
    if not decoded_bytes:
        return []
    text = decoded_bytes.decode("utf-8", errors="replace")
    if text.startswith("\ufeff"):
        text = text[1:]
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if not normalized:
        return []
    lines = normalized.split("\n")
    if normalized.endswith("\n") and lines:
        lines.pop()
    return lines


def slice_decoded_lines(
    lines: Sequence[str],
    *,
    start_line: int,
    end_line: int,
) -> tuple[int, int, str, str]:
    """Slice 1-based inclusive `[start_line..end_line]` with safe bounds and `Lxxx` labels."""
    total = len(lines)
    if total == 0 or start_line > total:
        return 0, 0, "", ""
    eff_start = max(1, start_line)
    eff_end = min(max(eff_start, end_line), total)
    selected = lines[eff_start - 1 : eff_end]
    raw_content = "\n".join(selected)
    # Preserve the trailing newline when the slice ends on an empty line so line-preserving
    # consumers (e.g. splice_source_lines) count the exact same number of lines as Git.
    if selected and selected[-1] == "":
        raw_content = f"{raw_content}\n"
    annotated = "\n".join(f"L{idx}: {line}" for idx, line in enumerate(selected, start=eff_start))
    return eff_start, eff_end, raw_content, annotated


@dataclass(frozen=True, slots=True)
class BlobSliceRequest:
    """Target 1-based inclusive line slice to load transiently into RAM."""

    repo_full_name: str
    file_path: str
    start_line: int
    end_line: int
    blob_sha: str | None = None
    ref: str | None = None
    slice_id: str | None = None
    prefer_in_memory: bool = False

    def __post_init__(self) -> None:
        validate_repo_full_name(self.repo_full_name)
        validate_safe_repo_path(self.file_path)
        if isinstance(self.start_line, bool) or not isinstance(self.start_line, int):
            raise ValueError("start_line must be a positive integer.")
        if isinstance(self.end_line, bool) or not isinstance(self.end_line, int):
            raise ValueError("end_line must be a positive integer.")
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ValueError("start_line and end_line must satisfy 1 <= start_line <= end_line.")
        has_sha = isinstance(self.blob_sha, str) and bool(self.blob_sha.strip())
        if has_sha and self.blob_sha is not None:
            validate_safe_blob_sha(self.blob_sha)
        has_ref = isinstance(self.ref, str) and bool(self.ref.strip())
        if not (has_sha or has_ref or self.prefer_in_memory):
            raise ValueError("BlobSliceRequest requires blob_sha, ref, or prefer_in_memory=True.")


@dataclass(frozen=True, slots=True)
class BlobSliceResult:
    """Bounded RAM slice extracted from a GitHub blob with full-file token metadata only."""

    request: BlobSliceRequest
    status: SliceStatus
    resolved_blob_sha: str | None
    start_line: int
    end_line: int
    total_file_lines: int
    full_file_tokens: int
    content: str
    annotated_content: str


@dataclass(frozen=True, slots=True)
class BlobBatchFetchReport:
    """Batch telemetry and ordered slice results from GitHubBlobFetcher.fetch_slices."""

    slices: tuple[BlobSliceResult, ...]
    unique_blobs_requested: int
    unique_blobs_fetched: int
    skipped_blobs: int
    full_file_tokens_by_path: dict[str, int]
    elapsed_ms: float


type _BlobFetchKey = tuple[Literal["blob"], str, str] | tuple[Literal["contents"], str, str, str]


class GitHubBlobFetcher:
    """Async HTTP/2 RAM blob fetcher deduplicating `(repo, blob_sha)` and freeing buffers."""

    def __init__(
        self,
        auth_provider: GitHubAuthProvider | None,
        *,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.github.com",
        timeout_seconds: float = 10.0,
        max_concurrency: int = DEFAULT_BLOB_FETCH_CONCURRENCY,
        max_blob_bytes: int = MAX_BLOB_FETCH_BYTES,
        allow_public_fallback: bool = False,
    ) -> None:
        self._auth = auth_provider
        self._client = http_client
        self._owns_client = http_client is None
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_concurrency = validate_positive_int(max_concurrency, name="max_concurrency")
        self._max_blob_bytes = validate_positive_int(max_blob_bytes, name="max_blob_bytes")
        self._allow_public_fallback = allow_public_fallback

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is not None and not self._owns_client and self._client.is_closed:
            raise RuntimeError("Injected HTTP client has been closed externally.")
        if self._client is None or (self._owns_client and self._client.is_closed):
            self._client = httpx.AsyncClient(
                http2=True,
                timeout=self._timeout_seconds,
                follow_redirects=False,
                limits=httpx.Limits(max_connections=24, max_keepalive_connections=16),
            )
        return self._client

    async def __aenter__(self) -> Self:
        self._ensure_client()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close internally created HTTP/2 pool without touching externally injected clients."""
        if self._owns_client and self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def _resolve_headers(
        self,
        *,
        slug: str,
        installation_id: int | None = None,
    ) -> dict[str, str]:
        if self._auth is None:
            if self._allow_public_fallback:
                return _public_github_headers()
            raise GitHubAuthError("GitHub authentication provider is required.")
        try:
            headers = await self._auth.get_auth_headers(
                installation_id=installation_id,
                repo_full_name=slug,
            )
            return {**headers, "User-Agent": f"codesight-ai/{__version__}"}
        except GitHubAuthError:
            if self._allow_public_fallback:
                return _public_github_headers()
            raise

    @staticmethod
    def _build_skipped_result(
        request: BlobSliceRequest,
        *,
        status: SliceStatus,
        resolved_blob_sha: str | None = None,
        total_file_lines: int = 0,
        full_file_tokens: int = 0,
    ) -> BlobSliceResult:
        return BlobSliceResult(
            request=request,
            status=status,
            resolved_blob_sha=resolved_blob_sha or request.blob_sha,
            start_line=0,
            end_line=0,
            total_file_lines=total_file_lines,
            full_file_tokens=full_file_tokens,
            content="",
            annotated_content="",
        )

    @staticmethod
    def _slice_bytes_for_requests(
        decoded_bytes: bytes,
        indexed_requests: Sequence[tuple[int, BlobSliceRequest]],
        *,
        resolved_blob_sha: str | None,
    ) -> tuple[list[tuple[int, BlobSliceResult]], int]:
        """Slice requested line ranges from RAM bytes and immediately drop raw buffers."""
        full_tokens = estimate_tokens(decoded_bytes)
        raw_lines: list[str] | None = None
        results: list[tuple[int, BlobSliceResult]] = []
        try:
            raw_lines = _split_lines_strict(decoded_bytes)
            total_lines = len(raw_lines)
            if total_lines == 0:
                for orig_idx, req in indexed_requests:
                    results.append(
                        (
                            orig_idx,
                            GitHubBlobFetcher._build_skipped_result(
                                req,
                                status="empty",
                                resolved_blob_sha=resolved_blob_sha,
                                full_file_tokens=full_tokens,
                            ),
                        )
                    )
                return results, full_tokens

            for orig_idx, req in indexed_requests:
                eff_start, eff_end, content, annotated = slice_decoded_lines(
                    raw_lines,
                    start_line=req.start_line,
                    end_line=req.end_line,
                )
                if eff_start == 0:
                    results.append(
                        (
                            orig_idx,
                            GitHubBlobFetcher._build_skipped_result(
                                req,
                                status="not_found",
                                resolved_blob_sha=resolved_blob_sha,
                                total_file_lines=total_lines,
                                full_file_tokens=full_tokens,
                            ),
                        )
                    )
                    continue
                results.append(
                    (
                        orig_idx,
                        BlobSliceResult(
                            request=req,
                            status="ok",
                            resolved_blob_sha=resolved_blob_sha or req.blob_sha,
                            start_line=eff_start,
                            end_line=eff_end,
                            total_file_lines=total_lines,
                            full_file_tokens=full_tokens,
                            content=content,
                            annotated_content=annotated,
                        ),
                    )
                )
            return results, full_tokens
        finally:
            if raw_lines is not None:
                raw_lines.clear()

    async def _fetch_remote_group(
        self,
        key: _BlobFetchKey,
        indexed_requests: Sequence[tuple[int, BlobSliceRequest]],
        *,
        headers_by_slug: dict[str, dict[str, str]],
        refresh_lock: asyncio.Lock,
        installation_id: int | None,
        semaphore: asyncio.Semaphore,
    ) -> tuple[list[tuple[int, BlobSliceResult]], bool, dict[str, int]]:
        """Fetch one unique GitHub blob or Contents entry in RAM, slice, and wipe buffers."""
        if key[0] == "blob":
            _, slug, blob_sha = key
            safe_sha = validate_safe_blob_sha(blob_sha)
            encoded_sha = quote(safe_sha, safe="")
            url = f"{self._base_url}/repos/{slug}/git/blobs/{encoded_sha}"
            context = f"blob {safe_sha[:12]} in '{slug}'"
            fallback_sha: str | None = safe_sha
        else:
            _, slug, ref, norm_path = key
            safe_path = validate_safe_repo_path(norm_path)
            encoded_path = quote(safe_path, safe="/")
            encoded_ref = quote(ref, safe="")
            url = f"{self._base_url}/repos/{slug}/contents/{encoded_path}?ref={encoded_ref}"
            context = f"contents '{safe_path}@{ref[:12]}' in '{slug}'"
            fallback_sha = None

        max_envelope_bytes = int(self._max_blob_bytes * 1.5) + 16_384
        client = self._ensure_client()
        active_headers: dict[str, str] = headers_by_slug.get(slug, {})

        stream_res: BoundedStreamResult | None = None
        decoded_bytes: bytes | None = None
        stream_body: bytes | None = None
        status_code = 0
        resp_headers = httpx.Headers()
        exceeded_limit = False
        try:
            for attempt in range(2):
                async with semaphore:
                    try:
                        stream_res = await stream_same_origin_bounded(
                            client,
                            url,
                            headers=active_headers,
                            api_base=self._base_url,
                            max_bytes=max_envelope_bytes,
                            error_factory=_make_blob_fetch_error,
                            early_return_statuses=frozenset({404}),
                        )
                    except (httpx.TimeoutException, httpx.TransportError) as exc:
                        logger.warning(
                            "github_blob_fetcher_transport_error",
                            repo=slug,
                            error_type=type(exc).__name__,
                        )
                        raise RetryableTaskError(
                            f"Transient network failure fetching {context}."
                        ) from exc

                status_code = stream_res.status_code
                resp_headers = stream_res.headers
                exceeded_limit = stream_res.exceeded_limit
                stream_body = stream_res.body_bytes
                stream_res = None

                if status_code == 401 and installation_id is not None and self._auth is not None:
                    failed_auth = active_headers.get("Authorization")
                    async with refresh_lock:
                        latest_headers = headers_by_slug.get(slug)
                        if (
                            latest_headers is not None
                            and latest_headers.get("Authorization") != failed_auth
                        ):
                            active_headers = latest_headers
                        else:
                            await self._auth.invalidate_installation_token(installation_id)
                            if attempt == 0:
                                fresh_headers = await self._resolve_headers(
                                    slug=slug,
                                    installation_id=installation_id,
                                )
                                headers_by_slug[slug] = fresh_headers
                                active_headers = fresh_headers
                            else:
                                headers_by_slug.pop(slug, None)
                    if attempt == 0:
                        stream_body = None
                        continue
                break

            if status_code == 404:
                return (
                    [
                        (
                            idx,
                            self._build_skipped_result(
                                req, status="not_found", resolved_blob_sha=fallback_sha
                            ),
                        )
                        for idx, req in indexed_requests
                    ],
                    False,
                    {},
                )
            if _is_github_too_large_response(status_code, stream_body):
                logger.warning(
                    "github_blob_fetcher_contents_too_large",
                    repo=slug,
                    status_code=status_code,
                )
                return (
                    [
                        (
                            idx,
                            self._build_skipped_result(
                                req, status="oversize", resolved_blob_sha=fallback_sha
                            ),
                        )
                        for idx, req in indexed_requests
                    ],
                    False,
                    {},
                )
            if status_code != 200:
                stream_body = None
                _raise_for_github_status(
                    status_code,
                    resp_headers,
                    slug=slug,
                    context=context,
                )
            if exceeded_limit or stream_body is None:
                return (
                    [
                        (
                            idx,
                            self._build_skipped_result(
                                req, status="oversize", resolved_blob_sha=fallback_sha
                            ),
                        )
                        for idx, req in indexed_requests
                    ],
                    False,
                    {},
                )

            decoded_bytes, decode_status, resolved_sha = decode_github_base64_blob(
                stream_body,
                max_bytes=self._max_blob_bytes,
            )
            stream_body = None

            if decode_status != "ok" or decoded_bytes is None:
                mapped_status: SliceStatus = (
                    "oversize"
                    if decode_status == "oversize"
                    else ("binary" if decode_status == "binary" else "not_found")
                )
                return (
                    [
                        (
                            idx,
                            self._build_skipped_result(
                                req,
                                status=mapped_status,
                                resolved_blob_sha=resolved_sha or fallback_sha,
                            ),
                        )
                        for idx, req in indexed_requests
                    ],
                    False,
                    {},
                )

            sliced, full_tokens = self._slice_bytes_for_requests(
                decoded_bytes,
                indexed_requests,
                resolved_blob_sha=resolved_sha or fallback_sha,
            )
            tokens_by_path: dict[str, int] = {}
            for _, req in indexed_requests:
                norm = normalize_diff_path(req.file_path).rstrip("/")
                tokens_by_path[norm] = max(tokens_by_path.get(norm, 0), full_tokens)
            return sliced, True, tokens_by_path
        finally:
            stream_res = None
            decoded_bytes = None
            stream_body = None
            active_headers = {}

    async def fetch_slices(
        self,
        requests: Sequence[BlobSliceRequest],
        *,
        installation_id: int | None = None,
        in_memory_sources: Mapping[str, str | bytes] | None = None,
    ) -> BlobBatchFetchReport:
        """Deduplicate unique blobs, fetch concurrently into RAM, slice lines, and free buffers."""
        started = time.perf_counter()
        if not requests:
            return BlobBatchFetchReport(
                slices=(),
                unique_blobs_requested=0,
                unique_blobs_fetched=0,
                skipped_blobs=0,
                full_file_tokens_by_path={},
                elapsed_ms=0.0,
            )

        norm_mem_by_path: dict[str, bytes] = {}
        raw_mem_by_key: dict[str, bytes] = {}
        if in_memory_sources:
            for raw_k, raw_v in in_memory_sources.items():
                raw_bytes = raw_v.encode("utf-8") if isinstance(raw_v, str) else bytes(raw_v)
                raw_mem_by_key[raw_k.strip()] = raw_bytes
                norm_k = normalize_diff_path(raw_k).rstrip("/")
                if norm_k:
                    norm_mem_by_path[norm_k] = raw_bytes

        ordered_results: list[BlobSliceResult | None] = [None] * len(requests)
        full_tokens_by_path: dict[str, int] = {}
        remote_groups: dict[_BlobFetchKey, list[tuple[int, BlobSliceRequest]]] = {}
        local_groups: dict[str, list[tuple[int, BlobSliceRequest]]] = {}
        local_payloads: dict[str, bytes] = {}
        skipped_local = 0

        for idx, req in enumerate(requests):
            slug = validate_repo_full_name(req.repo_full_name)
            norm_path = validate_safe_repo_path(req.file_path)
            sha = (
                validate_safe_blob_sha(req.blob_sha)
                if isinstance(req.blob_sha, str) and req.blob_sha.strip()
                else None
            )
            ref = req.ref.strip() if isinstance(req.ref, str) and req.ref.strip() else None

            mem_bytes: bytes | None = None
            mem_key: str | None = None
            if req.prefer_in_memory and norm_path in norm_mem_by_path:
                mem_bytes = norm_mem_by_path[norm_path]
                mem_key = f"mem_path:{norm_path}"
            elif sha is not None and sha in raw_mem_by_key:
                mem_bytes = raw_mem_by_key[sha]
                mem_key = f"mem_sha:{sha}"

            if mem_bytes is not None and mem_key is not None:
                local_groups.setdefault(mem_key, []).append((idx, req))
                local_payloads[mem_key] = mem_bytes
            elif sha is not None:
                remote_groups.setdefault(("blob", slug, sha), []).append((idx, req))
            elif ref is not None:
                remote_groups.setdefault(("contents", slug, ref, norm_path), []).append((idx, req))
            else:
                ordered_results[idx] = self._build_skipped_result(req, status="not_found")
                skipped_local += 1

        try:
            # Resolve in-memory sources synchronously (0 network calls)
            for mem_key, group in local_groups.items():
                raw_bytes = local_payloads[mem_key]
                if len(raw_bytes) > self._max_blob_bytes:
                    for orig_idx, req in group:
                        ordered_results[orig_idx] = self._build_skipped_result(
                            req, status="oversize"
                        )
                    skipped_local += 1
                    continue
                if b"\x00" in raw_bytes:
                    for orig_idx, req in group:
                        ordered_results[orig_idx] = self._build_skipped_result(req, status="binary")
                    skipped_local += 1
                    continue
                sliced, full_tokens = self._slice_bytes_for_requests(
                    raw_bytes,
                    group,
                    resolved_blob_sha=(
                        group[0][1].blob_sha if mem_key.startswith("mem_sha:") else None
                    ),
                )
                for orig_idx, res in sliced:
                    ordered_results[orig_idx] = res
                    norm = normalize_diff_path(res.request.file_path).rstrip("/")
                    full_tokens_by_path[norm] = max(full_tokens_by_path.get(norm, 0), full_tokens)
        finally:
            norm_mem_by_path.clear()
            raw_mem_by_key.clear()
            local_payloads.clear()

        unique_fetched = 0
        skipped_remote = 0
        if remote_groups:
            unique_slugs = sorted({key[1] for key in remote_groups})
            headers_by_slug: dict[str, dict[str, str]] = {}
            refresh_lock = asyncio.Lock()
            try:
                for slug in unique_slugs:
                    headers_by_slug[slug] = await self._resolve_headers(
                        slug=slug,
                        installation_id=installation_id,
                    )

                semaphore = asyncio.Semaphore(self._max_concurrency)
                tasks = [
                    asyncio.create_task(
                        self._fetch_remote_group(
                            key,
                            group,
                            headers_by_slug=headers_by_slug,
                            refresh_lock=refresh_lock,
                            installation_id=installation_id,
                            semaphore=semaphore,
                        )
                    )
                    for key, group in remote_groups.items()
                ]
                try:
                    group_outputs = await asyncio.gather(*tasks)
                except BaseException:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
            finally:
                headers_by_slug.clear()

            for sliced_items, fetched_ok, path_tokens in group_outputs:
                if fetched_ok:
                    unique_fetched += 1
                else:
                    skipped_remote += 1
                for orig_idx, res in sliced_items:
                    ordered_results[orig_idx] = res
                for path_key, tok_count in path_tokens.items():
                    full_tokens_by_path[path_key] = max(
                        full_tokens_by_path.get(path_key, 0), tok_count
                    )

        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        final_slices = tuple(res for res in ordered_results if res is not None)
        logger.info(
            "github_blobs_sliced_in_ram",
            slices_requested=len(requests),
            unique_blobs_requested=len(remote_groups),
            unique_blobs_fetched=unique_fetched,
            skipped_blobs=skipped_remote + skipped_local,
            latency_ms=elapsed_ms,
        )
        return BlobBatchFetchReport(
            slices=final_slices,
            unique_blobs_requested=len(remote_groups),
            unique_blobs_fetched=unique_fetched,
            skipped_blobs=skipped_remote + skipped_local,
            full_file_tokens_by_path=full_tokens_by_path,
            elapsed_ms=elapsed_ms,
        )
