"""Real-world Milestone 2 dual-language live evaluation harness and KPI validator (CA-16)."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import math
import os
import statistics
from collections import Counter
from collections.abc import Awaitable, Callable, Hashable, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any, Final

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
from codesight.diff.file_filter import normalize_diff_path
from codesight.diff.models import ParsedFileDiff, ParsedPullRequestDiff
from codesight.diff.smart_diff_parser import SmartDiffParser
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

type IndexFn = Callable[..., Awaitable[InitialIndexReport]]
type PointsLoaderFn = Callable[
    [QdrantZeroCodeStore, str],
    Awaitable[tuple[QdrantStoredPoint, ...]],
]
type AuditorFn = Callable[..., int]
type DiskScannerFn = Callable[[str, str, Sequence[str]], list[str]]

logger = structlog.get_logger("codesight.rag.live_eval")


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


@dataclass(frozen=True, slots=True)
class M2RepoEvalSummary:
    """Per-repository indexing, Recall@5, warm latency, token-savings, and doc-score summary."""

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

    def to_dict(self) -> dict[str, Any]:
        """Serialize report into a JSON-compatible dictionary."""
        return asdict(self)


@dataclass(frozen=True, slots=True)
class _CrossFileProbeCandidate:
    file_path: str
    blob_sha: str
    language: str
    symbol_name: str
    symbol_kind: str
    signature: str
    start_line: int
    end_line: int
    called_symbols: tuple[str, ...]
    imports: tuple[str, ...]
    cyclomatic_complexity: int
    expected_callers: tuple[str, ...]
    expected_callees: tuple[str, ...]
    sort_rank: tuple[int, int, int, int, int, int, int, int, int]


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


def _validate_repo_summary_kpis(summary: M2RepoEvalSummary, violations: list[str]) -> None:
    slug = summary.repo_full_name or "<unknown>"
    if summary.cases_evaluated <= 0:
        violations.append(f"{slug}: evaluated 0 cross-file probe cases.")
        return
    if not math.isfinite(summary.recall_at_5) or summary.recall_at_5 < M2_MIN_RECALL_AT_5:
        violations.append(
            f"{slug}: recall_at_5 {summary.recall_at_5:.4f} < {M2_MIN_RECALL_AT_5:.2f}"
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


def _parse_pos_int(raw: object, *, default: int = 1) -> int:
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 1:
        return raw
    return default


def _parse_str_tuple(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    return tuple(item.strip() for item in raw if isinstance(item, str) and item.strip())


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


def _select_cross_file_probes(
    points: Sequence[QdrantStoredPoint],
    *,
    repo_full_name: str,
    max_probes: int,
) -> tuple[list[_CrossFileProbeCandidate], int, int]:
    """Build deterministic cross-file modification probes from Qdrant AST graph edges."""
    code_points: list[QdrantStoredPoint] = []
    doc_count = 0
    for pt in points:
        chunk_type = str(pt.payload.get("chunk_type") or "").strip().lower()
        if chunk_type == "code":
            code_points.append(pt)
        elif chunk_type == "architecture_doc":
            doc_count += 1

    chunks_per_file: Counter[str] = Counter()
    symbol_def_count: Counter[str] = Counter()
    callers_by_symbol: dict[str, dict[tuple[str, str], QdrantStoredPoint]] = {}
    defs_by_symbol: dict[str, list[QdrantStoredPoint]] = {}

    for pt in code_points:
        payload = pt.payload
        norm_path = normalize_diff_path(str(payload.get("file_path") or "")).rstrip("/")
        sym_name = str(payload.get("symbol_name") or "").strip()
        if not norm_path or not sym_name:
            continue
        chunks_per_file[norm_path] += 1
        symbol_def_count[sym_name] += 1
        defs_by_symbol.setdefault(sym_name, []).append(pt)

        for called in _parse_str_tuple(payload.get("called_symbols")):
            callers_by_symbol.setdefault(called, {})[(norm_path, sym_name)] = pt

    candidates: list[_CrossFileProbeCandidate] = []
    for pt in code_points:
        payload = pt.payload
        sym_name = str(payload.get("symbol_name") or "").strip()
        def_file = normalize_diff_path(str(payload.get("file_path") or "")).rstrip("/")
        def_sha = str(payload.get("blob_sha") or "").strip()
        def_lang = str(payload.get("language") or "python").strip().lower()
        def_kind = str(payload.get("symbol_kind") or "function").strip()
        sig = str(payload.get("signature") or sym_name).strip()
        start_line = _parse_pos_int(payload.get("start_line"), default=1)
        end_line = max(start_line, _parse_pos_int(payload.get("end_line"), default=start_line))
        called_symbols = _parse_str_tuple(payload.get("called_symbols"))
        imports = _parse_str_tuple(payload.get("imports"))
        cc_raw = payload.get("cyclomatic_complexity")
        cc = cc_raw if isinstance(cc_raw, int) and not isinstance(cc_raw, bool) else 1

        if not sym_name or not def_file or not def_sha or is_noise_symbol(sym_name):
            continue

        all_callers = callers_by_symbol.get(sym_name, {})
        cross_callers = [
            caller_pt for (c_file, _c_sym), caller_pt in all_callers.items() if c_file != def_file
        ]
        if not cross_callers:
            continue

        same_file_callers = sum(1 for (c_file, _c_sym) in all_callers if c_file == def_file)
        expected_callers = tuple(
            sorted(
                {
                    _canonical_target(
                        normalize_diff_path(str(c.payload.get("file_path") or "")).rstrip("/"),
                        str(c.payload.get("symbol_name") or "").strip(),
                    )
                    for c in cross_callers
                }
            )
        )

        expected_callee_set: set[str] = set()
        for raw_call in called_symbols:
            if "." in raw_call:
                receiver, callee_target = raw_call.rsplit(".", 1)
                if receiver in BUILTIN_NOISE_SYMBOLS:
                    continue
            else:
                callee_target = raw_call
            if is_noise_symbol(callee_target) or callee_target in AMBIGUOUS_METHOD_SYMBOLS:
                continue
            for callee_pt in defs_by_symbol.get(callee_target, ()):
                callee_file = normalize_diff_path(
                    str(callee_pt.payload.get("file_path") or "")
                ).rstrip("/")
                if callee_file and callee_file != def_file:
                    expected_callee_set.add(_canonical_target(callee_file, callee_target))

        has_multi_chunk_caller = any(
            chunks_per_file[normalize_diff_path(str(c.payload.get("file_path") or "")).rstrip("/")]
            >= 2
            for c in cross_callers
        )
        rank = (
            int(not _is_test_path(def_file)),
            int(sym_name not in AMBIGUOUS_METHOD_SYMBOLS),
            int(symbol_def_count[sym_name] == 1),
            int(def_kind in {"function", "method", "class"}),
            int(chunks_per_file[def_file] >= 2),
            int(has_multi_chunk_caller),
            int(same_file_callers <= 2),
            int(1 <= len(cross_callers) <= 50),
            min(len(cross_callers), 8),
        )
        candidates.append(
            _CrossFileProbeCandidate(
                file_path=def_file,
                blob_sha=def_sha,
                language=def_lang,
                symbol_name=sym_name,
                symbol_kind=def_kind,
                signature=sig,
                start_line=start_line,
                end_line=end_line,
                called_symbols=called_symbols,
                imports=imports,
                cyclomatic_complexity=max(1, cc),
                expected_callers=expected_callers,
                expected_callees=tuple(sorted(expected_callee_set)),
                sort_rank=rank,
            )
        )

    if not candidates:
        raise ValueError(
            f"Repository '{repo_full_name}' has no indexed symbols with cross-file callers "
            "in Qdrant."
        )

    candidates.sort(
        key=lambda c: (
            tuple(-x for x in c.sort_rank),
            c.file_path,
            c.start_line,
            c.symbol_name,
        )
    )

    # First pass picks the top probe from distinct files; second pass backfills remaining slots.
    selected: list[_CrossFileProbeCandidate] = []
    seen_keys: set[tuple[str, str, int]] = set()
    seen_files: set[str] = set()
    for cand in candidates:
        if cand.file_path in seen_files:
            continue
        seen_files.add(cand.file_path)
        seen_keys.add((cand.file_path, cand.symbol_name, cand.start_line))
        selected.append(cand)
        if len(selected) >= max_probes:
            return selected, len(code_points), doc_count

    for cand in candidates:
        key = (cand.file_path, cand.symbol_name, cand.start_line)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        selected.append(cand)
        if len(selected) >= max_probes:
            break

    return selected, len(code_points), doc_count


def _resolve_probe_chunk_and_ast(
    probe: _CrossFileProbeCandidate,
    *,
    source_text: str,
    registry: ASTParserRegistry,
) -> tuple[Any, CodeSymbolChunk]:
    parsed_ast = registry.parse_file(source_text, file_path=probe.file_path)
    if parsed_ast is not None and parsed_ast.chunks:
        for chunk in parsed_ast.chunks:
            if chunk.symbol_name == probe.symbol_name and chunk.start_line == probe.start_line:
                return parsed_ast, chunk
        for chunk in parsed_ast.chunks:
            if chunk.symbol_name == probe.symbol_name:
                return parsed_ast, chunk

    ast_lang = (
        parsed_ast.language
        if parsed_ast is not None
        else registry.resolve_language(probe.file_path)
    )
    fallback_lang = ast_lang or ("python" if probe.language == "python" else "typescript")
    kind_map: dict[str, SymbolKind] = {
        "function": "function",
        "method": "method",
        "class": "class",
        "interface": "interface",
        "type_alias": "type_alias",
    }
    symbol_kind: SymbolKind = kind_map.get(probe.symbol_kind, "function")
    fallback_chunk = CodeSymbolChunk(
        file_path=probe.file_path,
        language=fallback_lang,
        symbol_name=probe.symbol_name,
        qualified_name=probe.symbol_name,
        symbol_kind=symbol_kind,
        start_line=probe.start_line,
        end_line=probe.end_line,
        signature=probe.signature,
        docstring=None,
        decorators=(),
        imports=probe.imports,
        called_symbols=probe.called_symbols,
        cyclomatic_complexity=probe.cyclomatic_complexity,
        is_async=False,
    )
    return parsed_ast, fallback_chunk


def _build_probe_diff(
    probe: _CrossFileProbeCandidate,
    *,
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
    comment_suffix = "  # codesight-eval" if probe.language == "python" else " // codesight-eval"
    diff_text = (
        f"diff --git a/{probe.file_path} b/{probe.file_path}\n"
        f"--- a/{probe.file_path}\n"
        f"+++ b/{probe.file_path}\n"
        f"@@ -{line_no},1 +{line_no},1 @@ {orig}\n"
        f"-{orig}\n"
        f"+{orig}{comment_suffix}\n"
    )
    parsed = diff_parser.parse_diff(diff_text)
    if parsed.files:
        patched_files = tuple(
            replace(f, blob_sha=probe.blob_sha) if f.file_path == probe.file_path else f
            for f in parsed.files
        )
        return replace(parsed, files=patched_files)
    synthetic_file = ParsedFileDiff(
        file_path=probe.file_path,
        old_path=probe.file_path,
        new_path=probe.file_path,
        change_type="modified",
        hunks=(),
        annotated_diff=diff_text,
        valid_commentable_lines={line_no},
        right_line_to_hunk={line_no: 1},
        blob_sha=probe.blob_sha,
    )
    return replace(parsed, files=(synthetic_file,), annotated_diff=diff_text)


def _extract_body_canaries(
    source_text: str,
    *,
    start_line: int,
    end_line: int,
    payload_json_blobs: Sequence[str],
    collected: list[str],
    max_canaries: int = _MAX_BODY_CANARIES_PER_REPO,
) -> None:
    if len(collected) >= max_canaries:
        return
    lines = source_text.splitlines()
    if not lines:
        return
    lo = max(1, start_line + 1)
    hi = min(len(lines), end_line)
    for min_len in (_MIN_CANARY_LEN, _FALLBACK_CANARY_LEN):
        for idx in range(lo, hi + 1):
            candidate = lines[idx - 1].strip()
            if not (min_len <= len(candidate) <= _MAX_CANARY_LEN):
                continue
            if "\r" in candidate or "\n" in candidate or "\x00" in candidate:
                continue
            if candidate in collected:
                continue
            if any(candidate in blob for blob in payload_json_blobs):
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
    max_probes: int,
    doc_score_threshold: float,
    payload_json_collector: list[str],
    body_canary_collector: list[str],
) -> tuple[M2RepoEvalSummary, list[M2EvalCase]]:
    loader = points_loader if points_loader is not None else _default_load_repo_points
    repo_points = await loader(store, repo_full_name)
    for pt in repo_points:
        payload_json_collector.append(json.dumps(pt.payload, ensure_ascii=False, sort_keys=True))

    candidate_pool, code_count, doc_count = _select_cross_file_probes(
        repo_points,
        repo_full_name=repo_full_name,
        max_probes=max(max_probes * 3, max_probes),
    )

    prepared_probes: list[
        tuple[_CrossFileProbeCandidate, str, PRFileContext, ParsedPullRequestDiff, CodeSymbolChunk]
    ] = []
    for probe in candidate_pool:
        fetch_report = await blob_fetcher.fetch_slices(
            [
                BlobSliceRequest(
                    repo_full_name=repo_full_name,
                    file_path=probe.file_path,
                    blob_sha=probe.blob_sha,
                    start_line=1,
                    end_line=_MAX_FILE_SLICE_LINES,
                )
            ]
        )
        if not fetch_report.slices:
            continue
        blob_slice = fetch_report.slices[0]
        if blob_slice.status != "ok" or not blob_slice.content:
            continue

        source_text = blob_slice.content
        parsed_ast, target_chunk = _resolve_probe_chunk_and_ast(
            probe,
            source_text=source_text,
            registry=registry,
        )
        parsed_diff = _build_probe_diff(
            probe,
            source_text=source_text,
            target_line=target_chunk.start_line,
            diff_parser=diff_parser,
        )
        file_ctx = PRFileContext(
            file_path=probe.file_path,
            mode="ast",
            language=target_chunk.language,
            parsed_file=parsed_ast,
            touched_chunks=(target_chunk,),
        )
        prepared_probes.append((probe, source_text, file_ctx, parsed_diff, target_chunk))
        if len(prepared_probes) >= max_probes:
            break

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
    for probe, source_text, file_ctx, parsed_diff, target_chunk in prepared_probes:
        assembled = await retriever.retrieve_and_pack(
            [file_ctx],
            repo_full_name=repo_full_name,
            blob_fetcher=blob_fetcher,
            packer=packer,
            parsed_diff=parsed_diff,
            head_ref=index_report.commit_sha,
            in_memory_sources={probe.file_path: source_text},
        )
        retrieved_callers = tuple(
            _canonical_target(c.file_path, c.symbol_name) for c in assembled.retrieval.callers
        )
        retrieved_callees = tuple(
            _canonical_target(c.file_path, c.symbol_name) for c in assembled.retrieval.callees
        )
        recall = compute_recall_at_k(retrieved_callers, set(probe.expected_callers), k=5)
        callee_recall = compute_recall_at_k(retrieved_callees, set(probe.expected_callees), k=5)

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
            file_path=probe.file_path,
            symbol=probe.symbol_name,
            recall_at_5=round(recall, 4),
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
            payload_json_blobs=payload_json_collector,
            collected=body_canary_collector,
        )

        metrics = assembled.packed_bundle.metrics
        eval_cases.append(
            M2EvalCase(
                repo_full_name=repo_full_name,
                language=language_label,
                file_path=probe.file_path,
                symbol_name=probe.symbol_name,
                expected_callers=probe.expected_callers,
                retrieved_callers=retrieved_callers,
                expected_callees=probe.expected_callees,
                retrieved_callees=retrieved_callees,
                recall_at_5=recall,
                callee_recall_at_5=callee_recall,
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

        payload_json_blobs: list[str] = []
        py_body_canaries: list[str] = []
        ts_body_canaries: list[str] = []

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
            max_probes=eff_probes,
            doc_score_threshold=doc_score_threshold,
            payload_json_collector=payload_json_blobs,
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
            max_probes=eff_probes,
            doc_score_threshold=doc_score_threshold,
            payload_json_collector=payload_json_blobs,
            body_canary_collector=ts_body_canaries,
        )

        body_canaries = [
            canary
            for canary in (*py_body_canaries, *ts_body_canaries)
            if not any(canary in blob for blob in payload_json_blobs)
        ]

        all_cases = (*py_cases, *ts_cases)
        warm_latencies = sorted(c.warm_retrieval_ms for c in all_cases)
        p50_ms = round(statistics.median(warm_latencies), 2)
        p95_idx = min(len(warm_latencies) - 1, math.ceil(0.95 * len(warm_latencies)) - 1)
        p95_ms = round(warm_latencies[p95_idx], 2)
        cold_ms = round(max(py_summary.cold_retrieval_ms, ts_summary.cold_retrieval_ms), 2)
        mean_recall = round(sum(c.recall_at_5 for c in all_cases) / len(all_cases), 4)
        mean_savings = round(sum(c.tokens_saved_pct for c in all_cases) / len(all_cases), 2)
        max_aux = max(c.packed_aux_tokens for c in all_cases)
        raw_doc_scores = tuple(score for case in all_cases for _label, score in case.doc_raw_scores)

        # Dual-layer NFR-3 audit: Qdrant full-collection payload/index audit + container disk scan
        resolved_auditor = auditor_module
        if (auditor_fn is None or disk_scanner_fn is None) and resolved_auditor is None:
            resolved_auditor = _load_zerocode_auditor_module()

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
        )
        zerocode_violations = 0 if audit_rc == 0 else max(1, int(audit_rc))

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

        scan_markers = tuple(body_canaries) if body_canaries else (_STATIC_FALLBACK_CANARY,)
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
