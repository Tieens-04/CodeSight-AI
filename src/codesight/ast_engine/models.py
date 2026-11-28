"""Immutable AST domain models and NFR-3 Zero-Code Persistence payload guards."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final, Literal

from codesight.diff.file_filter import normalize_diff_path

type ASTLanguage = Literal["python", "typescript", "tsx"]
type SymbolKind = Literal["function", "method", "class", "interface", "type_alias", "section"]
type ComplexityChangeKind = Literal["added", "modified", "removed"]
type FallbackReason = Literal[
    "unsupported_extension",
    "missing_source",
    "deleted_file",
    "parse_failure",
]

MAX_SIGNATURE_CHARS: Final[int] = 300


def sanitize_signature(raw_header: str, *, max_chars: int = MAX_SIGNATURE_CHARS) -> str:
    """Collapse a symbol header into a single-line signature capped at `max_chars`."""
    collapsed = " ".join(raw_header.split())
    if collapsed.endswith(":"):
        collapsed = collapsed[:-1].rstrip()
    if max_chars <= 0:
        return ""
    if len(collapsed) <= max_chars:
        return collapsed
    if max_chars <= 3:
        return collapsed[:max_chars]
    return f"{collapsed[: max_chars - 3].rstrip()}..."


def build_deterministic_point_id(
    repo_full_name: str,
    file_path: str,
    blob_sha: str,
    symbol_name: str,
    start_line: int,
) -> str:
    """Return canonical UUIDv5 Qdrant point ID for a symbol/section at `(file_path, start_line)`."""
    if isinstance(start_line, bool) or not isinstance(start_line, int) or start_line < 1:
        raise ValueError("start_line must be an integer >= 1.")
    norm_path = normalize_diff_path(file_path).rstrip("/") or file_path
    seed = f"{repo_full_name}:{norm_path}:{blob_sha}:{symbol_name}:{start_line}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, seed))


@dataclass(frozen=True, slots=True)
class CodeSymbolChunk:
    """AST semantic unit with transient RAM metadata and NFR-3 safe Qdrant payload."""

    file_path: str
    language: ASTLanguage
    symbol_name: str
    qualified_name: str
    symbol_kind: SymbolKind
    start_line: int
    end_line: int
    signature: str
    docstring: str | None = None
    decorators: tuple[str, ...] = ()
    imports: tuple[str, ...] = ()
    called_symbols: tuple[str, ...] = ()
    cyclomatic_complexity: int = 1
    is_async: bool = False
    is_subchunk: bool = False
    subchunk_index: int | None = None

    @property
    def line_count(self) -> int:
        """Return 1-based inclusive line span covered by this chunk."""
        return max(1, self.end_line - self.start_line + 1)

    def build_point_id(self, repo: str, blob_sha: str) -> str:
        """Return deterministic UUIDv5 Qdrant point ID for this symbol and blob SHA."""
        return build_deterministic_point_id(
            repo,
            self.file_path,
            blob_sha,
            self.symbol_name,
            self.start_line,
        )

    def build_embedding_text(self) -> str:
        """Assemble transient RAM text for embedding without persisting source code."""
        norm_path = normalize_diff_path(self.file_path) or self.file_path
        lines: list[str] = [
            f"file: {norm_path}",
            f"symbol: {self.qualified_name} ({self.symbol_kind})",
        ]
        if self.decorators:
            lines.append(f"decorators: {' '.join(self.decorators)}")
        lines.append(f"signature: {self.signature}")
        if self.docstring:
            lines.append(f"docstring: {self.docstring}")
        if self.called_symbols:
            lines.append(f"calls: {', '.join(self.called_symbols)}")
        return "\n".join(lines)

    def to_qdrant_payload(
        self,
        *,
        repo_full_name: str,
        blob_sha: str,
        repo_id: str | None = None,
        indexed_at: str | None = None,
    ) -> dict[str, Any]:
        """Build an NFR-3 compliant Qdrant payload strictly within the allowed key list."""
        norm_path = normalize_diff_path(self.file_path) or self.file_path
        payload: dict[str, Any] = {
            "repo_full_name": repo_full_name,
            "file_path": norm_path,
            "blob_sha": blob_sha,
            "chunk_type": "code",
            "language": self.language,
            "symbol_name": self.symbol_name,
            "symbol_kind": self.symbol_kind,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "signature": sanitize_signature(self.signature),
            "called_symbols": list(self.called_symbols),
            "imports": list(self.imports),
            "cyclomatic_complexity": self.cyclomatic_complexity,
        }
        if repo_id is not None:
            payload["repo_id"] = repo_id
        if indexed_at is not None:
            payload["indexed_at"] = indexed_at
        return payload


@dataclass(frozen=True, slots=True)
class ParsedASTFile:
    """Complete AST extraction result for one source file in RAM."""

    file_path: str
    language: ASTLanguage
    imports: tuple[str, ...]
    chunks: tuple[CodeSymbolChunk, ...]
    has_syntax_error: bool = False


@dataclass(frozen=True, slots=True)
class FunctionComplexity:
    """Static complexity metrics computed directly from a function or method AST."""

    symbol_name: str
    qualified_name: str
    symbol_kind: Literal["function", "method"]
    start_line: int
    end_line: int
    line_count: int
    cyclomatic_complexity: int
    max_nesting_depth: int
    parameter_count: int
    is_async: bool = False
    has_syntax_error: bool = False


@dataclass(frozen=True, slots=True)
class ComplexityDelta:
    """Before/after Cyclomatic Complexity delta for one function/method in a PR."""

    symbol_name: str
    qualified_name: str
    change_kind: ComplexityChangeKind
    before_cc: int
    after_cc: int
    delta_cc: int
    before_metrics: FunctionComplexity | None = None
    after_metrics: FunctionComplexity | None = None


@dataclass(frozen=True, slots=True)
class ComplexityDiffSummary:
    """Aggregated static complexity change summary across a modified file."""

    deltas: tuple[ComplexityDelta, ...]
    net_delta_cc: int
    max_delta_cc: int
    max_after_cc: int
    high_complexity_functions: tuple[FunctionComplexity, ...] = ()
    has_syntax_error: bool = False


@dataclass(frozen=True, slots=True)
class TestCompanionReport:
    """Result of detecting added source logic lines lacking companion test changes."""

    __test__ = False

    added_source_logic_lines: int
    added_test_logic_lines: int
    threshold: int
    is_untested: bool
    source_files: tuple[str, ...] = ()
    test_files: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class HunkContextSlice:
    """Transient RAM-only diff hunk fallback slice (never persisted to Qdrant, NFR-3)."""

    file_path: str
    hunk_id: int
    start_line: int
    end_line: int
    section_header: str
    annotated_text: str


@dataclass(frozen=True, slots=True)
class PRFileContext:
    """Unified AST or Hunk-fallback context produced by ASTParserRegistry for a PR file."""

    file_path: str
    mode: Literal["ast", "hunk_fallback"]
    language: ASTLanguage | None = None
    parsed_file: ParsedASTFile | None = None
    touched_chunks: tuple[CodeSymbolChunk, ...] = ()
    removed_symbols: tuple[str, ...] = ()
    complexity: ComplexityDiffSummary | None = None
    hunk_slices: tuple[HunkContextSlice, ...] = ()
    fallback_reason: FallbackReason | None = None
