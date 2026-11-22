"""Unified multi-language AST parser registry with safe RAM-only Hunk fallback."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

import structlog
from tree_sitter import Language, Node

from codesight.ast_engine._ast_utils import (
    DEFAULT_MAX_CHUNK_LINES,
    append_unique,
    count_syntax_error_nodes,
    resolve_ast_language,
    span_touches_deletion_anchors,
    span_touches_sorted_lines,
)
from codesight.ast_engine._python_ast import PYTHON_LANGUAGE
from codesight.ast_engine._typescript_ast import TSX_LANGUAGE, TYPESCRIPT_LANGUAGE
from codesight.ast_engine.complexity import (
    _analyze_python_tree,
    _analyze_typescript_tree,
    _compute_complexity_delta_from_metrics,
)
from codesight.ast_engine.models import (
    ASTLanguage,
    CodeSymbolChunk,
    FallbackReason,
    FunctionComplexity,
    HunkContextSlice,
    ParsedASTFile,
    PRFileContext,
)
from codesight.ast_engine.python_parser import PythonASTParser
from codesight.ast_engine.typescript_parser import TypeScriptASTParser
from codesight.diff.file_filter import normalize_diff_path
from codesight.diff.models import ParsedFileDiff

logger = structlog.get_logger("codesight.ast_engine")

_LANGUAGE_GRAMMARS: Final[dict[ASTLanguage, Language]] = {
    "python": PYTHON_LANGUAGE,
    "typescript": TYPESCRIPT_LANGUAGE,
    "tsx": TSX_LANGUAGE,
}


def _build_hunk_slices(file_path: str, parsed_diff: ParsedFileDiff) -> tuple[HunkContextSlice, ...]:
    slices: list[HunkContextSlice] = []
    for hunk in parsed_diff.hunks:
        start_line = hunk.new_start
        end_line = hunk.new_start + hunk.new_count - 1 if hunk.new_count > 0 else hunk.new_start
        slices.append(
            HunkContextSlice(
                file_path=file_path,
                hunk_id=hunk.hunk_id,
                start_line=start_line,
                end_line=max(start_line, end_line),
                section_header=hunk.section_header,
                annotated_text=hunk.annotated_text,
            )
        )
    return tuple(slices)


def _collect_standalone_deletion_anchors(
    parsed_diff: ParsedFileDiff,
) -> tuple[tuple[int | None, int | None], ...]:
    """Return `(prev_right, next_right)` anchors around pure-deletion runs in `-U3` hunks."""
    if parsed_diff.change_type == "deleted":
        return ()

    anchors: list[tuple[int | None, int | None]] = []
    for hunk in parsed_diff.hunks:
        logical_lines = [line for line in hunk.lines if line.kind != "no_newline"]
        idx = 0
        total = len(logical_lines)
        while idx < total:
            if logical_lines[idx].kind != "delete":
                idx += 1
                continue
            run_start = idx
            while idx < total and logical_lines[idx].kind == "delete":
                idx += 1
            prev_line = logical_lines[run_start - 1] if run_start > 0 else None
            next_line = logical_lines[idx] if idx < total else None
            if (prev_line is not None and prev_line.kind == "add") or (
                next_line is not None and next_line.kind == "add"
            ):
                continue
            prev_right = (
                prev_line.right_line
                if prev_line is not None and prev_line.kind == "context"
                else None
            )
            next_right = (
                next_line.right_line
                if next_line is not None and next_line.kind == "context"
                else None
            )
            if prev_right is not None or next_right is not None:
                anchors.append((prev_right, next_right))
    return tuple(anchors)


def _filter_touched_chunks(
    chunks: tuple[CodeSymbolChunk, ...],
    changed_right_lines: set[int],
    deletion_anchors: Sequence[tuple[int | None, int | None]] = (),
) -> tuple[CodeSymbolChunk, ...]:
    if not chunks or (not changed_right_lines and not deletion_anchors):
        return ()
    sorted_lines = sorted(changed_right_lines)
    return tuple(
        chunk
        for chunk in chunks
        if span_touches_sorted_lines(chunk.start_line, chunk.end_line, sorted_lines)
        or span_touches_deletion_anchors(chunk.start_line, chunk.end_line, deletion_anchors)
    )


def _collect_removed_symbols(
    before_parsed: ParsedASTFile | None,
    after_parsed: ParsedASTFile,
    *,
    before_error_count: int | None = None,
    after_error_count: int | None = None,
) -> tuple[str, ...]:
    """Return ordered unique symbol names removed or renamed between before/after ASTs."""
    if before_parsed is None or not before_parsed.chunks:
        return ()
    if after_parsed.has_syntax_error and not after_parsed.chunks:
        return ()
    if before_error_count is not None and after_error_count is not None:
        if after_error_count > before_error_count:
            return ()
    elif after_parsed.has_syntax_error and not before_parsed.has_syntax_error:
        return ()
    after_symbol_names = {chunk.symbol_name for chunk in after_parsed.chunks}
    after_qualified_names = {chunk.qualified_name for chunk in after_parsed.chunks}
    removed: list[str] = []
    seen: set[str] = set()
    for chunk in before_parsed.chunks:
        if chunk.qualified_name in after_qualified_names:
            continue
        if chunk.symbol_name not in after_symbol_names or "." not in chunk.qualified_name:
            append_unique(removed, seen, chunk.symbol_name)
            continue
        parent_qname = chunk.qualified_name.rsplit(".", 1)[0]
        if parent_qname in after_qualified_names:
            append_unique(removed, seen, chunk.symbol_name)
    return tuple(removed)


class ASTParserRegistry:
    """Dispatch Python & TypeScript/TSX AST parsing with zero-crash Hunk context fallback."""

    def __init__(self, *, max_chunk_lines: int = DEFAULT_MAX_CHUNK_LINES) -> None:
        self._py_parser = PythonASTParser(max_chunk_lines=max_chunk_lines)
        self._ts_parser = TypeScriptASTParser(max_chunk_lines=max_chunk_lines)

    def resolve_language(self, file_path: str) -> ASTLanguage | None:
        """Return canonical `ASTLanguage` for supported files, or `None` for fallback formats."""
        return resolve_ast_language(file_path)

    def supports(self, file_path: str) -> bool:
        """Return True if `file_path` is handled by a tree-sitter AST parser."""
        return self.resolve_language(file_path) is not None

    def get_tree_sitter_language(self, file_path: str) -> Language | None:
        """Return pre-compiled `tree_sitter.Language` handle for `file_path` (for CA-20)."""
        language = self.resolve_language(file_path)
        if language is None:
            return None
        return _LANGUAGE_GRAMMARS[language]

    def _parse_for_language(
        self,
        source: str | bytes,
        *,
        file_path: str,
        language: ASTLanguage,
    ) -> ParsedASTFile:
        if language == "python":
            return self._py_parser.parse(source, file_path=file_path)
        return self._ts_parser.parse(source, file_path=file_path, language=language)

    def _parse_tree_for_language(
        self,
        source: str | bytes,
        *,
        file_path: str,
        language: ASTLanguage,
    ) -> tuple[ParsedASTFile, Node | None, bytes]:
        if language == "python":
            return self._py_parser._parse_tree(source, file_path=file_path)
        return self._ts_parser._parse_tree(source, file_path=file_path, language=language)

    def parse_file(
        self,
        source: str | bytes,
        *,
        file_path: str,
    ) -> ParsedASTFile | None:
        """Parse a supported source file in RAM, returning `None` for unsupported formats."""
        norm_path = normalize_diff_path(file_path).rstrip("/") or "<memory>"
        language = self.resolve_language(norm_path)
        if language is None:
            return None

        try:
            return self._parse_for_language(source, file_path=norm_path, language=language)
        except Exception as exc:
            logger.warning(
                "ast_registry_parse_failed",
                file_path=norm_path,
                language=language,
                error_type=type(exc).__name__,
            )
            return ParsedASTFile(
                file_path=norm_path,
                language=language,
                imports=(),
                chunks=(),
                has_syntax_error=True,
            )

    def _make_hunk_fallback(
        self,
        file_path: str,
        parsed_diff: ParsedFileDiff,
        *,
        language: ASTLanguage | None,
        reason: FallbackReason,
    ) -> PRFileContext:
        return PRFileContext(
            file_path=file_path,
            mode="hunk_fallback",
            language=language,
            parsed_file=None,
            touched_chunks=(),
            complexity=None,
            hunk_slices=_build_hunk_slices(file_path, parsed_diff),
            fallback_reason=reason,
        )

    def resolve_pr_file(
        self,
        parsed_diff: ParsedFileDiff,
        *,
        after_source: str | bytes | None = None,
        before_source: str | bytes | None = None,
        high_cc_threshold: int = 10,
    ) -> PRFileContext:
        """Resolve AST context + delta CC for a PR file, falling back safely to diff hunks."""
        if isinstance(high_cc_threshold, bool) or high_cc_threshold < 0:
            raise ValueError("high_cc_threshold must be a non-negative integer.")

        norm_path = normalize_diff_path(parsed_diff.file_path).rstrip("/") or parsed_diff.file_path
        language = self.resolve_language(norm_path)

        if parsed_diff.change_type == "deleted":
            return self._make_hunk_fallback(
                norm_path,
                parsed_diff,
                language=language,
                reason="deleted_file",
            )
        if language is None:
            return self._make_hunk_fallback(
                norm_path,
                parsed_diff,
                language=None,
                reason="unsupported_extension",
            )
        if after_source is None:
            return self._make_hunk_fallback(
                norm_path,
                parsed_diff,
                language=language,
                reason="missing_source",
            )

        try:
            parsed_file, after_root, after_bytes = self._parse_tree_for_language(
                after_source,
                file_path=norm_path,
                language=language,
            )
            changed_lines = parsed_diff.changed_right_lines
            deletion_anchors = _collect_standalone_deletion_anchors(parsed_diff)
            touched_chunks = _filter_touched_chunks(
                parsed_file.chunks,
                changed_lines,
                deletion_anchors,
            )
            after_list: tuple[FunctionComplexity, ...]
            after_has_error: bool
            if after_root is None:
                after_list, after_has_error = (), False
            elif language == "python":
                after_list, after_has_error = _analyze_python_tree(after_root, after_bytes)
            else:
                after_list, after_has_error = _analyze_typescript_tree(after_root, after_bytes)

            before_language = (
                self.resolve_language(parsed_diff.old_path) if parsed_diff.old_path else None
            ) or language
            before_parsed: ParsedASTFile | None = None
            before_root: Node | None = None
            before_list: tuple[FunctionComplexity, ...] = ()
            before_has_error = False
            if before_source is not None:
                before_path = (
                    normalize_diff_path(parsed_diff.old_path).rstrip("/")
                    if parsed_diff.old_path
                    else norm_path
                ) or norm_path
                before_parsed, before_root, before_bytes = self._parse_tree_for_language(
                    before_source,
                    file_path=before_path,
                    language=before_language,
                )
                if before_root is not None:
                    if before_language == "python":
                        before_list, before_has_error = _analyze_python_tree(
                            before_root, before_bytes
                        )
                    else:
                        before_list, before_has_error = _analyze_typescript_tree(
                            before_root, before_bytes
                        )
            before_error_count = (
                count_syntax_error_nodes(before_root) if before_root is not None else 0
            )
            after_error_count = (
                count_syntax_error_nodes(after_root) if after_root is not None else 0
            )
            removed_symbols = _collect_removed_symbols(
                before_parsed,
                parsed_file,
                before_error_count=before_error_count,
                after_error_count=after_error_count,
            )
            complexity = _compute_complexity_delta_from_metrics(
                before_list,
                before_has_error,
                after_list,
                after_has_error,
                changed_right_lines=changed_lines,
                deletion_anchors=deletion_anchors,
                high_cc_threshold=high_cc_threshold,
            )
            return PRFileContext(
                file_path=norm_path,
                mode="ast",
                language=language,
                parsed_file=parsed_file,
                touched_chunks=touched_chunks,
                removed_symbols=removed_symbols,
                complexity=complexity,
                hunk_slices=(),
                fallback_reason=None,
            )
        except Exception as exc:
            logger.warning(
                "ast_registry_fallback_triggered",
                file_path=norm_path,
                language=language,
                fallback_reason="parse_failure",
                error_type=type(exc).__name__,
            )
            return self._make_hunk_fallback(
                norm_path,
                parsed_diff,
                language=language,
                reason="parse_failure",
            )
