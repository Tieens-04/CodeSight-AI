"""CLI `codesight index <repo>` command for initial Zero-Code AST & doc indexing (CA-14)."""

from __future__ import annotations

import asyncio
import sys
from typing import Final
from urllib.parse import urlsplit

import httpx
import redis.asyncio as redis_async
import redis.exceptions
import sqlalchemy.exc as sa_exc
import structlog
import typer
from qdrant_client.http.exceptions import ApiException
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TaskID,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from codesight.core.config import Settings, get_settings
from codesight.core.errors import RateLimitTaskError, RetryableTaskError
from codesight.db.session import create_db_engine, create_session_factory
from codesight.rag.embedder import GeminiEmbedder
from codesight.rag.indexer import (
    AsyncRepoIndexLock,
    IndexProgressSnapshot,
    InitialIndexReport,
    InitialRepoIndexer,
    ProgressCallback,
    RepoIndexError,
)
from codesight.rag.models import EmbeddingConfigError, QdrantSchemaMismatchError
from codesight.rag.qdrant_store import QdrantZeroCodeStore
from codesight.vcs.github_auth import (
    GitHubAuthConfigError,
    GitHubAuthError,
    GitHubAuthProvider,
    validate_repo_full_name,
)

DEFAULT_ALLOWED_REPO_HOSTS: Final[frozenset[str]] = frozenset({"github.com", "www.github.com"})

logger = structlog.get_logger("codesight.cli.index")


def parse_github_repo_arg(
    raw_repo: str,
    *,
    allowed_hosts: frozenset[str] = DEFAULT_ALLOWED_REPO_HOSTS,
) -> str:
    """Parse either an `owner/repo` slug or an HTTPS `github.com/owner/repo` URL."""
    cleaned = raw_repo.strip()
    if not cleaned:
        raise ValueError("Repository argument must not be empty.")

    if "://" in cleaned:
        parsed = urlsplit(cleaned)
        if parsed.scheme.lower() != "https":
            raise ValueError("GitHub repository URL must use 'https://' scheme.")
        if parsed.username or parsed.password:
            raise ValueError("GitHub repository URL must not embed credentials.")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("GitHub repository URL contains an invalid port.") from exc
        if port is not None:
            raise ValueError("GitHub repository URL must not specify a custom port.")

        hostname = (parsed.hostname or "").lower()
        if hostname not in allowed_hosts:
            raise ValueError(f"Unsupported repository host {hostname!r}; expected github.com.")

        segments = [seg for seg in parsed.path.split("/") if seg]
        if len(segments) != 2:
            raise ValueError(
                "Expected GitHub repository URL format: https://github.com/<owner>/<repo>"
            )
        owner, repo = segments
        if repo.lower().endswith(".git"):
            repo = repo[:-4]
        candidate = f"{owner}/{repo}"
    else:
        candidate = cleaned

    try:
        return validate_repo_full_name(candidate)
    except GitHubAuthConfigError as exc:
        raise ValueError(str(exc)) from exc


