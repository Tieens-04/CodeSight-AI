"""In-memory tree-sitter suggestion splice validator & Markdown suggestion sanitizer (CA-20)."""

from __future__ import annotations

import ast
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol

import structlog
from tree_sitter import Parser

from codesight.ast_engine._ast_utils import count_syntax_error_nodes, to_utf8_bytes
from codesight.ast_engine.registry import ASTParserRegistry
from codesight.diff.file_filter import normalize_diff_path
from codesight.diff.models import ParsedFileDiff


class SuggestionInput(Protocol):
    """Structural protocol for suggestion objects passed into AST validation."""

    @property
    def start_line(self) -> int: ...
    @property
    def end_line(self) -> int: ...
    @property
    def replacement_code(self) -> str: ...
    @property
    def explanation(self) -> str | None: ...


@dataclass(frozen=True, slots=True)
class _InlineSuggestion:
    start_line: int
    end_line: int
    replacement_code: str
    explanation: str | None = None


_LF_LINE_RE: Final[re.Pattern[str]] = re.compile(r"[^\n]*\n|[^\n]+")
_FENCE_OPEN_RE: Final[re.Pattern[str]] = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE_RE: Final[re.Pattern[str]] = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")
_SUGGESTION_INFO_RE: Final[re.Pattern[str]] = re.compile(r"^suggestion(?:\s+.*)?$", re.IGNORECASE)
_FALLBACK_STRIPPED_BODY: Final[str] = (
    "*(Code suggestion omitted because syntax or diff-hunk validation failed.)*"
)

logger = structlog.get_logger("codesight.ast_engine.suggestion_validator")


@dataclass(frozen=True, slots=True)
class SuggestionValidationResult:
    """Outcome of validating a CodeSuggestion against RIGHT-side hunks and full-file AST."""

    is_safe: bool
    reason: str | None
    suggestion: Any
    preserved_explanation: str | None
    syntax_error_count: int = 0


@dataclass(frozen=True, slots=True)
class _FenceBlock:
    open_line_idx: int
    close_line_idx: int
    is_closed: bool
    is_suggestion: bool
    inner_code: str


def _decode_source_text(source: str | bytes) -> str | None:
    if isinstance(source, str):
        return source
    try:
        return source.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _detect_eol(lines: list[str], start_line: int, end_line: int) -> str:
    for ln in lines[start_line - 1 : end_line]:
        if ln.endswith("\r\n"):
            return "\r\n"
        if ln.endswith("\n"):
            return "\n"
    for ln in lines:
        if ln.endswith("\r\n"):
            return "\r\n"
    return "\n"


def _split_lines_keepends_lf(text: str) -> list[str]:
    return _LF_LINE_RE.findall(text)


def splice_source_lines(
    full_source: str | bytes,
    start_line: int,
    end_line: int,
    replacement_code: str,
) -> bytes | None:
    """Splice `replacement_code` over 1-based `[start_line, end_line]` in RAM preserving EOLs."""
    text = _decode_source_text(full_source)
    if text is None:
        return None
    lines = _split_lines_keepends_lf(text)
    if not lines or start_line < 1 or end_line < start_line or end_line > len(lines):
        return None

    eol = _detect_eol(lines, start_line, end_line)
    normalized_rep = replacement_code.replace("\r\n", "\n").replace("\r", "\n").removesuffix("\n")
    rep_lines = normalized_rep.split("\n")
    replaced_has_trailing_eol = end_line < len(lines) or lines[end_line - 1].endswith(("\n", "\r"))

    formatted_rep: list[str] = []
    last_idx = len(rep_lines) - 1
    for idx, line in enumerate(rep_lines):
        if idx < last_idx or replaced_has_trailing_eol:
            formatted_rep.append(f"{line}{eol}")
        else:
            formatted_rep.append(line)

    patched = "".join([*lines[: start_line - 1], *formatted_rep, *lines[end_line:]])
    return to_utf8_bytes(patched)


def _validate_hunk_coords(
    *,
    start_line: int,
    end_line: int,
    parsed_file: ParsedFileDiff | None,
    right_line_hunks: Mapping[int, int] | None,
) -> str | None:
    if start_line < 1 or end_line < start_line:
        return "invalid_range"

    if parsed_file is not None:
        if parsed_file.change_type == "deleted":
            return "deleted_file"
        hunk_map: Mapping[int, int] = parsed_file.right_line_to_hunk
    elif right_line_hunks is not None:
        hunk_map = right_line_hunks
    else:
        return None

    start_hunk = hunk_map.get(start_line)
    if start_hunk is None:
        return "outside_diff_hunk"
    if start_line == end_line:
        return None

    end_hunk = hunk_map.get(end_line)
    if end_hunk is None:
        return "outside_diff_hunk"
    if end_hunk != start_hunk:
        return "cross_hunk_range"
    for line_no in range(start_line + 1, end_line):
        hunk_id = hunk_map.get(line_no)
        if hunk_id is None:
            return "outside_diff_hunk"
        if hunk_id != start_hunk:
            return "cross_hunk_range"
    return None


