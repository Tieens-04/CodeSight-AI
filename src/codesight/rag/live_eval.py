"""Real-world Milestone 2 dual-language live evaluation harness and KPI validator (CA-16)."""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import math
import os
import re
import statistics
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Hashable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any, Final

import asyncpg
import httpx
import redis.asyncio as redis_async
import redis.exceptions
import structlog
from qdrant_client import models
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from codesight import __version__
from codesight.ast_engine.models import CodeSymbolChunk, PRFileContext, SymbolKind
from codesight.ast_engine.registry import ASTParserRegistry
from codesight.core.config import Settings
from codesight.db.models import Base
from codesight.db.session import create_db_engine, create_session_factory
from codesight.diff.file_filter import normalize_diff_path
from codesight.diff.models import ParsedFileDiff, ParsedPullRequestDiff
from codesight.diff.smart_diff_parser import SmartDiffParser
from codesight.rag.doc_chunker import is_architecture_doc_path
from codesight.rag.embedder import GeminiEmbedder
from codesight.rag.indexer import InitialIndexReport
from codesight.rag.models import (
    EmbeddingConfigError,
    QdrantStoredPoint,
    ZeroCodeViolationError,
    validate_positive_int,
)
from codesight.rag.qdrant_store import QdrantZeroCodeStore
from codesight.rag.retriever import (
    AMBIGUOUS_METHOD_SYMBOLS,
    BUILTIN_NOISE_SYMBOLS,
    DEFAULT_DOC_LIMIT,
    DEFAULT_DOC_SCORE_THRESHOLD,
    HybridContextRetriever,
    is_noise_symbol,
)
from codesight.rag.token_packer import DEFAULT_AUX_TOKEN_BUDGET, TokenBudgetPacker
from codesight.vcs.blob_fetcher import BlobSliceRequest, GitHubBlobFetcher
from codesight.vcs.github_auth import (
    GITHUB_ACCEPT_HEADER,
    GITHUB_API_VERSION,
    GitHubAuthConfigError,
    GitHubAuthError,
    GitHubAuthProvider,
    validate_repo_full_name,
)

DEFAULT_LIVE_PY_REPO: Final[str] = "Tieens-04/CodeSight-AI"
DEFAULT_GITHUB_API_URL: Final[str] = "https://api.github.com"
DEFAULT_PROBES_PER_REPO: Final[int] = 3
M2_MIN_RECALL_AT_5: Final[float] = 0.85
M2_MAX_RETRIEVAL_MS: Final[float] = 850.0
M2_MIN_TOKENS_SAVED_PCT: Final[float] = 45.0
M2_MAX_AUX_TOKENS: Final[int] = DEFAULT_AUX_TOKEN_BUDGET

_TS_REPO_LANGUAGES: Final[frozenset[str]] = frozenset({"TypeScript", "TSX"})
_SCROLL_PAGE_SIZE: Final[int] = 256
_MAX_FILE_SLICE_LINES: Final[int] = 200_000
_MIN_CANARY_LEN: Final[int] = 24
_FALLBACK_CANARY_LEN: Final[int] = 12
_MAX_CANARY_LEN: Final[int] = 120
_MAX_BODY_CANARIES_PER_REPO: Final[int] = 6
_STATIC_FALLBACK_CANARY: Final[str] = "CODESIGHT_NFR3_SYNTHETIC_BODY_CANARY"
_SAFE_COLLECTION_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SAFE_DB_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^codesight_m2_eval_[0-9a-f]{8}$")

_TS_CONTROL_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        "if",
        "else",
        "for",
        "while",
        "do",
        "switch",
        "case",
        "catch",
        "try",
        "finally",
        "return",
        "throw",
        "typeof",
        "instanceof",
        "delete",
        "void",
        "await",
        "yield",
        "import",
        "export",
        "function",
        "class",
        "interface",
        "type",
        "constructor",
        "super",
        "new",
    }
)
_TS_FUNC_DECL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\n)[ \t]*(?:export[ \t]+)?(?:default[ \t]+)?(?:async[ \t]+)?function[ \t]+"
    r"([A-Za-z_$][\w$]*)[ \t]*(?:<[^>{}()]*>)?[ \t]*\("
)
_TS_ARROW_DECL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\n)[ \t]*(?:export[ \t]+)?(?:const|let|var)[ \t]+([A-Za-z_$][\w$]*)"
    r"[ \t]*(?::[^=;]+)?=[ \t]*(?:async[ \t]*)?(?:<[^>{}()]*>[ \t]*)?"
    r"(?:\([^)]*\)|[A-Za-z_$][\w$]*)[ \t]*(?::[^=>;\n]+)?=>"
)
_TS_METHOD_DECL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:^|\n)[ \t]+(?:(?:public|private|protected|static|readonly|override|async)[ \t]+)*"
    r"([A-Za-z_$][\w$]*)[ \t]*(?:<[^>{}()]*>)?[ \t]*\([^;{}]*\)[ \t]*(?::[^{;\n]+)?\{"
)
_TS_CALL_RE: Final[re.Pattern[str]] = re.compile(
    r"(?:([A-Za-z_$][\w$]*)[ \t]*\.[ \t]*)?([A-Za-z_$][\w$]*)[ \t]*(?:<[^>;{}()\n]*>)?[ \t]*\("
)
_JSX_COMPONENT_RE: Final[re.Pattern[str]] = re.compile(r"<\s*([A-Z][\w$]*)")
_MD_HEADING_RE: Final[re.Pattern[str]] = re.compile(r"^ {0,3}(#{1,4})[ \t]+(.+?)[ \t]*#*[ \t]*$")
_MD_FENCE_RE: Final[re.Pattern[str]] = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_WORD_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"[^A-Za-z0-9]+")
_CAMEL_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"([a-z0-9])([A-Z])")
_BARE_DELIMITER_LINES: Final[frozenset[str]] = frozenset(
    {"}", "};", "},", ")", ");", "],", "]", '"""', "'''"}
)

type IndexFn = Callable[..., Awaitable[InitialIndexReport]]
type PointsLoaderFn = Callable[
    [QdrantZeroCodeStore, str],
    Awaitable[tuple[QdrantStoredPoint, ...]],
]
type AuditorFn = Callable[..., int]
type DiskScannerFn = Callable[[str, str, Sequence[str]], list[str]]

logger = structlog.get_logger("codesight.rag.live_eval")


def validate_eval_collection_name(raw_name: str) -> str:
    """Validate an isolated Qdrant evaluation collection identifier."""
    cleaned = raw_name.strip()
    if not cleaned or ".." in cleaned or not _SAFE_COLLECTION_NAME_RE.fullmatch(cleaned):
        raise ValueError(
            f"Invalid Qdrant evaluation collection name {raw_name!r}; expected safe identifier."
        )
    return cleaned


@asynccontextmanager
async def isolated_eval_session_factory(
    settings: Settings,
) -> AsyncIterator[tuple[Settings, async_sessionmaker[AsyncSession]]]:
    """Create a temporary PostgreSQL database so evaluation never mutates worker cursors."""
    db_name = f"codesight_m2_eval_{uuid.uuid4().hex[:8]}"
    if not _SAFE_DB_NAME_RE.fullmatch(db_name):
        raise ValueError(f"Generated unsafe temporary evaluation database name: {db_name!r}")

    created_db = False
    admin_conn = await asyncpg.connect(dsn=settings.postgres_dsn)
    try:
        await admin_conn.execute(f'CREATE DATABASE "{db_name}"')
        created_db = True
    finally:
        await admin_conn.close()

    eval_db_settings = settings.model_copy(update={"postgres_db": db_name})
    engine = create_db_engine(eval_db_settings)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield eval_db_settings, create_session_factory(engine)
    finally:
        await engine.dispose()
        if created_db:
            try:
                cleanup_conn = await asyncpg.connect(dsn=settings.postgres_dsn)
                try:
                    await cleanup_conn.execute(
                        "SELECT pg_terminate_backend(pid) "
                        "FROM pg_stat_activity "
                        "WHERE datname = $1 AND pid <> pg_backend_pid()",
                        db_name,
                    )
                    await cleanup_conn.execute(f'DROP DATABASE IF EXISTS "{db_name}"')
                finally:
                    await cleanup_conn.close()
            except Exception as exc:
                logger.warning(
                    "m2_live_eval_isolated_db_cleanup_failed",
                    db_name=db_name,
                    error_type=type(exc).__name__,
                )


@dataclass(frozen=True, slots=True)
class M2ProbeGroundTruth:
    """Independently verified ground-truth targets for one cross-file modification probe."""

    file_path: str
    symbol_name: str
    expected_callers: tuple[str, ...] = ()
    expected_callees: tuple[str, ...] = ()
    expected_docs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class M2RepoGroundTruth:
    """Independently verified evaluation probes for a Python or TypeScript repository."""

    repo_full_name: str
    language: str
    probes: tuple[M2ProbeGroundTruth, ...]


@dataclass(frozen=True, slots=True)
class M2EvalCase:
    """Single cross-file modification probe evaluated against live Qdrant + GitHub + Gemini."""

    repo_full_name: str
    language: str
    file_path: str
    symbol_name: str
    expected_callers: tuple[str, ...]
    retrieved_callers: tuple[str, ...]
    expected_callees: tuple[str, ...]
    retrieved_callees: tuple[str, ...]
    recall_at_5: float
    callee_recall_at_5: float
    cold_retrieval_ms: float
    warm_retrieval_ms: float
    qdrant_ms: float
    blob_fetch_ms: float
    pack_ms: float
    packed_aux_tokens: int
    baseline_tokens: int
    codesight_tokens: int
    tokens_saved_pct: float
    doc_hits_at_prod_threshold: int
    doc_max_raw_score: float | None
    doc_raw_scores: tuple[tuple[str, float], ...]
    doc_search_degraded: bool
    blobs_fetched: int
    expected_docs: tuple[str, ...] = ()
    retrieved_docs: tuple[str, ...] = ()
    caller_recall_at_5: float | None = None
    doc_recall_at_5: float = 0.0


@dataclass(frozen=True, slots=True)
class M2RepoEvalSummary:
    """Per-repository indexing, per-tier Recall@5, warm latency, and token-savings summary."""

    repo_full_name: str
    language: str
    upserted_points: int
    code_chunks: int
    doc_chunks: int
    index_elapsed_ms: float
    cases_evaluated: int
    recall_at_5: float
    p50_retrieval_ms: float
    p95_retrieval_ms: float
    cold_retrieval_ms: float
    tokens_saved_pct: float
    max_packed_aux_tokens: int
    doc_threshold_used: float
    doc_max_raw_score: float | None
    doc_hits_above_threshold: int
    caller_recall_at_5: float = 1.0
    callee_recall_at_5: float = 1.0
    doc_recall_at_5: float = 1.0
    caller_cases_evaluated: int = 1
    callee_cases_evaluated: int = 1
    doc_cases_evaluated: int = 1
    degraded_doc_cases: int = 0
    ground_truth_source: str = "independent_source_scan"


