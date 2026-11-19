"""Async HTTP/2 GitHub Pull Request diff client with RAM-only bounds and retry mapping."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Self
from urllib.parse import urljoin, urlsplit

import httpx
import structlog

from codesight.core.errors import (
    RateLimitTaskError,
    RetryableTaskError,
    parse_retry_after_header,
)
from codesight.diff.file_filter import FileFilter
from codesight.diff.models import ParsedPullRequestDiff, PatchFileEntry
from codesight.diff.smart_diff_parser import SmartDiffParser
from codesight.vcs.github_auth import (
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
_REDIRECT_STATUSES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})

logger = structlog.get_logger("codesight.vcs.pr_client")


@dataclass(frozen=True, slots=True)
class FetchedPullRequestDiff:
    """In-memory filtered PR diff bundle paired with the upstream fetch strategy used."""

    parsed_diff: ParsedPullRequestDiff
    diff_source: Literal["diff", "files_api"]


def _validate_positive_int(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def _origin_tuple(url: str) -> tuple[str, str, int | None]:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port is None:
        port = 443 if scheme == "https" else (80 if scheme == "http" else None)
    return scheme, host, port


def _resolve_same_origin_redirect(
    current_url: str,
    resp: httpx.Response,
    expected_origin: tuple[str, str, int | None],
) -> str:
    location = resp.headers.get("location")
    if not isinstance(location, str) or not location:
        raise GitHubPublishError(
            "GitHub API returned redirect without Location header.",
            status_code=resp.status_code,
        )
    next_url = urljoin(current_url, location)
    if _origin_tuple(next_url) != expected_origin:
        raise GitHubPublishError(
            "Blocked cross-origin redirect while contacting GitHub API.",
            status_code=resp.status_code,
        )
    return next_url


def _is_rate_limited_403(headers: httpx.Headers) -> bool:
    return bool(headers.get("retry-after") or headers.get("x-ratelimit-remaining") == "0")


def _raise_for_github_status(resp: httpx.Response, *, slug: str, context: str) -> None:
    code = resp.status_code
    if code == 200:
        return
    if code == 429 or (code == 403 and _is_rate_limited_403(resp.headers)):
        delay = parse_retry_after_header(resp.headers)
        logger.warning(
            "github_pr_client_rate_limited",
            repo=slug,
            status_code=code,
            retry_after_seconds=delay,
        )
        raise RateLimitTaskError(
            f"GitHub rate limit ({code}) while fetching {context}.",
            retry_after_seconds=delay,
        )
    if code in {401, 403}:
        logger.error("github_pr_client_auth_error", repo=slug, status_code=code)
        raise GitHubAuthError(
            f"GitHub API returned HTTP {code} while fetching {context}.",
            status_code=code,
        )
    if 500 <= code < 600:
        logger.warning("github_pr_client_server_error", repo=slug, status_code=code)
        raise RetryableTaskError(f"GitHub API returned HTTP {code} while fetching {context}.")
    logger.error("github_pr_client_http_error", repo=slug, status_code=code)
    raise GitHubPublishError(
        f"GitHub API rejected fetch for {context} with HTTP {code}.",
        status_code=code,
    )


class GitHubPullRequestClient:
    """Fetches PR unified diffs (with paginated /files fallback) in RAM over HTTP/2."""

    def __init__(
        self,
        auth_provider: GitHubAuthProvider,
        *,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = "https://api.github.com",
        timeout_seconds: float = 15.0,
        max_diff_bytes: int = MAX_DIFF_BYTES,
    ) -> None:
        self._auth = auth_provider
        self._client = http_client
        self._owns_client = http_client is None
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_diff_bytes = max_diff_bytes

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
        expected_origin = _origin_tuple(self._base_url)
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

            if resp.status_code not in _REDIRECT_STATUSES:
                return resp
            current_url = _resolve_same_origin_redirect(current_url, resp, expected_origin)
        raise GitHubPublishError("Exceeded maximum same-origin redirects contacting GitHub API.")

    async def _fetch_bounded_raw_diff(
        self,
        url: str,
        *,
        slug: str,
        pull_number: int,
        headers: dict[str, str],
        max_redirects: int = 3,
    ) -> str | None:
        client = self._ensure_client()
        expected_origin = _origin_tuple(self._base_url)
        current_url = url
        for _ in range(max_redirects + 1):
            try:
                async with client.stream(
                    "GET",
                    current_url,
                    headers=headers,
                    follow_redirects=False,
                ) as resp:
                    if resp.status_code in _REDIRECT_STATUSES:
                        current_url = _resolve_same_origin_redirect(
                            current_url, resp, expected_origin
                        )
                        continue

                    if resp.status_code in {406, 422}:
                        logger.info(
                            "github_pr_diff_fallback_to_files_api",
                            repo=slug,
                            pr_number=pull_number,
                            status_code=resp.status_code,
                        )
                        return None

                    if resp.status_code != 200:
                        await resp.aread()
                        _raise_for_github_status(
                            resp,
                            slug=slug,
                            context=f"PR {slug}#{pull_number} diff",
                        )

                    raw_len = resp.headers.get("content-length")
                    if raw_len is not None:
                        try:
                            declared_len = int(raw_len.strip())
                        except ValueError:
                            declared_len = -1
                        if declared_len > self._max_diff_bytes:
                            logger.warning(
                                "github_pr_diff_exceeded_max_bytes",
                                repo=slug,
                                pr_number=pull_number,
                                declared_bytes=declared_len,
                                max_bytes=self._max_diff_bytes,
                            )
                            return None

                    chunks: list[bytes] = []
                    total_bytes = 0
                    async for chunk in resp.aiter_bytes():
                        total_bytes += len(chunk)
                        if total_bytes > self._max_diff_bytes:
                            logger.warning(
                                "github_pr_diff_exceeded_max_bytes",
                                repo=slug,
                                pr_number=pull_number,
                                received_bytes=total_bytes,
                                max_bytes=self._max_diff_bytes,
                            )
                            return None
                        chunks.append(chunk)

                    return b"".join(chunks).decode("utf-8", errors="replace")
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                logger.warning(
                    "github_pr_client_transport_error",
                    repo=slug,
                    error_type=type(exc).__name__,
                )
                raise RetryableTaskError(
                    f"Transient network failure streaming diff for {slug}#{pull_number}."
                ) from exc

        raise GitHubPublishError("Exceeded maximum same-origin redirects contacting GitHub API.")

    async def _fetch_paginated_files(
        self,
        *,
        slug: str,
        pull_number: int,
        headers: dict[str, str],
    ) -> list[PatchFileEntry]:
        entries: list[PatchFileEntry] = []
        for page in range(1, MAX_FILE_PAGES + 1):
            url = (
                f"{self._base_url}/repos/{slug}/pulls/{pull_number}/files?per_page=100&page={page}"
            )
            resp = await self._get_same_origin(url, slug=slug, headers=headers)
            _raise_for_github_status(resp, slug=slug, context=f"PR {slug}#{pull_number} files")
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

            for item in items:
                if not isinstance(item, dict):
                    continue
                filename = item.get("filename")
                if not isinstance(filename, str) or not filename:
                    continue
                raw_patch = item.get("patch")
                patch_str = raw_patch if isinstance(raw_patch, str) else None
                raw_prev = item.get("previous_filename")
                prev_str = raw_prev if isinstance(raw_prev, str) else None
                entries.append(
                    PatchFileEntry(
                        filename=filename,
                        status=str(item.get("status") or "modified"),
                        previous_filename=prev_str,
                        patch=patch_str,
                    )
                )

            if len(items) < 100:
                break

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
        valid_pr = _validate_positive_int(pull_number, name="pull_number")
        file_filter = (
            FileFilter.from_repo_config(repo_config) if repo_config is not None else FileFilter()
        )

        json_headers = await self._auth.get_auth_headers(
            installation_id=installation_id,
            repo_full_name=slug,
        )
        diff_headers = {**json_headers, "Accept": GITHUB_DIFF_ACCEPT_HEADER}
        pr_url = f"{self._base_url}/repos/{slug}/pulls/{valid_pr}"

        raw_diff_text = await self._fetch_bounded_raw_diff(
            pr_url,
            slug=slug,
            pull_number=valid_pr,
            headers=diff_headers,
        )

        parser = SmartDiffParser(file_filter=file_filter)
        diff_source: Literal["diff", "files_api"]
        if raw_diff_text is None:
            diff_source = "files_api"
            entries = await self._fetch_paginated_files(
                slug=slug,
                pull_number=valid_pr,
                headers=json_headers,
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