async def run_cli_index(
    repo_full_name: str,
    *,
    branch: str | None = None,
    recreate: bool = False,
    allow_non_default_branch: bool = False,
    embed_code_chunks: bool | None = None,
    settings: Settings | None = None,
    http_client: httpx.AsyncClient | None = None,
    redis_client: redis_async.Redis | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    store: QdrantZeroCodeStore | None = None,
    embedder: GeminiEmbedder | None = None,
    api_base_url: str = "https://api.github.com",
    progress_callback: ProgressCallback | None = None,
) -> InitialIndexReport:
    """Execute initial repository indexing against PostgreSQL and Qdrant in RAM waves."""
    active_settings = settings if settings is not None else get_settings()
    effective_embed_code = (
        active_settings.index_embed_code_chunks if embed_code_chunks is None else embed_code_chunks
    )
    owned_http: httpx.AsyncClient | None = None
    owned_redis: redis_async.Redis | None = None
    owned_engine: AsyncEngine | None = None
    owned_store: QdrantZeroCodeStore | None = None
    owned_embedder: GeminiEmbedder | None = None

    try:
        active_http = http_client
        if active_http is None:
            owned_http = httpx.AsyncClient(
                http2=True,
                timeout=15.0,
                follow_redirects=False,
                limits=httpx.Limits(max_connections=24, max_keepalive_connections=16),
            )
            active_http = owned_http

        active_redis = redis_client
        if active_redis is None:
            owned_redis = redis_async.from_url(
                active_settings.redis_url,
                decode_responses=True,
                socket_timeout=active_settings.health_timeout_seconds,
                socket_connect_timeout=active_settings.health_timeout_seconds,
            )
            active_redis = owned_redis

        active_session_factory = session_factory
        if active_session_factory is None:
            owned_engine = create_db_engine(active_settings)
            active_session_factory = create_session_factory(owned_engine)

        active_store = store
        if active_store is None:
            owned_store = QdrantZeroCodeStore(settings=active_settings)
            active_store = owned_store

        active_embedder = embedder
        if active_embedder is None:
            if active_settings.has_gemini_api_key:
                owned_embedder = GeminiEmbedder(settings=active_settings)
                active_embedder = owned_embedder
            elif effective_embed_code:
                raise EmbeddingConfigError(
                    "GEMINI_API_KEY is required when embed_code_chunks=True."
                )

        auth: GitHubAuthProvider | None = (
            GitHubAuthProvider(
                active_settings,
                redis_client=active_redis,
                http_client=active_http,
                base_url=api_base_url,
            )
            if active_settings.has_github_credentials
            else None
        )

        repo_lock = AsyncRepoIndexLock(active_redis)
        indexer = InitialRepoIndexer(
            auth_provider=auth,
            embedder=active_embedder,
            store=active_store,
            http_client=active_http,
            session_factory=active_session_factory,
            repo_lock=repo_lock,
            base_url=api_base_url,
            allow_public_fallback=True,
            embed_code_chunks=effective_embed_code,
        )
        try:
            return await indexer.index_repository(
                repo_full_name,
                ref=branch,
                recreate=recreate,
                persist_cursor=True,
                allow_non_default_branch=allow_non_default_branch,
                progress_callback=progress_callback,
            )
        finally:
            await indexer.aclose()
    finally:
        if owned_embedder is not None:
            await owned_embedder.aclose()
        if owned_store is not None:
            await owned_store.aclose()
        if owned_redis is not None:
            try:
                await owned_redis.aclose()
            except (redis.exceptions.RedisError, OSError):
                pass
        if owned_http is not None:
            await owned_http.aclose()
        if owned_engine is not None:
            await owned_engine.dispose()


def render_index_report(console: Console, report: InitialIndexReport) -> None:
    """Render structured Zero-Code indexing summary Panel and metrics Table to stdout."""
    short_sha = report.commit_sha[:12]
    if report.persisted_cursor:
        cursor_label = f"[bold green]saved ({escape(short_sha)})[/bold green]"
    elif report.truncated_tree:
        cursor_label = "[bold yellow]not updated (incomplete: truncated Git tree)[/bold yellow]"
    elif report.indexed_branch != report.default_branch:
        cursor_label = "[yellow]not updated (non-default branch)[/yellow]"
    else:
        cursor_label = "[yellow]not updated (cursor not advanced)[/yellow]"
    summary_lines = [
        f"[bold cyan]{escape(report.repo_full_name)}[/bold cyan] "
        f"([green]{escape(report.indexed_branch)}[/green] @ [dim]{escape(short_sha)}[/dim])",
        f"Default Branch: [white]{escape(report.default_branch)}[/white] "
        f"| PostgreSQL `last_indexed_sha`: {cursor_label}",
        f"Elapsed: [bold]{report.elapsed_ms:.1f} ms[/bold] "
        f"| Truncated Tree: {'yes' if report.truncated_tree else 'no'}",
    ]
    console.print(Panel("\n".join(summary_lines), title="CodeSight-AI Repository Index Report"))

    table = Table(title="Zero-Code Qdrant Index Summary")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right", style="bold white")
    table.add_row("Git Tree Blobs", str(report.tree_entries))
    table.add_row("Eligible Files (AST + Arch Docs)", str(report.eligible_files))
    table.add_row("Unchanged Files Skipped (0 API calls)", str(report.unchanged_files_skipped))
    table.add_row("Files Fetched into RAM", str(report.fetched_files))
    table.add_row("Filtered / Unsupported Files Skipped", str(report.skipped_files))
    table.add_row("AST Code Chunks Extracted", str(report.code_chunks))
    table.add_row("Architecture Doc Sections Extracted", str(report.doc_chunks))
    table.add_row("Vectors Embedded (768-dim)", str(report.embedded_points))
    table.add_row("Qdrant Points Upserted", str(report.upserted_points))
    table.add_row("Stale Points Pruned", str(report.stale_points_deleted))
    console.print(table)