@dataclass(frozen=True, slots=True)
class M2LiveEvaluationReport:
    """Complete Milestone 2 dual-language live evaluation report and NFR-3 audit telemetry."""

    py_repo: str
    ts_repo: str
    collection_name: str
    recall_at_5: float
    p50_retrieval_ms: float
    tokens_saved_pct: float
    zerocode_violations: int
    p95_retrieval_ms: float = 0.0
    cold_retrieval_ms: float = 0.0
    max_packed_aux_tokens: int = 0
    doc_score_threshold: float = DEFAULT_DOC_SCORE_THRESHOLD
    doc_raw_scores_observed: tuple[float, ...] = ()
    disk_violations: tuple[str, ...] = ()
    py_summary: M2RepoEvalSummary | None = None
    ts_summary: M2RepoEvalSummary | None = None
    cases: tuple[M2EvalCase, ...] = ()
    caller_recall_at_5: float = 1.0
    callee_recall_at_5: float = 1.0
    doc_recall_at_5: float = 1.0
    degraded_doc_cases: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize report into a JSON-compatible dictionary."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _IndependentSymbolDef:
    file_path: str
    language: str
    symbol_name: str
    start_line: int
    end_line: int
    body_start_line: int
    called_names: tuple[str, ...]


def compute_recall_at_k[TItem: Hashable](
    retrieved_items: Sequence[TItem],
    expected_targets: AbstractSet[TItem],
    *,
    k: int = 5,
) -> float:
    """Compute strict `Recall@k = |Top-k & Expected| / min(k, |Expected|)`."""
    eff_k = validate_positive_int(k, name="k")
    if not expected_targets:
        return 1.0
    top_k = set(retrieved_items[:eff_k])
    denom = min(eff_k, len(expected_targets))
    return len(top_k & expected_targets) / float(denom)


def _validate_target_entry(raw_entry: object, *, field_name: str) -> str:
    if not isinstance(raw_entry, str) or "::" not in raw_entry:
        raise ValueError(f"Expected 'file_path::symbol_name' in {field_name}, got {raw_entry!r}.")
    raw_file, raw_sym = raw_entry.split("::", 1)
    norm_file = normalize_diff_path(raw_file).rstrip("/")
    clean_sym = raw_sym.strip()
    if not norm_file or not clean_sym:
        raise ValueError(f"Invalid empty path or symbol in {field_name}: {raw_entry!r}.")
    return f"{norm_file}::{clean_sym}"


def load_m2_ground_truth(path: Path | str) -> dict[str, M2RepoGroundTruth]:
    """Load and validate an independent Milestone 2 ground-truth JSON manifest."""
    manifest_path = Path(path)
    raw_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw_payload, dict) or not isinstance(raw_payload.get("repos"), list):
        raise ValueError(f"Ground-truth manifest {manifest_path} must contain a 'repos' list.")

    loaded: dict[str, M2RepoGroundTruth] = {}
    for repo_obj in raw_payload["repos"]:
        if not isinstance(repo_obj, dict):
            raise ValueError("Each entry in 'repos' must be a JSON object.")
        slug = _validate_slug_as_value_error(
            str(repo_obj.get("repo_full_name") or ""),
            label="ground_truth.repo_full_name",
        )
        language = str(repo_obj.get("language") or "python").strip().lower()
        if language not in {"python", "typescript"}:
            raise ValueError(
                f"Unsupported language {language!r} for '{slug}'; expected python or typescript."
            )
        raw_probes = repo_obj.get("probes")
        if not isinstance(raw_probes, list) or not raw_probes:
            raise ValueError(f"Ground-truth entry for '{slug}' must define at least one probe.")

        probes: list[M2ProbeGroundTruth] = []
        for p_obj in raw_probes:
            if not isinstance(p_obj, dict):
                raise ValueError(f"Invalid probe entry in '{slug}'.")
            norm_path = normalize_diff_path(str(p_obj.get("file_path") or "")).rstrip("/")
            sym_name = str(p_obj.get("symbol_name") or "").strip()
            if not norm_path or not sym_name:
                raise ValueError(f"Probe in '{slug}' requires non-empty file_path and symbol_name.")
            callers = tuple(
                _validate_target_entry(x, field_name=f"{slug}.expected_callers")
                for x in (p_obj.get("expected_callers") or ())
            )
            callees = tuple(
                _validate_target_entry(x, field_name=f"{slug}.expected_callees")
                for x in (p_obj.get("expected_callees") or ())
            )
            docs = tuple(
                _validate_target_entry(x, field_name=f"{slug}.expected_docs")
                for x in (p_obj.get("expected_docs") or ())
            )
            probes.append(
                M2ProbeGroundTruth(
                    file_path=norm_path,
                    symbol_name=sym_name,
                    expected_callers=callers,
                    expected_callees=callees,
                    expected_docs=docs,
                )
            )
        loaded[slug.lower()] = M2RepoGroundTruth(
            repo_full_name=slug,
            language=language,
            probes=tuple(probes),
        )
    return loaded


def _validate_repo_summary_kpis(summary: M2RepoEvalSummary, violations: list[str]) -> None:
    slug = summary.repo_full_name or "<unknown>"
    if summary.cases_evaluated <= 0:
        violations.append(f"{slug}: evaluated 0 cross-file probe cases.")
        return
    if summary.caller_cases_evaluated <= 0:
        violations.append(f"{slug}: evaluated 0 caller ground-truth cases.")
    if summary.callee_cases_evaluated <= 0:
        violations.append(f"{slug}: evaluated 0 callee ground-truth cases.")
    if summary.doc_cases_evaluated <= 0:
        violations.append(f"{slug}: evaluated 0 architecture_doc ground-truth cases.")
    if summary.degraded_doc_cases > 0:
        violations.append(f"{slug}: doc_search_degraded on {summary.degraded_doc_cases} case(s).")
    if not math.isfinite(summary.recall_at_5) or summary.recall_at_5 < M2_MIN_RECALL_AT_5:
        violations.append(
            f"{slug}: recall_at_5 {summary.recall_at_5:.4f} < {M2_MIN_RECALL_AT_5:.2f}"
        )
    if (
        not math.isfinite(summary.caller_recall_at_5)
        or summary.caller_recall_at_5 < M2_MIN_RECALL_AT_5
    ):
        violations.append(
            f"{slug}: caller_recall_at_5 {summary.caller_recall_at_5:.4f} < "
            f"{M2_MIN_RECALL_AT_5:.2f}"
        )
    if (
        not math.isfinite(summary.callee_recall_at_5)
        or summary.callee_recall_at_5 < M2_MIN_RECALL_AT_5
    ):
        violations.append(
            f"{slug}: callee_recall_at_5 {summary.callee_recall_at_5:.4f} < "
            f"{M2_MIN_RECALL_AT_5:.2f}"
        )
    if not math.isfinite(summary.doc_recall_at_5) or summary.doc_recall_at_5 < M2_MIN_RECALL_AT_5:
        violations.append(
            f"{slug}: doc_recall_at_5 {summary.doc_recall_at_5:.4f} < {M2_MIN_RECALL_AT_5:.2f}"
        )
    if (
        not math.isfinite(summary.p50_retrieval_ms)
        or summary.p50_retrieval_ms >= M2_MAX_RETRIEVAL_MS
    ):
        violations.append(
            f"{slug}: p50_retrieval_ms {summary.p50_retrieval_ms:.2f}ms >= "
            f"{M2_MAX_RETRIEVAL_MS:.1f}ms"
        )
    if (
        not math.isfinite(summary.tokens_saved_pct)
        or summary.tokens_saved_pct < M2_MIN_TOKENS_SAVED_PCT
    ):
        violations.append(
            f"{slug}: tokens_saved_pct {summary.tokens_saved_pct:.2f}% < "
            f"{M2_MIN_TOKENS_SAVED_PCT:.1f}%"
        )
    if summary.max_packed_aux_tokens > M2_MAX_AUX_TOKENS:
        violations.append(
            f"{slug}: max_packed_aux_tokens {summary.max_packed_aux_tokens} > {M2_MAX_AUX_TOKENS}"
        )


