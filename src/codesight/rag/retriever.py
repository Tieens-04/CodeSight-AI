"""Hybrid Qdrant context retriever (Callers, Callees, Docs) & RAM PR context assembler."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final

import structlog

from codesight.ast_engine.models import PRFileContext
from codesight.core.errors import RateLimitTaskError, RetryableTaskError
from codesight.diff.file_filter import estimate_tokens, normalize_diff_path
from codesight.diff.models import ParsedFileDiff, ParsedPullRequestDiff
from codesight.rag.embedder import GeminiEmbedder
from codesight.rag.models import QdrantStoredPoint, validate_positive_int
from codesight.rag.qdrant_store import QdrantZeroCodeStore
from codesight.rag.token_packer import (
    ContextCandidateSlice,
    ContextTier,
    PackedContextBundle,
    TokenBudgetPacker,
)
from codesight.vcs.blob_fetcher import (
    MAX_BLOB_FETCH_BYTES,
    BlobBatchFetchReport,
    BlobSliceRequest,
    GitHubBlobFetcher,
    validate_safe_blob_sha,
    validate_safe_repo_path,
)
from codesight.vcs.github_auth import validate_repo_full_name

DEFAULT_DOC_SCORE_THRESHOLD: Final[float] = 0.65
DEFAULT_CALLER_LIMIT: Final[int] = 20
DEFAULT_CALLEE_LIMIT: Final[int] = 20
DEFAULT_DOC_LIMIT: Final[int] = 6
MAX_QUERY_CHARS: Final[int] = 1200
_MIN_SYMBOL_LEN: Final[int] = 3
_ZERO_SHA: Final[str] = "0" * 40
_MAX_LOOKUP_SYMBOLS: Final[int] = 200
_MIN_SCROLL_WINDOW: Final[int] = 256
_MAX_SCROLL_WINDOW: Final[int] = 512

COMMON_NOISE_SYMBOLS: Final[frozenset[str]] = frozenset(
    {
        # Python builtins / ubiquitous container & logging methods
        "get",
        "set",
        "items",
        "keys",
        "values",
        "append",
        "extend",
        "pop",
        "remove",
        "clear",
        "copy",
        "update",
        "strip",
        "lstrip",
        "rstrip",
        "split",
        "rsplit",
        "join",
        "replace",
        "lower",
        "upper",
        "format",
        "encode",
        "decode",
        "startswith",
        "endswith",
        "read",
        "write",
        "close",
        "open",
        "len",
        "str",
        "int",
        "float",
        "bool",
        "list",
        "dict",
        "tuple",
        "print",
        "range",
        "enumerate",
        "zip",
        "isinstance",
        "issubclass",
        "getattr",
        "setattr",
        "hasattr",
        "super",
        "type",
        "min",
        "max",
        "sum",
        "any",
        "all",
        "sorted",
        "reversed",
        "info",
        "debug",
        "warning",
        "error",
        "exception",
        "critical",
        "log",
        # TypeScript / JavaScript / React ubiquitous methods & hooks
        "map",
        "filter",
        "reduce",
        "forEach",
        "find",
        "some",
        "every",
        "includes",
        "indexOf",
        "push",
        "shift",
        "unshift",
        "slice",
        "splice",
        "concat",
        "flat",
        "flatMap",
        "toString",
        "valueOf",
        "hasOwnProperty",
        "then",
        "catch",
        "finally",
        "resolve",
        "reject",
        "stringify",
        "parse",
        "assign",
        "entries",
        "fromEntries",
        "trim",
        "match",
        "test",
        "exec",
        "preventDefault",
        "stopPropagation",
        "useState",
        "useEffect",
        "useMemo",
        "useCallback",
        "useRef",
        "useContext",
        "default",
    }
)
_GENERIC_FILE_STEMS: Final[frozenset[str]] = frozenset(
    {"__init__", "index", "main", "mod", "types", "models", "utils", "helpers", "common"}
)
_IMPORT_SPLIT_RE: Final[re.Pattern[str]] = re.compile(r"[./\\:@]+")

logger = structlog.get_logger("codesight.rag.retriever")


def is_noise_symbol(
    symbol_name: str,
    *,
    noise_symbols: frozenset[str] = COMMON_NOISE_SYMBOLS,
) -> bool:
    """Return True when a bare identifier is too ubiquitous for high-signal Qdrant lookup."""
    cleaned = symbol_name.strip()
    if len(cleaned) < _MIN_SYMBOL_LEN:
        return True
    if cleaned.startswith("__") and cleaned.endswith("__"):
        return True
    return cleaned in noise_symbols


def _extract_file_stem(file_path: str) -> str:
    basename = file_path.rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    return stem.strip().lower()


def _extract_parent_dir(file_path: str) -> str:
    return file_path.rsplit("/", 1)[0] if "/" in file_path else ""


def _extract_top_package(file_path: str) -> str:
    parts = [part for part in file_path.split("/") if part]
    if len(parts) >= 2 and parts[0] in {"src", "lib", "app", "packages", "web"}:
        return "/".join(parts[:2])
    return parts[0] if parts else ""


def _safe_norm_path(raw_path: str) -> str | None:
    try:
        return validate_safe_repo_path(raw_path)
    except ValueError:
        return None


def _safe_blob_sha(raw_sha: str | None) -> str | None:
    if not isinstance(raw_sha, str):
        return None
    cleaned = raw_sha.strip()
    if not cleaned or cleaned == _ZERO_SHA:
        return None
    try:
        return validate_safe_blob_sha(cleaned)
    except ValueError:
        return None


def _tokenize_single_import(raw: str) -> set[str]:
    tokens: set[str] = set()
    for tok in _IMPORT_SPLIT_RE.split(raw.strip()):
        cleaned = tok.strip().lower()
        if cleaned and cleaned != "*":
            tokens.add(cleaned)
    return tokens


def _tokenize_imports(imports: Sequence[str]) -> set[str]:
    tokens: set[str] = set()
    for raw in imports:
        tokens.update(_tokenize_single_import(raw))
    return tokens


def _dedupe_ordered(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in values:
        item = raw.strip()
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def build_architecture_query(file_contexts: Sequence[PRFileContext]) -> str | None:
    """Synthesize one concise batch semantic query for Architecture Docs retrieval."""
    parts: list[str] = []
    files: list[str] = []
    symbols: list[str] = []
    imports: list[str] = []
    docs: list[str] = []

    for ctx in file_contexts:
        norm_path = normalize_diff_path(ctx.file_path).rstrip("/")
        if norm_path:
            files.append(norm_path)
        for chunk in ctx.touched_chunks:
            symbols.append(f"{chunk.qualified_name}: {chunk.signature}")
            if chunk.docstring:
                first_line = " ".join(chunk.docstring.split())[:160]
                if first_line:
                    docs.append(first_line)
            imports.extend(chunk.imports[:8])
        for hunk in ctx.hunk_slices:
            if hunk.section_header:
                symbols.append(hunk.section_header)

    unique_files = _dedupe_ordered(files)
    unique_symbols = _dedupe_ordered(symbols)
    unique_imports = _dedupe_ordered(imports)
    unique_docs = _dedupe_ordered(docs)

    if not (unique_files or unique_symbols or unique_docs):
        return None

    if unique_files:
        parts.append("modified files: " + ", ".join(unique_files[:12]))
    if unique_symbols:
        parts.append("modified symbols: " + "; ".join(unique_symbols[:16]))
    if unique_docs:
        parts.append("intent: " + "; ".join(unique_docs[:6]))
    if unique_imports:
        parts.append("imports: " + ", ".join(unique_imports[:16]))

    combined = "\n".join(parts).strip()
    if not combined:
        return None
    return combined[:MAX_QUERY_CHARS]


@dataclass(frozen=True, slots=True)
class RetrievedContextCandidate:
    """Zero-code NFR-3 metadata candidate pointing to a line range in GitHub."""

    tier: ContextTier
    point_id: str
    repo_full_name: str
    file_path: str
    blob_sha: str | None
    ref: str | None
    symbol_name: str
    symbol_kind: str
    signature: str
    start_line: int
    end_line: int
    relevance_score: float
    cyclomatic_complexity: int = 0
    matched_symbols: tuple[str, ...] = ()
    focus_lines: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class HybridRetrievalResult:
    """Parallel Qdrant retrieval result containing zero raw source code (NFR-3)."""

    enclosing: tuple[RetrievedContextCandidate, ...]
    callers: tuple[RetrievedContextCandidate, ...]
    callees: tuple[RetrievedContextCandidate, ...]
    architecture_docs: tuple[RetrievedContextCandidate, ...]
    query_text: str | None
    doc_search_degraded: bool
    qdrant_ms: float


@dataclass(frozen=True, slots=True)
class AssembledPRContext:
    """End-to-end PR context bundle with split Qdrant / RAM Blob / Pack latency telemetry."""

    retrieval: HybridRetrievalResult
    blob_report: BlobBatchFetchReport
    packed_bundle: PackedContextBundle
    qdrant_ms: float
    blob_fetch_ms: float
    pack_ms: float
    total_ms: float


def _parse_int_field(payload: Mapping[str, object], key: str, *, default: int = 1) -> int:
    raw = payload.get(key)
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return default


def _parse_str_list_field(payload: Mapping[str, object], key: str) -> tuple[str, ...]:
    raw = payload.get(key)
    if isinstance(raw, list):
        return tuple(item.strip() for item in raw if isinstance(item, str) and item.strip())
    return ()


def _extract_root_dir(file_path: str) -> str:
    return file_path.split("/", 1)[0] if "/" in file_path else ""


@dataclass(frozen=True, slots=True)
class _ModifiedFootprint:
    """Normalized PR-change footprint used to score caller/callee affinity without data clumps."""

    modified_symbol_keys: frozenset[tuple[str, str]]
    modified_file_paths: frozenset[str]
    modified_dirs: frozenset[str]
    modified_top_pkgs: frozenset[str]
    modified_root_dirs: frozenset[str]
    modified_stems: frozenset[str]
    modified_import_sets: tuple[set[str], ...]


def _score_code_affinity(
    *,
    cand_file_path: str,
    cand_imports: Sequence[str],
    cand_symbol_name: str,
    cand_cc: int,
    matched_symbols: Sequence[str],
    footprint: _ModifiedFootprint,
    is_caller: bool,
) -> float:
    score = 0.0
    if is_caller:
        caller_import_tokens = _tokenize_imports(cand_imports)
        matched_lower = {sym.lower() for sym in matched_symbols}
        if caller_import_tokens & footprint.modified_stems:
            score += 3.0
        if caller_import_tokens & matched_lower:
            score += 1.5
    else:
        callee_stem = _extract_file_stem(cand_file_path)
        has_specific_stem = bool(callee_stem and callee_stem not in _GENERIC_FILE_STEMS)
        sym_lower = cand_symbol_name.lower()
        stem_matched = False
        exact_pair_matched = False
        sym_only_matched = False
        for imp_tokens in footprint.modified_import_sets:
            has_stem = has_specific_stem and callee_stem in imp_tokens
            has_sym = sym_lower in imp_tokens
            if has_stem and has_sym:
                exact_pair_matched = True
                stem_matched = True
                break
            if has_stem:
                stem_matched = True
            elif has_sym:
                sym_only_matched = True
        if exact_pair_matched:
            score += 4.5
        elif stem_matched:
            score += 3.0
        elif sym_only_matched:
            score += 0.5

    cand_dir = _extract_parent_dir(cand_file_path)
    cand_top = _extract_top_package(cand_file_path)
    cand_root = _extract_root_dir(cand_file_path)
    if cand_file_path in footprint.modified_file_paths or (
        cand_dir and cand_dir in footprint.modified_dirs
    ):
        score += 2.0
    elif cand_top and cand_top in footprint.modified_top_pkgs:
        score += 1.0
    elif cand_root and cand_root in footprint.modified_root_dirs:
        score += 0.5

    score += min(len(matched_symbols), 5) * 0.5
    score += min(max(0, cand_cc), 20) * 0.02
    return round(score, 4)


class HybridContextRetriever:
    """Runs parallel Callers, Callees, and Architecture Docs queries on Qdrant gRPC."""

    def __init__(
        self,
        *,
        store: QdrantZeroCodeStore,
        embedder: GeminiEmbedder | None = None,
        doc_score_threshold: float = DEFAULT_DOC_SCORE_THRESHOLD,
        caller_limit: int = DEFAULT_CALLER_LIMIT,
        callee_limit: int = DEFAULT_CALLEE_LIMIT,
        doc_limit: int = DEFAULT_DOC_LIMIT,
        extra_noise_symbols: Sequence[str] = (),
    ) -> None:
        if (
            isinstance(doc_score_threshold, bool)
            or not isinstance(doc_score_threshold, (int, float))
            or not (0.0 <= float(doc_score_threshold) <= 1.0)
        ):
            raise ValueError("doc_score_threshold must be between 0.0 and 1.0.")
        self._store = store
        self._embedder = embedder
        self._doc_score_threshold = float(doc_score_threshold)
        self._caller_limit = validate_positive_int(caller_limit, name="caller_limit")
        self._callee_limit = validate_positive_int(callee_limit, name="callee_limit")
        self._doc_limit = validate_positive_int(doc_limit, name="doc_limit")
        extra_clean = {sym.strip() for sym in extra_noise_symbols if sym.strip()}
        self._noise_symbols = COMMON_NOISE_SYMBOLS | frozenset(extra_clean)

    async def _search_architecture_docs(
        self,
        query_text: str | None,
        *,
        repo_full_name: str,
        repo_id: str | None,
    ) -> tuple[tuple[QdrantStoredPoint, ...], bool]:
        if self._embedder is None or query_text is None:
            return (), False
        try:
            query_vec = await self._embedder.embed_query(query_text)
            points = await self._store.search_similar(
                query_vec,
                repo_full_name=repo_full_name,
                repo_id=repo_id,
                chunk_type="architecture_doc",
                score_threshold=self._doc_score_threshold,
                limit=self._doc_limit,
            )
            return points, False
        except (
            RetryableTaskError,
            RateLimitTaskError,
            TimeoutError,
            ConnectionError,
            OSError,
        ) as exc:
            logger.warning(
                "hybrid_retriever_doc_search_degraded",
                repo=repo_full_name,
                error_type=type(exc).__name__,
            )
            return (), True

    async def retrieve(
        self,
        file_contexts: Sequence[PRFileContext],
        *,
        repo_full_name: str,
        repo_id: str | None = None,
        parsed_diff: ParsedPullRequestDiff | None = None,
        head_ref: str | None = None,
    ) -> HybridRetrievalResult:
        """Run 3 parallel Qdrant lookups (Callers, Callees, Architecture Docs) in zero-code mode."""
        slug = validate_repo_full_name(repo_full_name)
        started = time.perf_counter()

        diff_by_path: dict[str, ParsedFileDiff] = {}
        deleted_paths: set[str] = set()
        renamed_old_paths: set[str] = set()
        if parsed_diff is not None:
            for pf in parsed_diff.files:
                norm_pf = _safe_norm_path(pf.file_path)
                if norm_pf:
                    diff_by_path[norm_pf] = pf
                    if pf.change_type == "deleted":
                        deleted_paths.add(norm_pf)
                if pf.change_type == "renamed" and pf.old_path:
                    norm_old = _safe_norm_path(pf.old_path)
                    if norm_old:
                        renamed_old_paths.add(norm_old)

        enclosing: list[RetrievedContextCandidate] = []
        raw_modified_symbols: list[str] = []
        raw_called_symbols: list[str] = []
        modified_symbol_keys: set[tuple[str, str]] = set()
        modified_file_paths: set[str] = set()
        modified_dirs: set[str] = set()
        modified_top_pkgs: set[str] = set()
        modified_root_dirs: set[str] = set()
        modified_stems: set[str] = set()
        modified_imports: list[str] = []
        ast_live_entries: list[tuple[str, PRFileContext, str | None]] = []
        ast_live_paths: set[str] = set()
        touched_chunk_ids: set[tuple[str, str, int]] = set()

        for ctx in file_contexts:
            norm_path = _safe_norm_path(ctx.file_path)
            if not norm_path:
                continue
            file_diff = diff_by_path.get(norm_path)
            if ctx.fallback_reason == "deleted_file" or (
                file_diff is not None and file_diff.change_type == "deleted"
            ):
                deleted_paths.add(norm_path)
                continue

            modified_file_paths.add(norm_path)
            parent_dir = _extract_parent_dir(norm_path)
            if parent_dir:
                modified_dirs.add(parent_dir)
            top_pkg = _extract_top_package(norm_path)
            if top_pkg:
                modified_top_pkgs.add(top_pkg)
            root_dir = _extract_root_dir(norm_path)
            if root_dir:
                modified_root_dirs.add(root_dir)
            stem = _extract_file_stem(norm_path)
            if stem and stem not in _GENERIC_FILE_STEMS:
                modified_stems.add(stem)

            if file_diff is not None and file_diff.change_type == "renamed" and file_diff.old_path:
                old_norm = _safe_norm_path(file_diff.old_path)
                if old_norm:
                    renamed_old_paths.add(old_norm)
                    old_stem = _extract_file_stem(old_norm)
                    if old_stem and old_stem not in _GENERIC_FILE_STEMS:
                        modified_stems.add(old_stem)

            file_blob_sha = _safe_blob_sha(file_diff.blob_sha) if file_diff is not None else None
            changed_right = (
                tuple(sorted(file_diff.changed_right_lines)) if file_diff is not None else ()
            )

            if ctx.parsed_file is not None:
                modified_imports.extend(ctx.parsed_file.imports)
                if ctx.mode == "ast":
                    ast_live_paths.add(norm_path)
                    ast_live_entries.append((norm_path, ctx, file_blob_sha))

            if ctx.mode == "ast" and ctx.touched_chunks:
                for idx, chunk in enumerate(ctx.touched_chunks):
                    modified_symbol_keys.add((norm_path, chunk.symbol_name))
                    touched_chunk_ids.add((norm_path, chunk.qualified_name, chunk.start_line))
                    modified_imports.extend(chunk.imports)
                    if not is_noise_symbol(chunk.symbol_name, noise_symbols=self._noise_symbols):
                        raw_modified_symbols.append(chunk.symbol_name)
                    for callee in chunk.called_symbols:
                        if not is_noise_symbol(callee, noise_symbols=self._noise_symbols):
                            raw_called_symbols.append(callee)
                    focus = tuple(
                        ln for ln in changed_right if chunk.start_line <= ln <= chunk.end_line
                    )
                    enclosing.append(
                        RetrievedContextCandidate(
                            tier="enclosing_symbol",
                            point_id=f"enclosing:{norm_path}:{chunk.symbol_name}:{chunk.start_line}:{idx}",
                            repo_full_name=slug,
                            file_path=norm_path,
                            blob_sha=file_blob_sha,
                            ref=head_ref,
                            symbol_name=chunk.symbol_name,
                            symbol_kind=chunk.symbol_kind,
                            signature=chunk.signature,
                            start_line=chunk.start_line,
                            end_line=chunk.end_line,
                            relevance_score=10.0,
                            cyclomatic_complexity=chunk.cyclomatic_complexity,
                            matched_symbols=(chunk.symbol_name,),
                            focus_lines=focus,
                        )
                    )
            elif ctx.mode == "ast" and file_diff is not None and file_diff.hunks:
                # Module-level AST edit outside any function/class: slice PR-head hunk window
                for diff_hunk in file_diff.hunks:
                    h_start = max(1, diff_hunk.new_start)
                    h_end = (
                        max(h_start, diff_hunk.new_start + diff_hunk.new_count - 1)
                        if diff_hunk.new_count > 0
                        else h_start
                    )
                    focus = tuple(ln for ln in changed_right if h_start <= ln <= h_end)
                    label = diff_hunk.section_header.strip() or f"module_hunk_{diff_hunk.hunk_id}"
                    enclosing.append(
                        RetrievedContextCandidate(
                            tier="enclosing_symbol",
                            point_id=f"hunk:{norm_path}:{diff_hunk.hunk_id}:{h_start}",
                            repo_full_name=slug,
                            file_path=norm_path,
                            blob_sha=file_blob_sha,
                            ref=head_ref,
                            symbol_name=label,
                            symbol_kind="module_hunk",
                            signature=label,
                            start_line=h_start,
                            end_line=h_end,
                            relevance_score=9.0,
                            cyclomatic_complexity=0,
                            matched_symbols=(),
                            focus_lines=focus,
                        )
                    )
            elif ctx.hunk_slices:
                for hunk_slice in ctx.hunk_slices:
                    focus = tuple(
                        ln
                        for ln in changed_right
                        if hunk_slice.start_line <= ln <= hunk_slice.end_line
                    )
                    label = hunk_slice.section_header.strip() or f"hunk_{hunk_slice.hunk_id}"
                    enclosing.append(
                        RetrievedContextCandidate(
                            tier="enclosing_symbol",
                            point_id=f"hunk:{norm_path}:{hunk_slice.hunk_id}:{hunk_slice.start_line}",
                            repo_full_name=slug,
                            file_path=norm_path,
                            blob_sha=file_blob_sha,
                            ref=head_ref,
                            symbol_name=label,
                            symbol_kind="hunk",
                            signature=label,
                            start_line=max(1, hunk_slice.start_line),
                            end_line=max(max(1, hunk_slice.start_line), hunk_slice.end_line),
                            relevance_score=9.0,
                            cyclomatic_complexity=0,
                            matched_symbols=(),
                            focus_lines=focus,
                        )
                    )

        target_caller_symbols = _dedupe_ordered(raw_modified_symbols)[:_MAX_LOOKUP_SYMBOLS]
        target_callee_symbols = _dedupe_ordered(raw_called_symbols)[:_MAX_LOOKUP_SYMBOLS]
        target_caller_set = set(target_caller_symbols)
        target_callee_set = set(target_callee_symbols)
        footprint = _ModifiedFootprint(
            modified_symbol_keys=frozenset(modified_symbol_keys),
            modified_file_paths=frozenset(modified_file_paths),
            modified_dirs=frozenset(modified_dirs),
            modified_top_pkgs=frozenset(modified_top_pkgs),
            modified_root_dirs=frozenset(modified_root_dirs),
            modified_stems=frozenset(modified_stems),
            modified_import_sets=tuple(
                toks
                for raw in _dedupe_ordered(modified_imports)
                if (toks := _tokenize_single_import(raw))
            ),
        )
        non_deleted_contexts = [
            ctx
            for ctx in file_contexts
            if ctx.fallback_reason != "deleted_file"
            and (_safe_norm_path(ctx.file_path) or "") not in deleted_paths
        ]
        query_text = build_architecture_query(non_deleted_contexts)

        caller_fetch_limit = min(
            max(self._caller_limit * 8, _MIN_SCROLL_WINDOW), _MAX_SCROLL_WINDOW
        )
        callee_fetch_limit = min(
            max(self._callee_limit * 8, _MIN_SCROLL_WINDOW), _MAX_SCROLL_WINDOW
        )

        async def _empty_points() -> tuple[QdrantStoredPoint, ...]:
            return ()

        callers_task = asyncio.create_task(
            self._store.find_by_called_symbols(
                target_caller_symbols,
                repo_full_name=slug,
                repo_id=repo_id,
                limit=caller_fetch_limit,
            )
            if target_caller_symbols
            else _empty_points()
        )
        callees_task = asyncio.create_task(
            self._store.find_by_symbol_names(
                target_callee_symbols,
                repo_full_name=slug,
                repo_id=repo_id,
                limit=callee_fetch_limit,
            )
            if target_callee_symbols
            else _empty_points()
        )
        docs_task = asyncio.create_task(
            self._search_architecture_docs(
                query_text,
                repo_full_name=slug,
                repo_id=repo_id,
            )
        )

        try:
            raw_callers, raw_callees, (raw_docs, doc_degraded) = await asyncio.gather(
                callers_task,
                callees_task,
                docs_task,
            )
        except BaseException:
            for task in (callers_task, callees_task, docs_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(callers_task, callees_task, docs_task, return_exceptions=True)
            raise

        excluded_qdrant_paths = deleted_paths | renamed_old_paths
        local_callers, local_callees = self._extract_local_pr_candidates(
            ast_live_entries=ast_live_entries,
            touched_chunk_ids=touched_chunk_ids,
            slug=slug,
            head_ref=head_ref,
            target_caller_set=target_caller_set,
            target_callee_set=target_callee_set,
            footprint=footprint,
        )

        callers = self._rank_code_points(
            raw_callers,
            tier="caller",
            slug=slug,
            target_symbols=target_caller_set,
            footprint=footprint,
            excluded_paths=excluded_qdrant_paths | ast_live_paths,
            extra_candidates=local_callers,
            limit=self._caller_limit,
        )
        callees = self._rank_code_points(
            raw_callees,
            tier="callee",
            slug=slug,
            target_symbols=target_callee_set,
            footprint=footprint,
            excluded_paths=excluded_qdrant_paths | ast_live_paths,
            extra_candidates=local_callees,
            limit=self._callee_limit,
        )
        docs = self._build_doc_candidates(
            raw_docs,
            slug=slug,
            excluded_paths=excluded_qdrant_paths,
        )

        qdrant_ms = round((time.perf_counter() - started) * 1000.0, 2)
        logger.info(
            "hybrid_context_retrieved",
            repo=slug,
            enclosing_count=len(enclosing),
            callers_count=len(callers),
            callees_count=len(callees),
            docs_count=len(docs),
            doc_search_degraded=doc_degraded,
            qdrant_ms=qdrant_ms,
        )
        return HybridRetrievalResult(
            enclosing=tuple(enclosing),
            callers=callers,
            callees=callees,
            architecture_docs=docs,
            query_text=query_text,
            doc_search_degraded=doc_degraded,
            qdrant_ms=qdrant_ms,
        )

    @staticmethod
    def _extract_local_pr_candidates(
        *,
        ast_live_entries: Sequence[tuple[str, PRFileContext, str | None]],
        touched_chunk_ids: set[tuple[str, str, int]],
        slug: str,
        head_ref: str | None,
        target_caller_set: set[str],
        target_callee_set: set[str],
        footprint: _ModifiedFootprint,
    ) -> tuple[list[RetrievedContextCandidate], list[RetrievedContextCandidate]]:
        """Resolve non-touched same-file callers/callees directly from PR-head AST chunks."""
        local_callers: list[RetrievedContextCandidate] = []
        local_callees: list[RetrievedContextCandidate] = []
        for norm_path, ctx, file_blob_sha in ast_live_entries:
            parsed = ctx.parsed_file
            if parsed is None:
                continue
            for chunk in parsed.chunks:
                if (norm_path, chunk.qualified_name, chunk.start_line) in touched_chunk_ids:
                    continue
                if target_caller_set:
                    matched_calls = tuple(
                        sym for sym in chunk.called_symbols if sym in target_caller_set
                    )
                    if matched_calls:
                        score = _score_code_affinity(
                            cand_file_path=norm_path,
                            cand_imports=chunk.imports,
                            cand_symbol_name=chunk.symbol_name,
                            cand_cc=chunk.cyclomatic_complexity,
                            matched_symbols=matched_calls,
                            footprint=footprint,
                            is_caller=True,
                        )
                        local_callers.append(
                            RetrievedContextCandidate(
                                tier="caller",
                                point_id=(
                                    f"pr_head:caller:{norm_path}:{chunk.qualified_name}:{chunk.start_line}"
                                ),
                                repo_full_name=slug,
                                file_path=norm_path,
                                blob_sha=file_blob_sha,
                                ref=head_ref,
                                symbol_name=chunk.symbol_name,
                                symbol_kind=chunk.symbol_kind,
                                signature=chunk.signature,
                                start_line=chunk.start_line,
                                end_line=chunk.end_line,
                                relevance_score=score,
                                cyclomatic_complexity=chunk.cyclomatic_complexity,
                                matched_symbols=matched_calls,
                            )
                        )
                if chunk.symbol_name in target_callee_set:
                    score = _score_code_affinity(
                        cand_file_path=norm_path,
                        cand_imports=chunk.imports,
                        cand_symbol_name=chunk.symbol_name,
                        cand_cc=chunk.cyclomatic_complexity,
                        matched_symbols=(chunk.symbol_name,),
                        footprint=footprint,
                        is_caller=False,
                    )
                    local_callees.append(
                        RetrievedContextCandidate(
                            tier="callee",
                            point_id=(
                                f"pr_head:callee:{norm_path}:{chunk.qualified_name}:{chunk.start_line}"
                            ),
                            repo_full_name=slug,
                            file_path=norm_path,
                            blob_sha=file_blob_sha,
                            ref=head_ref,
                            symbol_name=chunk.symbol_name,
                            symbol_kind=chunk.symbol_kind,
                            signature=chunk.signature,
                            start_line=chunk.start_line,
                            end_line=chunk.end_line,
                            relevance_score=score,
                            cyclomatic_complexity=chunk.cyclomatic_complexity,
                            matched_symbols=(chunk.symbol_name,),
                        )
                    )
        return local_callers, local_callees

    def _rank_code_points(
        self,
        points: Sequence[QdrantStoredPoint],
        *,
        tier: ContextTier,
        slug: str,
        target_symbols: set[str],
        footprint: _ModifiedFootprint,
        excluded_paths: set[str],
        extra_candidates: Sequence[RetrievedContextCandidate] = (),
        limit: int,
    ) -> tuple[RetrievedContextCandidate, ...]:
        is_caller = tier == "caller"
        seen_ids: set[str] = {cand.point_id for cand in extra_candidates}
        ranked: list[RetrievedContextCandidate] = list(extra_candidates)

        for pt in points:
            if pt.point_id in seen_ids:
                continue
            seen_ids.add(pt.point_id)
            payload = pt.payload
            raw_path = payload.get("file_path")
            raw_sha = payload.get("blob_sha")
            raw_sym = payload.get("symbol_name")
            if (
                not isinstance(raw_path, str)
                or not isinstance(raw_sha, str)
                or not isinstance(raw_sym, str)
            ):
                continue
            norm_path = _safe_norm_path(raw_path)
            blob_sha = _safe_blob_sha(raw_sha)
            sym_name = raw_sym.strip()
            if not norm_path or not blob_sha or not sym_name:
                continue

            if norm_path in excluded_paths:
                continue
            if (norm_path, sym_name) in footprint.modified_symbol_keys:
                continue

            start_line = max(1, _parse_int_field(payload, "start_line", default=1))
            end_line = max(start_line, _parse_int_field(payload, "end_line", default=start_line))
            cc = _parse_int_field(payload, "cyclomatic_complexity", default=0)
            called = _parse_str_list_field(payload, "called_symbols")
            imports = _parse_str_list_field(payload, "imports")

            if is_caller:
                matched = tuple(sym for sym in called if sym in target_symbols)
                if not matched:
                    continue
            else:
                if sym_name not in target_symbols:
                    continue
                matched = (sym_name,)

            affinity = _score_code_affinity(
                cand_file_path=norm_path,
                cand_imports=imports,
                cand_symbol_name=sym_name,
                cand_cc=cc,
                matched_symbols=matched,
                footprint=footprint,
                is_caller=is_caller,
            )
            raw_kind = payload.get("symbol_kind")
            raw_sig = payload.get("signature")
            ranked.append(
                RetrievedContextCandidate(
                    tier=tier,
                    point_id=pt.point_id,
                    repo_full_name=slug,
                    file_path=norm_path,
                    blob_sha=blob_sha,
                    ref=None,
                    symbol_name=sym_name,
                    symbol_kind=raw_kind.strip()
                    if isinstance(raw_kind, str) and raw_kind.strip()
                    else "function",
                    signature=raw_sig.strip()
                    if isinstance(raw_sig, str) and raw_sig.strip()
                    else sym_name,
                    start_line=start_line,
                    end_line=end_line,
                    relevance_score=affinity,
                    cyclomatic_complexity=cc,
                    matched_symbols=matched,
                )
            )

        ranked.sort(key=lambda c: (-c.relevance_score, c.file_path, c.start_line, c.point_id))
        return tuple(ranked[:limit])

    def _build_doc_candidates(
        self,
        points: Sequence[QdrantStoredPoint],
        *,
        slug: str,
        excluded_paths: set[str],
    ) -> tuple[RetrievedContextCandidate, ...]:
        seen_ids: set[str] = set()
        docs: list[RetrievedContextCandidate] = []
        for pt in points:
            if pt.point_id in seen_ids:
                continue
            seen_ids.add(pt.point_id)
            score = float(pt.score) if pt.score is not None else 0.0
            if score < self._doc_score_threshold:
                continue
            payload = pt.payload
            raw_path = payload.get("file_path")
            raw_sha = payload.get("blob_sha")
            raw_sym = payload.get("symbol_name")
            if (
                not isinstance(raw_path, str)
                or not isinstance(raw_sha, str)
                or not isinstance(raw_sym, str)
            ):
                continue
            norm_path = _safe_norm_path(raw_path)
            blob_sha = _safe_blob_sha(raw_sha)
            sym_name = raw_sym.strip()
            if not norm_path or not blob_sha or not sym_name or norm_path in excluded_paths:
                continue
            start_line = max(1, _parse_int_field(payload, "start_line", default=1))
            end_line = max(start_line, _parse_int_field(payload, "end_line", default=start_line))
            raw_sig = payload.get("signature")
            docs.append(
                RetrievedContextCandidate(
                    tier="architecture_doc",
                    point_id=pt.point_id,
                    repo_full_name=slug,
                    file_path=norm_path,
                    blob_sha=blob_sha,
                    ref=None,
                    symbol_name=sym_name,
                    symbol_kind="section",
                    signature=raw_sig.strip()
                    if isinstance(raw_sig, str) and raw_sig.strip()
                    else sym_name,
                    start_line=start_line,
                    end_line=end_line,
                    relevance_score=round(score, 4),
                    cyclomatic_complexity=0,
                    matched_symbols=(),
                )
            )
        docs.sort(key=lambda c: (-c.relevance_score, c.file_path, c.start_line, c.point_id))
        return tuple(docs[: self._doc_limit])

    async def retrieve_and_pack(
        self,
        file_contexts: Sequence[PRFileContext],
        *,
        repo_full_name: str,
        blob_fetcher: GitHubBlobFetcher,
        packer: TokenBudgetPacker | None = None,
        repo_id: str | None = None,
        parsed_diff: ParsedPullRequestDiff | None = None,
        head_ref: str | None = None,
        installation_id: int | None = None,
        in_memory_sources: Mapping[str, str | bytes] | None = None,
    ) -> AssembledPRContext:
        """Run retrieve -> concurrent RAM blob slice -> TokenBudgetPacker with split telemetry."""
        total_started = time.perf_counter()
        active_packer = packer if packer is not None else TokenBudgetPacker()

        retrieval = await self.retrieve(
            file_contexts,
            repo_full_name=repo_full_name,
            repo_id=repo_id,
            parsed_diff=parsed_diff,
            head_ref=head_ref,
        )

        deleted_paths: set[str] = set()
        modified_pr_paths: set[str] = set()
        if parsed_diff is not None:
            for pf in parsed_diff.files:
                norm_pf = _safe_norm_path(pf.file_path)
                if not norm_pf:
                    continue
                if pf.change_type == "deleted":
                    deleted_paths.add(norm_pf)
                else:
                    modified_pr_paths.add(norm_pf)

        # Map transient RAM hunk fallback text by point_id for unfetchable/oversize files
        hunk_fallback_text_by_id: dict[str, str] = {}
        for ctx in file_contexts:
            norm_path = _safe_norm_path(ctx.file_path)
            if not norm_path:
                continue
            if ctx.fallback_reason == "deleted_file" or norm_path in deleted_paths:
                deleted_paths.add(norm_path)
                modified_pr_paths.discard(norm_path)
                continue
            modified_pr_paths.add(norm_path)
            if ctx.hunk_slices:
                for hunk in ctx.hunk_slices:
                    pid = f"hunk:{norm_path}:{hunk.hunk_id}:{hunk.start_line}"
                    hunk_fallback_text_by_id[pid] = hunk.annotated_text

        norm_in_mem_paths: set[str] = set()
        norm_in_mem_tokens: dict[str, int] = {}
        if in_memory_sources:
            for key, raw_v in in_memory_sources.items():
                norm_k = _safe_norm_path(key)
                if not norm_k:
                    continue
                norm_in_mem_paths.add(norm_k)
                if norm_k in modified_pr_paths:
                    raw_b = raw_v.encode("utf-8") if isinstance(raw_v, str) else bytes(raw_v)
                    if 0 < len(raw_b) <= MAX_BLOB_FETCH_BYTES and b"\x00" not in raw_b:
                        norm_in_mem_tokens[norm_k] = estimate_tokens(raw_b)

        all_candidates: list[RetrievedContextCandidate] = [
            *retrieval.enclosing,
            *retrieval.callers,
            *retrieval.callees,
            *retrieval.architecture_docs,
        ]

        fetch_requests: list[BlobSliceRequest] = []
        fetch_candidates: list[RetrievedContextCandidate] = []
        inline_fallback_slices: list[ContextCandidateSlice] = []

        for cand in all_candidates:
            is_pr_head = cand.tier == "enclosing_symbol" or cand.point_id.startswith("pr_head:")
            has_mem = is_pr_head and cand.file_path in norm_in_mem_paths
            has_locator = bool(cand.blob_sha or cand.ref or has_mem)
            if has_locator:
                fetch_requests.append(
                    BlobSliceRequest(
                        repo_full_name=cand.repo_full_name,
                        file_path=cand.file_path,
                        start_line=cand.start_line,
                        end_line=cand.end_line,
                        blob_sha=cand.blob_sha,
                        ref=cand.ref,
                        slice_id=cand.point_id,
                        prefer_in_memory=is_pr_head,
                    )
                )
                fetch_candidates.append(cand)
            elif cand.point_id in hunk_fallback_text_by_id:
                inline_fallback_slices.append(
                    ContextCandidateSlice(
                        tier=cand.tier,
                        file_path=cand.file_path,
                        source_key=f"hunk:{cand.point_id}",
                        start_line=cand.start_line,
                        end_line=cand.end_line,
                        content=hunk_fallback_text_by_id[cand.point_id],
                        symbol_name=cand.symbol_name,
                        symbol_kind=cand.symbol_kind,
                        signature=cand.signature,
                        relevance_score=cand.relevance_score,
                        full_file_tokens=0,
                        focus_lines=cand.focus_lines,
                        use_line_numbers=False,
                    )
                )

        blob_report = await blob_fetcher.fetch_slices(
            fetch_requests,
            installation_id=installation_id,
            in_memory_sources=in_memory_sources,
        )

        pack_started = time.perf_counter()
        packer_candidates: list[ContextCandidateSlice] = list(inline_fallback_slices)
        for cand, slice_res in zip(fetch_candidates, blob_report.slices, strict=True):
            if slice_res.status != "ok" or not slice_res.content:
                fallback_text = hunk_fallback_text_by_id.get(cand.point_id)
                if fallback_text:
                    packer_candidates.append(
                        ContextCandidateSlice(
                            tier=cand.tier,
                            file_path=cand.file_path,
                            source_key=f"hunk:{cand.point_id}",
                            start_line=cand.start_line,
                            end_line=cand.end_line,
                            content=fallback_text,
                            symbol_name=cand.symbol_name,
                            symbol_kind=cand.symbol_kind,
                            signature=cand.signature,
                            relevance_score=cand.relevance_score,
                            full_file_tokens=slice_res.full_file_tokens,
                            focus_lines=cand.focus_lines,
                            use_line_numbers=False,
                        )
                    )
                continue
            is_pr_head = cand.tier == "enclosing_symbol" or cand.point_id.startswith("pr_head:")
            source_key = (
                slice_res.resolved_blob_sha
                or cand.blob_sha
                or cand.ref
                or ("head" if is_pr_head else cand.file_path)
            )
            packer_candidates.append(
                ContextCandidateSlice(
                    tier=cand.tier,
                    file_path=cand.file_path,
                    source_key=source_key,
                    start_line=slice_res.start_line,
                    end_line=slice_res.end_line,
                    content=slice_res.content,
                    symbol_name=cand.symbol_name,
                    symbol_kind=cand.symbol_kind,
                    signature=cand.signature,
                    relevance_score=cand.relevance_score,
                    full_file_tokens=slice_res.full_file_tokens,
                    focus_lines=cand.focus_lines,
                    use_line_numbers=True,
                )
            )

        modified_file_tokens: dict[str, int] = {}
        for mod_path in modified_pr_paths:
            tok = blob_report.full_file_tokens_by_path.get(mod_path, 0)
            if tok <= 0:
                tok = norm_in_mem_tokens.get(mod_path, 0)
            if tok > 0:
                modified_file_tokens[mod_path] = tok

        diff_text = parsed_diff.annotated_diff if parsed_diff is not None else None
        skipped_noise_tokens = (
            parsed_diff.metrics.saved_estimated_tokens if parsed_diff is not None else 0
        )
        packed_bundle = active_packer.pack(
            packer_candidates,
            diff_text=diff_text,
            modified_file_tokens=modified_file_tokens,
            skipped_noise_tokens=skipped_noise_tokens,
        )
        pack_ms = round((time.perf_counter() - pack_started) * 1000.0, 2)
        total_ms = round((time.perf_counter() - total_started) * 1000.0, 2)

        logger.info(
            "hybrid_context_assembled",
            repo=repo_full_name,
            qdrant_ms=retrieval.qdrant_ms,
            blob_fetch_ms=blob_report.elapsed_ms,
            pack_ms=pack_ms,
            total_ms=total_ms,
            packed_aux_tokens=packed_bundle.metrics.packed_aux_tokens,
            tokens_saved_pct=packed_bundle.metrics.tokens_saved_pct,
        )
        return AssembledPRContext(
            retrieval=retrieval,
            blob_report=blob_report,
            packed_bundle=packed_bundle,
            qdrant_ms=retrieval.qdrant_ms,
            blob_fetch_ms=blob_report.elapsed_ms,
            pack_ms=pack_ms,
            total_ms=total_ms,
        )
