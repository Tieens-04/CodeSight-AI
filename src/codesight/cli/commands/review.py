"""CLI `codesight review` command implementing fast HTTP/2 PR diff dry-run analysis."""

from __future__ import annotations

import asyncio
import base64
import binascii
import sys
import time
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, cast
from urllib.parse import quote, urlsplit

import httpx
import structlog
import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from codesight import __version__
from codesight.config_reader.repo_config import (
    CONFIG_FILENAMES,
    MAX_REPO_CONFIG_BYTES,
    RepoConfig,
    parse_repo_config_yaml,
)
from codesight.core.config import Settings, get_settings
from codesight.core.errors import is_rate_limited_403
from codesight.diff.file_filter import FileFilter
from codesight.diff.models import ParsedPullRequestDiff, PatchFileEntry
from codesight.diff.smart_diff_parser import SmartDiffParser
from codesight.vcs._http import (
    REDIRECT_STATUSES,
    BoundedStreamResult,
    origin_tuple,
    parse_github_files_page,
    resolve_same_origin_redirect,
    stream_same_origin_bounded,
)
from codesight.vcs.github_auth import (
    GITHUB_ACCEPT_HEADER,
    GITHUB_API_VERSION,
    GitHubAuthConfigError,
    GitHubAuthError,
    GitHubAuthProvider,
    validate_repo_full_name,
)

DEFAULT_ALLOWED_PR_HOSTS: Final[frozenset[str]] = frozenset({"github.com", "www.github.com"})
GITHUB_DIFF_ACCEPT_HEADER: Final[str] = "application/vnd.github.diff"
MAX_FILE_PAGES: Final[int] = 30
MAX_DIFF_BYTES: Final[int] = 20 * 1024 * 1024

logger = structlog.get_logger("codesight.cli.review")


class CLIReviewError(RuntimeError):
    """Raised when fetching or validating a Pull Request for CLI review fails."""


@dataclass(frozen=True, slots=True)
class ParsedPRUrl:
    """Validated GitHub Pull Request coordinates parsed from a PR URL."""

    owner: str
    repo: str
    pull_number: int

    @property
    def repo_full_name(self) -> str:
        """Return canonical `owner/repo` slug."""
        return f"{self.owner}/{self.repo}"


@dataclass(frozen=True, slots=True)
class PRDryRunBundle:
    """In-memory diff analysis report bundle for `codesight review --dry-run`."""

    pr_ref: ParsedPRUrl
    title: str
    state: str
    base_ref: str
    head_ref: str
    head_sha: str
    repo_config: RepoConfig
    parsed_diff: ParsedPullRequestDiff
    diff_source: Literal["diff", "files_api"]
    elapsed_ms: float


def parse_github_pr_url(
    pr_url: str,
    *,
    allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_PR_HOSTS,
) -> ParsedPRUrl:
    """Validate and extract owner, repo, and PR number from an HTTPS GitHub PR URL."""
    cleaned = pr_url.strip()
    parsed = urlsplit(cleaned)
    if parsed.scheme.lower() != "https":
        raise ValueError("Pull Request URL must use 'https://' scheme.")
    if parsed.username or parsed.password:
        raise ValueError("Pull Request URL must not embed credentials.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Pull Request URL contains an invalid port.") from exc
    if port is not None:
        raise ValueError("Pull Request URL must not specify a custom port.")

    hostname = (parsed.hostname or "").lower()
    if hostname not in allowed_hosts:
        raise ValueError(f"Unsupported PR host {hostname!r}; expected github.com.")

    segments = [seg for seg in parsed.path.split("/") if seg]
    if len(segments) < 4 or segments[2] != "pull":
        raise ValueError(
            "Expected GitHub PR URL format: https://github.com/<owner>/<repo>/pull/<number>"
        )

    raw_number = segments[3]
    if not raw_number.isascii() or not raw_number.isdigit():
        raise ValueError(f"Invalid Pull Request number {raw_number!r}.")
    pull_number = int(raw_number)
    if pull_number < 1:
        raise ValueError("Pull Request number must be >= 1.")

    try:
        slug = validate_repo_full_name(f"{segments[0]}/{segments[1]}")
    except GitHubAuthConfigError as exc:
        raise ValueError(str(exc)) from exc

    owner, repo = slug.split("/", 1)
    return ParsedPRUrl(owner=owner, repo=repo, pull_number=pull_number)