def validate_m2_kpi_thresholds(report: M2LiveEvaluationReport) -> list[str]:
    """Return human-readable Milestone 2 KPI violations (empty list means all gates passed)."""
    violations: list[str] = []

    if not report.py_repo.strip():
        violations.append("py_repo must not be blank.")
    if not report.ts_repo.strip():
        violations.append(
            "ts_repo must not be blank (real TypeScript/TSX repository evaluation is required)."
        )
    if not math.isfinite(report.recall_at_5) or report.recall_at_5 < M2_MIN_RECALL_AT_5:
        violations.append(f"recall_at_5 {report.recall_at_5:.4f} < {M2_MIN_RECALL_AT_5:.2f}")
    if (
        not math.isfinite(report.caller_recall_at_5)
        or report.caller_recall_at_5 < M2_MIN_RECALL_AT_5
    ):
        violations.append(
            f"caller_recall_at_5 {report.caller_recall_at_5:.4f} < {M2_MIN_RECALL_AT_5:.2f}"
        )
    if (
        not math.isfinite(report.callee_recall_at_5)
        or report.callee_recall_at_5 < M2_MIN_RECALL_AT_5
    ):
        violations.append(
            f"callee_recall_at_5 {report.callee_recall_at_5:.4f} < {M2_MIN_RECALL_AT_5:.2f}"
        )
    if not math.isfinite(report.doc_recall_at_5) or report.doc_recall_at_5 < M2_MIN_RECALL_AT_5:
        violations.append(
            f"doc_recall_at_5 {report.doc_recall_at_5:.4f} < {M2_MIN_RECALL_AT_5:.2f}"
        )
    if report.degraded_doc_cases > 0:
        violations.append(
            f"doc_search_degraded={report.degraded_doc_cases} > 0 (architecture doc search failed)."
        )
    if not math.isfinite(report.p50_retrieval_ms) or report.p50_retrieval_ms >= M2_MAX_RETRIEVAL_MS:
        violations.append(
            f"p50_retrieval_ms {report.p50_retrieval_ms:.2f}ms >= {M2_MAX_RETRIEVAL_MS:.1f}ms"
        )
    if (
        not math.isfinite(report.tokens_saved_pct)
        or report.tokens_saved_pct < M2_MIN_TOKENS_SAVED_PCT
    ):
        violations.append(
            f"tokens_saved_pct {report.tokens_saved_pct:.2f}% < {M2_MIN_TOKENS_SAVED_PCT:.1f}%"
        )
    if report.max_packed_aux_tokens > M2_MAX_AUX_TOKENS:
        violations.append(
            f"max_packed_aux_tokens {report.max_packed_aux_tokens} > {M2_MAX_AUX_TOKENS}"
        )
    if report.zerocode_violations > 0:
        violations.append(
            f"zerocode_violations={report.zerocode_violations} > 0 (NFR-3 violation)."
        )
    if report.disk_violations:
        violations.append(
            f"disk_violations={list(report.disk_violations)} (NFR-3 disk check failed)."
        )

    for summary in (report.py_summary, report.ts_summary):
        if summary is not None:
            _validate_repo_summary_kpis(summary, violations)

    if report.cases:
        degraded_cases = [
            f"{c.repo_full_name}:{c.file_path}::{c.symbol_name}"
            for c in report.cases
            if c.doc_search_degraded
        ]
        if degraded_cases:
            violations.append(
                f"doc_search_degraded=True on probe case(s): {', '.join(degraded_cases)}"
            )
            if all("doc_recall_at_5" not in v for v in violations):
                violations.append(
                    "doc_recall_at_5 invalidated by degraded architecture doc search."
                )

        caller_scores = [
            c.caller_recall_at_5 if c.caller_recall_at_5 is not None else c.recall_at_5
            for c in report.cases
            if c.expected_callers
        ]
        if caller_scores:
            mean_c = sum(caller_scores) / len(caller_scores)
            if mean_c < M2_MIN_RECALL_AT_5 and all(
                "caller_recall_at_5" not in v for v in violations
            ):
                violations.append(
                    f"cases caller_recall_at_5 {mean_c:.4f} < {M2_MIN_RECALL_AT_5:.2f}"
                )

        callee_scores = [c.callee_recall_at_5 for c in report.cases if c.expected_callees]
        if callee_scores:
            mean_e = sum(callee_scores) / len(callee_scores)
            if mean_e < M2_MIN_RECALL_AT_5 and all(
                "callee_recall_at_5" not in v for v in violations
            ):
                violations.append(
                    f"cases callee_recall_at_5 {mean_e:.4f} < {M2_MIN_RECALL_AT_5:.2f}"
                )

        doc_scores = [c.doc_recall_at_5 for c in report.cases if c.expected_docs]
        if doc_scores:
            mean_d = sum(doc_scores) / len(doc_scores)
            if mean_d < M2_MIN_RECALL_AT_5 and all("doc_recall_at_5" not in v for v in violations):
                violations.append(f"cases doc_recall_at_5 {mean_d:.4f} < {M2_MIN_RECALL_AT_5:.2f}")

    return violations


def _validate_slug_as_value_error(raw_slug: str, *, label: str) -> str:
    cleaned = raw_slug.strip()
    if not cleaned:
        raise ValueError(f"{label} must be a non-empty 'owner/repo' slug.")
    try:
        return validate_repo_full_name(cleaned)
    except GitHubAuthConfigError as exc:
        raise ValueError(f"Invalid {label} {cleaned!r}: {exc}") from exc


async def discover_live_ts_repo(
    *,
    settings: Settings,
    http_client: httpx.AsyncClient | None = None,
    redis_client: redis_async.Redis | None = None,
    api_base_url: str = DEFAULT_GITHUB_API_URL,
) -> str:
    """Auto-discover a real TypeScript/TSX repository via `/user/repos?sort=updated&per_page=50`."""
    if not settings.has_github_token:
        raise ValueError(
            "CODESIGHT_LIVE_TS_REPO must be set to a real TypeScript/TSX repository slug "
            "(GITHUB_TOKEN is unavailable for /user/repos auto-discovery)."
        )

    auth = GitHubAuthProvider(
        settings,
        redis_client=redis_client,
        http_client=http_client,
        base_url=api_base_url,
    )
    try:
        token = await auth.get_token()
    except GitHubAuthError as exc:
        raise ValueError(
            "CODESIGHT_LIVE_TS_REPO must be set to a real TypeScript/TSX repository slug "
            f"(failed to obtain GitHub token: {exc})."
        ) from exc

    headers = {
        "Accept": GITHUB_ACCEPT_HEADER,
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
        "Authorization": f"Bearer {token.get_secret_value()}",
        "User-Agent": f"codesight-ai/{__version__}",
    }
    url = f"{api_base_url.rstrip('/')}/user/repos?sort=updated&per_page=50"

    owned_http: httpx.AsyncClient | None = None
    active_http = http_client
    if active_http is None:
        owned_http = httpx.AsyncClient(http2=True, timeout=10.0, follow_redirects=False)
        active_http = owned_http

    try:
        try:
            resp = await active_http.get(url, headers=headers, follow_redirects=False)
        except httpx.HTTPError as exc:
            raise ValueError(
                "CODESIGHT_LIVE_TS_REPO must be set to a real TypeScript/TSX repository slug "
                f"(GitHub /user/repos request failed: {type(exc).__name__})."
            ) from exc

        if resp.status_code != 200:
            raise ValueError(
                "CODESIGHT_LIVE_TS_REPO must be set to a real TypeScript/TSX repository slug "
                f"(GitHub /user/repos returned HTTP {resp.status_code})."
            )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ValueError(
                "CODESIGHT_LIVE_TS_REPO must be set to a real TypeScript/TSX repository slug "
                "(GitHub /user/repos returned invalid JSON)."
            ) from exc
    finally:
        if owned_http is not None:
            await owned_http.aclose()

    if not isinstance(payload, list):
        raise ValueError(
            "CODESIGHT_LIVE_TS_REPO must be set to a real TypeScript/TSX repository slug "
            "(unexpected /user/repos response format)."
        )

    active_candidates: list[str] = []
    archived_candidates: list[str] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        raw_lang = item.get("language")
        if not isinstance(raw_lang, str) or raw_lang.strip() not in _TS_REPO_LANGUAGES:
            continue
        raw_slug = item.get("full_name")
        if not isinstance(raw_slug, str) or not raw_slug.strip():
            continue
        try:
            slug = validate_repo_full_name(raw_slug)
        except GitHubAuthConfigError:
            continue
        if bool(item.get("archived", False)):
            archived_candidates.append(slug)
        else:
            active_candidates.append(slug)

    candidates = active_candidates or archived_candidates
    if not candidates:
        raise ValueError(
            "CODESIGHT_LIVE_TS_REPO must be set to a real TypeScript/TSX repository slug "
            "(no TypeScript/TSX repository found in /user/repos?sort=updated&per_page=50)."
        )
    selected = candidates[0]

    logger.info("m2_live_eval_ts_repo_auto_discovered", ts_repo=selected)
    return selected


def _resolve_py_repo(py_repo: str | None, *, settings: Settings) -> str:
    if py_repo is not None:
        return _validate_slug_as_value_error(py_repo, label="py_repo")
    candidate = (
        (settings.codesight_live_py_repo or "").strip()
        or os.getenv("CODESIGHT_LIVE_PY_REPO", "").strip()
        or DEFAULT_LIVE_PY_REPO
    )
    return _validate_slug_as_value_error(candidate, label="CODESIGHT_LIVE_PY_REPO")


async def _resolve_ts_repo(
    ts_repo: str | None,
    *,
    settings: Settings,
    http_client: httpx.AsyncClient | None,
    redis_client: redis_async.Redis | None,
    api_base_url: str,
) -> str:
    explicit = (ts_repo or "").strip()
    if explicit:
        return _validate_slug_as_value_error(explicit, label="ts_repo")
    settings_ts = (settings.codesight_live_ts_repo or "").strip()
    if settings_ts:
        return _validate_slug_as_value_error(settings_ts, label="CODESIGHT_LIVE_TS_REPO")
    env_ts = os.getenv("CODESIGHT_LIVE_TS_REPO", "").strip()
    if env_ts:
        return _validate_slug_as_value_error(env_ts, label="CODESIGHT_LIVE_TS_REPO")
    return await discover_live_ts_repo(
        settings=settings,
        http_client=http_client,
        redis_client=redis_client,
        api_base_url=api_base_url,
    )


def _is_test_path(file_path: str) -> bool:
    lower = file_path.lower()
    if lower.startswith(("tests/", "test/", "__tests__/")):
        return True
    if "/tests/" in lower or "/test/" in lower or "/__tests__/" in lower:
        return True
    basename = lower.rsplit("/", 1)[-1]
    return (
        basename.startswith("test_")
        or basename.endswith("_test.py")
        or ".test." in basename
        or ".spec." in basename
    )


def _canonical_target(file_path: str, symbol_name: str) -> str:
    return f"{file_path}::{symbol_name}"


async def _default_load_repo_points(
    store: QdrantZeroCodeStore,
    repo_full_name: str,
) -> tuple[QdrantStoredPoint, ...]:
    scroll_filter = models.Filter(
        must=[
            models.FieldCondition(
                key="repo_full_name",
                match=models.MatchValue(value=repo_full_name),
            )
        ]
    )
    collected: list[QdrantStoredPoint] = []
    next_offset: Any = None
    while True:
        records, next_offset = await store.client.scroll(
            collection_name=store.collection_name,
            scroll_filter=scroll_filter,
            limit=_SCROLL_PAGE_SIZE,
            offset=next_offset,
            with_payload=True,
            with_vectors=False,
        )
        if not records:
            break
        for rec in records:
            collected.append(
                QdrantStoredPoint(
                    point_id=str(rec.id),
                    payload=dict(rec.payload or {}),
                    score=None,
                )
            )
        if next_offset is None:
            break
    return tuple(collected)


