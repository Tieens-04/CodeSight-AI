"""Unified diff parsing, noise filtering, and GitHub line coordinate mapping."""

from codesight.diff.file_filter import (
    DEFAULT_EXCLUSION_RULES,
    FileFilter,
    estimate_tokens,
    is_binary_patch,
    normalize_diff_path,
)
from codesight.diff.hunk_mapper import HUNK_HEADER_RE, DiffHunkMapper, split_diff_lines
from codesight.diff.models import (
    DiffHunk,
    DiffLine,
    DiffLineKind,
    FileChangeType,
    FilterDecision,
    FilterMetrics,
    FilterReason,
    ParsedFileDiff,
    ParsedPullRequestDiff,
    PatchFileEntry,
    SkippedFileRecord,
)
from codesight.diff.smart_diff_parser import SmartDiffParser, unquote_git_path

__all__ = [
    "DEFAULT_EXCLUSION_RULES",
    "HUNK_HEADER_RE",
    "DiffHunk",
    "DiffHunkMapper",
    "DiffLine",
    "DiffLineKind",
    "FileChangeType",
    "FileFilter",
    "FilterDecision",
    "FilterMetrics",
    "FilterReason",
    "ParsedFileDiff",
    "ParsedPullRequestDiff",
    "PatchFileEntry",
    "SkippedFileRecord",
    "SmartDiffParser",
    "estimate_tokens",
    "is_binary_patch",
    "normalize_diff_path",
    "split_diff_lines",
    "unquote_git_path",
]