def format_line_ranges(lines: set[int] | Sequence[int]) -> str:
    """Format sorted 1-based line numbers into compact spans (e.g. `L10, L20-L23`)."""
    sorted_lines = sorted(set(lines))
    if not sorted_lines:
        return "—"

    ranges: list[str] = []
    start = sorted_lines[0]
    end = sorted_lines[0]
    for num in sorted_lines[1:]:
        if num == end + 1:
            end = num
        else:
            ranges.append(f"L{start}" if start == end else f"L{start}-L{end}")
            start = end = num
    ranges.append(f"L{start}" if start == end else f"L{start}-L{end}")
    return ", ".join(ranges)


def _public_headers(accept: str = GITHUB_ACCEPT_HEADER) -> dict[str, str]:
    return {
        "Accept": accept,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "User-Agent": f"codesight-ai-cli/{__version__}",
    }


def _make_cli_error(message: str, _status_code: int | None) -> CLIReviewError:
    return CLIReviewError(message)


async def _resolve_cli_headers(
    settings: Settings,
    *,
    repo_full_name: str,
    http_client: httpx.AsyncClient,
    api_base: str = "https://api.github.com",
) -> dict[str, str]:
    if not settings.has_github_credentials:
        return _public_headers()

    provider = GitHubAuthProvider(settings, http_client=http_client, base_url=api_base)
    try:
        headers = await provider.get_auth_headers(repo_full_name=repo_full_name)
    except GitHubAuthConfigError as exc:
        raise CLIReviewError(f"Invalid GitHub authentication configuration: {exc}") from exc
    except GitHubAuthError:
        return _public_headers()

    return {
        **headers,
        "User-Agent": f"codesight-ai-cli/{__version__}",
    }


async def _get_same_origin(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    api_base: str,
    max_redirects: int = 3,
) -> httpx.Response:
    expected_origin = origin_tuple(api_base)
    current_url = url
    for _ in range(max_redirects + 1):
        resp = await client.get(current_url, headers=headers, follow_redirects=False)
        if resp.status_code not in REDIRECT_STATUSES:
            return resp
        current_url = resolve_same_origin_redirect(
            current_url,
            resp,
            expected_origin,
            error_factory=_make_cli_error,
        )
    raise CLIReviewError("Exceeded maximum same-origin redirects contacting GitHub API.")


async def _get_same_origin_bounded_diff(
    client: httpx.AsyncClient,
    url: str,
    *,
    headers: dict[str, str],
    api_base: str,
    max_bytes: int,
    max_redirects: int = 3,
) -> BoundedStreamResult:
    result = await stream_same_origin_bounded(
        client,
        url,
        headers=headers,
        api_base=api_base,
        max_bytes=max_bytes,
        error_factory=_make_cli_error,
        max_redirects=max_redirects,
        early_return_statuses=frozenset({406, 422}),
    )
    if result.exceeded_limit:
        logger.warning(
            "cli_review_diff_exceeded_max_bytes",
            declared_bytes=result.declared_bytes,
            received_bytes=result.received_bytes,
            max_bytes=max_bytes,
        )
    return result


async def _gather_or_cancel[T](*coros: Coroutine[Any, Any, T]) -> list[T]:
    tasks = [asyncio.create_task(coro) for coro in coros]
    try:
        return list(await asyncio.gather(*tasks))
    except BaseException as exc:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        if isinstance(exc, asyncio.CancelledError):
            current = asyncio.current_task()
            if current is not None and current.cancelling() > 0:
                raise
            raise CLIReviewError("Network error contacting GitHub API: CancelledError.") from exc
        if isinstance(exc, httpx.HTTPError):
            raise CLIReviewError(
                f"Network error contacting GitHub API: {type(exc).__name__}."
            ) from exc
        raise


async def _gather_pair_or_cancel[A, B](
    coro_a: Coroutine[Any, Any, A],
    coro_b: Coroutine[Any, Any, B],
) -> tuple[A, B]:
    results = await _gather_or_cancel(
        cast(Coroutine[Any, Any, Any], coro_a),
        cast(Coroutine[Any, Any, Any], coro_b),
    )
    return cast(A, results[0]), cast(B, results[1])


