"""CodeSight-AI Typer CLI with strict stdout/stderr stream isolation."""

from __future__ import annotations

import sys

import structlog
import typer
from rich.console import Console

from codesight import __version__
from codesight.core.config import get_settings
from codesight.core.logging import configure_logging

app = typer.Typer(
    name="codesight",
    help="CodeSight-AI Autonomous PR Reviewer & Technical Debt Radar CLI.",
    no_args_is_help=True,
    add_completion=False,
)

logger = structlog.get_logger("codesight.cli")


@app.callback()
def main(
    verbose: bool = typer.Option(
        False,
        "--verbose",
        "-v",
        help="Emit structured JSON diagnostic logs to stderr.",
    ),
) -> None:
    """Configure CLI logging to stderr while keeping stdout clean."""
    level = "DEBUG" if verbose else "WARNING"
    configure_logging(level=level, service_name="codesight-cli", stream=sys.stderr)


@app.command()
def version() -> None:
    """Display CodeSight-AI version and configured host port endpoints."""
    settings = get_settings()
    console = Console(file=sys.stdout, soft_wrap=True)
    console.print(
        f"[bold cyan]CodeSight-AI[/bold cyan] v{__version__} "
        f"(API :{settings.api_port} | Postgres :{settings.postgres_port} "
        f"| Redis :{settings.redis_port} | Qdrant :{settings.qdrant_http_port})"
    )
    logger.debug(
        "cli_version_displayed",
        version=__version__,
        api_port=settings.api_port,
        postgres_port=settings.postgres_port,
        redis_port=settings.redis_port,
        qdrant_http_port=settings.qdrant_http_port,
    )