def _collect_py_calls_within_scope(root: ast.AST) -> tuple[str, ...]:
    """Collect called symbol names inside one Python AST scope without entering nested defs."""
    found: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        cleaned = name.strip()
        if cleaned and not is_noise_symbol(cleaned) and cleaned not in seen:
            seen.add(cleaned)
            found.append(cleaned)

    stack: list[ast.AST] = list(ast.iter_child_nodes(root))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, ast.Call):
            fn = node.func
            call_name = ""
            if isinstance(fn, ast.Name):
                call_name = fn.id
                _add(fn.id)
            elif isinstance(fn, ast.Attribute):
                receiver = fn.value.id if isinstance(fn.value, ast.Name) else ""
                call_name = fn.attr
                if receiver not in BUILTIN_NOISE_SYMBOLS:
                    _add(fn.attr)
            if call_name in {"Depends", "Security"} and node.args:
                first_arg = node.args[0]
                if isinstance(first_arg, ast.Name):
                    _add(first_arg.id)
                elif isinstance(first_arg, ast.Attribute):
                    _add(first_arg.attr)
        stack.extend(ast.iter_child_nodes(node))
    return tuple(found)


def _scan_python_source_independent(
    source_text: str,
    *,
    file_path: str,
) -> list[_IndependentSymbolDef]:
    """Extract Python definitions and outgoing calls via stdlib `ast` (zero tree-sitter/Qdrant)."""
    try:
        tree = ast.parse(source_text)
    except SyntaxError:
        return []

    definitions: list[_IndependentSymbolDef] = []

    def _visit_block(stmts: Sequence[ast.stmt]) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                start_ln = stmt.lineno
                end_ln = stmt.end_lineno or start_ln
                body_ln = stmt.body[0].lineno if stmt.body else start_ln + 1
                definitions.append(
                    _IndependentSymbolDef(
                        file_path=file_path,
                        language="python",
                        symbol_name=stmt.name,
                        start_line=start_ln,
                        end_line=end_ln,
                        body_start_line=body_ln,
                        called_names=_collect_py_calls_within_scope(stmt),
                    )
                )
                _visit_block(stmt.body)
            elif isinstance(stmt, ast.ClassDef):
                has_methods = any(
                    isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                    for child in stmt.body
                )
                if not has_methods:
                    start_ln = stmt.lineno
                    end_ln = stmt.end_lineno or start_ln
                    body_ln = stmt.body[0].lineno if stmt.body else start_ln + 1
                    definitions.append(
                        _IndependentSymbolDef(
                            file_path=file_path,
                            language="python",
                            symbol_name=stmt.name,
                            start_line=start_ln,
                            end_line=end_ln,
                            body_start_line=body_ln,
                            called_names=_collect_py_calls_within_scope(stmt),
                        )
                    )
                _visit_block(stmt.body)

    _visit_block(tree.body)
    return definitions


def _mask_ts_comments_and_strings(source: str) -> str:
    """Mask TS/TSX comments and string/template text while preserving newlines and `${...}`."""
    out: list[str] = []
    idx = 0
    n = len(source)
    state = "code"
    quote_ch = ""
    template_expr_depth = 0
    template_stack: list[int] = []

    while idx < n:
        ch = source[idx]
        nxt = source[idx + 1] if idx + 1 < n else ""

        if state == "code":
            if ch == "/" and nxt == "/":
                out.extend((" ", " "))
                idx += 2
                state = "line_comment"
                continue
            if ch == "/" and nxt == "*":
                out.extend((" ", " "))
                idx += 2
                state = "block_comment"
                continue
            if ch in ("'", '"'):
                quote_ch = ch
                out.append(" ")
                idx += 1
                state = "string"
                continue
            if ch == "`":
                out.append(" ")
                idx += 1
                state = "template"
                continue
            if ch == "{" and template_stack:
                template_expr_depth += 1
            elif ch == "}" and template_stack and template_expr_depth == template_stack[-1]:
                template_stack.pop()
                out.append(" ")
                idx += 1
                state = "template"
                continue
            out.append(ch)
            idx += 1
            continue

        if state == "line_comment":
            if ch == "\n":
                out.append("\n")
                state = "code"
            else:
                out.append(" ")
            idx += 1
            continue

        if state == "block_comment":
            if ch == "*" and nxt == "/":
                out.extend((" ", " "))
                idx += 2
                state = "code"
            else:
                out.append("\n" if ch == "\n" else " ")
                idx += 1
            continue

        if state == "string":
            if ch == "\\" and idx + 1 < n:
                out.append("\n" if ch == "\n" else " ")
                out.append("\n" if nxt == "\n" else " ")
                idx += 2
                continue
            if ch == quote_ch:
                out.append(" ")
                state = "code"
            else:
                out.append("\n" if ch == "\n" else " ")
            idx += 1
            continue

        # state == "template"
        if ch == "\\" and idx + 1 < n:
            out.append("\n" if ch == "\n" else " ")
            out.append("\n" if nxt == "\n" else " ")
            idx += 2
            continue
        if ch == "$" and nxt == "{":
            template_stack.append(template_expr_depth)
            out.extend((" ", " "))
            idx += 2
            state = "code"
            continue
        if ch == "`":
            out.append(" ")
            state = "code"
        else:
            out.append("\n" if ch == "\n" else " ")
        idx += 1

    return "".join(out)


def _find_matching_delimiter(text: str, open_idx: int, open_ch: str, close_ch: str) -> int:
    depth = 0
    for idx in range(open_idx, len(text)):
        ch = text[idx]
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return idx
    return len(text) - 1


def _line_at_offset(line_offsets: Sequence[int], offset: int) -> int:
    lo = 0
    hi = len(line_offsets) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if line_offsets[mid] <= offset:
            lo = mid + 1
        else:
            hi = mid - 1
    return max(1, lo)


def _scan_ts_source_independent(
    source_text: str,
    *,
    file_path: str,
) -> list[_IndependentSymbolDef]:
    """Extract TS/TSX definitions and calls/JSX tags from raw source (zero tree-sitter/Qdrant)."""
    masked = _mask_ts_comments_and_strings(source_text)
    line_offsets = [0]
    for idx, ch in enumerate(masked):
        if ch == "\n":
            line_offsets.append(idx + 1)

    lang = "tsx" if file_path.lower().endswith(".tsx") else "typescript"
    raw_spans: list[tuple[int, int, int, str]] = []
    seen_starts: set[tuple[int, str]] = set()

    for match in _TS_FUNC_DECL_RE.finditer(masked):
        sym = match.group(1)
        if sym in _TS_CONTROL_KEYWORDS:
            continue
        decl_start = match.start() + (1 if masked[match.start()] == "\n" else 0)
        paren_open = match.end() - 1
        paren_close = _find_matching_delimiter(masked, paren_open, "(", ")")
        brace_open = masked.find("{", paren_close + 1)
        semi_pos = masked.find(";", paren_close + 1)
        if brace_open == -1 or (semi_pos != -1 and semi_pos < brace_open):
            continue
        brace_close = _find_matching_delimiter(masked, brace_open, "{", "}")
        seen_starts.add((decl_start, sym))
        raw_spans.append((decl_start, brace_open, brace_close, sym))

    for match in _TS_ARROW_DECL_RE.finditer(masked):
        sym = match.group(1)
        if sym in _TS_CONTROL_KEYWORDS:
            continue
        decl_start = match.start() + (1 if masked[match.start()] == "\n" else 0)
        if (decl_start, sym) in seen_starts:
            continue
        arrow_end = match.end()
        rest_idx = arrow_end
        while rest_idx < len(masked) and masked[rest_idx] in " \t\r\n":
            rest_idx += 1
        if rest_idx < len(masked) and masked[rest_idx] == "{":
            body_end = _find_matching_delimiter(masked, rest_idx, "{", "}")
            body_open = rest_idx
        else:
            semi = masked.find(";", rest_idx)
            nl = masked.find("\n", rest_idx)
            candidates = [pos for pos in (semi, nl) if pos != -1]
            body_end = min(candidates) if candidates else len(masked) - 1
            body_open = arrow_end
        seen_starts.add((decl_start, sym))
        raw_spans.append((decl_start, body_open, body_end, sym))

    for match in _TS_METHOD_DECL_RE.finditer(masked):
        sym = match.group(1)
        if sym in _TS_CONTROL_KEYWORDS:
            continue
        decl_start = match.start() + (1 if masked[match.start()] == "\n" else 0)
        if (decl_start, sym) in seen_starts:
            continue
        brace_open = match.end() - 1
        brace_close = _find_matching_delimiter(masked, brace_open, "{", "}")
        seen_starts.add((decl_start, sym))
        raw_spans.append((decl_start, brace_open, brace_close, sym))

    raw_spans.sort(key=lambda item: (item[0], item[2]))
    definitions: list[_IndependentSymbolDef] = []
    for idx, (decl_start, body_open, body_end, sym) in enumerate(raw_spans):
        nested_ranges = [
            (other_start, other_end)
            for j, (other_start, _ob, other_end, _os) in enumerate(raw_spans)
            if j != idx and body_open < other_start and other_end <= body_end
        ]
        chars = list(masked[body_open : body_end + 1])
        for n_start, n_end in nested_ranges:
            rel_lo = max(0, n_start - body_open)
            rel_hi = min(len(chars), n_end - body_open + 1)
            for pos in range(rel_lo, rel_hi):
                if chars[pos] != "\n":
                    chars[pos] = " "
        body_masked = "".join(chars)

        called: list[str] = []
        seen_calls: set[str] = set()
        for call_match in _TS_CALL_RE.finditer(body_masked):
            receiver = (call_match.group(1) or "").strip()
            callee = call_match.group(2).strip()
            if callee in _TS_CONTROL_KEYWORDS or callee == sym:
                continue
            if receiver and receiver in BUILTIN_NOISE_SYMBOLS:
                continue
            if not is_noise_symbol(callee) and callee not in seen_calls:
                seen_calls.add(callee)
                called.append(callee)
        for jsx_match in _JSX_COMPONENT_RE.finditer(body_masked):
            comp = jsx_match.group(1).strip()
            if comp != sym and not is_noise_symbol(comp) and comp not in seen_calls:
                seen_calls.add(comp)
                called.append(comp)

        start_ln = _line_at_offset(line_offsets, decl_start)
        end_ln = _line_at_offset(line_offsets, body_end)
        open_ln = _line_at_offset(line_offsets, body_open)
        body_ln = min(end_ln, open_ln + 1) if open_ln < end_ln else open_ln
        definitions.append(
            _IndependentSymbolDef(
                file_path=file_path,
                language=lang,
                symbol_name=sym,
                start_line=start_ln,
                end_line=end_ln,
                body_start_line=body_ln,
                called_names=tuple(called),
            )
        )
    return definitions