def index_command(
    repo: str = typer.Argument(
        ...,
        help="GitHub repository slug (`owner/repo`) or HTTPS URL (`https://github.com/owner/repo`).",
    ),
    branch: str | None = typer.Option(
        None,
        "--branch",
        "-b",
        help="Optional branch/ref to index (defaults to repository default_branch).",
    ),
    recreate: bool = typer.Option(
        False,
        "--recreate",
        help="Delete existing Qdrant points for this repository before indexing from scratch.",
    ),
    allow_non_default_branch: bool = typer.Option(
        False,
        "--allow-non-default-branch",
        help=(
            "Allow indexing a non-default branch via `--branch`. "
            "Non-default runs skip PostgreSQL cursor updates and stale pruning."
        ),
    ),
    embed_code_chunks: bool | None = typer.Option(
        None,
        "--embed-code-chunks/--no-embed-code-chunks",
        help=(
            "Embed AST code chunks via Gemini instead of using zero-API placeholder vectors "
            "(defaults to INDEX_EMBED_CODE_CHUNKS)."
        ),
    ),
) -> None:
    """Index a GitHub repository into Qdrant (Zero-Code) and save last_indexed_sha in Postgres."""
    err_console = Console(file=sys.stderr, soft_wrap=True)

    try:
        repo_slug = parse_github_repo_arg(repo)
    except ValueError as exc:
        err_console.print(f"[red]Invalid repository:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc

    settings = get_settings()
    if not settings.has_gemini_api_key:
        err_console.print(
            "[red]Missing configuration:[/red] Set [bold]GEMINI_API_KEY[/bold] in `.env` "
            "before running `codesight index`."
        )
        raise typer.Exit(code=2)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=err_console,
        transient=True,
    ) as progress:
        task_id: TaskID = progress.add_task(f"Indexing {escape(repo_slug)}...", total=None)

        def _on_progress(snap: IndexProgressSnapshot) -> None:
            total = max(snap.total_files, 1)
            completed = snap.processed_files if snap.total_files > 0 else 1
            progress.update(
                task_id,
                total=total,
                completed=completed,
                description=(
                    f"Indexing {escape(repo_slug)} "
                    f"(upserted={snap.upserted_points}, skipped={snap.unchanged_files})"
                ),
            )

        try:
            report = asyncio.run(
                run_cli_index(
                    repo_slug,
                    branch=branch,
                    recreate=recreate,
                    allow_non_default_branch=allow_non_default_branch,
                    embed_code_chunks=embed_code_chunks,
                    settings=settings,
                    progress_callback=_on_progress,
                )
            )
        except Exception as exc:
            if isinstance(exc, (EmbeddingConfigError, GitHubAuthConfigError, ValueError)):
                err_console.print(f"[red]Configuration error:[/red] {escape(str(exc))}")
                raise typer.Exit(code=2) from exc
            if isinstance(
                exc,
                (
                    RepoIndexError,
                    GitHubAuthError,
                    RateLimitTaskError,
                    RetryableTaskError,
                    QdrantSchemaMismatchError,
                    ApiException,
                    sa_exc.SQLAlchemyError,
                    OSError,
                ),
            ) or any(cls.__module__.startswith("grpc") for cls in type(exc).__mro__):
                err_console.print(f"[red]Indexing failed:[/red] {escape(str(exc))}")
                raise typer.Exit(code=1) from exc
            raise

    if not report.persisted_cursor and report.indexed_branch != report.default_branch:
        err_console.print(
            f"[yellow]Notice:[/yellow] Indexed non-default branch "
            f"'{escape(report.indexed_branch)}'; PostgreSQL `last_indexed_sha` and stale pruning "
            f"apply only to default branch '{escape(report.default_branch)}'."
        )

    out_console = Console(file=sys.stdout)
    render_index_report(out_console, report)
    if report.truncated_tree:
        err_console.print(
            f"[red]Incomplete index:[/red] Git tree for '{escape(report.repo_full_name)}' @ "
            f"{escape(report.commit_sha[:12])} remained truncated after subtree traversal; "
            "PostgreSQL `last_indexed_sha` was preserved."
        )
        raise typer.Exit(code=1)
    logger.debug(
        "cli_index_completed",
        repo=report.repo_full_name,
        commit_sha=report.commit_sha,
        upserted_points=report.upserted_points,
        elapsed_ms=report.elapsed_ms,
    )
