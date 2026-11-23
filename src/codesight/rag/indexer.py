"""Initial repository indexer and FR-07 incremental default-branch syncer (NFR-3 RAM-only)."""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from collections.abc import Set as AbstractSet
from contextlib import AsyncExitStack, asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal, Self, cast
from urllib.parse import quote

import httpx
import redis.asyncio as redis_async
import redis.exceptions
import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from codesight import __version__
from codesight.ast_engine.registry import ASTParserRegistry
from codesight.config_reader.repo_config import CONFIG_FILENAMES, RepoConfig, RepoConfigLoader
from codesight.core.errors import (
    RateLimitTaskError,
    RetryableTaskError,
    is_rate_limited_403,
    parse_retry_after_header,
)
from codesight.db.models import Repository
from codesight.db.repositories import _UNSET, upsert_repository_guarded
from codesight.diff.file_filter import FileFilter, normalize_diff_path
from codesight.rag.doc_chunker import MarkdownDocChunker, is_architecture_doc_path
from codesight.rag.embedder import ExistingPointIdChecker, GeminiEmbedder
from codesight.rag.models import (
    CODE_CHUNK_PLACEHOLDER_VECTOR,
    MAX_TRANSIENT_SNIPPET_CHARS,
    EmbeddingConfigError,
    QdrantZeroCodePoint,
    TransientEmbeddingItem,
    ZeroCodeViolationError,
    validate_positive_int,
    validate_uuid_point_id,
    validate_zero_code_payload,
)
from codesight.rag.qdrant_store import QdrantPointRef, QdrantZeroCodeStore
from codesight.vcs._http import (
    REDIRECT_STATUSES,
    origin_tuple,
    resolve_same_origin_redirect,
    stream_same_origin_bounded,
)
from codesight.vcs.github_auth import (
    GITHUB_ACCEPT_HEADER,
    GITHUB_API_VERSION,
    GitHubAuthError,
    GitHubAuthProvider,
    validate_repo_full_name,
)

MAX_INDEXABLE_BLOB_BYTES: Final[int] = 512 * 1024  # 512 KiB per blob ceiling
DEFAULT_BLOB_CONCURRENCY: Final[int] = 16
DEFAULT_INDEX_WAVE_SIZE: Final[int] = 32
MAX_COMPARE_FILES: Final[int] = 250
REPO_INDEX_LOCK_TTL_SECONDS: Final[int] = 90
DEFAULT_LOCK_RETRY_AFTER_SECONDS: Final[float] = 15.0
DEFAULT_LOCK_ACQUIRE_TIMEOUT_SECONDS: Final[float] = 5.0
DEFAULT_LOCK_POLL_INTERVAL_SECONDS: Final[float] = 0.1
MAX_SUBTREE_REQUESTS: Final[int] = 1024
MAX_TREE_DEPTH: Final[int] = 32
SYMLINK_GIT_MODE: Final[str] = "120000"
GITLINK_SUBMODULE_MODE: Final[str] = "160000"
_ZERO_SHA: Final[str] = "0" * 40
_CONFIG_FILENAME_SET: Final[frozenset[str]] = frozenset(name.lower() for name in CONFIG_FILENAMES)
_RELEASE_LOCK_LUA: Final[str] = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""
_RENEW_LOCK_LUA: Final[str] = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("expire", KEYS[1], tonumber(ARGV[2]))
else
    return 0
end
"""

type SyncMode = Literal[
    "incremental_compare",
    "bootstrap_full_index",
    "fallback_full_reconcile",
    "noop_already_indexed",
    "skipped_stale_push",
]
type ProgressCallback = Callable[[IndexProgressSnapshot], None]

logger = structlog.get_logger("codesight.rag.indexer")


class RepoIndexError(RuntimeError):
    """Raised when GitHub repository indexing or incremental sync encounters a permanent error."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class RepoMetadata:
    """Normalized GitHub repository metadata required for indexing."""

    github_repo_id: int | None
    full_name: str
    default_branch: str


@dataclass(frozen=True, slots=True)
class GitTreeBlobEntry:
    """Single blob entry returned by GitHub Git Trees API (`recursive=1`)."""

    path: str
    blob_sha: str
    size: int | None
    mode: str


@dataclass(frozen=True, slots=True)
class GitTreeSnapshot:
    """In-memory snapshot of a repository Git Tree."""

    sha: str
    truncated: bool
    entries: tuple[GitTreeBlobEntry, ...]


@dataclass(frozen=True, slots=True)
class CompareFileEntry:
    """Changed file entry returned by GitHub Compare API (`base...head`)."""

    filename: str
    status: str
    blob_sha: str | None
    previous_filename: str | None = None


@dataclass(frozen=True, slots=True)
class CompareSnapshot:
    """Normalized GitHub Compare API response for `base...head`."""

    status: str
    total_commits: int
    files: tuple[CompareFileEntry, ...]
    not_found: bool = False


@dataclass(frozen=True, slots=True)
class IndexProgressSnapshot:
    """Progress telemetry emitted after each indexing wave for CLI progress rendering."""

    stage: str
    processed_files: int
    total_files: int
    upserted_points: int
    unchanged_files: int


@dataclass(frozen=True, slots=True)
class InitialIndexReport:
    """Structured telemetry report produced by InitialRepoIndexer."""

    repo_full_name: str
    repo_id: str | None
    default_branch: str
    indexed_branch: str
    commit_sha: str
    tree_entries: int
    eligible_files: int
    unchanged_files_skipped: int
    fetched_files: int
    skipped_files: int
    code_chunks: int
    doc_chunks: int
    embedded_points: int
    upserted_points: int
    stale_points_deleted: int
    persisted_cursor: bool
    truncated_tree: bool
    elapsed_ms: float
    skipped_guard_files: int = 0


@dataclass(frozen=True, slots=True)
class IncrementalSyncReport:
    """Structured telemetry report produced by IncrementalBranchSyncer."""

    repo_full_name: str
    repo_id: str | None
    base_sha: str | None
    after_sha: str
    mode: SyncMode
    changed_files: int
    indexed_files: int
    deleted_file_paths: int
    upserted_points: int
    stale_points_deleted: int
    persisted_cursor: bool
    elapsed_ms: float
    skipped_guard_files: int = 0
    truncated_tree: bool = False


def _make_index_error(message: str, status_code: int | None) -> RepoIndexError:
    return RepoIndexError(message, status_code=status_code)


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
            "github_index_client_rate_limited",
            repo=slug,
            status_code=status_code,
            retry_after_seconds=delay,
        )
        raise RateLimitTaskError(
            f"GitHub rate limit ({status_code}) while fetching {context}.",
            retry_after_seconds=delay,
        )
    if status_code in {401, 403}:
        logger.error("github_index_client_auth_error", repo=slug, status_code=status_code)
        raise GitHubAuthError(
            f"GitHub API returned HTTP {status_code} while fetching {context}.",
            status_code=status_code,
        )
    if 500 <= status_code < 600:
        logger.warning("github_index_client_server_error", repo=slug, status_code=status_code)
        raise RetryableTaskError(
            f"GitHub API returned HTTP {status_code} while fetching {context}."
        )
    logger.error("github_index_client_http_error", repo=slug, status_code=status_code)
    raise RepoIndexError(
        f"GitHub API rejected request for {context} with HTTP {status_code}.",
        status_code=status_code,
    )