def _scan_markdown_sections_independent(
    markdown_text: str,
    *,
    file_path: str,
) -> list[tuple[str, str]]:
    """Extract `(canonical_doc_target, section_text)` from raw Markdown independently."""
    lines = markdown_text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    heading_stack: dict[int, str] = {}
    current_title = ""
    current_lines: list[str] = []
    in_fence = False

    for raw_line in lines:
        if _MD_FENCE_RE.match(raw_line):
            in_fence = not in_fence
            current_lines.append(raw_line)
            continue
        if not in_fence:
            h_match = _MD_HEADING_RE.match(raw_line)
            if h_match is not None:
                if current_title and any(ln.strip() for ln in current_lines):
                    sections.append((current_title, list(current_lines)))
                level = len(h_match.group(1))
                title = h_match.group(2).strip()
                for k in [lvl for lvl in heading_stack if lvl >= level]:
                    del heading_stack[k]
                heading_stack[level] = title
                current_title = " > ".join(heading_stack[lvl] for lvl in sorted(heading_stack))
                current_lines = [raw_line]
                continue
        current_lines.append(raw_line)

    if current_title and any(ln.strip() for ln in current_lines):
        sections.append((current_title, list(current_lines)))
    elif not sections and any(ln.strip() for ln in lines):
        stem = Path(file_path).stem or file_path
        sections.append((stem, lines))

    return [
        (_canonical_target(file_path, sec_title), "\n".join(sec_lines))
        for sec_title, sec_lines in sections
    ]


def _tokenize_identifier(text: str) -> set[str]:
    decamel = _CAMEL_SPLIT_RE.sub(r"\1 \2", text)
    return {
        tok.lower()
        for tok in _WORD_SPLIT_RE.split(decamel)
        if len(tok) >= 3 and tok.lower() not in {"src", "lib", "app", "web", "py", "ts", "tsx"}
    }


