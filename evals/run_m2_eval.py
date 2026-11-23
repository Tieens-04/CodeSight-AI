"""Thin CLI runner for Milestone 2 real-world dual-language live evaluation (CA-16)."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from qdrant_client import AsyncQdrantClient

from codesight.core.config import Settings
from codesight.core.logging import configure_logging
from codesight.rag.live_eval import (
    DEFAULT_LIVE_PY_REPO,
    DEFAULT_PROBES_PER_REPO,
    M2RepoGroundTruth,
    isolated_eval_session_factory,
    load_m2_ground_truth,
    run_milestone2_live_evaluation,
    validate_eval_collection_name,
    validate_m2_kpi_thresholds,
)
from codesight.rag.qdrant_store import QdrantZeroCodeStore
from codesight.rag.retriever import DEFAULT_DOC_SCORE_THRESHOLD

DEFAULT_GROUND_TRUTH_PATH = Path(__file__).resolve().with_name("m2_ground_truth.json")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run live Milestone 2 evaluation against real Python + TypeScript/TSX GitHub repos."
        )
    )
    parser.add_argument(
        "--py-repo",
        default=None,
        help=(
            f"Python GitHub repository slug (defaults to CODESIGHT_LIVE_PY_REPO or "
            f"{DEFAULT_LIVE_PY_REPO})."
        ),
    )
    parser.add_argument(
        "--ts-repo",
        default=None,
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
        "--ground-truth",
        default=None,
        help=(
            "Optional path to curated Milestone 2 ground-truth JSON manifest "
            f"(defaults to {DEFAULT_GROUND_TRUTH_PATH.name} when present)."
        ),
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


def _resolve_ground_truth(raw_arg: str | None) -> dict[str, M2RepoGroundTruth]:
    if raw_arg is not None and raw_arg.strip():
        return load_m2_ground_truth(raw_arg.strip())
    env_path = os.getenv("CODESIGHT_M2_GROUND_TRUTH", "").strip()
    if env_path:
        return load_m2_ground_truth(env_path)
    if DEFAULT_GROUND_TRUTH_PATH.is_file():
        return load_m2_ground_truth(DEFAULT_GROUND_TRUTH_PATH)
    return {}


async def _run_async(
    args: argparse.Namespace,
    settings: Settings,
    *,
    py_repo: str,
    ts_repo: str,
    ground_truth: dict[str, M2RepoGroundTruth] | None = None,
) -> int:
    collection_name = validate_eval_collection_name(args.collection)
    qdrant_cleanup = AsyncQdrantClient(
        host=settings.qdrant_host,
        port=settings.qdrant_http_port,
        grpc_port=settings.qdrant_grpc_port,
        prefer_grpc=True,
        timeout=15,
    )
    eval_store: QdrantZeroCodeStore | None = None
    try:
        if await qdrant_cleanup.collection_exists(collection_name):
            raise ValueError(
                f"Qdrant collection '{collection_name}' already exists; pass a fresh "
                "--collection name to avoid overwriting or deleting existing data."
            )

        async with isolated_eval_session_factory(settings) as (eval_db_settings, session_factory):
            eval_store = QdrantZeroCodeStore(
                settings=eval_db_settings,
                client=qdrant_cleanup,
                collection_name=collection_name,
            )
            report = await run_milestone2_live_evaluation(
                settings=eval_db_settings,
                py_repo=py_repo,
                ts_repo=ts_repo,
                collection_name=collection_name,
                session_factory=session_factory,
                store=eval_store,
                ground_truth=ground_truth,
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
        if eval_store is not None and eval_store.created_collection and not args.keep_collection:
            try:
                if await qdrant_cleanup.collection_exists(collection_name):
                    await qdrant_cleanup.delete_collection(collection_name)
            except Exception as exc:
                print(
                    f"[WARN] Failed cleaning up Qdrant collection '{collection_name}': {exc}",
                    file=sys.stderr,
                )
        await qdrant_cleanup.close()


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

    resolved_py_repo = (
        args.py_repo
        if args.py_repo is not None
        else (
            (settings.codesight_live_py_repo or "").strip()
            or os.getenv("CODESIGHT_LIVE_PY_REPO", "").strip()
            or DEFAULT_LIVE_PY_REPO
        )
    )
    resolved_ts_repo = (
        args.ts_repo
        if args.ts_repo is not None
        else (
            (settings.codesight_live_ts_repo or "").strip()
            or os.getenv("CODESIGHT_LIVE_TS_REPO", "").strip()
        )
    )

    try:
        ground_truth = _resolve_ground_truth(args.ground_truth)
        return asyncio.run(
            _run_async(
                args,
                settings,
                py_repo=resolved_py_repo,
                ts_repo=resolved_ts_repo,
                ground_truth=ground_truth,
            )
        )
    except (ValueError, OSError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