def _scan_fenced_blocks(body: str) -> list[_FenceBlock]:
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[_FenceBlock] = []
    open_idx: int | None = None
    open_char = ""
    open_len = 0
    open_is_suggestion = False

    for idx, line in enumerate(lines):
        if open_idx is None:
            match = _FENCE_OPEN_RE.match(line)
            if match is None:
                continue
            marker = match.group(2)
            info = match.group(3).strip()
            # CommonMark §4.5: backtick info strings cannot contain backticks.
            if marker[0] == "`" and "`" in info:
                continue
            open_idx = idx
            open_char = marker[0]
            open_len = len(marker)
            open_is_suggestion = bool(_SUGGESTION_INFO_RE.match(info))
            continue

        close_match = _FENCE_CLOSE_RE.match(line)
        if (
            close_match is not None
            and close_match.group(1)[0] == open_char
            and len(close_match.group(1)) >= open_len
        ):
            inner = "\n".join(lines[open_idx + 1 : idx])
            blocks.append(
                _FenceBlock(
                    open_line_idx=open_idx,
                    close_line_idx=idx,
                    is_closed=True,
                    is_suggestion=open_is_suggestion,
                    inner_code=inner,
                )
            )
            open_idx = None

    if open_idx is not None and open_is_suggestion:
        blocks.append(
            _FenceBlock(
                open_line_idx=open_idx,
                close_line_idx=len(lines) - 1,
                is_closed=False,
                is_suggestion=True,
                inner_code="\n".join(lines[open_idx + 1 :]),
            )
        )
    return blocks


def _strip_fence_blocks_from_body(body: str, blocks_to_strip: list[_FenceBlock]) -> str:
    lines = body.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    remove_indices: set[int] = set()
    for blk in blocks_to_strip:
        remove_indices.update(range(blk.open_line_idx, blk.close_line_idx + 1))

    kept = [line for idx, line in enumerate(lines) if idx not in remove_indices]
    collapsed = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    return collapsed if collapsed else _FALLBACK_STRIPPED_BODY


def _has_python_syntax_or_indent_error(patched_bytes: bytes) -> bool:
    try:
        ast.parse(patched_bytes)
    except (SyntaxError, ValueError):
        return True
    return False