def _match_expected_docs_for_probe(
    *,
    file_path: str,
    symbol_name: str,
    doc_sections: Sequence[tuple[str, str]],
) -> tuple[str, ...]:
    if not doc_sections:
        return ()
    sym_lower = symbol_name.lower()
    stem = Path(file_path).stem.lower()
    probe_tokens = _tokenize_identifier(symbol_name) | _tokenize_identifier(stem)

    scored: list[tuple[int, str]] = []
    for target_id, sec_body in doc_sections:
        lower_body = f"{target_id}\n{sec_body}".lower()
        score = 0
        if sym_lower in lower_body:
            score += 10
        if stem and len(stem) >= 4 and stem in lower_body:
            score += 5
        sec_tokens = _tokenize_identifier(lower_body)
        score += len(probe_tokens & sec_tokens) * 2
        if score > 0:
            scored.append((score, target_id))

    if scored:
        scored.sort(key=lambda item: (-item[0], item[1]))
        top_score = scored[0][0]
        return tuple(target_id for sc, target_id in scored[:2] if sc >= max(2, top_score // 2))
    if len(doc_sections) == 1:
        return (doc_sections[0][0],)
    return ()


def build_independent_repo_ground_truth(
    raw_files_by_path: Mapping[str, str],
    *,
    repo_full_name: str,
    language: str,
    max_probes: int = DEFAULT_PROBES_PER_REPO,
) -> M2RepoGroundTruth:
    """Construct verified caller, callee, and doc targets from raw source files (zero Qdrant)."""
    eff_max = validate_positive_int(max_probes, name="max_probes")
    all_defs: list[_IndependentSymbolDef] = []
    doc_sections: list[tuple[str, str]] = []

    for raw_path in sorted(raw_files_by_path):
        norm_path = normalize_diff_path(raw_path).rstrip("/")
        if not norm_path:
            continue
        text = raw_files_by_path[raw_path]
        lower_path = norm_path.lower()
        if lower_path.endswith(".py"):
            all_defs.extend(_scan_python_source_independent(text, file_path=norm_path))
        elif lower_path.endswith((".ts", ".tsx")):
            all_defs.extend(_scan_ts_source_independent(text, file_path=norm_path))
        elif is_architecture_doc_path(norm_path):
            doc_sections.extend(_scan_markdown_sections_independent(text, file_path=norm_path))

    defs_by_name: dict[str, list[_IndependentSymbolDef]] = {}
    defs_per_file: dict[str, int] = {}
    callers_by_name: dict[str, list[_IndependentSymbolDef]] = {}

    for sym_def in all_defs:
        defs_by_name.setdefault(sym_def.symbol_name, []).append(sym_def)
        defs_per_file[sym_def.file_path] = defs_per_file.get(sym_def.file_path, 0) + 1
        for called in sym_def.called_names:
            callers_by_name.setdefault(called, []).append(sym_def)

    ranked_candidates: list[tuple[tuple[int, ...], str, int, M2ProbeGroundTruth]] = []
    for sym_def in all_defs:
        if is_noise_symbol(sym_def.symbol_name):
            continue
        cross_callers = sorted(
            {
                _canonical_target(caller.file_path, caller.symbol_name)
                for caller in callers_by_name.get(sym_def.symbol_name, ())
                if caller.file_path != sym_def.file_path
            }
        )
        if not cross_callers:
            continue

        cross_callees_set: set[str] = set()
        for called_name in sym_def.called_names:
            if is_noise_symbol(called_name) or called_name in AMBIGUOUS_METHOD_SYMBOLS:
                continue
            for callee_def in defs_by_name.get(called_name, ()):
                if callee_def.file_path != sym_def.file_path:
                    cross_callees_set.add(
                        _canonical_target(callee_def.file_path, callee_def.symbol_name)
                    )
        cross_callees = tuple(sorted(cross_callees_set))
        expected_docs = _match_expected_docs_for_probe(
            file_path=sym_def.file_path,
            symbol_name=sym_def.symbol_name,
            doc_sections=doc_sections,
        )

        rank = (
            int(not _is_test_path(sym_def.file_path)),
            int(bool(cross_callees)),
            int(bool(expected_docs)),
            int(sym_def.symbol_name not in AMBIGUOUS_METHOD_SYMBOLS),
            int(len(defs_by_name.get(sym_def.symbol_name, ())) == 1),
            int(defs_per_file.get(sym_def.file_path, 0) >= 2),
            int(1 <= len(cross_callers) <= 50),
            min(len(cross_callers), 8),
        )
        ranked_candidates.append(
            (
                rank,
                sym_def.file_path,
                sym_def.start_line,
                M2ProbeGroundTruth(
                    file_path=sym_def.file_path,
                    symbol_name=sym_def.symbol_name,
                    expected_callers=tuple(cross_callers),
                    expected_callees=cross_callees,
                    expected_docs=expected_docs,
                ),
            )
        )

    if not ranked_candidates:
        raise ValueError(
            f"Repository '{repo_full_name}' has no symbols with cross-file callers in raw source."
        )

    ranked_candidates.sort(
        key=lambda item: (
            tuple(-x for x in item[0]),
            item[1],
            item[2],
            item[3].symbol_name,
        )
    )

    selected: list[M2ProbeGroundTruth] = []
    seen_keys: set[tuple[str, str]] = set()
    seen_files: set[str] = set()
    for _rank, fpath, _line, probe in ranked_candidates:
        if fpath in seen_files:
            continue
        seen_files.add(fpath)
        seen_keys.add((fpath, probe.symbol_name))
        selected.append(probe)
        if len(selected) >= eff_max:
            break

    if len(selected) < eff_max:
        for _rank, fpath, _line, probe in ranked_candidates:
            key = (fpath, probe.symbol_name)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            selected.append(probe)
            if len(selected) >= eff_max:
                break

    return M2RepoGroundTruth(
        repo_full_name=repo_full_name,
        language=language,
        probes=tuple(selected),
    )


def _resolve_probe_chunk_and_ast(
    *,
    file_path: str,
    symbol_name: str,
    language_hint: str,
    source_text: str,
    registry: ASTParserRegistry,
) -> tuple[Any, CodeSymbolChunk]:
    parsed_ast = registry.parse_file(source_text, file_path=file_path)
    if parsed_ast is not None and parsed_ast.chunks:
        for chunk in parsed_ast.chunks:
            if chunk.symbol_name == symbol_name:
                return parsed_ast, chunk

    ast_lang = (
        parsed_ast.language if parsed_ast is not None else registry.resolve_language(file_path)
    )
    fallback_lang = ast_lang or ("python" if language_hint == "python" else "typescript")
    line_count = max(1, len(source_text.splitlines()))
    symbol_kind: SymbolKind = "function"
    fallback_chunk = CodeSymbolChunk(
        file_path=file_path,
        language=fallback_lang,
        symbol_name=symbol_name,
        qualified_name=symbol_name,
        symbol_kind=symbol_kind,
        start_line=1,
        end_line=line_count,
        signature=symbol_name,
        docstring=None,
        decorators=(),
        imports=(),
        called_symbols=(),
        cyclomatic_complexity=1,
        is_async=False,
    )
    return parsed_ast, fallback_chunk


def _build_probe_diff(
    *,
    file_path: str,
    blob_sha: str,
    language: str,
    source_text: str,
    target_line: int,
    diff_parser: SmartDiffParser,
) -> ParsedPullRequestDiff:
    lines = source_text.splitlines()
    if not lines:
        return ParsedPullRequestDiff(
            files=(),
            skipped_files=(),
            metrics=diff_parser.parse_diff("").metrics,
            annotated_diff="",
        )
    line_idx = max(0, min(len(lines) - 1, target_line - 1))
    line_no = line_idx + 1
    orig = lines[line_idx]
    comment_suffix = "  # codesight-eval" if language == "python" else " // codesight-eval"
    diff_text = (
        f"diff --git a/{file_path} b/{file_path}\n"
        f"--- a/{file_path}\n"
        f"+++ b/{file_path}\n"
        f"@@ -{line_no},1 +{line_no},1 @@ {orig}\n"
        f"-{orig}\n"
        f"+{orig}{comment_suffix}\n"
    )
    parsed = diff_parser.parse_diff(diff_text)
    if parsed.files:
        patched_files = tuple(
            replace(f, blob_sha=blob_sha) if f.file_path == file_path else f for f in parsed.files
        )
        return replace(parsed, files=patched_files)
    synthetic_file = ParsedFileDiff(
        file_path=file_path,
        old_path=file_path,
        new_path=file_path,
        change_type="modified",
        hunks=(),
        annotated_diff=diff_text,
        valid_commentable_lines={line_no},
        right_line_to_hunk={line_no: 1},
        blob_sha=blob_sha,
    )
    return replace(parsed, files=(synthetic_file,), annotated_diff=diff_text)


def _resolve_body_start_line(
    source_text: str,
    *,
    language: str,
    start_line: int,
    end_line: int,
) -> int:
    """Locate the first interior body line strictly past a function/method signature header."""
    if language == "python":
        try:
            tree = ast.parse(source_text)
            best_body_line: int | None = None
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    node_end = node.end_lineno or node.lineno
                    if node.lineno == start_line and node.body:
                        return node.body[0].lineno
                    if node.lineno <= start_line <= node_end and node.body:
                        cand_ln = node.body[0].lineno
                        if cand_ln > start_line and (
                            best_body_line is None or cand_ln < best_body_line
                        ):
                            best_body_line = cand_ln
            if best_body_line is not None:
                return best_body_line
        except SyntaxError:
            pass

        lines = source_text.splitlines()
        depth = 0
        for ln_no in range(max(1, start_line), min(len(lines), end_line) + 1):
            raw = lines[ln_no - 1]
            for ch in raw:
                if ch in "([{":
                    depth += 1
                elif ch in ")]}":
                    depth = max(0, depth - 1)
                elif ch == ":" and depth == 0:
                    return min(end_line, ln_no + 1)
        return min(end_line, start_line + 1)

    masked = _mask_ts_comments_and_strings(source_text)
    masked_lines = masked.splitlines()
    paren_depth = 0
    saw_open_paren = False
    for ln_no in range(max(1, start_line), min(len(masked_lines), end_line) + 1):
        line_txt = masked_lines[ln_no - 1]
        for idx, ch in enumerate(line_txt):
            if ch == "(":
                paren_depth += 1
                saw_open_paren = True
            elif ch == ")":
                paren_depth = max(0, paren_depth - 1)
            elif paren_depth == 0 and saw_open_paren:
                if ch == "{":
                    return min(end_line, ln_no + 1)
                if ch == "=" and idx + 1 < len(line_txt) and line_txt[idx + 1] == ">":
                    return min(end_line, ln_no + 1)
    return min(end_line, start_line + 1)


def _extract_body_canaries(
    source_text: str,
    *,
    start_line: int,
    end_line: int,
    collected: list[str],
    language: str = "python",
    max_canaries: int = _MAX_BODY_CANARIES_PER_REPO,
) -> None:
    """Extract interior function-body canary lines purely from source syntax (never payload)."""
    if len(collected) >= max_canaries:
        return
    lines = source_text.splitlines()
    if not lines:
        return
    lo = max(
        1,
        _resolve_body_start_line(
            source_text,
            language=language,
            start_line=start_line,
            end_line=end_line,
        ),
    )
    hi = min(len(lines), end_line)
    for min_len in (_MIN_CANARY_LEN, _FALLBACK_CANARY_LEN):
        for idx in range(lo, hi + 1):
            candidate = lines[idx - 1].strip()
            if not (min_len <= len(candidate) <= _MAX_CANARY_LEN):
                continue
            if "\r" in candidate or "\n" in candidate or "\x00" in candidate:
                continue
            if candidate in _BARE_DELIMITER_LINES or candidate.isidentifier():
                continue
            if candidate.startswith(("import ", "from ")):
                continue
            if candidate in collected:
                continue
            collected.append(candidate)
            if len(collected) >= max_canaries:
                return


async def _measure_diagnostic_doc_scores(
    *,
    embedder: GeminiEmbedder | None,
    store: QdrantZeroCodeStore,
    repo_full_name: str,
    query_text: str | None,
    has_docs: bool,
) -> tuple[tuple[tuple[str, float], ...], float | None]:
    if not has_docs or embedder is None or not query_text:
        return (), None
    try:
        query_vec = await embedder.embed_query(query_text)
        diag_docs = await store.search_similar(
            query_vec,
            repo_full_name=repo_full_name,
            chunk_type="architecture_doc",
            score_threshold=0.0,
            limit=DEFAULT_DOC_LIMIT,
        )
    except ZeroCodeViolationError:
        raise
    except Exception as exc:
        logger.warning(
            "m2_live_eval_diagnostic_doc_probe_failed",
            repo=repo_full_name,
            error_type=type(exc).__name__,
        )
        return (), None

    scores = tuple(
        (
            _canonical_target(
                normalize_diff_path(str(pt.payload.get("file_path") or "")).rstrip("/"),
                str(pt.payload.get("symbol_name") or "").strip(),
            ),
            round(float(pt.score), 4),
        )
        for pt in diag_docs
        if pt.score is not None
    )
    max_score = max((score for _, score in scores), default=None)
    return scores, max_score


async def _fetch_repo_blobs_for_ground_truth(
    *,
    repo_full_name: str,
    file_blob_shas: Mapping[str, str],
    blob_fetcher: GitHubBlobFetcher,
) -> dict[str, str]:
    if not file_blob_shas:
        return {}
    requests = [
        BlobSliceRequest(
            repo_full_name=repo_full_name,
            file_path=fpath,
            blob_sha=bsha,
            start_line=1,
            end_line=_MAX_FILE_SLICE_LINES,
        )
        for fpath, bsha in sorted(file_blob_shas.items())
    ]
    report = await blob_fetcher.fetch_slices(requests)
    fetched: dict[str, str] = {}
    for blob_slice in report.slices:
        if blob_slice.status == "ok" and blob_slice.content:
            norm_path = normalize_diff_path(blob_slice.request.file_path).rstrip("/")
            if norm_path:
                fetched[norm_path] = blob_slice.content
    return fetched


async def _evaluate_single_repo(
    *,
    repo_full_name: str,
    language_label: str,
    index_report: InitialIndexReport,
    store: QdrantZeroCodeStore,
    embedder: GeminiEmbedder | None,
    retriever: HybridContextRetriever,
    blob_fetcher: GitHubBlobFetcher,
    packer: TokenBudgetPacker,
    registry: ASTParserRegistry,
    diff_parser: SmartDiffParser,
    points_loader: PointsLoaderFn | None,
    curated_ground_truth: M2RepoGroundTruth | None,
    max_probes: int,
    doc_score_threshold: float,
    scrolled_points_collector: list[QdrantStoredPoint],
    body_canary_collector: list[str],
) -> tuple[M2RepoEvalSummary, list[M2EvalCase]]:
    loader = points_loader if points_loader is not None else _default_load_repo_points
    repo_points = await loader(store, repo_full_name)
    scrolled_points_collector.extend(repo_points)

    code_count = 0
    doc_count = 0
    file_blob_shas: dict[str, str] = {}
    for pt in repo_points:
        chunk_type = str(pt.payload.get("chunk_type") or "").strip().lower()
        if chunk_type == "code":
            code_count += 1
        elif chunk_type == "architecture_doc":
            doc_count += 1
        fpath = normalize_diff_path(str(pt.payload.get("file_path") or "")).rstrip("/")
        bsha = str(pt.payload.get("blob_sha") or "").strip()
        if fpath and bsha and fpath not in file_blob_shas:
            file_blob_shas[fpath] = bsha

    if curated_ground_truth is None and code_count == 0:
        raise ValueError(
            f"Repository '{repo_full_name}' has no indexed symbols with cross-file callers."
        )

    fetched_files = await _fetch_repo_blobs_for_ground_truth(
        repo_full_name=repo_full_name,
        file_blob_shas=file_blob_shas,
        blob_fetcher=blob_fetcher,
    )
    if not fetched_files:
        raise ValueError(
            f"Repository '{repo_full_name}' yielded 0 fetchable cross-file probe blobs."
        )

    if curated_ground_truth is not None:
        gt_source = "curated"
        selected_probes = list(curated_ground_truth.probes[:max_probes])
        for gt_probe in selected_probes:
            if gt_probe.file_path not in fetched_files:
                raise ValueError(
                    f"Curated ground-truth file '{gt_probe.file_path}' for '{repo_full_name}' "
                    "was not found in indexed repository blobs."
                )
    else:
        gt_source = "independent_source_scan"
        derived_gt = build_independent_repo_ground_truth(
            fetched_files,
            repo_full_name=repo_full_name,
            language=language_label,
            max_probes=max_probes,
        )
        selected_probes = list(derived_gt.probes[:max_probes])

    prepared_probes: list[
        tuple[M2ProbeGroundTruth, str, PRFileContext, ParsedPullRequestDiff, CodeSymbolChunk]
    ] = []
    for gt_probe in selected_probes:
        source_text = fetched_files.get(gt_probe.file_path)
        if not source_text:
            continue
        blob_sha = file_blob_shas.get(gt_probe.file_path, "")
        parsed_ast, target_chunk = _resolve_probe_chunk_and_ast(
            file_path=gt_probe.file_path,
            symbol_name=gt_probe.symbol_name,
            language_hint=language_label,
            source_text=source_text,
            registry=registry,
        )
        parsed_diff = _build_probe_diff(
            file_path=gt_probe.file_path,
            blob_sha=blob_sha,
            language=language_label,
            source_text=source_text,
            target_line=target_chunk.start_line,
            diff_parser=diff_parser,
        )
        file_ctx = PRFileContext(
            file_path=gt_probe.file_path,
            mode="ast",
            language=target_chunk.language,
            parsed_file=parsed_ast,
            touched_chunks=(target_chunk,),
        )
        prepared_probes.append((gt_probe, source_text, file_ctx, parsed_diff, target_chunk))

    if not prepared_probes:
        raise ValueError(
            f"Repository '{repo_full_name}' yielded 0 fetchable cross-file probe blobs."
        )

    # Execute 1 un-averaged warmup run to prime HTTP/2 TLS + Qdrant gRPC connections.
    w_probe, w_src, w_ctx, w_diff, _ = prepared_probes[0]
    warmup_assembled = await retriever.retrieve_and_pack(
        [w_ctx],
        repo_full_name=repo_full_name,
        blob_fetcher=blob_fetcher,
        packer=packer,
        parsed_diff=w_diff,
        head_ref=index_report.commit_sha,
        in_memory_sources={w_probe.file_path: w_src},
    )
    cold_ms = warmup_assembled.total_ms

    eval_cases: list[M2EvalCase] = []
    for gt_probe, source_text, file_ctx, parsed_diff, target_chunk in prepared_probes:
        assembled = await retriever.retrieve_and_pack(
            [file_ctx],
            repo_full_name=repo_full_name,
            blob_fetcher=blob_fetcher,
            packer=packer,
            parsed_diff=parsed_diff,
            head_ref=index_report.commit_sha,
            in_memory_sources={gt_probe.file_path: source_text},
        )
        retrieved_callers = tuple(
            _canonical_target(c.file_path, c.symbol_name) for c in assembled.retrieval.callers
        )
        retrieved_callees = tuple(
            _canonical_target(c.file_path, c.symbol_name) for c in assembled.retrieval.callees
        )
        retrieved_docs = tuple(
            _canonical_target(d.file_path, d.symbol_name)
            for d in assembled.retrieval.architecture_docs
        )

        caller_recall = compute_recall_at_k(retrieved_callers, set(gt_probe.expected_callers), k=5)
        callee_recall = compute_recall_at_k(retrieved_callees, set(gt_probe.expected_callees), k=5)
        doc_recall = (
            0.0
            if assembled.retrieval.doc_search_degraded
            else compute_recall_at_k(retrieved_docs, set(gt_probe.expected_docs), k=5)
        )

        active_group_scores: list[float] = []
        if gt_probe.expected_callers:
            active_group_scores.append(caller_recall)
        if gt_probe.expected_callees:
            active_group_scores.append(callee_recall)
        if gt_probe.expected_docs or assembled.retrieval.doc_search_degraded:
            active_group_scores.append(doc_recall)
        combined_case_recall = (
            round(sum(active_group_scores) / len(active_group_scores), 4)
            if active_group_scores
            else caller_recall
        )

        doc_raw_scores, doc_max_raw_score = await _measure_diagnostic_doc_scores(
            embedder=embedder,
            store=store,
            repo_full_name=repo_full_name,
            query_text=assembled.retrieval.query_text,
            has_docs=doc_count > 0,
        )
        logger.info(
            "m2_live_eval_probe_completed",
            repo=repo_full_name,
            file_path=gt_probe.file_path,
            symbol=gt_probe.symbol_name,
            caller_recall_at_5=round(caller_recall, 4),
            callee_recall_at_5=round(callee_recall, 4),
            doc_recall_at_5=round(doc_recall, 4),
            cold_ms=cold_ms,
            warm_ms=assembled.total_ms,
            tokens_saved_pct=assembled.packed_bundle.metrics.tokens_saved_pct,
            doc_hits_prod=len(assembled.retrieval.architecture_docs),
            doc_max_raw_score=doc_max_raw_score,
            doc_raw_scores=list(doc_raw_scores),
        )

        _extract_body_canaries(
            source_text,
            start_line=target_chunk.start_line,
            end_line=target_chunk.end_line,
            language=target_chunk.language,
            collected=body_canary_collector,
        )

        metrics = assembled.packed_bundle.metrics
        eval_cases.append(
            M2EvalCase(
                repo_full_name=repo_full_name,
                language=language_label,
                file_path=gt_probe.file_path,
                symbol_name=gt_probe.symbol_name,
                expected_callers=gt_probe.expected_callers,
                retrieved_callers=retrieved_callers,
                expected_callees=gt_probe.expected_callees,
                retrieved_callees=retrieved_callees,
                expected_docs=gt_probe.expected_docs,
                retrieved_docs=retrieved_docs,
                recall_at_5=combined_case_recall,
                caller_recall_at_5=caller_recall,
                callee_recall_at_5=callee_recall,
                doc_recall_at_5=doc_recall,
                cold_retrieval_ms=cold_ms,
                warm_retrieval_ms=assembled.total_ms,
                qdrant_ms=assembled.qdrant_ms,
                blob_fetch_ms=assembled.blob_fetch_ms,
                pack_ms=assembled.pack_ms,
                packed_aux_tokens=metrics.packed_aux_tokens,
                baseline_tokens=metrics.baseline_tokens,
                codesight_tokens=metrics.codesight_tokens,
                tokens_saved_pct=metrics.tokens_saved_pct,
                doc_hits_at_prod_threshold=len(assembled.retrieval.architecture_docs),
                doc_max_raw_score=doc_max_raw_score,
                doc_raw_scores=doc_raw_scores,
                doc_search_degraded=assembled.retrieval.doc_search_degraded,
                blobs_fetched=assembled.blob_report.unique_blobs_fetched,
            )
        )

    warm_latencies = sorted(c.warm_retrieval_ms for c in eval_cases)
    p50_ms = round(statistics.median(warm_latencies), 2)
    p95_idx = min(len(warm_latencies) - 1, math.ceil(0.95 * len(warm_latencies)) - 1)
    p95_ms = round(warm_latencies[p95_idx], 2)

    caller_cases = [c for c in eval_cases if c.expected_callers]
    callee_cases = [c for c in eval_cases if c.expected_callees]
    doc_cases = [c for c in eval_cases if c.expected_docs]
    degraded_cnt = sum(1 for c in eval_cases if c.doc_search_degraded)

    mean_caller_recall = (
        round(
            sum(
                (c.caller_recall_at_5 if c.caller_recall_at_5 is not None else c.recall_at_5)
                for c in caller_cases
            )
            / len(caller_cases),
            4,
        )
        if caller_cases
        else 0.0
    )
    mean_callee_recall = (
        round(sum(c.callee_recall_at_5 for c in callee_cases) / len(callee_cases), 4)
        if callee_cases
        else 0.0
    )
    mean_doc_recall = (
        0.0
        if degraded_cnt > 0
        else (
            round(sum(c.doc_recall_at_5 for c in doc_cases) / len(doc_cases), 4)
            if doc_cases
            else 0.0
        )
    )
    mean_recall = round(sum(c.recall_at_5 for c in eval_cases) / len(eval_cases), 4)
    mean_savings = round(sum(c.tokens_saved_pct for c in eval_cases) / len(eval_cases), 2)
    max_aux = max(c.packed_aux_tokens for c in eval_cases)
    doc_max = max(
        (c.doc_max_raw_score for c in eval_cases if c.doc_max_raw_score is not None),
        default=None,
    )
    doc_hits = sum(c.doc_hits_at_prod_threshold for c in eval_cases)

    summary = M2RepoEvalSummary(
        repo_full_name=repo_full_name,
        language=language_label,
        upserted_points=index_report.upserted_points,
        code_chunks=max(index_report.code_chunks, code_count),
        doc_chunks=max(index_report.doc_chunks, doc_count),
        index_elapsed_ms=index_report.elapsed_ms,
        cases_evaluated=len(eval_cases),
        recall_at_5=mean_recall,
        caller_recall_at_5=mean_caller_recall,
        callee_recall_at_5=mean_callee_recall,
        doc_recall_at_5=mean_doc_recall,
        caller_cases_evaluated=len(caller_cases),
        callee_cases_evaluated=len(callee_cases),
        doc_cases_evaluated=len(doc_cases),
        degraded_doc_cases=degraded_cnt,
        ground_truth_source=gt_source,
        p50_retrieval_ms=p50_ms,
        p95_retrieval_ms=p95_ms,
        cold_retrieval_ms=cold_ms,
        tokens_saved_pct=mean_savings,
        max_packed_aux_tokens=max_aux,
        doc_threshold_used=doc_score_threshold,
        doc_max_raw_score=doc_max,
        doc_hits_above_threshold=doc_hits,
    )
    return summary, eval_cases


def _load_zerocode_auditor_module() -> ModuleType:
    for parent in Path(__file__).resolve().parents:
        candidate = (
            parent
            / ".agents"
            / "skills"
            / "zerocode-ast-rag"
            / "scripts"
            / "audit_qdrant_zerocode.py"
        )
        if candidate.is_file():
            spec = importlib.util.spec_from_file_location("audit_qdrant_zerocode", candidate)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"Unable to load Zero-Code auditor module from {candidate}.")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise FileNotFoundError(
        "Could not locate .agents/skills/zerocode-ast-rag/scripts/audit_qdrant_zerocode.py."
    )


async def _default_index_repo(
    repo_full_name: str,
    *,
    settings: Settings,
    http_client: httpx.AsyncClient,
    redis_client: redis_async.Redis,
    session_factory: async_sessionmaker[AsyncSession],
    store: QdrantZeroCodeStore,
    embedder: GeminiEmbedder,
    embed_code_chunks: bool,
) -> InitialIndexReport:
    from codesight.cli.commands.index import run_cli_index

    return await run_cli_index(
        repo_full_name,
        settings=settings,
        http_client=http_client,
        redis_client=redis_client,
        session_factory=session_factory,
        store=store,
        embedder=embedder,
        embed_code_chunks=embed_code_chunks,
    )


def _normalize_ground_truth_map(
    ground_truth: Mapping[str, M2RepoGroundTruth] | Sequence[M2RepoGroundTruth] | None,
) -> dict[str, M2RepoGroundTruth]:
    if ground_truth is None:
        return {}
    if isinstance(ground_truth, Mapping):
        return {k.strip().lower(): v for k, v in ground_truth.items()}
    return {entry.repo_full_name.strip().lower(): entry for entry in ground_truth}


async def run_milestone2_live_evaluation(
    *,
    settings: Settings,
    py_repo: str | None = None,
    ts_repo: str | None = None,
    collection_name: str | None = None,
    session_factory: async_sessionmaker[AsyncSession],
    http_client: httpx.AsyncClient | None = None,
    redis_client: redis_async.Redis | None = None,
    store: QdrantZeroCodeStore | None = None,
    embedder: GeminiEmbedder | None = None,
    blob_fetcher: GitHubBlobFetcher | None = None,
    index_fn: IndexFn | None = None,
    points_loader: PointsLoaderFn | None = None,
    auditor_fn: AuditorFn | None = None,
    disk_scanner_fn: DiskScannerFn | None = None,
    auditor_module: Any = None,
    ground_truth: Mapping[str, M2RepoGroundTruth] | Sequence[M2RepoGroundTruth] | None = None,
    max_probes_per_repo: int = DEFAULT_PROBES_PER_REPO,
    doc_score_threshold: float = DEFAULT_DOC_SCORE_THRESHOLD,
    api_base_url: str = DEFAULT_GITHUB_API_URL,
    qdrant_container: str | None = None,
) -> M2LiveEvaluationReport:
    """Index real Python + TypeScript/TSX repos and verify Milestone 2 KPI & NFR-3 gates."""
    eff_probes = validate_positive_int(max_probes_per_repo, name="max_probes_per_repo")
    py_slug = _resolve_py_repo(py_repo, settings=settings)
    eff_collection = (
        collection_name.strip()
        if isinstance(collection_name, str) and collection_name.strip()
        else settings.qdrant_collection_name
    )
    if not eff_collection:
        raise ValueError("collection_name must not be empty.")

    gt_by_slug = _normalize_ground_truth_map(ground_truth)
    eval_settings = settings.model_copy(
        update={
            "qdrant_collection_name": eff_collection,
            "index_embed_code_chunks": False,
        }
    )

    owned_http: httpx.AsyncClient | None = None
    owned_redis: redis_async.Redis | None = None
    owned_store: QdrantZeroCodeStore | None = None
    owned_embedder: GeminiEmbedder | None = None
    owned_blob_fetcher: GitHubBlobFetcher | None = None

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
                eval_settings.redis_url,
                decode_responses=True,
                socket_timeout=eval_settings.health_timeout_seconds,
                socket_connect_timeout=eval_settings.health_timeout_seconds,
            )
            active_redis = owned_redis

        ts_slug = await _resolve_ts_repo(
            ts_repo,
            settings=eval_settings,
            http_client=active_http,
            redis_client=active_redis,
            api_base_url=api_base_url,
        )

        active_embedder = embedder
        if active_embedder is None:
            if not eval_settings.has_gemini_api_key:
                raise EmbeddingConfigError(
                    "GEMINI_API_KEY is required for live Milestone 2 evaluation."
                )
            owned_embedder = GeminiEmbedder(settings=eval_settings)
            active_embedder = owned_embedder

        active_store = store
        if active_store is None:
            owned_store = QdrantZeroCodeStore(
                settings=eval_settings,
                collection_name=eff_collection,
            )
            active_store = owned_store

        active_blob_fetcher = blob_fetcher
        if active_blob_fetcher is None:
            auth_provider = (
                GitHubAuthProvider(
                    eval_settings,
                    redis_client=active_redis,
                    http_client=active_http,
                    base_url=api_base_url,
                )
                if eval_settings.has_github_credentials
                else None
            )
            owned_blob_fetcher = GitHubBlobFetcher(
                auth_provider,
                http_client=active_http,
                base_url=api_base_url,
                allow_public_fallback=True,
            )
            active_blob_fetcher = owned_blob_fetcher

        indexer = index_fn if index_fn is not None else _default_index_repo
        py_index_report = await indexer(
            py_slug,
            settings=eval_settings,
            http_client=active_http,
            redis_client=active_redis,
            session_factory=session_factory,
            store=active_store,
            embedder=active_embedder,
            embed_code_chunks=False,
        )
        ts_index_report = await indexer(
            ts_slug,
            settings=eval_settings,
            http_client=active_http,
            redis_client=active_redis,
            session_factory=session_factory,
            store=active_store,
            embedder=active_embedder,
            embed_code_chunks=False,
        )

        canonical_py_slug = (py_index_report.repo_full_name or py_slug).strip() or py_slug
        canonical_ts_slug = (ts_index_report.repo_full_name or ts_slug).strip() or ts_slug

        retriever = HybridContextRetriever(
            store=active_store,
            embedder=active_embedder,
            doc_score_threshold=doc_score_threshold,
        )
        packer = TokenBudgetPacker(budget_tokens=M2_MAX_AUX_TOKENS)
        registry = ASTParserRegistry()
        diff_parser = SmartDiffParser()

        scrolled_points: list[QdrantStoredPoint] = []
        py_body_canaries: list[str] = []
        ts_body_canaries: list[str] = []

        py_gt = gt_by_slug.get(canonical_py_slug.lower()) or gt_by_slug.get(py_slug.lower())
        ts_gt = gt_by_slug.get(canonical_ts_slug.lower()) or gt_by_slug.get(ts_slug.lower())

        py_summary, py_cases = await _evaluate_single_repo(
            repo_full_name=canonical_py_slug,
            language_label="python",
            index_report=py_index_report,
            store=active_store,
            embedder=active_embedder,
            retriever=retriever,
            blob_fetcher=active_blob_fetcher,
            packer=packer,
            registry=registry,
            diff_parser=diff_parser,
            points_loader=points_loader,
            curated_ground_truth=py_gt,
            max_probes=eff_probes,
            doc_score_threshold=doc_score_threshold,
            scrolled_points_collector=scrolled_points,
            body_canary_collector=py_body_canaries,
        )
        ts_summary, ts_cases = await _evaluate_single_repo(
            repo_full_name=canonical_ts_slug,
            language_label="typescript",
            index_report=ts_index_report,
            store=active_store,
            embedder=active_embedder,
            retriever=retriever,
            blob_fetcher=active_blob_fetcher,
            packer=packer,
            registry=registry,
            diff_parser=diff_parser,
            points_loader=points_loader,
            curated_ground_truth=ts_gt,
            max_probes=eff_probes,
            doc_score_threshold=doc_score_threshold,
            scrolled_points_collector=scrolled_points,
            body_canary_collector=ts_body_canaries,
        )

        # S2-05: Select body markers purely from source syntax (never filtered against payloads!)
        body_canaries = list(dict.fromkeys((*py_body_canaries, *ts_body_canaries)))
        scan_markers = tuple(body_canaries) if body_canaries else (_STATIC_FALLBACK_CANARY,)

        all_cases = (*py_cases, *ts_cases)
        warm_latencies = sorted(c.warm_retrieval_ms for c in all_cases)
        p50_ms = round(statistics.median(warm_latencies), 2)
        p95_idx = min(len(warm_latencies) - 1, math.ceil(0.95 * len(warm_latencies)) - 1)
        p95_ms = round(warm_latencies[p95_idx], 2)
        cold_ms = round(max(py_summary.cold_retrieval_ms, ts_summary.cold_retrieval_ms), 2)

        caller_cases = [c for c in all_cases if c.expected_callers]
        callee_cases = [c for c in all_cases if c.expected_callees]
        doc_cases = [c for c in all_cases if c.expected_docs]
        total_degraded_docs = sum(1 for c in all_cases if c.doc_search_degraded)

        overall_caller_recall = (
            round(
                sum(
                    (c.caller_recall_at_5 if c.caller_recall_at_5 is not None else c.recall_at_5)
                    for c in caller_cases
                )
                / len(caller_cases),
                4,
            )
            if caller_cases
            else 0.0
        )
        overall_callee_recall = (
            round(sum(c.callee_recall_at_5 for c in callee_cases) / len(callee_cases), 4)
            if callee_cases
            else 0.0
        )
        overall_doc_recall = (
            0.0
            if total_degraded_docs > 0
            else (
                round(sum(c.doc_recall_at_5 for c in doc_cases) / len(doc_cases), 4)
                if doc_cases
                else 0.0
            )
        )
        mean_recall = round(sum(c.recall_at_5 for c in all_cases) / len(all_cases), 4)
        mean_savings = round(sum(c.tokens_saved_pct for c in all_cases) / len(all_cases), 2)
        max_aux = max(c.packed_aux_tokens for c in all_cases)
        raw_doc_scores = tuple(score for case in all_cases for _label, score in case.doc_raw_scores)

        # Dual-layer NFR-3 audit using the EXACT SAME `scan_markers` for payload AND disk scans!
        resolved_auditor = auditor_module
        if resolved_auditor is None:
            resolved_auditor = await asyncio.to_thread(_load_zerocode_auditor_module)

        active_auditor = (
            auditor_fn if auditor_fn is not None else resolved_auditor.audit_live_collection
        )
        active_disk_scanner = (
            disk_scanner_fn
            if disk_scanner_fn is not None
            else resolved_auditor.scan_container_storage_for_canaries
        )

        audit_rc = await asyncio.to_thread(
            active_auditor,
            eval_settings.qdrant_http_url,
            eff_collection,
            scan_all=True,
            strict_indexes=True,
            canary_substrings=scan_markers,
        )
        in_memory_payload_violations = resolved_auditor.audit_payloads(
            [{"id": pt.point_id, "payload": pt.payload} for pt in scrolled_points],
            canary_substrings=scan_markers,
        )
        auditor_violations = 0 if audit_rc == 0 else max(1, int(audit_rc))
        zerocode_violations = max(auditor_violations, len(in_memory_payload_violations))

        container_name = (
            (qdrant_container or "").strip()
            or (eval_settings.codesight_qdrant_container or "").strip()
            or os.getenv("CODESIGHT_QDRANT_CONTAINER", "").strip()
            or "codesight-qdrant"
        )
        disk_violations_list: list[str] = []
        control_hits = await asyncio.to_thread(
            active_disk_scanner,
            container_name,
            eff_collection,
            (canonical_py_slug,),
        )
        if not control_hits or not all(hit.startswith("NFR-3 disk breach") for hit in control_hits):
            disk_violations_list.append(
                f"Positive control failed for '{canonical_py_slug}' in container "
                f"'{container_name}': {control_hits!r}"
            )

        leak_hits = await asyncio.to_thread(
            active_disk_scanner,
            container_name,
            eff_collection,
            scan_markers,
        )
        disk_violations_list.extend(leak_hits)

        report = M2LiveEvaluationReport(
            py_repo=canonical_py_slug,
            ts_repo=canonical_ts_slug,
            collection_name=eff_collection,
            recall_at_5=mean_recall,
            caller_recall_at_5=overall_caller_recall,
            callee_recall_at_5=overall_callee_recall,
            doc_recall_at_5=overall_doc_recall,
            degraded_doc_cases=total_degraded_docs,
            p50_retrieval_ms=p50_ms,
            tokens_saved_pct=mean_savings,
            zerocode_violations=zerocode_violations,
            p95_retrieval_ms=p95_ms,
            cold_retrieval_ms=cold_ms,
            max_packed_aux_tokens=max_aux,
            doc_score_threshold=doc_score_threshold,
            doc_raw_scores_observed=raw_doc_scores,
            disk_violations=tuple(disk_violations_list),
            py_summary=py_summary,
            ts_summary=ts_summary,
            cases=all_cases,
        )
        logger.info(
            "m2_live_eval_completed",
            py_repo=canonical_py_slug,
            ts_repo=canonical_ts_slug,
            collection=eff_collection,
            recall_at_5=report.recall_at_5,
            caller_recall_at_5=report.caller_recall_at_5,
            callee_recall_at_5=report.callee_recall_at_5,
            doc_recall_at_5=report.doc_recall_at_5,
            p50_retrieval_ms=report.p50_retrieval_ms,
            cold_retrieval_ms=report.cold_retrieval_ms,
            tokens_saved_pct=report.tokens_saved_pct,
            zerocode_violations=report.zerocode_violations,
            disk_violations=len(report.disk_violations),
            py_canaries=len(py_body_canaries),
            ts_canaries=len(ts_body_canaries),
        )
        return report
    finally:
        if owned_blob_fetcher is not None:
            await owned_blob_fetcher.aclose()
        if owned_store is not None:
            await owned_store.aclose()
        if owned_embedder is not None:
            await owned_embedder.aclose()
        if owned_redis is not None:
            try:
                await owned_redis.aclose()
            except (redis.exceptions.RedisError, OSError):
                pass
        if owned_http is not None:
            await owned_http.aclose()