def _raise_for_github_status(
    status_code: int,
    headers: httpx.Headers,
    *,
    context: str,
) -> None:
    if status_code == 200:
        return
    if status_code == 404:
        raise CLIReviewError(f"{context} not found on GitHub (HTTP 404).")
    if status_code == 429 or (status_code == 403 and is_rate_limited_403(headers)):
        raise CLIReviewError(
            f"GitHub API rate limit reached (HTTP {status_code}) while fetching {context}. "
            "Set GITHUB_TOKEN or retry later."
        )
    if status_code in {401, 403}:
        raise CLIReviewError(
            f"GitHub API authentication/permission failure (HTTP {status_code}) for {context}."
        )
    raise CLIReviewError(f"GitHub API returned unexpected HTTP {status_code} for {context}.")


def _decode_repo_config_response(resp: httpx.Response, *, filename: str) -> RepoConfig | None:
    if resp.status_code != 200:
        return None
    try:
        envelope = resp.json()
    except ValueError:
        return RepoConfig()

    if not isinstance(envelope, dict) or envelope.get("type") != "file":
        return RepoConfig()
    declared_size = envelope.get("size")
    if isinstance(declared_size, int) and declared_size > MAX_REPO_CONFIG_BYTES:
        return RepoConfig()
    if envelope.get("encoding") != "base64" or not isinstance(envelope.get("content"), str):
        return RepoConfig()

    compact_b64 = "".join(envelope["content"].split())
    try:
        decoded_bytes = base64.b64decode(compact_b64.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        return RepoConfig()

    return parse_repo_config_yaml(decoded_bytes, source=filename)


async def _fetch_optional_repo_config(
    client: httpx.AsyncClient,
    *,
    api_base: str,
    slug: str,
    base_ref: str,
    headers: dict[str, str],
) -> RepoConfig:
    encoded_ref = quote(base_ref.strip() or "main", safe="")
    urls = [f"{api_base}/repos/{slug}/contents/{fn}?ref={encoded_ref}" for fn in CONFIG_FILENAMES]
    try:
        responses = await _gather_or_cancel(
            *(_get_same_origin(client, url, headers=headers, api_base=api_base) for url in urls)
        )
    except CLIReviewError:
        return RepoConfig()

    for filename, resp in zip(CONFIG_FILENAMES, responses, strict=True):
        cfg = _decode_repo_config_response(resp, filename=filename)
        if cfg is not None:
            return cfg
    return RepoConfig()


async def _fetch_paginated_files(
    client: httpx.AsyncClient,
    *,
    api_base: str,
    slug: str,
    pull_number: int,
    headers: dict[str, str],
) -> list[PatchFileEntry]:
    entries: list[PatchFileEntry] = []
    for page in range(1, MAX_FILE_PAGES + 1):
        url = f"{api_base}/repos/{slug}/pulls/{pull_number}/files?per_page=100&page={page}"
        try:
            resp = await _get_same_origin(client, url, headers=headers, api_base=api_base)
        except httpx.HTTPError as exc:
            raise CLIReviewError(f"Network error fetching PR files: {type(exc).__name__}.") from exc

        _raise_for_github_status(
            resp.status_code,
            resp.headers,
            context=f"PR {slug}#{pull_number} files",
        )
        try:
            items = resp.json()
        except ValueError as exc:
            raise CLIReviewError("GitHub PR files endpoint returned invalid JSON.") from exc

        if not isinstance(items, list):
            raise CLIReviewError("GitHub PR files endpoint returned non-list payload.")

        entries.extend(parse_github_files_page(items))

        if len(items) < 100:
            break

    return entries


async def fetch_pull_request_dry_run(
    pr_ref: ParsedPRUrl,
    *,
    settings: Settings | None = None,
    http_client: httpx.AsyncClient | None = None,
    api_base_url: str = "https://api.github.com",
) -> PRDryRunBundle:
    """Fetch PR metadata + diff over HTTP/2 and analyze hunks & token savings in RAM."""
    started = time.perf_counter()
    active_settings = settings if settings is not None else get_settings()
    slug = pr_ref.repo_full_name
    api_base = api_base_url.rstrip("/")

    owned_client: httpx.AsyncClient | None = None
    client = http_client
    if client is None:
        owned_client = httpx.AsyncClient(
            http2=True,
            timeout=15.0,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
        )
        client = owned_client

    try:
        json_headers = await _resolve_cli_headers(
            active_settings,
            repo_full_name=slug,
            http_client=client,
            api_base=api_base,
        )
        diff_headers = {**json_headers, "Accept": GITHUB_DIFF_ACCEPT_HEADER}
        pr_url = f"{api_base}/repos/{slug}/pulls/{pr_ref.pull_number}"

        meta_resp, diff_res = await _gather_pair_or_cancel(
            _get_same_origin(client, pr_url, headers=json_headers, api_base=api_base),
            _get_same_origin_bounded_diff(
                client,
                pr_url,
                headers=diff_headers,
                api_base=api_base,
                max_bytes=MAX_DIFF_BYTES,
            ),
        )

        _raise_for_github_status(
            meta_resp.status_code,
            meta_resp.headers,
            context=f"Pull Request {slug}#{pr_ref.pull_number}",
        )
        try:
            meta_json = meta_resp.json()
        except ValueError as exc:
            raise CLIReviewError("GitHub PR metadata endpoint returned invalid JSON.") from exc
        if not isinstance(meta_json, dict):
            raise CLIReviewError("GitHub PR metadata endpoint returned unexpected shape.")

        base_obj = meta_json.get("base")
        head_obj = meta_json.get("head")
        base_ref = str(base_obj.get("ref") or "main") if isinstance(base_obj, dict) else "main"
        base_repo = base_obj.get("repo") if isinstance(base_obj, dict) else None
        config_ref = (
            str(base_repo.get("default_branch") or base_ref)
            if isinstance(base_repo, dict)
            else base_ref
        )
        head_ref = str(head_obj.get("ref") or "HEAD") if isinstance(head_obj, dict) else "HEAD"
        head_sha = str(head_obj.get("sha") or "") if isinstance(head_obj, dict) else ""

        if meta_resp.headers.get("x-ratelimit-remaining") == "0":
            repo_config = RepoConfig()
        else:
            repo_config = await _fetch_optional_repo_config(
                client,
                api_base=api_base,
                slug=slug,
                base_ref=config_ref,
                headers=json_headers,
            )

        parser = SmartDiffParser(file_filter=FileFilter.from_repo_config(repo_config))
        diff_source: Literal["diff", "files_api"]
        if diff_res.exceeded_limit or diff_res.status_code in {406, 422}:
            diff_source = "files_api"
            entries = await _fetch_paginated_files(
                client,
                api_base=api_base,
                slug=slug,
                pull_number=pr_ref.pull_number,
                headers=json_headers,
            )
            parsed_diff = parser.parse_patch_entries(entries)
        elif diff_res.status_code == 200 and diff_res.body_bytes is not None:
            diff_source = "diff"
            raw_diff_text = diff_res.body_bytes.decode("utf-8", errors="replace")
            del diff_res
            parsed_diff = parser.parse_diff(raw_diff_text)
            del raw_diff_text
        else:
            _raise_for_github_status(
                diff_res.status_code,
                diff_res.headers,
                context=f"Pull Request {slug}#{pr_ref.pull_number} diff",
            )
            raise CLIReviewError("Unexpected diff fetch error.")  # pragma: no cover

        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return PRDryRunBundle(
            pr_ref=pr_ref,
            title=str(meta_json.get("title") or "(untitled PR)"),
            state=str(meta_json.get("state") or "unknown"),
            base_ref=base_ref,
            head_ref=head_ref,
            head_sha=head_sha,
            repo_config=repo_config,
            parsed_diff=parsed_diff,
            diff_source=diff_source,
            elapsed_ms=elapsed_ms,
        )
    finally:
        if owned_client is not None:
            await owned_client.aclose()


def render_dry_run_report(console: Console, bundle: PRDryRunBundle) -> None:
    """Render Rich summary panel, kept-file hunk coordinates, and skipped token savings."""
    m = bundle.parsed_diff.metrics
    short_sha = bundle.head_sha[:12] if bundle.head_sha else "unknown"
    summary_lines = [
        f"[bold cyan]{escape(bundle.pr_ref.repo_full_name)}#{bundle.pr_ref.pull_number}"
        f"[/bold cyan] [bold]{escape(bundle.title)}[/bold] "
        f"([magenta]{escape(bundle.state)}[/magenta])",
        f"Branch: [green]{escape(bundle.base_ref)}[/green] <- "
        f"[yellow]{escape(bundle.head_ref)}[/yellow] @ [dim]{escape(short_sha)}[/dim] "
        f"| Source: [dim]{bundle.diff_source}[/dim] "
        f"| Elapsed: [bold]{bundle.elapsed_ms:.1f} ms[/bold]",
        f"Policy: min_debt_score={bundle.repo_config.min_debt_score}, "
        f"review_action={bundle.repo_config.review_action}, "
        f"config_hash={bundle.repo_config.config_hash}",
        f"Files: total={m.total_files}, kept={m.kept_files}, skipped={m.skipped_files} "
        f"| Tokens: {m.raw_estimated_tokens} -> {m.kept_estimated_tokens} "
        f"(saved [bold green]{m.saved_estimated_tokens} / {m.tokens_saved_pct:.2f}%[/bold green])",
    ]
    console.print(Panel("\n".join(summary_lines), title="CodeSight-AI Dry-Run Diff Report"))

    if bundle.parsed_diff.files:
        kept_table = Table(title="Kept Files & Hunk Coordinates (RIGHT-side Guard)")
        kept_table.add_column("File", style="cyan", overflow="fold")
        kept_table.add_column("Change", style="magenta")
        kept_table.add_column("Hunks", style="white", overflow="fold")
        kept_table.add_column("Commentable RIGHT Lines", style="green", overflow="fold")

        for parsed_file in bundle.parsed_diff.files:
            if parsed_file.hunks:
                hunk_desc = "; ".join(
                    f"#{h.hunk_id}: -{h.old_start},{h.old_count} +{h.new_start},{h.new_count}"
                    for h in parsed_file.hunks
                )
            else:
                hunk_desc = "0 hunks"
            kept_table.add_row(
                escape(parsed_file.file_path),
                escape(parsed_file.change_type),
                escape(hunk_desc),
                escape(format_line_ranges(parsed_file.valid_commentable_lines)),
            )
        console.print(kept_table)
    else:
        console.print("[yellow]No reviewable code files remained after filtering.[/yellow]")

    if bundle.parsed_diff.skipped_files:
        skipped_table = Table(title="Filtered / Skipped Files (Token Savings)")
        skipped_table.add_column("File", style="dim", overflow="fold")
        skipped_table.add_column("Reason", style="yellow")
        skipped_table.add_column("Pattern", style="dim", overflow="fold")
        skipped_table.add_column("Lines", justify="right")
        skipped_table.add_column("Saved Tokens", justify="right", style="green")

        for rec in bundle.parsed_diff.skipped_files:
            skipped_table.add_row(
                escape(rec.file_path),
                escape(str(rec.reason)),
                escape(rec.matched_pattern or "—"),
                str(rec.raw_lines),
                str(rec.estimated_tokens),
            )
        console.print(skipped_table)


def review_command(
    pr_url: str = typer.Argument(
        ...,
        help="HTTPS GitHub Pull Request URL (e.g. https://github.com/owner/repo/pull/123).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Analyze PR diff, noise filtering, token savings, and hunk coordinates.",
    ),
) -> None:
    """Inspect a GitHub Pull Request diff and print SmartDiffParser diagnostics."""
    err_console = Console(file=sys.stderr, soft_wrap=True)
    if not dry_run:
        err_console.print(
            "[red]Error:[/red] Full AI review execution is wired in Sprint 3. "
            "Pass [bold]--dry-run[/bold] to run diff & hunk inspection now."
        )
        raise typer.Exit(code=2)

    try:
        pr_ref = parse_github_pr_url(pr_url)
    except ValueError as exc:
        err_console.print(f"[red]Invalid PR URL:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    try:
        bundle = asyncio.run(fetch_pull_request_dry_run(pr_ref, settings=get_settings()))
    except CLIReviewError as exc:
        err_console.print(f"[red]GitHub fetch failed:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc

    out_console = Console(file=sys.stdout)
    render_dry_run_report(out_console, bundle)

    m = bundle.parsed_diff.metrics
    logger.debug(
        "cli_review_dry_run_completed",
        repo=pr_ref.repo_full_name,
        pr_number=pr_ref.pull_number,
        diff_source=bundle.diff_source,
        kept_files=m.kept_files,
        skipped_files=m.skipped_files,
        tokens_saved_pct=m.tokens_saved_pct,
        elapsed_ms=bundle.elapsed_ms,
    )
