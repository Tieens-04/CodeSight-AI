"""Shared internal HTTP helpers for GitHub VCS clients and CLI commands."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urljoin, urlsplit

import httpx

from codesight.diff.models import PatchFileEntry

REDIRECT_STATUSES: Final[frozenset[int]] = frozenset({301, 302, 303, 307, 308})
METHOD_PRESERVING_REDIRECT_STATUSES: Final[frozenset[int]] = frozenset({307, 308})


def validate_positive_int(value: int, *, name: str) -> int:
    """Validate that value is a strict positive integer (rejecting booleans)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer.")
    return value


def origin_tuple(url: str) -> tuple[str, str, int | None]:
    """Return normalized (scheme, host, port) tuple for same-origin redirect verification."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    if port is None:
        port = 443 if scheme == "https" else (80 if scheme == "http" else None)
    return scheme, host, port


def resolve_same_origin_redirect(
    current_url: str,
    resp: httpx.Response,
    expected_origin: tuple[str, str, int | None],
    *,
    error_factory: Callable[[str, int], Exception],
) -> str:
    """Resolve a redirect Location header and block any cross-origin target."""
    location = resp.headers.get("location")
    if not isinstance(location, str) or not location:
        raise error_factory(
            "GitHub API returned redirect without Location header.",
            resp.status_code,
        )
    next_url = urljoin(current_url, location)
    if origin_tuple(next_url) != expected_origin:
        raise error_factory(
            "Blocked cross-origin redirect while contacting GitHub API.",
            resp.status_code,
        )
    return next_url


@dataclass(frozen=True, slots=True)
class BoundedStreamResult:
    """Decoded in-memory result of a same-origin bounded HTTP GET stream."""

    status_code: int
    headers: httpx.Headers
    body_bytes: bytes | None
    exceeded_limit: bool = False
    declared_bytes: int | None = None
    received_bytes: int | None = None


async def stream_same_origin_bounded(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    api_base: str,
    max_bytes: int,
    error_factory: Callable[[str, int | None], Exception],
    max_redirects: int = 3,
    early_return_statuses: frozenset[int] = frozenset(),
) -> BoundedStreamResult:
    """Stream a GET response in RAM with same-origin redirect checks and hard byte ceiling.

    Always consumes already-decompressed bytes directly from `resp.aiter_bytes()` /
    `resp.aread()` without reconstructing `httpx.Response` with `Content-Encoding` headers,
    eliminating double-gunzip `httpx.DecodingError` crashes on gzipped responses.
    """
    expected_origin = origin_tuple(api_base)
    current_url = url
    for _ in range(max_redirects + 1):
        async with client.stream(
            "GET",
            current_url,
            headers=headers,
            follow_redirects=False,
        ) as resp:
            if resp.status_code in REDIRECT_STATUSES:
                current_url = resolve_same_origin_redirect(
                    current_url,
                    resp,
                    expected_origin,
                    error_factory=error_factory,
                )
                continue

            if resp.status_code in early_return_statuses:
                return BoundedStreamResult(
                    status_code=resp.status_code,
                    headers=resp.headers,
                    body_bytes=None,
                )

            if resp.status_code != 200:
                err_body = await resp.aread()
                return BoundedStreamResult(
                    status_code=resp.status_code,
                    headers=resp.headers,
                    body_bytes=err_body,
                )

            raw_len = resp.headers.get("content-length")
            if raw_len is not None:
                try:
                    declared_len = int(raw_len.strip())
                except ValueError:
                    declared_len = -1
                if declared_len > max_bytes:
                    return BoundedStreamResult(
                        status_code=200,
                        headers=resp.headers,
                        body_bytes=None,
                        exceeded_limit=True,
                        declared_bytes=declared_len,
                    )

            chunks: list[bytes] = []
            total_bytes = 0
            async for chunk in resp.aiter_bytes():
                total_bytes += len(chunk)
                if total_bytes > max_bytes:
                    return BoundedStreamResult(
                        status_code=200,
                        headers=resp.headers,
                        body_bytes=None,
                        exceeded_limit=True,
                        received_bytes=total_bytes,
                    )
                chunks.append(chunk)

            return BoundedStreamResult(
                status_code=200,
                headers=resp.headers,
                body_bytes=b"".join(chunks),
                received_bytes=total_bytes,
            )

    raise error_factory("Exceeded maximum same-origin redirects contacting GitHub API.", None)


def parse_github_files_page(items: Sequence[Any]) -> list[PatchFileEntry]:
    """Parse one page of GitHub `GET /pulls/{number}/files` JSON items into PatchFileEntry list."""
    entries: list[PatchFileEntry] = []
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
        raw_sha = item.get("sha")
        blob_sha = raw_sha.strip() if isinstance(raw_sha, str) and raw_sha.strip() else None
        raw_changes = item.get("changes")
        changes = (
            raw_changes
            if (
                isinstance(raw_changes, int)
                and not isinstance(raw_changes, bool)
                and raw_changes >= 0
            )
            else None
        )
        entries.append(
            PatchFileEntry(
                filename=filename,
                status=str(item.get("status") or "modified"),
                previous_filename=prev_str,
                patch=patch_str,
                blob_sha=blob_sha,
                changes=changes,
            )
        )
    return entries