class GitHubRepoIndexClient:
    """Async HTTP/2 GitHub client for Git Trees, bounded RAM Blobs, and Compare diffs."""

    def __init__(
        self,
        auth_provider: GitHubAuthProvider | None,
        *,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.github.com",
        timeout_seconds: float = 15.0,
        allow_public_fallback: bool = False,
    ) -> None:
        self._auth = auth_provider
        self._client = http_client
        self._owns_client = http_client is None
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
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
        """Close owned HTTP/2 pool without touching externally injected clients."""
        if self._owns_client and self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def resolve_headers(
        self,
        *,
        slug: str,
        installation_id: int | None = None,
    ) -> dict[str, str]:
        """Resolve GitHub API authentication headers (App token, PAT, or public fallback)."""
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

    async def _get_same_origin(
        self,
        url: str,
        *,
        slug: str,
        headers: dict[str, str],
        max_redirects: int = 3,
    ) -> httpx.Response:
        client = self._ensure_client()
        expected_origin = origin_tuple(self._base_url)
        current_url = url
        for _ in range(max_redirects + 1):
            try:
                resp = await client.get(current_url, headers=headers, follow_redirects=False)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                logger.warning(
                    "github_index_client_transport_error",
                    repo=slug,
                    error_type=type(exc).__name__,
                )
                raise RetryableTaskError(
                    f"Transient network failure contacting GitHub GET {url}."
                ) from exc

            if resp.status_code not in REDIRECT_STATUSES:
                return resp
            current_url = resolve_same_origin_redirect(
                current_url,
                resp,
                expected_origin,
                error_factory=_make_index_error,
            )
        raise RepoIndexError("Exceeded maximum same-origin redirects contacting GitHub API.")

    async def _get_json(
        self,
        url: str,
        *,
        slug: str,
        context: str,
        installation_id: int | None = None,
        allow_404: bool = False,
    ) -> dict[str, Any] | None:
        headers = await self.resolve_headers(slug=slug, installation_id=installation_id)
        resp = await self._get_same_origin(url, slug=slug, headers=headers)
        if resp.status_code == 401 and installation_id is not None and self._auth is not None:
            await self._auth.invalidate_installation_token(installation_id)
        if allow_404 and resp.status_code == 404:
            return None
        _raise_for_github_status(resp.status_code, resp.headers, slug=slug, context=context)
        try:
            parsed = resp.json()
        except ValueError as exc:
            raise RepoIndexError(
                f"GitHub API returned invalid JSON for {context}.",
                status_code=resp.status_code,
            ) from exc
        if not isinstance(parsed, dict):
            raise RepoIndexError(
                f"GitHub API returned non-object JSON payload for {context}.",
                status_code=resp.status_code,
            )
        return parsed

    async def fetch_repo_metadata(
        self,
        repo_full_name: str,
        *,
        installation_id: int | None = None,
    ) -> RepoMetadata:
        """Fetch repository ID, canonical slug, and default_branch."""
        slug = validate_repo_full_name(repo_full_name)
        data = await self._get_json(
            f"{self._base_url}/repos/{slug}",
            slug=slug,
            context=f"repository '{slug}' metadata",
            installation_id=installation_id,
        )
        assert data is not None
        raw_id = data.get("id")
        gh_repo_id = (
            raw_id
            if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id > 0
            else None
        )
        raw_full_name = data.get("full_name")
        canonical = (
            validate_repo_full_name(raw_full_name)
            if isinstance(raw_full_name, str) and raw_full_name.strip()
            else slug
        )
        raw_branch = data.get("default_branch")
        default_branch = (
            raw_branch.strip() if isinstance(raw_branch, str) and raw_branch.strip() else "main"
        )
        return RepoMetadata(
            github_repo_id=gh_repo_id,
            full_name=canonical,
            default_branch=default_branch,
        )

    async def fetch_branch_head_sha(
        self,
        repo_full_name: str,
        ref: str,
        *,
        installation_id: int | None = None,
    ) -> str:
        """Resolve a branch or commit ref to its canonical commit SHA."""
        slug = validate_repo_full_name(repo_full_name)
        cleaned_ref = ref.strip()
        if not cleaned_ref:
            raise ValueError("ref must be a non-empty branch name or commit SHA.")
        encoded_ref = quote(cleaned_ref, safe="")
        data = await self._get_json(
            f"{self._base_url}/repos/{slug}/commits/{encoded_ref}",
            slug=slug,
            context=f"commit '{cleaned_ref}' for '{slug}'",
            installation_id=installation_id,
        )
        assert data is not None
        raw_sha = data.get("sha")
        if not isinstance(raw_sha, str) or not raw_sha.strip():
            raise RepoIndexError(
                f"GitHub commits endpoint response missing 'sha' for '{slug}@{cleaned_ref}'."
            )
        return raw_sha.strip()

    @staticmethod
    def _parse_tree_blob_item(
        item: object,
        *,
        prefix: str = "",
    ) -> GitTreeBlobEntry | None:
        if not isinstance(item, dict) or item.get("type") != "blob":
            return None
        raw_path = item.get("path")
        raw_sha = item.get("sha")
        if not isinstance(raw_path, str) or not isinstance(raw_sha, str):
            return None
        joined = f"{prefix}/{raw_path}" if prefix else raw_path
        norm_path = normalize_diff_path(joined).rstrip("/")
        blob_sha = raw_sha.strip()
        if not norm_path or not blob_sha:
            return None
        raw_size = item.get("size")
        size = (
            raw_size
            if isinstance(raw_size, int) and not isinstance(raw_size, bool) and raw_size >= 0
            else None
        )
        mode = str(item.get("mode") or "100644")
        return GitTreeBlobEntry(path=norm_path, blob_sha=blob_sha, size=size, mode=mode)

    async def _walk_git_tree_non_recursive(
        self,
        *,
        slug: str,
        root_sha: str,
        fallback_entries: Sequence[GitTreeBlobEntry],
        installation_id: int | None,
    ) -> GitTreeSnapshot:
        """Traverse non-recursive `/git/trees/{sha}` subtrees when `recursive=1` was truncated."""
        by_path: dict[str, GitTreeBlobEntry] = {}
        frontier: list[tuple[str, str, int]] = [("", root_sha, 0)]
        visited: set[tuple[str, str]] = {("", root_sha)}
        enqueued_count = 1
        still_truncated = False
        sem = asyncio.Semaphore(DEFAULT_BLOB_CONCURRENCY)

        async def _fetch_dir(
            prefix: str,
            node_sha: str,
            depth: int,
        ) -> tuple[str, int, dict[str, Any]]:
            async with sem:
                encoded = quote(node_sha.strip(), safe="")
                payload = await self._get_json(
                    f"{self._base_url}/repos/{slug}/git/trees/{encoded}",
                    slug=slug,
                    context=f"git subtree '{node_sha}' for '{slug}'",
                    installation_id=installation_id,
                )
                if payload is None:
                    raise RepoIndexError(
                        f"GitHub Git Trees response missing payload for '{slug}@{node_sha}'."
                    )
                return prefix, depth, payload

        while frontier:
            dir_results = await asyncio.gather(*(_fetch_dir(p, s, d) for p, s, d in frontier))
            next_frontier: list[tuple[str, str, int]] = []
            for prefix, depth, sub_data in dir_results:
                if bool(sub_data.get("truncated")):
                    still_truncated = True
                raw_items = sub_data.get("tree")
                if not isinstance(raw_items, list):
                    raise RepoIndexError(
                        f"GitHub Git Trees response missing 'tree' list for '{slug}'."
                    )
                for item in raw_items:
                    if not isinstance(item, dict):
                        continue
                    item_type = item.get("type")
                    if item_type == "blob":
                        blob_entry = self._parse_tree_blob_item(item, prefix=prefix)
                        if blob_entry is not None:
                            by_path[blob_entry.path] = blob_entry
                        continue
                    if item_type != "tree":
                        continue
                    mode = str(item.get("mode") or "040000")
                    if mode == GITLINK_SUBMODULE_MODE:
                        continue
                    raw_name = item.get("path")
                    raw_sub_sha = item.get("sha")
                    if not isinstance(raw_name, str) or not isinstance(raw_sub_sha, str):
                        continue
                    sub_sha = raw_sub_sha.strip()
                    joined_dir = f"{prefix}/{raw_name}" if prefix else raw_name
                    norm_dir = normalize_diff_path(joined_dir).rstrip("/")
                    if not norm_dir or not sub_sha:
                        continue
                    if depth + 1 > MAX_TREE_DEPTH or enqueued_count >= MAX_SUBTREE_REQUESTS:
                        still_truncated = True
                        continue
                    visit_key = (norm_dir, sub_sha)
                    if visit_key in visited:
                        continue
                    visited.add(visit_key)
                    enqueued_count += 1
                    next_frontier.append((norm_dir, sub_sha, depth + 1))
            frontier = next_frontier

        if still_truncated:
            for existing in fallback_entries:
                by_path.setdefault(existing.path, existing)

        return GitTreeSnapshot(
            sha=root_sha,
            truncated=still_truncated,
            entries=tuple(by_path.values()),
        )

    async def fetch_git_tree(
        self,
        repo_full_name: str,
        tree_sha: str,
        *,
        installation_id: int | None = None,
    ) -> GitTreeSnapshot:
        """Fetch recursive Git tree metadata for `tree_sha`, walking subtrees if truncated."""
        slug = validate_repo_full_name(repo_full_name)
        cleaned_tree_sha = tree_sha.strip()
        encoded_sha = quote(cleaned_tree_sha, safe="")
        data = await self._get_json(
            f"{self._base_url}/repos/{slug}/git/trees/{encoded_sha}?recursive=1",
            slug=slug,
            context=f"git tree '{tree_sha}' for '{slug}'",
            installation_id=installation_id,
        )
        if data is None:
            raise RepoIndexError(f"GitHub Git Trees response empty for '{slug}'.")
        raw_tree = data.get("tree")
        if not isinstance(raw_tree, list):
            raise RepoIndexError(f"GitHub Git Trees response missing 'tree' list for '{slug}'.")

        entries: list[GitTreeBlobEntry] = []
        for item in raw_tree:
            blob_entry = self._parse_tree_blob_item(item)
            if blob_entry is not None:
                entries.append(blob_entry)

        resolved_sha = str(data.get("sha") or cleaned_tree_sha)
        if bool(data.get("truncated")):
            logger.info(
                "repo_git_tree_recursive_truncated_walking_subtrees",
                repo=slug,
                tree_sha=resolved_sha,
                partial_entries=len(entries),
            )
            return await self._walk_git_tree_non_recursive(
                slug=slug,
                root_sha=cleaned_tree_sha,
                fallback_entries=entries,
                installation_id=installation_id,
            )

        return GitTreeSnapshot(
            sha=resolved_sha,
            truncated=False,
            entries=tuple(entries),
        )

    async def fetch_blob_bytes(
        self,
        repo_full_name: str,
        blob_sha: str,
        *,
        installation_id: int | None = None,
        max_bytes: int = MAX_INDEXABLE_BLOB_BYTES,
        headers: dict[str, str] | None = None,
    ) -> bytes | None:
        """Fetch and decode a Git blob into RAM, returning None if missing, huge, or binary."""
        slug = validate_repo_full_name(repo_full_name)
        encoded_sha = quote(blob_sha.strip(), safe="")
        url = f"{self._base_url}/repos/{slug}/git/blobs/{encoded_sha}"
        active_headers = (
            headers
            if headers is not None
            else await self.resolve_headers(slug=slug, installation_id=installation_id)
        )
        client = self._ensure_client()

        # Base64 JSON envelope overhead is ~4/3x plus newlines and JSON metadata
        max_envelope_bytes = int(max_bytes * 1.5) + 16_384
        for attempt in range(2):
            try:
                stream_res = await stream_same_origin_bounded(
                    client,
                    url,
                    headers=active_headers,
                    api_base=self._base_url,
                    max_bytes=max_envelope_bytes,
                    error_factory=_make_index_error,
                    early_return_statuses=frozenset({404}),
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                logger.warning(
                    "github_blob_transport_error",
                    repo=slug,
                    blob_sha=blob_sha[:12],
                    error_type=type(exc).__name__,
                )
                raise RetryableTaskError(
                    f"Transient network failure fetching blob {blob_sha[:12]} for '{slug}'."
                ) from exc

            if (
                stream_res.status_code == 401
                and installation_id is not None
                and self._auth is not None
            ):
                await self._auth.invalidate_installation_token(installation_id)
                if headers is not None and attempt == 0:
                    active_headers = await self.resolve_headers(
                        slug=slug, installation_id=installation_id
                    )
                    continue
            break

        if stream_res.status_code == 404:
            return None
        if stream_res.status_code != 200:
            _raise_for_github_status(
                stream_res.status_code,
                stream_res.headers,
                slug=slug,
                context=f"blob {blob_sha[:12]} in '{slug}'",
            )
        if stream_res.exceeded_limit or stream_res.body_bytes is None:
            return None

        try:
            payload = json.loads(stream_res.body_bytes)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None

        declared_size = payload.get("size")
        if isinstance(declared_size, int) and declared_size > max_bytes:
            return None

        encoding = payload.get("encoding")
        raw_content = payload.get("content")
        if encoding != "base64" or not isinstance(raw_content, str):
            return None

        compact = "".join(raw_content.split())
        try:
            decoded = base64.b64decode(compact.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError, ValueError):
            return None

        if len(decoded) > max_bytes or b"\x00" in decoded:
            return None
        return decoded

    async def fetch_compare(
        self,
        repo_full_name: str,
        base_sha: str,
        head_sha: str,
        *,
        installation_id: int | None = None,
    ) -> CompareSnapshot:
        """Fetch GitHub Compare API (`base...head`), returning `not_found=True` on HTTP 404."""
        slug = validate_repo_full_name(repo_full_name)
        encoded_base = quote(base_sha.strip(), safe="")
        encoded_head = quote(head_sha.strip(), safe="")
        data = await self._get_json(
            f"{self._base_url}/repos/{slug}/compare/{encoded_base}...{encoded_head}",
            slug=slug,
            context=f"compare {base_sha[:12]}...{head_sha[:12]} for '{slug}'",
            installation_id=installation_id,
            allow_404=True,
        )
        if data is None:
            return CompareSnapshot(status="not_found", total_commits=0, files=(), not_found=True)

        status_str = str(data.get("status") or "unknown").strip().lower()
        raw_commits = data.get("total_commits")
        total_commits = (
            raw_commits
            if isinstance(raw_commits, int)
            and not isinstance(raw_commits, bool)
            and raw_commits >= 0
            else 0
        )
        raw_files = data.get("files")
        parsed_files: list[CompareFileEntry] = []
        if isinstance(raw_files, list):
            for item in raw_files:
                if not isinstance(item, dict):
                    continue
                raw_name = item.get("filename")
                if not isinstance(raw_name, str) or not raw_name.strip():
                    continue
                norm_name = normalize_diff_path(raw_name).rstrip("/")
                if not norm_name:
                    continue
                raw_prev = item.get("previous_filename")
                prev_name = (
                    normalize_diff_path(raw_prev).rstrip("/")
                    if isinstance(raw_prev, str) and raw_prev.strip()
                    else None
                )
                raw_sha = item.get("sha")
                blob_sha = raw_sha.strip() if isinstance(raw_sha, str) and raw_sha.strip() else None
                file_status = str(item.get("status") or "modified").strip().lower()
                parsed_files.append(
                    CompareFileEntry(
                        filename=norm_name,
                        status=file_status,
                        blob_sha=blob_sha,
                        previous_filename=prev_name or None,
                    )
                )

        return CompareSnapshot(
            status=status_str,
            total_commits=total_commits,
            files=tuple(parsed_files),
            not_found=False,
        )


class RepoIndexLockLease:
    """Active distributed lock lease enforcing pre-write ownership verification."""

    def __init__(
        self,
        *,
        scope_key: str,
        verify_fn: Callable[[], Awaitable[bool]] | None = None,
        lease_deadline: float | None = None,
        ttl_seconds: int = REPO_INDEX_LOCK_TTL_SECONDS,
    ) -> None:
        self._scope_key = scope_key
        self._verify_fn = verify_fn
        self._lease_deadline = lease_deadline
        self._ttl_seconds = ttl_seconds
        self._lost_reason: str | None = None

    @property
    def is_lost(self) -> bool:
        """Return True when lock ownership was lost or the Redis lease TTL expired."""
        if self._lost_reason is not None:
            return True
        if self._lease_deadline is not None and time.monotonic() >= self._lease_deadline:
            self._lost_reason = "lease_expired"
            return True
        return False

    def mark_lost(self, reason: str) -> None:
        """Mark the lease as permanently lost for the remainder of the context."""
        if self._lost_reason is None:
            self._lost_reason = reason

    def refresh_deadline(self, *, renewed_at: float) -> None:
        """Advance local lease deadline after a confirmed Redis TTL renewal."""
        if self._lost_reason is None and self._lease_deadline is not None:
            self._lease_deadline = renewed_at + self._ttl_seconds

    async def ensure_held(self) -> None:
        """Verify lock ownership in Redis before mutating Qdrant or PostgreSQL."""
        if self._verify_fn is None:
            return
        if self.is_lost:
            raise RetryableTaskError(
                f"Repository index lock ownership lost for '{self._scope_key}' "
                f"({self._lost_reason}); aborting write.",
                retry_after_seconds=DEFAULT_LOCK_RETRY_AFTER_SECONDS,
            )
        renew_started = time.monotonic()
        still_owned = await self._verify_fn()
        if not still_owned:
            self.mark_lost("ownership_lost")
            raise RetryableTaskError(
                f"Repository index lock ownership lost for '{self._scope_key}'; aborting write.",
                retry_after_seconds=DEFAULT_LOCK_RETRY_AFTER_SECONDS,
            )
        self.refresh_deadline(renewed_at=renew_started)


class CompositeRepoLockLease:
    """Aggregates zero or more RepoIndexLockLeases for multi-scope repository locking."""

    def __init__(self, leases: Sequence[RepoIndexLockLease] = ()) -> None:
        self._leases: list[RepoIndexLockLease] = list(leases)

    @property
    def leases(self) -> tuple[RepoIndexLockLease, ...]:
        """Return immutable snapshot of tracked lock leases."""
        return tuple(self._leases)

    def add(self, lease: RepoIndexLockLease) -> None:
        """Append a newly acquired lock lease to the composite guard."""
        self._leases.append(lease)

    async def ensure_held(self) -> None:
        """Ensure every underlying lock scope is still actively owned."""
        for lease in self._leases:
            await lease.ensure_held()


class AsyncRepoIndexLock:
    """Per-repository distributed Redis lock preventing concurrent push sync races."""

    def __init__(
        self,
        redis_client: redis_async.Redis | None,
        *,
        ttl_seconds: int = REPO_INDEX_LOCK_TTL_SECONDS,
        acquire_timeout_seconds: float | None = None,
        poll_interval_seconds: float = DEFAULT_LOCK_POLL_INTERVAL_SECONDS,
    ) -> None:
        self._client = redis_client
        self._ttl_seconds = validate_positive_int(ttl_seconds, name="ttl_seconds")
        timeout = (
            DEFAULT_LOCK_ACQUIRE_TIMEOUT_SECONDS
            if acquire_timeout_seconds is None
            else acquire_timeout_seconds
        )
        self._acquire_timeout = max(0.0, timeout)
        self._poll_interval = max(0.005, poll_interval_seconds)

    @staticmethod
    def lock_key(scope_key: str) -> str:
        """Return canonical Redis lock key for a repository indexing scope."""
        return f"repo_index_lock:{scope_key}"

    async def _renew_strict(self, key: str, token: str) -> bool:
        if self._client is None:
            return False
        eval_fn = getattr(self._client, "eval", None)
        if callable(eval_fn):
            res = await eval_fn(_RENEW_LOCK_LUA, 1, key, token, self._ttl_seconds)
            return bool(int(res or 0))
        current = await self._client.get(key)
        if isinstance(current, (bytes, bytearray)):
            current = current.decode("utf-8", errors="ignore")
        if current == token:
            expire_fn = getattr(self._client, "expire", None)
            if callable(expire_fn):
                return bool(await expire_fn(key, self._ttl_seconds))
            await self._client.set(key, token, ex=self._ttl_seconds)
            return True
        return False

    async def _renew_heartbeat_step(self, key: str, token: str) -> tuple[bool, bool]:
        """Return `(renewed_ok, had_transient_redis_error)` for background heartbeat ticks."""
        try:
            return await self._renew_strict(key, token), False
        except (redis.exceptions.RedisError, OSError) as exc:
            logger.warning(
                "repo_index_lock_renew_failed",
                lock_key=key,
                error_type=type(exc).__name__,
            )
            return False, True

    async def _verify_for_write(self, key: str, token: str, *, scope_key: str) -> bool:
        try:
            still_owned = await self._renew_strict(key, token)
        except (redis.exceptions.RedisError, OSError) as exc:
            logger.warning(
                "repo_index_lock_verify_failed",
                lock_key=key,
                scope_key=scope_key,
                error_type=type(exc).__name__,
            )
            raise RetryableTaskError(
                f"Redis unavailable while verifying repository index lock for '{scope_key}'; "
                "aborting write.",
                retry_after_seconds=DEFAULT_LOCK_RETRY_AFTER_SECONDS,
            ) from exc
        if not still_owned:
            logger.warning(
                "repo_index_lock_ownership_lost",
                lock_key=key,
                scope_key=scope_key,
            )
        return still_owned

    async def _release(self, key: str, token: str) -> None:
        if self._client is None:
            return
        eval_fn = getattr(self._client, "eval", None)
        try:
            if callable(eval_fn):
                await eval_fn(_RELEASE_LOCK_LUA, 1, key, token)
            else:
                current = await self._client.get(key)
                if isinstance(current, (bytes, bytearray)):
                    current = current.decode("utf-8", errors="ignore")
                if current == token:
                    await self._client.delete(key)
        except (redis.exceptions.RedisError, OSError) as exc:
            logger.warning(
                "repo_index_lock_release_failed",
                lock_key=key,
                error_type=type(exc).__name__,
            )

    @asynccontextmanager
    async def hold(self, scope_key: str) -> AsyncIterator[RepoIndexLockLease]:
        """Acquire exclusive repo index lock with heartbeat renewal or raise RetryableTaskError."""
        if self._client is None:
            yield RepoIndexLockLease(scope_key=scope_key)
            return

        key = self.lock_key(scope_key)
        token = uuid.uuid4().hex
        acquired = False
        acquired_at = 0.0
        deadline = time.monotonic() + self._acquire_timeout

        while True:
            attempt_started = time.monotonic()
            try:
                ok = await self._client.set(key, token, nx=True, ex=self._ttl_seconds)
                if ok:
                    acquired = True
                    acquired_at = attempt_started
                    break
            except (redis.exceptions.RedisError, OSError) as exc:
                logger.warning(
                    "repo_index_lock_unavailable",
                    lock_key=key,
                    scope_key=scope_key,
                    error_type=type(exc).__name__,
                )
                raise RetryableTaskError(
                    f"Redis unavailable while acquiring repository index lock for '{scope_key}'.",
                    retry_after_seconds=DEFAULT_LOCK_RETRY_AFTER_SECONDS,
                ) from exc

            if time.monotonic() >= deadline:
                raise RetryableTaskError(
                    f"Repository index lock busy for '{scope_key}'; retry scheduled.",
                    retry_after_seconds=DEFAULT_LOCK_RETRY_AFTER_SECONDS,
                )
            await asyncio.sleep(self._poll_interval)

        lease = RepoIndexLockLease(
            scope_key=scope_key,
            verify_fn=lambda: self._verify_for_write(key, token, scope_key=scope_key),
            lease_deadline=acquired_at + self._ttl_seconds,
            ttl_seconds=self._ttl_seconds,
        )
        heartbeat_task: asyncio.Task[None] | None = None
        if acquired:
            interval = (
                max(0.25, min(0.5, self._ttl_seconds / 3.0))
                if self._ttl_seconds <= 1
                else max(0.5, self._ttl_seconds / 3.0)
            )

            async def _heartbeat() -> None:
                try:
                    while True:
                        await asyncio.sleep(interval)
                        renew_started = time.monotonic()
                        renewed_ok, transient_err = await self._renew_heartbeat_step(key, token)
                        if renewed_ok:
                            lease.refresh_deadline(renewed_at=renew_started)
                            continue
                        if not transient_err:
                            lease.mark_lost("ownership_lost")
                            logger.warning(
                                "repo_index_lock_ownership_lost",
                                lock_key=key,
                                scope_key=scope_key,
                            )
                            return
                        if lease.is_lost:
                            logger.warning(
                                "repo_index_lock_ownership_lost",
                                lock_key=key,
                                scope_key=scope_key,
                            )
                            return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    lease.mark_lost("heartbeat_failed")
                    logger.warning(
                        "repo_index_lock_heartbeat_failed",
                        lock_key=key,
                        scope_key=scope_key,
                        error_type=type(exc).__name__,
                    )

            heartbeat_task = asyncio.create_task(_heartbeat())

        try:
            yield lease
        finally:
            try:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await heartbeat_task
            finally:
                if acquired:
                    await self._release(key, token)


def _parse_uuid(raw: str | None) -> uuid.UUID | None:
    if not raw:
        return None
    try:
        return uuid.UUID(raw)
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class _RepositorySyncState:
    repo_id: str | None
    last_indexed_sha: str | None
    default_branch: str | None
    installation_id: int | None
    is_stale_conflict: bool = False


async def _read_repository_sync_state(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repo_id: str | None = None,
    repo_full_name: str | None = None,
    github_repo_id: int | None = None,
) -> _RepositorySyncState:
    async with session_factory() as session:
        row: Repository | None = None
        repo_uuid = _parse_uuid(repo_id)
        if repo_uuid is not None:
            row = (
                await session.execute(sa.select(Repository).where(Repository.id == repo_uuid))
            ).scalar_one_or_none()
        if row is None and repo_full_name:
            row = (
                await session.execute(
                    sa.select(Repository).where(Repository.full_name == repo_full_name)
                )
            ).scalar_one_or_none()
        if row is None:
            return _RepositorySyncState(
                repo_id=repo_id,
                last_indexed_sha=None,
                default_branch=None,
                installation_id=None,
                is_stale_conflict=False,
            )
        stale_slug = bool(
            repo_full_name
            and row.full_name.lower() != repo_full_name.lower()
            and "__stale_" in row.full_name
        )
        gh_id_conflict = (
            github_repo_id is not None
            and row.github_repo_id is not None
            and row.github_repo_id != github_repo_id
        )
        return _RepositorySyncState(
            repo_id=str(row.id),
            last_indexed_sha=row.last_indexed_sha,
            default_branch=row.default_branch,
            installation_id=row.installation_id,
            is_stale_conflict=stale_slug or gh_id_conflict,
        )


async def read_repository_state(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    repo_id: str | None = None,
    repo_full_name: str | None = None,
) -> tuple[str | None, str | None, str | None, int | None]:
    """Read `(repo_id, last_indexed_sha, default_branch, installation_id)` from PostgreSQL."""
    state = await _read_repository_sync_state(
        session_factory,
        repo_id=repo_id,
        repo_full_name=repo_full_name,
    )
    return state.repo_id, state.last_indexed_sha, state.default_branch, state.installation_id


async def upsert_repository_state(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    full_name: str,
    default_branch: str,
    github_repo_id: int | None = None,
    installation_id: int | None = None,
    last_indexed_sha: str | None = None,
    expected_previous_sha: str | object | None = _UNSET,
) -> tuple[str, bool]:
    """Idempotently upsert a Repository row and optionally advance `last_indexed_sha`."""
    async with session_factory() as session:
        try:
            row, cursor_updated = await upsert_repository_guarded(
                session,
                github_repo_id=github_repo_id,
                full_name=full_name,
                default_branch=default_branch,
                installation_id=installation_id,
                last_indexed_sha=last_indexed_sha,
                expected_previous_sha=expected_previous_sha,
            )
            repo_id_str = str(row.id)
            await session.commit()
            return repo_id_str, cursor_updated
        except (IntegrityError, RuntimeError) as exc:
            await session.rollback()
            raise RepoIndexError(
                f"Failed to upsert repository metadata for '{full_name}'."
            ) from exc
        except Exception:
            await session.rollback()
            raise


def _make_scoped_id_checker(
    store: QdrantZeroCodeStore,
    *,
    repo_full_name: str,
    repo_id: str | None,
) -> ExistingPointIdChecker:
    async def _check(point_ids: Sequence[str]) -> set[str]:
        return await store.get_existing_point_ids(
            point_ids,
            repo_full_name=repo_full_name,
            repo_id=repo_id,
        )

    return _check


async def _fetch_blobs_bounded[T](
    repo_client: GitHubRepoIndexClient,
    slug: str,
    entries: Sequence[T],
    *,
    get_blob_sha: Callable[[T], str],
    installation_id: int | None,
    max_bytes: int,
    concurrency: int,
) -> list[tuple[T, bytes | None]]:
    if not entries:
        return []
    headers = await repo_client.resolve_headers(slug=slug, installation_id=installation_id)
    sem = asyncio.Semaphore(concurrency)

    async def _fetch_one(entry: T) -> tuple[T, bytes | None]:
        async with sem:
            raw = await repo_client.fetch_blob_bytes(
                slug,
                get_blob_sha(entry),
                installation_id=installation_id,
                max_bytes=max_bytes,
                headers=headers,
            )
            return entry, raw

    tasks = [asyncio.create_task(_fetch_one(entry)) for entry in entries]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


def _extract_transient_code_snippet(
    lines: list[str],
    *,
    start_line: int,
    end_line: int,
) -> str | None:
    if not lines or start_line < 1 or end_line < start_line:
        return None
    sliced = "\n".join(lines[start_line - 1 : end_line]).strip()
    if not sliced:
        return None
    return sliced[:MAX_TRANSIENT_SNIPPET_CHARS]


def _extract_transient_items_from_blob(
    raw_bytes: bytes,
    *,
    file_path: str,
    blob_sha: str,
    repo_full_name: str,
    repo_id: str | None,
    indexed_at: str,
    ast_registry: ASTParserRegistry,
    doc_chunker: MarkdownDocChunker,
    embed_code_chunks: bool = False,
) -> tuple[list[TransientEmbeddingItem], int, int]:
    """Parse one in-RAM blob into TransientEmbeddingItems and return `(items, code, doc)` counts."""
    if ast_registry.supports(file_path):
        parsed = ast_registry.parse_file(raw_bytes, file_path=file_path)
        if parsed is None or not parsed.chunks:
            return [], 0, 0
        needs_snippets = embed_code_chunks and any(
            chunk.is_subchunk and not chunk.docstring for chunk in parsed.chunks
        )
        source_lines: list[str] = (
            raw_bytes.decode("utf-8", errors="replace")
            .replace("\r\n", "\n")
            .replace("\r", "\n")
            .split("\n")
            if needs_snippets
            else []
        )
        items: list[TransientEmbeddingItem] = []
        for chunk in parsed.chunks:
            snippet = (
                _extract_transient_code_snippet(
                    source_lines,
                    start_line=chunk.start_line,
                    end_line=chunk.end_line,
                )
                if needs_snippets and chunk.is_subchunk and not chunk.docstring
                else None
            )
            items.append(
                TransientEmbeddingItem.from_code_chunk(
                    chunk,
                    repo_full_name=repo_full_name,
                    blob_sha=blob_sha,
                    repo_id=repo_id,
                    transient_snippet=snippet,
                    indexed_at=indexed_at,
                )
            )
        source_lines.clear()
        return items, len(items), 0

    if is_architecture_doc_path(file_path):
        sections = doc_chunker.chunk_markdown(raw_bytes, file_path=file_path)
        if not sections:
            return [], 0, 0
        doc_items = [
            section.to_transient_embedding_item(
                repo_full_name=repo_full_name,
                blob_sha=blob_sha,
                repo_id=repo_id,
                indexed_at=indexed_at,
            )
            for section in sections
        ]
        return doc_items, 0, len(doc_items)

    return [], 0, 0


async def _prepare_and_embed_wave_items(
    wave_items: Sequence[TransientEmbeddingItem],
    *,
    embedder: GeminiEmbedder | None,
    scoped_id_checker: ExistingPointIdChecker | None,
    embed_code_chunks: bool,
) -> tuple[tuple[QdrantZeroCodePoint, ...], int]:
    """Deduplicate wave items, skip existing Qdrant IDs, and embed docs (or all if opt-in)."""
    if not wave_items:
        return (), 0

    try:
        if embed_code_chunks:
            if embedder is None:
                raise EmbeddingConfigError(
                    "GEMINI_API_KEY is required when embed_code_chunks=True."
                )
            embed_res = await embedder.embed_items(
                wave_items,
                existing_id_checker=scoped_id_checker,
            )
            return embed_res.points, embed_res.embedded_count

        unique_entries: list[tuple[str, dict[str, Any], TransientEmbeddingItem]] = []
        seen_ids: set[str] = set()
        for item in wave_items:
            canonical_id = validate_uuid_point_id(item.point_id)
            clean_payload = validate_zero_code_payload(item.payload, point_id=canonical_id)
            if canonical_id in seen_ids:
                item.clear_transient_text()
                continue
            seen_ids.add(canonical_id)
            unique_entries.append((canonical_id, clean_payload, item))

        existing_ids: AbstractSet[str] = frozenset()
        if scoped_id_checker is not None and unique_entries:
            raw_existing = await scoped_id_checker([cid for cid, _, _ in unique_entries])
            existing_ids = {eid.strip().lower() for eid in raw_existing}

        missing_entries: list[tuple[str, dict[str, Any], TransientEmbeddingItem]] = []
        for cid, clean_payload, item in unique_entries:
            if cid in existing_ids:
                item.clear_transient_text()
            else:
                missing_entries.append((cid, clean_payload, item))

        if not missing_entries:
            return (), 0

        placeholder_by_id: dict[str, QdrantZeroCodePoint] = {}
        doc_items: list[TransientEmbeddingItem] = []
        for cid, clean_payload, item in missing_entries:
            if item.chunk_type == "code":
                item.clear_transient_text()
                placeholder_by_id[cid] = QdrantZeroCodePoint(
                    point_id=cid,
                    vector=CODE_CHUNK_PLACEHOLDER_VECTOR,
                    payload=clean_payload,
                )
            else:
                doc_items.append(item)

        embedded_by_id: dict[str, QdrantZeroCodePoint] = {}
        embedded_count = 0
        if doc_items:
            if embedder is None:
                raise EmbeddingConfigError(
                    "GEMINI_API_KEY is required to embed architecture_doc chunks."
                )
            doc_res = await embedder.embed_items(doc_items, existing_id_checker=None)
            embedded_count = doc_res.embedded_count
            for pt in doc_res.points:
                embedded_by_id[pt.point_id] = pt

        ordered_points = tuple(
            placeholder_by_id[cid] if cid in placeholder_by_id else embedded_by_id[cid]
            for cid, _, _ in missing_entries
        )
        return ordered_points, embedded_count
    finally:
        for item in wave_items:
            item.clear_transient_text()


def _repo_lock_scopes(slug: str, repo_id: str | None) -> tuple[str, ...]:
    slug_lock_key = slug.lower()
    clean_repo_id = repo_id.strip() if repo_id and repo_id.strip() else None
    if clean_repo_id is not None and clean_repo_id.lower() != slug_lock_key:
        return (slug_lock_key, clean_repo_id)
    return (slug_lock_key,)


class InitialRepoIndexer:
    """Indexes a repository's AST symbols and architecture docs into Qdrant in RAM waves."""

    def __init__(
        self,
        *,
        auth_provider: GitHubAuthProvider | None,
        embedder: GeminiEmbedder | None = None,
        store: QdrantZeroCodeStore,
        http_client: httpx.AsyncClient | None = None,
        repo_client: GitHubRepoIndexClient | None = None,
        config_loader: RepoConfigLoader | None = None,
        ast_registry: ASTParserRegistry | None = None,
        doc_chunker: MarkdownDocChunker | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        repo_lock: AsyncRepoIndexLock | None = None,
        base_url: str = "https://api.github.com",
        blob_concurrency: int = DEFAULT_BLOB_CONCURRENCY,
        wave_size: int = DEFAULT_INDEX_WAVE_SIZE,
        max_blob_bytes: int = MAX_INDEXABLE_BLOB_BYTES,
        allow_public_fallback: bool = False,
        embed_code_chunks: bool = False,
    ) -> None:
        self._auth = auth_provider
        self._embedder = embedder
        self._store = store
        self._repo_client = (
            repo_client
            if repo_client is not None
            else GitHubRepoIndexClient(
                auth_provider,
                http_client=http_client,
                base_url=base_url,
                allow_public_fallback=allow_public_fallback,
            )
        )
        self._owns_repo_client = repo_client is None
        self._config_loader = (
            config_loader
            if config_loader is not None
            else (
                RepoConfigLoader(auth_provider, http_client=http_client, base_url=base_url)
                if auth_provider is not None
                else None
            )
        )
        self._ast_registry = ast_registry if ast_registry is not None else ASTParserRegistry()
        self._doc_chunker = doc_chunker if doc_chunker is not None else MarkdownDocChunker()
        self._session_factory = session_factory
        self._repo_lock = repo_lock
        self._blob_concurrency = validate_positive_int(blob_concurrency, name="blob_concurrency")
        self._wave_size = validate_positive_int(wave_size, name="wave_size")
        self._max_blob_bytes = validate_positive_int(max_blob_bytes, name="max_blob_bytes")
        self._allow_public_fallback = allow_public_fallback
        self._embed_code_chunks = embed_code_chunks

    @property
    def repo_client(self) -> GitHubRepoIndexClient:
        """Expose underlying GitHubRepoIndexClient for IncrementalBranchSyncer reuse."""
        return self._repo_client

    async def aclose(self) -> None:
        """Close owned HTTP client pools cleanly."""
        if self._owns_repo_client:
            await self._repo_client.aclose()

    async def _load_repo_config(
        self,
        slug: str,
        *,
        default_branch: str,
        ref: str,
        installation_id: int | None,
    ) -> RepoConfig:
        if self._config_loader is None:
            return RepoConfig()
        try:
            return await self._config_loader.load(
                slug,
                default_branch=default_branch,
                ref=ref,
                installation_id=installation_id,
            )
        except GitHubAuthError:
            if self._allow_public_fallback:
                return RepoConfig()
            raise

    async def _fetch_wave_blobs(
        self,
        slug: str,
        entries: Sequence[GitTreeBlobEntry],
        *,
        installation_id: int | None,
    ) -> list[tuple[GitTreeBlobEntry, bytes | None]]:
        return await _fetch_blobs_bounded(
            self._repo_client,
            slug,
            entries,
            get_blob_sha=lambda entry: entry.blob_sha,
            installation_id=installation_id,
            max_bytes=self._max_blob_bytes,
            concurrency=self._blob_concurrency,
        )

    async def index_repository(
        self,
        repo_full_name: str,
        *,
        ref: str | None = None,
        repo_id: str | None = None,
        installation_id: int | None = None,
        recreate: bool = False,
        persist_cursor: bool = True,
        is_default_branch_target: bool | None = None,
        allow_non_default_branch: bool = False,
        progress_callback: ProgressCallback | None = None,
        _skip_repo_lock: bool = False,
        _lock_lease: CompositeRepoLockLease | None = None,
    ) -> InitialIndexReport:
        """Index eligible Python/TS/TSX AST chunks and architecture docs for a repository."""
        started = time.perf_counter()
        slug = validate_repo_full_name(repo_full_name)
        if self._embed_code_chunks and self._embedder is None:
            raise EmbeddingConfigError("GEMINI_API_KEY is required when embed_code_chunks=True.")

        active_lease = CompositeRepoLockLease(_lock_lease.leases if _lock_lease is not None else ())
        async with AsyncExitStack() as lock_stack:
            if self._repo_lock is not None and not _skip_repo_lock:
                for scope_key in _repo_lock_scopes(slug, repo_id):
                    lease = await lock_stack.enter_async_context(self._repo_lock.hold(scope_key))
                    active_lease.add(lease)
            return await self._index_repository_unlocked(
                slug=slug,
                ref=ref,
                repo_id=repo_id,
                installation_id=installation_id,
                recreate=recreate,
                persist_cursor=persist_cursor,
                is_default_branch_target=is_default_branch_target,
                allow_non_default_branch=allow_non_default_branch,
                progress_callback=progress_callback,
                lock_lease=active_lease,
                started=started,
            )

    async def _index_repository_unlocked(
        self,
        *,
        slug: str,
        ref: str | None,
        repo_id: str | None,
        installation_id: int | None,
        recreate: bool,
        persist_cursor: bool,
        is_default_branch_target: bool | None,
        allow_non_default_branch: bool,
        progress_callback: ProgressCallback | None,
        lock_lease: CompositeRepoLockLease,
        started: float,
    ) -> InitialIndexReport:
        repo_meta = await self._repo_client.fetch_repo_metadata(
            slug,
            installation_id=installation_id,
        )
        canonical_slug = repo_meta.full_name
        target_branch = ref.strip() if ref is not None and ref.strip() else repo_meta.default_branch
        is_default_target = (
            is_default_branch_target
            if is_default_branch_target is not None
            else (target_branch == repo_meta.default_branch)
        )
        if recreate and not is_default_target:
            raise ValueError(
                f"--recreate is only supported when indexing default branch "
                f"'{repo_meta.default_branch}' (got '{target_branch}')."
            )
        if not is_default_target and not allow_non_default_branch:
            raise ValueError(
                f"Refusing to index non-default branch '{target_branch}' for '{canonical_slug}' "
                f"(default branch is '{repo_meta.default_branch}'); pass "
                "allow_non_default_branch=True (or --allow-non-default-branch) to override."
            )

        effective_repo_id = repo_id
        initial_last_indexed_sha: str | None = None
        if self._session_factory is not None:
            if effective_repo_id is None or is_default_target:
                await lock_lease.ensure_held()
                effective_repo_id, _ = await upsert_repository_state(
                    self._session_factory,
                    full_name=canonical_slug,
                    default_branch=repo_meta.default_branch,
                    github_repo_id=repo_meta.github_repo_id,
                    installation_id=installation_id,
                )
            if is_default_target and persist_cursor and not recreate:
                initial_state = await _read_repository_sync_state(
                    self._session_factory,
                    repo_id=effective_repo_id,
                    repo_full_name=canonical_slug,
                    github_repo_id=repo_meta.github_repo_id,
                )
                initial_last_indexed_sha = initial_state.last_indexed_sha

        commit_sha = await self._repo_client.fetch_branch_head_sha(
            canonical_slug,
            target_branch,
            installation_id=installation_id,
        )
        config_ref = commit_sha if is_default_target else repo_meta.default_branch
        repo_config = await self._load_repo_config(
            canonical_slug,
            default_branch=repo_meta.default_branch,
            ref=config_ref,
            installation_id=installation_id,
        )
        file_filter = FileFilter.from_repo_config(repo_config)

        await lock_lease.ensure_held()
        await self._store.ensure_collection()
        existing_refs: tuple[QdrantPointRef, ...]
        if recreate and is_default_target:
            await lock_lease.ensure_held()
            await self._store.delete_by_repo(
                repo_full_name=canonical_slug,
                repo_id=effective_repo_id,
                match_any_scope=True,
            )
            existing_refs = ()
        else:
            existing_refs = await self._store.list_repo_point_refs(
                repo_full_name=canonical_slug,
                repo_id=effective_repo_id,
                match_any_scope=True,
            )

        tree = await self._repo_client.fetch_git_tree(
            canonical_slug,
            commit_sha,
            installation_id=installation_id,
        )
        if tree.truncated:
            logger.warning(
                "repo_git_tree_truncated",
                repo=canonical_slug,
                commit_sha=commit_sha,
                entries=len(tree.entries),
            )

        refs_by_path: dict[str, list[QdrantPointRef]] = {}
        for ref_item in existing_refs:
            refs_by_path.setdefault(ref_item.file_path, []).append(ref_item)

        eligible_entries: list[GitTreeBlobEntry] = []
        skipped_files = 0
        for entry in tree.entries:
            if entry.mode == SYMLINK_GIT_MODE:
                skipped_files += 1
                continue
            if entry.size is not None and (entry.size == 0 or entry.size > self._max_blob_bytes):
                skipped_files += 1
                continue
            if file_filter.evaluate(entry.path).should_skip:
                skipped_files += 1
                continue
            if not (
                self._ast_registry.supports(entry.path) or is_architecture_doc_path(entry.path)
            ):
                skipped_files += 1
                continue
            eligible_entries.append(entry)

        current_blob_by_path: dict[str, str] = {e.path: e.blob_sha for e in eligible_entries}
        files_to_fetch: list[GitTreeBlobEntry] = []
        unchanged_files_skipped = 0
        for entry in eligible_entries:
            stored_refs = refs_by_path.get(entry.path)
            if stored_refs and all(
                ref_item.blob_sha == entry.blob_sha
                and (ref_item.repo_full_name is None or ref_item.repo_full_name == canonical_slug)
                and (effective_repo_id is None or ref_item.repo_id == effective_repo_id)
                for ref_item in stored_refs
            ):
                unchanged_files_skipped += 1
            else:
                files_to_fetch.append(entry)

        logger.info(
            "repo_initial_index_started",
            repo=canonical_slug,
            commit_sha=commit_sha,
            tree_entries=len(tree.entries),
            eligible_files=len(eligible_entries),
            unchanged_files_skipped=unchanged_files_skipped,
            files_to_fetch=len(files_to_fetch),
        )

        indexed_at = datetime.now(UTC).isoformat()
        total_to_fetch = len(files_to_fetch)
        processed_files = 0
        skipped_guard_files = 0
        skipped_guard_paths: set[str] = set()
        total_code_chunks = 0
        total_doc_chunks = 0
        total_embedded = 0
        total_upserted = 0
        live_point_ids: set[str] = set()
        scoped_id_checker = _make_scoped_id_checker(
            self._store,
            repo_full_name=canonical_slug,
            repo_id=effective_repo_id,
        )

        if progress_callback is not None:
            progress_callback(
                IndexProgressSnapshot(
                    stage="indexing",
                    processed_files=0,
                    total_files=total_to_fetch,
                    upserted_points=0,
                    unchanged_files=unchanged_files_skipped,
                )
            )

        for wave_start in range(0, total_to_fetch, self._wave_size):
            if wave_start > 0:
                await lock_lease.ensure_held()
            wave_entries = files_to_fetch[wave_start : wave_start + self._wave_size]
            fetched_pairs = await self._fetch_wave_blobs(
                canonical_slug,
                wave_entries,
                installation_id=installation_id,
            )

            wave_items: list[TransientEmbeddingItem] = []
            try:
                for entry, raw_bytes in fetched_pairs:
                    if raw_bytes is None:
                        continue
                    try:
                        file_items, code_cnt, doc_cnt = _extract_transient_items_from_blob(
                            raw_bytes,
                            file_path=entry.path,
                            blob_sha=entry.blob_sha,
                            repo_full_name=canonical_slug,
                            repo_id=effective_repo_id,
                            indexed_at=indexed_at,
                            ast_registry=self._ast_registry,
                            doc_chunker=self._doc_chunker,
                            embed_code_chunks=self._embed_code_chunks,
                        )
                        wave_items.extend(file_items)
                        live_point_ids.update(item.point_id for item in file_items)
                        total_code_chunks += code_cnt
                        total_doc_chunks += doc_cnt
                    except ZeroCodeViolationError as exc:
                        skipped_guard_files += 1
                        skipped_guard_paths.add(entry.path)
                        current_blob_by_path.pop(entry.path, None)
                        logger.warning(
                            "repo_index_file_skipped_zerocode_guard",
                            repo=canonical_slug,
                            file_path=entry.path,
                            blob_sha=entry.blob_sha,
                            error=str(exc),
                        )
                    finally:
                        del raw_bytes
                fetched_pairs.clear()

                if wave_items:
                    points, embedded_cnt = await _prepare_and_embed_wave_items(
                        wave_items,
                        embedder=self._embedder,
                        scoped_id_checker=scoped_id_checker,
                        embed_code_chunks=self._embed_code_chunks,
                    )
                    total_embedded += embedded_cnt
                    if points:
                        await lock_lease.ensure_held()
                        total_upserted += await self._store.upsert_points(points)
            finally:
                for item in wave_items:
                    item.clear_transient_text()

            processed_files += len(wave_entries)
            if progress_callback is not None:
                progress_callback(
                    IndexProgressSnapshot(
                        stage="indexing",
                        processed_files=processed_files,
                        total_files=total_to_fetch,
                        upserted_points=total_upserted,
                        unchanged_files=unchanged_files_skipped,
                    )
                )

        # Zero-downtime stale pruning AFTER new wave points are safely stored in Qdrant
        stale_points_deleted = 0
        if is_default_target and not recreate and existing_refs:
            stale_ids: list[str] = []
            for ref_item in existing_refs:
                if ref_item.point_id in live_point_ids:
                    continue
                tenant_mismatch = (
                    ref_item.repo_full_name is not None
                    and ref_item.repo_full_name != canonical_slug
                ) or (effective_repo_id is not None and ref_item.repo_id != effective_repo_id)
                if tenant_mismatch:
                    stale_ids.append(ref_item.point_id)
                    continue
                expected_sha = current_blob_by_path.get(ref_item.file_path)
                if expected_sha is not None:
                    if ref_item.blob_sha != expected_sha:
                        stale_ids.append(ref_item.point_id)
                elif not tree.truncated or ref_item.file_path in skipped_guard_paths:
                    stale_ids.append(ref_item.point_id)
            if stale_ids:
                await lock_lease.ensure_held()
                stale_points_deleted = await self._store.delete_points_by_ids(
                    stale_ids,
                    repo_full_name=canonical_slug,
                    repo_id=effective_repo_id,
                    match_any_scope=True,
                )

        cursor_persisted = False
        if tree.truncated:
            logger.warning(
                "repo_index_cursor_skipped_truncated_tree",
                repo=canonical_slug,
                commit_sha=commit_sha,
                entries=len(tree.entries),
            )
        elif self._session_factory is not None and persist_cursor and is_default_target:
            await lock_lease.ensure_held()
            effective_repo_id, cursor_persisted = await upsert_repository_state(
                self._session_factory,
                full_name=canonical_slug,
                default_branch=repo_meta.default_branch,
                github_repo_id=repo_meta.github_repo_id,
                installation_id=installation_id,
                last_indexed_sha=commit_sha,
                expected_previous_sha=_UNSET if recreate else initial_last_indexed_sha,
            )
            if not cursor_persisted:
                logger.warning(
                    "repo_index_cursor_update_skipped_stale",
                    repo=canonical_slug,
                    commit_sha=commit_sha,
                    expected_previous_sha=initial_last_indexed_sha,
                )

        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        logger.info(
            "repo_initial_index_completed",
            repo=canonical_slug,
            commit_sha=commit_sha,
            eligible_files=len(eligible_entries),
            unchanged_files_skipped=unchanged_files_skipped,
            fetched_files=total_to_fetch,
            skipped_guard_files=skipped_guard_files,
            code_chunks=total_code_chunks,
            doc_chunks=total_doc_chunks,
            upserted_points=total_upserted,
            stale_points_deleted=stale_points_deleted,
            persisted_cursor=cursor_persisted,
            elapsed_ms=elapsed_ms,
        )
        return InitialIndexReport(
            repo_full_name=canonical_slug,
            repo_id=effective_repo_id,
            default_branch=repo_meta.default_branch,
            indexed_branch=target_branch,
            commit_sha=commit_sha,
            tree_entries=len(tree.entries),
            eligible_files=len(eligible_entries),
            unchanged_files_skipped=unchanged_files_skipped,
            fetched_files=total_to_fetch,
            skipped_files=skipped_files,
            code_chunks=total_code_chunks,
            doc_chunks=total_doc_chunks,
            embedded_points=total_embedded,
            upserted_points=total_upserted,
            stale_points_deleted=stale_points_deleted,
            persisted_cursor=cursor_persisted,
            truncated_tree=tree.truncated,
            elapsed_ms=elapsed_ms,
            skipped_guard_files=skipped_guard_files,
        )


def _touches_repo_config(files: Sequence[CompareFileEntry]) -> bool:
    for entry in files:
        if entry.filename.lower() in _CONFIG_FILENAME_SET:
            return True
        if entry.previous_filename and entry.previous_filename.lower() in _CONFIG_FILENAME_SET:
            return True
    return False


class IncrementalBranchSyncer:
    """FR-07 default-branch push syncer using Compare API with Redis lock & full fallback."""

    def __init__(
        self,
        *,
        initial_indexer: InitialRepoIndexer,
        repo_client: GitHubRepoIndexClient,
        embedder: GeminiEmbedder | None = None,
        store: QdrantZeroCodeStore,
        config_loader: RepoConfigLoader | None = None,
        ast_registry: ASTParserRegistry | None = None,
        doc_chunker: MarkdownDocChunker | None = None,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        redis_client: redis_async.Redis | None = None,
        lock: AsyncRepoIndexLock | None = None,
        blob_concurrency: int = DEFAULT_BLOB_CONCURRENCY,
        wave_size: int = DEFAULT_INDEX_WAVE_SIZE,
        max_blob_bytes: int = MAX_INDEXABLE_BLOB_BYTES,
        embed_code_chunks: bool = False,
    ) -> None:
        self._initial_indexer = initial_indexer
        self._repo_client = repo_client
        self._embedder = embedder
        self._store = store
        self._config_loader = config_loader
        self._ast_registry = ast_registry if ast_registry is not None else ASTParserRegistry()
        self._doc_chunker = doc_chunker if doc_chunker is not None else MarkdownDocChunker()
        self._session_factory = session_factory
        self._lock = lock if lock is not None else AsyncRepoIndexLock(redis_client)
        self._blob_concurrency = validate_positive_int(blob_concurrency, name="blob_concurrency")
        self._wave_size = validate_positive_int(wave_size, name="wave_size")
        self._max_blob_bytes = validate_positive_int(max_blob_bytes, name="max_blob_bytes")
        self._embed_code_chunks = embed_code_chunks

    @staticmethod
    def _build_noop_report(
        *,
        slug: str,
        repo_id: str | None,
        base_sha: str | None,
        after_sha: str,
        mode: SyncMode,
        persisted_cursor: bool,
        started: float,
    ) -> IncrementalSyncReport:
        return IncrementalSyncReport(
            repo_full_name=slug,
            repo_id=repo_id,
            base_sha=base_sha,
            after_sha=after_sha,
            mode=mode,
            changed_files=0,
            indexed_files=0,
            deleted_file_paths=0,
            upserted_points=0,
            stale_points_deleted=0,
            persisted_cursor=persisted_cursor,
            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 2),
        )

    async def _resolve_non_ahead_target(
        self,
        *,
        slug: str,
        effective_branch: str,
        target_sha: str,
        base_sha: str | None,
        effective_repo_id: str | None,
        effective_inst_id: int | None,
        compare_status: str,
        fallback_reason: str,
        lock_lease: CompositeRepoLockLease,
        started: float,
    ) -> IncrementalSyncReport:
        branch_head = await self._repo_client.fetch_branch_head_sha(
            slug,
            effective_branch,
            installation_id=effective_inst_id,
        )
        if branch_head != target_sha:
            if compare_status == "behind" or (base_sha is not None and branch_head == base_sha):
                logger.info(
                    "repo_incremental_sync_stale_push_skipped",
                    repo=slug,
                    base_sha=base_sha,
                    after_sha=target_sha,
                    branch_head=branch_head,
                    compare_status=compare_status,
                )
                return self._build_noop_report(
                    slug=slug,
                    repo_id=effective_repo_id,
                    base_sha=base_sha,
                    after_sha=target_sha,
                    mode="skipped_stale_push",
                    persisted_cursor=False,
                    started=started,
                )
            return await self._run_full_fallback(
                slug=slug,
                after_sha=branch_head,
                base_sha=base_sha,
                repo_id=effective_repo_id,
                installation_id=effective_inst_id,
                mode="fallback_full_reconcile",
                reason=f"{fallback_reason}_head_advanced",
                lock_lease=lock_lease,
                started=started,
            )
        return await self._run_full_fallback(
            slug=slug,
            after_sha=target_sha,
            base_sha=base_sha,
            repo_id=effective_repo_id,
            installation_id=effective_inst_id,
            mode="fallback_full_reconcile",
            reason=fallback_reason,
            lock_lease=lock_lease,
            started=started,
        )

    async def _run_full_fallback(
        self,
        *,
        slug: str,
        after_sha: str,
        base_sha: str | None,
        repo_id: str | None,
        installation_id: int | None,
        mode: Literal["bootstrap_full_index", "fallback_full_reconcile"],
        reason: str,
        lock_lease: CompositeRepoLockLease,
        started: float,
    ) -> IncrementalSyncReport:
        logger.info(
            "repo_incremental_sync_fallback",
            repo=slug,
            base_sha=base_sha,
            after_sha=after_sha,
            mode=mode,
            reason=reason,
        )
        init_report = await self._initial_indexer.index_repository(
            slug,
            ref=after_sha,
            repo_id=repo_id,
            installation_id=installation_id,
            recreate=False,
            persist_cursor=True,
            is_default_branch_target=True,
            _skip_repo_lock=True,
            _lock_lease=lock_lease,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return IncrementalSyncReport(
            repo_full_name=init_report.repo_full_name,
            repo_id=init_report.repo_id,
            base_sha=base_sha,
            after_sha=after_sha,
            mode=mode,
            changed_files=init_report.fetched_files,
            indexed_files=max(0, init_report.fetched_files - init_report.skipped_guard_files),
            deleted_file_paths=0,
            upserted_points=init_report.upserted_points,
            stale_points_deleted=init_report.stale_points_deleted,
            persisted_cursor=init_report.persisted_cursor,
            elapsed_ms=elapsed_ms,
            skipped_guard_files=init_report.skipped_guard_files,
            truncated_tree=init_report.truncated_tree,
        )

    async def sync_push(
        self,
        *,
        repo_full_name: str,
        after_sha: str,
        default_branch: str = "main",
        pushed_ref: str | None = None,
        last_indexed_sha: str | object | None = _UNSET,
        repo_id: str | None = None,
        github_repo_id: int | None = None,
        installation_id: int | None = None,
    ) -> IncrementalSyncReport:
        """Synchronize Qdrant index for a push to the repository's default branch."""
        started = time.perf_counter()
        slug = validate_repo_full_name(repo_full_name)
        target_sha = after_sha.strip()
        if not target_sha:
            raise ValueError("after_sha must be a non-empty commit SHA.")
        if target_sha == _ZERO_SHA:
            return self._build_noop_report(
                slug=slug,
                repo_id=repo_id,
                base_sha=last_indexed_sha if isinstance(last_indexed_sha, str) else None,
                after_sha=target_sha,
                mode="skipped_stale_push",
                persisted_cursor=False,
                started=started,
            )
        if self._embed_code_chunks and self._embedder is None:
            raise EmbeddingConfigError("GEMINI_API_KEY is required when embed_code_chunks=True.")

        clean_repo_id = repo_id.strip() if repo_id and repo_id.strip() else None
        lock_scopes = _repo_lock_scopes(slug, clean_repo_id)
        lease_group = CompositeRepoLockLease()
        async with AsyncExitStack() as lock_stack:
            for scope_key in lock_scopes:
                lease = await lock_stack.enter_async_context(self._lock.hold(scope_key))
                lease_group.add(lease)

            effective_repo_id: str | None = clean_repo_id
            effective_branch = default_branch.strip() or "main"
            effective_inst_id = installation_id
            base_sha: str | None = (
                cast(str | None, last_indexed_sha) if last_indexed_sha is not _UNSET else None
            )

            if self._session_factory is not None:
                db_state = await _read_repository_sync_state(
                    self._session_factory,
                    repo_id=clean_repo_id,
                    repo_full_name=slug,
                    github_repo_id=github_repo_id,
                )
                if db_state.is_stale_conflict:
                    logger.info(
                        "repo_incremental_sync_stale_repo_skipped",
                        repo=slug,
                        repo_id=clean_repo_id,
                        db_repo_id=db_state.repo_id,
                        github_repo_id=github_repo_id,
                        after_sha=target_sha,
                    )
                    return self._build_noop_report(
                        slug=slug,
                        repo_id=db_state.repo_id or effective_repo_id,
                        base_sha=(
                            db_state.last_indexed_sha if last_indexed_sha is _UNSET else base_sha
                        ),
                        after_sha=target_sha,
                        mode="skipped_stale_push",
                        persisted_cursor=False,
                        started=started,
                    )
                if last_indexed_sha is _UNSET:
                    effective_repo_id = db_state.repo_id or effective_repo_id
                    base_sha = db_state.last_indexed_sha
                    if db_state.default_branch:
                        effective_branch = db_state.default_branch
                    if effective_inst_id is None:
                        effective_inst_id = db_state.installation_id

            cleaned_ref = pushed_ref.strip() if isinstance(pushed_ref, str) else ""
            if cleaned_ref and cleaned_ref != f"refs/heads/{effective_branch}":
                logger.info(
                    "repo_incremental_sync_non_default_ref_skipped",
                    repo=slug,
                    pushed_ref=cleaned_ref,
                    default_branch=effective_branch,
                )
                return self._build_noop_report(
                    slug=slug,
                    repo_id=effective_repo_id,
                    base_sha=base_sha,
                    after_sha=target_sha,
                    mode="skipped_stale_push",
                    persisted_cursor=False,
                    started=started,
                )

            if base_sha is not None and base_sha == target_sha:
                return self._build_noop_report(
                    slug=slug,
                    repo_id=effective_repo_id,
                    base_sha=base_sha,
                    after_sha=target_sha,
                    mode="noop_already_indexed",
                    persisted_cursor=False,
                    started=started,
                )

            if base_sha is None:
                return await self._run_full_fallback(
                    slug=slug,
                    after_sha=target_sha,
                    base_sha=None,
                    repo_id=effective_repo_id,
                    installation_id=effective_inst_id,
                    mode="bootstrap_full_index",
                    reason="missing_last_indexed_sha",
                    lock_lease=lease_group,
                    started=started,
                )

            compare = await self._repo_client.fetch_compare(
                slug,
                base_sha,
                target_sha,
                installation_id=effective_inst_id,
            )

            if compare.not_found:
                return await self._resolve_non_ahead_target(
                    slug=slug,
                    effective_branch=effective_branch,
                    target_sha=target_sha,
                    base_sha=base_sha,
                    effective_repo_id=effective_repo_id,
                    effective_inst_id=effective_inst_id,
                    compare_status="not_found",
                    fallback_reason="compare_404",
                    lock_lease=lease_group,
                    started=started,
                )

            if compare.status == "identical":
                persisted = False
                if self._session_factory is not None:
                    await lease_group.ensure_held()
                    effective_repo_id, persisted = await upsert_repository_state(
                        self._session_factory,
                        full_name=slug,
                        default_branch=effective_branch,
                        github_repo_id=github_repo_id,
                        installation_id=effective_inst_id,
                        last_indexed_sha=target_sha,
                        expected_previous_sha=base_sha,
                    )
                return self._build_noop_report(
                    slug=slug,
                    repo_id=effective_repo_id,
                    base_sha=base_sha,
                    after_sha=target_sha,
                    mode="noop_already_indexed",
                    persisted_cursor=persisted,
                    started=started,
                )

            if compare.status == "behind":
                return await self._resolve_non_ahead_target(
                    slug=slug,
                    effective_branch=effective_branch,
                    target_sha=target_sha,
                    base_sha=base_sha,
                    effective_repo_id=effective_repo_id,
                    effective_inst_id=effective_inst_id,
                    compare_status="behind",
                    fallback_reason="force_push_behind_revert",
                    lock_lease=lease_group,
                    started=started,
                )

            if compare.status != "ahead":
                return await self._resolve_non_ahead_target(
                    slug=slug,
                    effective_branch=effective_branch,
                    target_sha=target_sha,
                    base_sha=base_sha,
                    effective_repo_id=effective_repo_id,
                    effective_inst_id=effective_inst_id,
                    compare_status=compare.status,
                    fallback_reason=f"compare_status_{compare.status}",
                    lock_lease=lease_group,
                    started=started,
                )

            if (
                len(compare.files) >= MAX_COMPARE_FILES
                or compare.total_commits > MAX_COMPARE_FILES
                or _touches_repo_config(compare.files)
            ):
                fallback_reason = (
                    "repo_config_changed"
                    if _touches_repo_config(compare.files)
                    else "compare_truncated"
                )
                return await self._run_full_fallback(
                    slug=slug,
                    after_sha=target_sha,
                    base_sha=base_sha,
                    repo_id=effective_repo_id,
                    installation_id=effective_inst_id,
                    mode="fallback_full_reconcile",
                    reason=fallback_reason,
                    lock_lease=lease_group,
                    started=started,
                )

            if effective_repo_id is not None and await self._store.has_legacy_slug_points(
                repo_id=effective_repo_id,
                expected_repo_full_name=slug,
            ):
                return await self._run_full_fallback(
                    slug=slug,
                    after_sha=target_sha,
                    base_sha=base_sha,
                    repo_id=effective_repo_id,
                    installation_id=effective_inst_id,
                    mode="fallback_full_reconcile",
                    reason="repo_renamed",
                    lock_lease=lease_group,
                    started=started,
                )

            # Fast-path FR-07 incremental update (`status == "ahead"`, < 250 files)
            repo_config = (
                await self._config_loader.load(
                    slug,
                    default_branch=effective_branch,
                    ref=target_sha,
                    installation_id=effective_inst_id,
                )
                if self._config_loader is not None
                else RepoConfig()
            )
            file_filter = FileFilter.from_repo_config(repo_config)
            await lease_group.ensure_held()
            await self._store.ensure_collection()

            paths_to_delete: list[str] = []
            seen_delete: set[str] = set()
            entries_to_fetch: list[CompareFileEntry] = []

            def _add_delete(path: str | None) -> None:
                if path and path not in seen_delete:
                    seen_delete.add(path)
                    paths_to_delete.append(path)

            for entry in compare.files:
                if entry.status == "removed":
                    _add_delete(entry.filename)
                    continue
                if entry.status == "renamed":
                    _add_delete(entry.previous_filename)
                _add_delete(entry.filename)

                if (
                    entry.blob_sha
                    and not file_filter.evaluate(entry.filename).should_skip
                    and (
                        self._ast_registry.supports(entry.filename)
                        or is_architecture_doc_path(entry.filename)
                    )
                ):
                    entries_to_fetch.append(entry)

            indexed_at = datetime.now(UTC).isoformat()
            indexed_files = 0
            skipped_guard_files = 0
            upserted_points = 0
            kept_point_ids: list[str] = []
            scoped_id_checker = _make_scoped_id_checker(
                self._store,
                repo_full_name=slug,
                repo_id=effective_repo_id,
            )

            for wave_start in range(0, len(entries_to_fetch), self._wave_size):
                if wave_start > 0:
                    await lease_group.ensure_held()
                wave_entries = entries_to_fetch[wave_start : wave_start + self._wave_size]
                fetched_pairs = await _fetch_blobs_bounded(
                    self._repo_client,
                    slug,
                    wave_entries,
                    get_blob_sha=lambda e: e.blob_sha or "",
                    installation_id=effective_inst_id,
                    max_bytes=self._max_blob_bytes,
                    concurrency=self._blob_concurrency,
                )

                wave_items: list[TransientEmbeddingItem] = []
                try:
                    for entry, raw_bytes in fetched_pairs:
                        if raw_bytes is None or entry.blob_sha is None:
                            continue
                        try:
                            file_items, _, _ = _extract_transient_items_from_blob(
                                raw_bytes,
                                file_path=entry.filename,
                                blob_sha=entry.blob_sha,
                                repo_full_name=slug,
                                repo_id=effective_repo_id,
                                indexed_at=indexed_at,
                                ast_registry=self._ast_registry,
                                doc_chunker=self._doc_chunker,
                                embed_code_chunks=self._embed_code_chunks,
                            )
                            wave_items.extend(file_items)
                            indexed_files += 1
                        except ZeroCodeViolationError as exc:
                            skipped_guard_files += 1
                            logger.warning(
                                "repo_index_file_skipped_zerocode_guard",
                                repo=slug,
                                file_path=entry.filename,
                                blob_sha=entry.blob_sha,
                                error=str(exc),
                            )
                        finally:
                            del raw_bytes
                    fetched_pairs.clear()

                    kept_point_ids.extend(item.point_id for item in wave_items)
                    if wave_items:
                        points, _ = await _prepare_and_embed_wave_items(
                            wave_items,
                            embedder=self._embedder,
                            scoped_id_checker=scoped_id_checker,
                            embed_code_chunks=self._embed_code_chunks,
                        )
                        if points:
                            await lease_group.ensure_held()
                            upserted_points += await self._store.upsert_points(points)
                finally:
                    for item in wave_items:
                        item.clear_transient_text()

            # Delete 100% of old points for touched paths while keeping valid newly indexed IDs
            if paths_to_delete:
                await lease_group.ensure_held()
                await self._store.delete_by_file_paths(
                    paths_to_delete,
                    repo_full_name=slug,
                    repo_id=effective_repo_id,
                    exclude_point_ids=kept_point_ids,
                )

            cursor_persisted = False
            if self._session_factory is not None:
                await lease_group.ensure_held()
                effective_repo_id, cursor_persisted = await upsert_repository_state(
                    self._session_factory,
                    full_name=slug,
                    default_branch=effective_branch,
                    github_repo_id=github_repo_id,
                    installation_id=effective_inst_id,
                    last_indexed_sha=target_sha,
                    expected_previous_sha=base_sha,
                )

            elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
            logger.info(
                "repo_incremental_sync_completed",
                repo=slug,
                base_sha=base_sha,
                after_sha=target_sha,
                changed_files=len(compare.files),
                indexed_files=indexed_files,
                skipped_guard_files=skipped_guard_files,
                deleted_file_paths=len(paths_to_delete),
                upserted_points=upserted_points,
                persisted_cursor=cursor_persisted,
                elapsed_ms=elapsed_ms,
            )
            return IncrementalSyncReport(
                repo_full_name=slug,
                repo_id=effective_repo_id,
                base_sha=base_sha,
                after_sha=target_sha,
                mode="incremental_compare",
                changed_files=len(compare.files),
                indexed_files=indexed_files,
                deleted_file_paths=len(paths_to_delete),
                upserted_points=upserted_points,
                stale_points_deleted=0,
                persisted_cursor=cursor_persisted,
                elapsed_ms=elapsed_ms,
                skipped_guard_files=skipped_guard_files,
            )
