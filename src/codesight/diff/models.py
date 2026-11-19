"""Immutable domain models for file filtering, diff parsing, and GitHub line mapping."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

type FileChangeType = Literal["added", "modified", "deleted", "renamed", "copied"]
type DiffLineKind = Literal["context", "add", "delete", "no_newline"]


class FilterReason(StrEnum):
    """Reason classification produced by FileFilter for each PR file."""

    KEPT = "kept"
    LOCKFILE = "lockfile"
    BINARY = "binary"
    ASSET = "asset"
    MINIFIED = "minified"
    BUILD_OR_VENDOR = "build_or_vendor"
    MIGRATION = "migration"
    GENERATED = "generated"
    PATCH_TRUNCATED = "patch_truncated"
    CUSTOM_IGNORE = "custom_ignore"


@dataclass(frozen=True, slots=True)
class FilterDecision:
    """Result of evaluating a single file against exclusion rules."""

    should_skip: bool
    reason: FilterReason
    matched_pattern: str | None = None


@dataclass(frozen=True, slots=True)
class SkippedFileRecord:
    """Audit record and token-savings breakdown for an excluded PR file."""

    file_path: str
    reason: FilterReason
    matched_pattern: str | None
    raw_lines: int
    raw_bytes: int
    estimated_tokens: int


@dataclass(frozen=True, slots=True)
class FilterMetrics:
    """Aggregate KPI metrics measuring noise reduction and token savings."""

    total_files: int = 0
    kept_files: int = 0
    skipped_files: int = 0
    skipped_by_reason: dict[str, int] = field(default_factory=dict)
    raw_lines: int = 0
    kept_lines: int = 0
    skipped_lines: int = 0
    raw_bytes: int = 0
    kept_bytes: int = 0
    skipped_bytes: int = 0
    raw_estimated_tokens: int = 0
    kept_estimated_tokens: int = 0
    saved_estimated_tokens: int = 0
    tokens_saved_pct: float = 0.0

    @property
    def token_reduction_ratio(self) -> float:
        """Return token reduction as a normalized 0.0..1.0 ratio."""
        if self.raw_estimated_tokens <= 0:
            return 0.0
        return round(self.saved_estimated_tokens / self.raw_estimated_tokens, 4)


@dataclass(frozen=True, slots=True)
class DiffLine:
    """Single parsed line inside a unified diff hunk with LEFT/RIGHT coordinates."""

    kind: DiffLineKind
    content: str
    hunk_id: int
    left_line: int | None
    right_line: int | None
    llm_text: str


@dataclass(frozen=True, slots=True)
class DiffHunk:
    """Parsed unified diff hunk (`@@ -old_start,old_count +new_start,new_count @@`)."""

    hunk_id: int
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section_header: str
    lines: tuple[DiffLine, ...]
    annotated_text: str


@dataclass(frozen=True, slots=True)
class ParsedFileDiff:
    """Parsed diff and GitHub RIGHT-side commentability map for a single file."""

    file_path: str
    old_path: str | None
    new_path: str | None
    change_type: FileChangeType
    hunks: tuple[DiffHunk, ...]
    annotated_diff: str
    valid_commentable_lines: set[int] = field(default_factory=set)
    right_line_to_hunk: dict[int, int] = field(default_factory=dict)
    blob_sha: str | None = None

    @property
    def changed_right_lines(self) -> set[int]:
        """Return strictly added RIGHT lines (plus pure-deletion anchors) for AST symbol lookup."""
        if self.change_type == "deleted":
            return set()
        changed: set[int] = set()
        for hunk in self.hunks:
            for diff_line in hunk.lines:
                if diff_line.kind == "add" and diff_line.right_line is not None:
                    changed.add(diff_line.right_line)
            if hunk.new_count == 0 and hunk.new_start >= 1:
                changed.add(hunk.new_start)
        return changed

    def is_line_commentable(self, line: int) -> bool:
        """Return True if `line` is a valid RIGHT-side diff hunk line."""
        return not isinstance(line, bool) and line in self.valid_commentable_lines

    def is_range_commentable(self, start_line: int, end_line: int) -> bool:
        """Return True if `[start_line, end_line]` is valid and inside one single hunk."""
        if isinstance(start_line, bool) or isinstance(end_line, bool) or start_line >= end_line:
            return False
        start_hunk = self.right_line_to_hunk.get(start_line)
        end_hunk = self.right_line_to_hunk.get(end_line)
        return start_hunk is not None and start_hunk == end_hunk


@dataclass(frozen=True, slots=True)
class PatchFileEntry:
    """Input entry matching GitHub `GET /pulls/{number}/files` response items."""

    filename: str
    patch: str | None = None
    status: str = "modified"
    previous_filename: str | None = None
    is_binary: bool = False
    blob_sha: str | None = None
    changes: int | None = None


@dataclass(frozen=True, slots=True)
class ParsedPullRequestDiff:
    """Complete filtered PR diff bundle ready for LLM context and GitHub review guard."""

    files: tuple[ParsedFileDiff, ...]
    skipped_files: tuple[SkippedFileRecord, ...]
    metrics: FilterMetrics
    annotated_diff: str

    def get_file(self, file_path: str) -> ParsedFileDiff | None:
        """Look up a kept ParsedFileDiff by path, returning None if absent or filtered."""
        for parsed_file in self.files:
            if parsed_file.file_path == file_path:
                return parsed_file
        return None
