"""Async HTTP/2 GitHub Pull Request diff client with RAM-only bounds and retry mapping."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Final, Literal, Self

import httpx
import structlog

from codesight import __version__
from codesight.core.errors import (
    RateLimitTaskError,
    RetryableTaskError,
    is_rate_limited_403,
    parse_retry_after_header,
)
from codesight.diff.file_filter import FileFilter, normalize_diff_path
from codesight.diff.models import ParsedPullRequestDiff, PatchFileEntry
from codesight.diff.smart_diff_parser import SmartDiffParser
from codesight.vcs._http import (
    REDIRECT_STATUSES,
    origin_tuple,
    parse_github_files_page,
    resolve_same_origin_redirect,
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
from codesight.vcs.github_publisher import GitHubPublishError

if TYPE_CHECKING:
    from codesight.config_reader.repo_config import RepoConfig

GITHUB_DIFF_ACCEPT_HEADER: Final[str] = "application/vnd.github.diff"
MAX_DIFF_BYTES: Final[int] = 20 * 1024 * 1024
MAX_FILE_PAGES: Final[int] = 30

logger = structlog.get_logger("codesight.vcs.pr_client")


@dataclass(frozen=True, slots=True)
class FetchedPullRequestDiff:
    """In-memory filtered PR diff bundle paired with the upstream fetch strategy used."""

    parsed_diff: ParsedPullRequestDiff
    diff_source: Literal["diff", "files_api"]


def _make_publish_error(message: str, status_code: int | None) -> GitHubPublishError:
    return GitHubPublishError(message, status_code=status_code)


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
            "github_pr_client_rate_limited",
            repo=slug,
            status_code=status_code,
            retry_after_seconds=delay,
        )
        raise RateLimitTaskError(
            f"GitHub rate limit ({status_code}) while fetching {context}.",
            retry_after_seconds=delay,
        )
    if status_code in {401, 403}:
        logger.error("github_pr_client_auth_error", repo=slug, status_code=status_code)
        raise GitHubAuthError(
            f"GitHub API returned HTTP {status_code} while fetching {context}.",
            status_code=status_code,
        )
    if 500 <= status_code < 600:
        logger.warning("github_pr_client_server_error", repo=slug, status_code=status_code)
        raise RetryableTaskError(
            f"GitHub API returned HTTP {status_code} while fetching {context}."
        )
    logger.error("github_pr_client_http_error", repo=slug, status_code=status_code)
    raise GitHubPublishError(
        f"GitHub API rejected fetch for {context} with HTTP {status_code}.",
        status_code=status_code,
    )


def _public_github_headers() -> dict[str, str]:
    return {
        "Accept": GITHUB_ACCEPT_HEADER,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": f"codesight-ai/{__version__}",
    }


class GitHubPullRequestClient:
    """Fetches PR unified diffs (with paginated /files fallback) in RAM over HTTP/2."""

    def __init__(
        self,
        auth_provider: GitHubAuthProvider | None,
        *,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.github.com",
        timeout_seconds: float = 15.0,
        max_diff_bytes: int = MAX_DIFF_BYTES,
        allow_public_fallback: bool = False,
    ) -> None:
        self._auth = auth_provider
        self._client = http_client
        self._owns_client = http_client is None
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_diff_bytes = max_diff_bytes
        self._allow_public_fallback = allow_public_fallback

    async def _resolve_headers(
        self,
        *,
        slug: str,
        installation_id: int | None = None,
    ) -> tuple[dict[str, str], bool]:
        if self._auth is None:
            if self._allow_public_fallback:
                return _public_github_headers(), True
            raise GitHubAuthError("GitHub authentication provider is required.")
        try:
            headers = await self._auth.get_auth_headers(
                installation_id=installation_id,
                repo_full_name=slug,
            )
            return headers, False
        except GitHubAuthError:
            if self._allow_public_fallback:
                return _public_github_headers(), True
            raise

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is not None and not self._owns_client and self._client.is_closed:
            raise RuntimeError("Injected HTTP client has been closed externally.")
        if self._client is None or (self._owns_client and self._client.is_closed):
            self._client = httpx.AsyncClient(
                http2=True,
                timeout=self._timeout_seconds,
                follow_redirects=False,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._client

    async def __aenter__(self) -> Self:
        self._ensure_client()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the internally managed HTTP/2 client pool without closing injected clients."""
        if self._owns_client and self._client is not None and not self._client.is_closed:
            await self._client.aclose()

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
                    "github_pr_client_transport_error",
                    repo=slug,
                    error_type=type(exc).__name__,
                )
                raise RetryableTaskError(
                    f"Transient network failure calling GitHub GET {url}."
                ) from exc

            if resp.status_code not in REDIRECT_STATUSES:
                return resp
            current_url = resolve_same_origin_redirect(
                current_url,
                resp,
                expected_origin,
                error_factory=_make_publish_error,
            )
        raise GitHubPublishError("Exceeded maximum same-origin redirects contacting GitHub API.")

    async def _fetch_bounded_raw_diff(
        self,
        url: str,
        *,
        slug: str,
        pull_number: int,
        headers: dict[str, str],
        installation_id: int | None = None,
        max_redirects: int = 3,
    ) -> str | None:
        client = self._ensure_client()
        try:
            stream_res = await stream_same_origin_bounded(
                client,
                url,
                headers=headers,
                api_base=self._base_url,
                max_bytes=self._max_diff_bytes,
                error_factory=_make_publish_error,
                max_redirects=max_redirects,
                early_return_statuses=frozenset({406, 422}),
            )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            logger.warning(
                "github_pr_client_transport_error",
                repo=slug,
                error_type=type(exc).__name__,
            )
            raise RetryableTaskError(
                f"Transient network failure streaming diff for {slug}#{pull_number}."
            ) from exc

        if stream_res.status_code in {406, 422}:
            logger.info(
                "github_pr_diff_fallback_to_files_api",
                repo=slug,
                pr_number=pull_number,
                status_code=stream_res.status_code,
            )
            return None

        if stream_res.status_code == 401 and installation_id is not None and self._auth is not None:
            await self._auth.invalidate_installation_token(installation_id)

        if stream_res.status_code != 200:
            _raise_for_github_status(
                stream_res.status_code,
                stream_res.headers,
                slug=slug,
                context=f"PR {slug}#{pull_number} diff",
            )

        if stream_res.exceeded_limit or stream_res.body_bytes is None:
            logger.warning(
                "github_pr_diff_exceeded_max_bytes",
                repo=slug,
                pr_number=pull_number,
                declared_bytes=stream_res.declared_bytes,
                received_bytes=stream_res.received_bytes,
                max_bytes=self._max_diff_bytes,
            )
            return None

        return stream_res.body_bytes.decode("utf-8", errors="replace")

    async def _fetch_paginated_files(
        self,
        *,
        slug: str,
        pull_number: int,
        headers: dict[str, str],
        installation_id: int | None = None,
    ) -> list[PatchFileEntry]:
        entries: list[PatchFileEntry] = []
        for page in range(1, MAX_FILE_PAGES + 1):
            url = (
                f"{self._base_url}/repos/{slug}/pulls/{pull_number}/files?per_page=100&page={page}"
            )
            resp = await self._get_same_origin(url, slug=slug, headers=headers)
            if resp.status_code == 401 and installation_id is not None and self._auth is not None:
                await self._auth.invalidate_installation_token(installation_id)
            _raise_for_github_status(
                resp.status_code,
                resp.headers,
                slug=slug,
                context=f"PR {slug}#{pull_number} files",
            )
            try:
                items = resp.json()
            except ValueError as exc:
                raise GitHubPublishError(
                    "GitHub PR files endpoint returned invalid JSON.",
                    status_code=resp.status_code,
                ) from exc

            if not isinstance(items, list):
                raise GitHubPublishError(
                    "GitHub PR files endpoint returned non-list payload.",
                    status_code=resp.status_code,
                )

            entries.extend(parse_github_files_page(items))

            if len(items) < 100:
                break
            if page == MAX_FILE_PAGES:
                logger.warning(
                    "github_pr_files_pagination_cap_reached",
                    repo=slug,
                    pr_number=pull_number,
                    max_pages=MAX_FILE_PAGES,
                    fetched_files=len(entries),
                )

        return entries

    async def fetch_pull_request_diff(
        self,
        repo_full_name: str,
        pull_number: int,
        *,
        repo_config: RepoConfig | None = None,
        installation_id: int | None = None,
    ) -> FetchedPullRequestDiff:
        """Fetch PR unified diff in RAM, falling back to /pulls/{pr}/files when needed."""
        slug = validate_repo_full_name(repo_full_name)
        valid_pr = validate_positive_int(pull_number, name="pull_number")
        file_filter = (
            FileFilter.from_repo_config(repo_config) if repo_config is not None else FileFilter()
        )

        json_headers, _ = await self._resolve_headers(
            slug=slug,
            installation_id=installation_id,
        )
        diff_headers = {**json_headers, "Accept": GITHUB_DIFF_ACCEPT_HEADER}
        pr_url = f"{self._base_url}/repos/{slug}/pulls/{valid_pr}"

        raw_diff_text = await self._fetch_bounded_raw_diff(
            pr_url,
            slug=slug,
            pull_number=valid_pr,
            headers=diff_headers,
            installation_id=installation_id,
        )

        parser = SmartDiffParser(file_filter=file_filter)
        diff_source: Literal["diff", "files_api"]
        if raw_diff_text is None:
            diff_source = "files_api"
            entries = await self._fetch_paginated_files(
                slug=slug,
                pull_number=valid_pr,
                headers=json_headers,
                installation_id=installation_id,
            )
            parsed_diff = parser.parse_patch_entries(entries)
        else:
            diff_source = "diff"
            parsed_diff = parser.parse_diff(raw_diff_text)
            del raw_diff_text

        metrics = parsed_diff.metrics
        logger.info(
            "github_pr_diff_fetched",
            repo=slug,
            pr_number=valid_pr,
            diff_source=diff_source,
            total_files=metrics.total_files,
            kept_files=metrics.kept_files,
            skipped_files=metrics.skipped_files,
            tokens_saved_pct=metrics.tokens_saved_pct,
        )
        return FetchedPullRequestDiff(parsed_diff=parsed_diff, diff_source=diff_source)

    async def fetch_pr_file_blob_shas(
        self,
        repo_full_name: str,
        pull_number: int,
        *,
        installation_id: int | None = None,
    ) -> dict[str, str]:
        """Fetch full 40-hex Git blob SHAs per normalized file path via `/pulls/{pr}/files`."""
        slug = validate_repo_full_name(repo_full_name)
        valid_pr = validate_positive_int(pull_number, name="pull_number")
        headers, used_public = await self._resolve_headers(
            slug=slug,
            installation_id=installation_id,
        )
        try:
            entries = await self._fetch_paginated_files(
                slug=slug,
                pull_number=valid_pr,
                headers=headers,
                installation_id=installation_id,
            )
        except GitHubAuthError:
            if not self._allow_public_fallback or used_public:
                raise
            entries = await self._fetch_paginated_files(
                slug=slug,
                pull_number=valid_pr,
                headers=_public_github_headers(),
                installation_id=None,
            )
        sha_map: dict[str, str] = {}
        for entry in entries:
            if not entry.blob_sha:
                continue
            norm_path = normalize_diff_path(entry.filename).rstrip("/")
            if norm_path:
                sha_map[norm_path] = entry.blob_sha.strip().lower()
        return sha_map

    async def enrich_missing_blob_shas(
        self,
        repo_full_name: str,
        pull_number: int,
        parsed_diff: ParsedPullRequestDiff,
        *,
        installation_id: int | None = None,
    ) -> ParsedPullRequestDiff:
        """Enrich kept non-deleted files lacking full 40-hex blob_sha via `/pulls/{pr}/files`."""
        needs_enrichment = any(
            pf.change_type != "deleted" and len(pf.blob_sha or "") < 40 for pf in parsed_diff.files
        )
        if not needs_enrichment:
            return parsed_diff
        try:
            sha_map = await self.fetch_pr_file_blob_shas(
                repo_full_name,
                pull_number,
                installation_id=installation_id,
            )
        except RateLimitTaskError:
            raise
        except GitHubAuthError as exc:
            if not self._allow_public_fallback:
                raise
            logger.warning(
                "github_pr_blob_sha_enrichment_degraded",
                repo=repo_full_name,
                pr_number=pull_number,
                error_type=type(exc).__name__,
            )
            return parsed_diff
        except (RetryableTaskError, GitHubPublishError) as exc:
            logger.warning(
                "github_pr_blob_sha_enrichment_degraded",
                repo=repo_full_name,
                pr_number=pull_number,
                error_type=type(exc).__name__,
            )
            return parsed_diff
        return apply_blob_shas_to_parsed_diff(parsed_diff, sha_map)


def apply_blob_shas_to_parsed_diff(
    parsed_diff: ParsedPullRequestDiff,
    sha_map: Mapping[str, str],
) -> ParsedPullRequestDiff:
    """Return a copy of `parsed_diff` with missing/short `blob_sha` values filled from `sha_map`."""
    if not sha_map or not parsed_diff.files:
        return parsed_diff
    norm_shas = {
        normalize_diff_path(k).rstrip("/"): v.strip().lower()
        for k, v in sha_map.items()
        if isinstance(v, str) and v.strip()
    }
    updated_files = []
    changed = False
    for pf in parsed_diff.files:
        if pf.change_type != "deleted" and len(pf.blob_sha or "") < 40:
            norm_path = normalize_diff_path(pf.file_path).rstrip("/")
            resolved_sha = norm_shas.get(norm_path)
            if resolved_sha and resolved_sha != pf.blob_sha:
                updated_files.append(replace(pf, blob_sha=resolved_sha))
                changed = True
                continue
        updated_files.append(pf)
    if not changed:
        return parsed_diff
    return replace(parsed_diff, files=tuple(updated_files))