class TreeSitterSuggestionValidator:
    """Validate CodeSuggestions in RAM by splicing into `after_source` and running tree-sitter."""

    def __init__(self, *, registry: ASTParserRegistry | None = None) -> None:
        self._registry = registry if registry is not None else ASTParserRegistry()

    def is_suggestion_syntax_safe(
        self,
        full_source: str | bytes,
        start_line: int,
        end_line: int,
        replacement_code: str,
        file_path: str,
    ) -> bool:
        """Splice `replacement_code` into `full_source` in RAM and verify zero AST errors."""
        if (
            isinstance(start_line, bool)
            or not isinstance(start_line, int)
            or isinstance(end_line, bool)
            or not isinstance(end_line, int)
        ):
            return False
        return self.validate_suggestion(
            file_path,
            _InlineSuggestion(
                start_line=start_line,
                end_line=end_line,
                replacement_code=replacement_code,
                explanation=None,
            ),
            after_source=full_source,
        ).is_safe

    def validate_suggestion(
        self,
        file_path: str,
        suggestion: SuggestionInput,
        *,
        after_source: str | bytes | None,
        right_line_hunks: Mapping[int, int] | None = None,
        parsed_file: ParsedFileDiff | None = None,
    ) -> SuggestionValidationResult:
        """Verify RIGHT-side hunk coords and full-buffer AST syntax safety; strip if unsafe."""
        norm_path = normalize_diff_path(file_path).rstrip("/") or file_path
        preserved_expl = suggestion.explanation

        hunk_err = _validate_hunk_coords(
            start_line=suggestion.start_line,
            end_line=suggestion.end_line,
            parsed_file=parsed_file,
            right_line_hunks=right_line_hunks,
        )
        if hunk_err is not None:
            logger.info(
                "suggestion_validation_failed",
                file_path=norm_path,
                reason=hunk_err,
                start_line=suggestion.start_line,
                end_line=suggestion.end_line,
            )
            return SuggestionValidationResult(
                is_safe=False,
                reason=hunk_err,
                suggestion=None,
                preserved_explanation=preserved_expl,
            )

        if not suggestion.replacement_code.strip():
            logger.info(
                "suggestion_validation_failed",
                file_path=norm_path,
                reason="empty_suggestion",
                start_line=suggestion.start_line,
                end_line=suggestion.end_line,
            )
            return SuggestionValidationResult(
                is_safe=False,
                reason="empty_suggestion",
                suggestion=None,
                preserved_explanation=preserved_expl,
            )

        language = self._registry.resolve_language(norm_path)
        ts_lang = self._registry.get_tree_sitter_language(norm_path)
        if ts_lang is None or language is None:
            if after_source is not None:
                text = _decode_source_text(after_source)
                if text is None or suggestion.end_line > len(_split_lines_keepends_lf(text)):
                    logger.info(
                        "suggestion_validation_failed",
                        file_path=norm_path,
                        reason="line_out_of_bounds",
                        start_line=suggestion.start_line,
                        end_line=suggestion.end_line,
                    )
                    return SuggestionValidationResult(
                        is_safe=False,
                        reason="line_out_of_bounds",
                        suggestion=None,
                        preserved_explanation=preserved_expl,
                    )
            return SuggestionValidationResult(
                is_safe=True,
                reason=None,
                suggestion=suggestion,
                preserved_explanation=preserved_expl,
            )

        if after_source is None:
            logger.info(
                "suggestion_validation_failed",
                file_path=norm_path,
                reason="missing_after_source",
                start_line=suggestion.start_line,
                end_line=suggestion.end_line,
            )
            return SuggestionValidationResult(
                is_safe=False,
                reason="missing_after_source",
                suggestion=None,
                preserved_explanation=preserved_expl,
            )

        patched_bytes = splice_source_lines(
            after_source,
            suggestion.start_line,
            suggestion.end_line,
            suggestion.replacement_code,
        )
        if patched_bytes is None:
            logger.info(
                "suggestion_validation_failed",
                file_path=norm_path,
                reason="line_out_of_bounds",
                start_line=suggestion.start_line,
                end_line=suggestion.end_line,
            )
            return SuggestionValidationResult(
                is_safe=False,
                reason="line_out_of_bounds",
                suggestion=None,
                preserved_explanation=preserved_expl,
            )

        tree = Parser(ts_lang).parse(patched_bytes)
        py_invalid = language == "python" and _has_python_syntax_or_indent_error(patched_bytes)
        if tree.root_node.has_error or py_invalid:
            err_count = max(1, count_syntax_error_nodes(tree.root_node))
            logger.info(
                "suggestion_validation_failed",
                file_path=norm_path,
                reason="syntax_error",
                start_line=suggestion.start_line,
                end_line=suggestion.end_line,
                syntax_error_count=err_count,
            )
            return SuggestionValidationResult(
                is_safe=False,
                reason="syntax_error",
                suggestion=None,
                preserved_explanation=preserved_expl,
                syntax_error_count=err_count,
            )

        return SuggestionValidationResult(
            is_safe=True,
            reason=None,
            suggestion=suggestion,
            preserved_explanation=preserved_expl,
        )

    def sanitize_markdown_comment(
        self,
        file_path: str,
        body: str,
        *,
        start_line: int | None,
        line: int | None,
        after_source: str | bytes | None,
        right_line_hunks: Mapping[int, int] | None = None,
        parsed_file: ParsedFileDiff | None = None,
    ) -> tuple[str, bool, str | None]:
        """Strip unsafe `suggestion` fence blocks from Markdown body while preserving prose."""
        suggestion_blocks = [blk for blk in _scan_fenced_blocks(body) if blk.is_suggestion]
        if not suggestion_blocks:
            return body, False, None

        if len(suggestion_blocks) > 1:
            return (
                _strip_fence_blocks_from_body(body, suggestion_blocks),
                True,
                "multiple_suggestion_blocks",
            )

        block = suggestion_blocks[0]
        if not block.is_closed:
            return (
                _strip_fence_blocks_from_body(body, [block]),
                True,
                "unclosed_suggestion_fence",
            )

        if line is None:
            return (
                _strip_fence_blocks_from_body(body, [block]),
                True,
                "missing_line_coordinate",
            )

        eff_start = start_line if start_line is not None else line
        if (
            isinstance(eff_start, bool)
            or not isinstance(eff_start, int)
            or isinstance(line, bool)
            or not isinstance(line, int)
            or eff_start < 1
            or eff_start > line
        ):
            return (
                _strip_fence_blocks_from_body(body, [block]),
                True,
                "invalid_range",
            )
        candidate_suggestion = _InlineSuggestion(
            start_line=eff_start,
            end_line=line,
            replacement_code=block.inner_code,
            explanation=None,
        )

        result = self.validate_suggestion(
            file_path,
            candidate_suggestion,
            after_source=after_source,
            right_line_hunks=right_line_hunks,
            parsed_file=parsed_file,
        )
        if result.is_safe:
            return body, False, None

        return _strip_fence_blocks_from_body(body, [block]), True, result.reason


def is_suggestion_syntax_safe(
    full_source: str | bytes,
    start_line: int,
    end_line: int,
    replacement_code: str,
    file_path: str,
) -> bool:
    """Convenience function validating a spliced suggestion in RAM using tree-sitter."""
    return TreeSitterSuggestionValidator().is_suggestion_syntax_safe(
        full_source,
        start_line,
        end_line,
        replacement_code,
        file_path,
    )
