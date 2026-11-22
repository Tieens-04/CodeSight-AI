"""Thin CLI runner for Milestone 2 real-world dual-language live evaluation (CA-16)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections.abc import Sequence

from qdrant_client import AsyncQdrantClient
from sqlalchemy.ext.asyncio import async_sessionmaker

from codesight.core.config import Settings
from codesight.core.logging import configure_logging
from codesight.db.models import Base
from codesight.db.session import create_db_engine, create_session_factory
from codesight.rag.live_eval import (
    DEFAULT_LIVE_PY_REPO,
    DEFAULT_PROBES_PER_REPO,
    run_milestone2_live_evaluation,
    validate_m2_kpi_thresholds,
)
from codesight.rag.retriever import DEFAULT_DOC_SCORE_THRESHOLD


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run live Milestone 2 evaluation against real Python + TypeScript/TSX GitHub repos."
        )
    )
    parser.add_argument(
        "--py-repo",
        default=os.getenv("CODESIGHT_LIVE_PY_REPO", DEFAULT_LIVE_PY_REPO),
        help=f"Python GitHub repository slug (default: {DEFAULT_LIVE_PY_REPO}).",
    )
    parser.add_argument(
        "--ts-repo",
        default=os.getenv("CODESIGHT_LIVE_TS_REPO", ""),
        help=(
            "TypeScript/TSX GitHub repository slug "
            "(defaults to CODESIGHT_LIVE_TS_REPO or authenticated /user/repos discovery)."
        ),
    )
    parser.add_argument(
        "--collection",
        default=f"codesight_m2_live_eval_{uuid.uuid4().hex[:8]}",
        help="Isolated Qdrant collection name used during evaluation.",
    )
    parser.add_argument(
        "--keep-collection",
        action="store_true",
        help="Retain the evaluation Qdrant collection after completion instead of deleting it.",
    )
    parser.add_argument(
        "--max-probes-per-repo",
        type=int,
        default=DEFAULT_PROBES_PER_REPO,
        help=(
            "Maximum cross-file probe symbols evaluated per repo "
            f"(default: {DEFAULT_PROBES_PER_REPO})."
        ),
    )
    parser.add_argument(
        "--doc-threshold",
        type=float,
        default=DEFAULT_DOC_SCORE_THRESHOLD,
        help=f"Architecture doc cosine score threshold (default: {DEFAULT_DOC_SCORE_THRESHOLD}).",
    )
    return parser


async def _run_async(args: argparse.Namespace, settings: Settings) -> int:
    engine = create_db_engine(settings)
    qdrant_cleanup = AsyncQdrantClient(
        host=settings.qdrant_host,
        port=settings.qdrant_http_port,
        grpc_port=settings.qdrant_grpc_port,
        prefer_grpc=True,
        timeout=15,
    )
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        session_factory: async_sessionmaker = create_session_factory(engine)
        report = await run_milestone2_live_evaluation(
            settings=settings,
            py_repo=args.py_repo,
            ts_repo=args.ts_repo,
            collection_name=args.collection,
            session_factory=session_factory,
            max_probes_per_repo=args.max_probes_per_repo,
            doc_score_threshold=args.doc_threshold,
        )
        print(json.dumps(report.to_dict(), indent=2), file=sys.stdout)
        violations = validate_m2_kpi_thresholds(report)
        if violations:
            print("[FAIL] Milestone 2 live KPI validation failed:", file=sys.stderr)
            for item in violations:
                print(f"  - {item}", file=sys.stderr)
            return 1
        return 0
    finally:
        if not args.keep_collection:
            try:
                if await qdrant_cleanup.collection_exists(args.collection):
                    await qdrant_cleanup.delete_collection(args.collection)
            except Exception as exc:
                print(
                    f"[WARN] Failed cleaning up Qdrant collection '{args.collection}': {exc}",
                    file=sys.stderr,
                )
        await qdrant_cleanup.close()
        await engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entrypoint for executing the live Milestone 2 evaluation harness."""
    configure_logging(level="INFO", service_name="codesight-m2-eval", stream=sys.stderr)
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    settings = Settings(qdrant_collection_name=args.collection)

    if not settings.has_gemini_api_key:
        print(
            "[ERROR] GEMINI_API_KEY is required in .env to run live Milestone 2 evaluation.",
            file=sys.stderr,
        )
        return 2
    if not settings.has_github_credentials:
        print(
            "[ERROR] GITHUB_TOKEN or GitHub App credentials are required in .env to run "
            "live Milestone 2 evaluation.",
            file=sys.stderr,
        )
        return 2

    try:
        return asyncio.run(_run_async(args, settings))
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
