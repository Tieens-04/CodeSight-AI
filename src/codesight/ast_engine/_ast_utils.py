"""Shared language-agnostic tree-sitter byte, span, header, and routing utilities."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from pathlib import Path
from typing import Final

from tree_sitter import Node

from codesight.ast_engine.models import ASTLanguage
from codesight.diff.file_filter import normalize_diff_path

DEFAULT_MAX_CHUNK_LINES: Final[int] = 150
MIN_CHUNK_LINES: Final[int] = 10
MAX_SPLIT_DEPTH: Final[int] = 32

_AMBIENT_DECLARATION_SUFFIXES: Final[tuple[str, ...]] = (".d.ts", ".d.mts", ".d.cts", ".pyi")
_TS_EXTENSIONS: Final[frozenset[str]] = frozenset({".ts", ".mts", ".cts"})


def is_ambient_declaration_path(file_path: str) -> bool:
    """Return True if `file_path` is a type stub (`.d.ts`, `.d.mts`, `.d.cts`, `.pyi`)."""
    norm = normalize_diff_path(file_path).rstrip("/").lower()
    return bool(norm) and norm.endswith(_AMBIENT_DECLARATION_SUFFIXES)


def resolve_ast_language(file_path: str) -> ASTLanguage | None:
    """Resolve supported AST language from file path, excluding ambient declaration stubs."""
    norm = normalize_diff_path(file_path).rstrip("/").lower()
    if not norm or norm.endswith(_AMBIENT_DECLARATION_SUFFIXES):
        return None
    suffix = Path(norm).suffix
    if suffix == ".py":
        return "python"
    if suffix in _TS_EXTENSIONS:
        return "typescript"
    if suffix == ".tsx":
        return "tsx"
    return None


def span_touches_sorted_lines(
    start_line: int,
    end_line: int,
    sorted_lines: Sequence[int],
) -> bool:
    """Return True if `[start_line, end_line]` intersects `sorted_lines`."""
    idx = bisect_left(sorted_lines, start_line)
    return idx < len(sorted_lines) and sorted_lines[idx] <= end_line


def span_touches_deletion_anchors(
    start_line: int,
    end_line: int,
    deletion_anchors: Sequence[tuple[int | None, int | None]],
) -> bool:
    """Return True if `[start_line, end_line]` encloses a pure-deletion anchor `(prev, next)`."""
    for prev_right, next_right in deletion_anchors:
        if prev_right is not None and next_right is not None:
            if start_line <= prev_right and next_right <= end_line:
                return True
        elif prev_right is not None:
            if start_line <= prev_right <= end_line:
                return True
        elif next_right is not None:
            if start_line <= next_right <= end_line:
                return True
    return False


def to_utf8_bytes(source: str | bytes) -> bytes:
    """Convert source text or bytes into UTF-8 bytes purely in RAM."""
    if isinstance(source, bytes):
        return source
    return source.encode("utf-8")


def node_text(node: Node, source_bytes: bytes) -> str:
    """Decode a tree-sitter AST node span directly from UTF-8 byte offsets."""
    return source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def node_line_span(node: Node, source_bytes: bytes) -> tuple[int, int]:
    """Return 1-based inclusive `(start_line, end_line)` for an AST node."""
    start_line = node.start_point[0] + 1
    end_row, end_col = node.end_point
    if (
        end_col == 0
        and end_row > node.start_point[0]
        and node.end_byte > node.start_byte
        and source_bytes[node.end_byte - 1] == 0x0A
    ):
        end_line = end_row
    else:
        end_line = end_row + 1
    return start_line, max(start_line, end_line)


def append_unique(target: list[str], seen: set[str], value: str) -> None:
    """Append `value` to `target` if non-empty and not previously recorded."""
    if value and value not in seen:
        seen.add(value)
        target.append(value)


def strip_comments_in_span(
    root_node: Node,
    start_byte: int,
    end_byte: int,
    source_bytes: bytes,
    *,
    skip_node: Node | None = None,
) -> str:
    """Extract UTF-8 text in `[start_byte, end_byte)` with all AST `comment` nodes stripped."""
    if end_byte <= start_byte:
        return ""

    comment_spans: list[tuple[int, int]] = []
    stack: list[Node] = [
        child
        for child in reversed(root_node.children)
        if child is not skip_node and child.start_byte < end_byte and child.end_byte > start_byte
    ]
    while stack:
        current = stack.pop()
        if current.type == "comment":
            c_start = max(current.start_byte, start_byte)
            c_end = min(current.end_byte, end_byte)
            if c_end > c_start:
                comment_spans.append((c_start, c_end))
            continue
        for child in reversed(current.children):
            if (
                child is not skip_node
                and child.start_byte < end_byte
                and child.end_byte > start_byte
            ):
                stack.append(child)

    if not comment_spans:
        return source_bytes[start_byte:end_byte].decode("utf-8", errors="replace")

    comment_spans.sort()
    parts: list[bytes] = []
    cursor = start_byte
    for c_start, c_end in comment_spans:
        if c_start > cursor:
            parts.append(source_bytes[cursor:c_start])
        cursor = max(cursor, c_end)
    if cursor < end_byte:
        parts.append(source_bytes[cursor:end_byte])
    return b"".join(parts).decode("utf-8", errors="replace")


def _collect_child_blocks(
    compound_node: Node,
    *,
    block_type: str,
    compound_splittable_types: AbstractSet[str],
) -> list[Node]:
    blocks: list[Node] = []
    stack: list[Node] = [compound_node]
    while stack:
        current = stack.pop()
        for child in reversed(current.children):
            if child.type == block_type:
                blocks.append(child)
            elif child.type in compound_splittable_types:
                stack.append(child)
    return blocks


def collect_splittable_spans(
    block_node: Node,
    source_bytes: bytes,
    max_lines: int,
    *,
    block_type: str,
    compound_splittable_types: AbstractSet[str],
    _depth: int = 0,
) -> list[tuple[int, int]]:
    """Collect statement-level `(start_line, end_line)` spans inside a function block."""
    spans: list[tuple[int, int]] = []
    for stmt in block_node.named_children:
        if stmt.type == "comment":
            continue
        s_start, s_end = node_line_span(stmt, source_bytes)
        if s_end - s_start + 1 <= max_lines:
            spans.append((s_start, s_end))
            continue

        if _depth < MAX_SPLIT_DEPTH:
            sub_blocks = _collect_child_blocks(
                stmt,
                block_type=block_type,
                compound_splittable_types=compound_splittable_types,
            )
            if sub_blocks:
                sub_spans: list[tuple[int, int]] = []
                for sub_block in sub_blocks:
                    sub_spans.extend(
                        collect_splittable_spans(
                            sub_block,
                            source_bytes,
                            max_lines,
                            block_type=block_type,
                            compound_splittable_types=compound_splittable_types,
                            _depth=_depth + 1,
                        )
                    )
                if sub_spans:
                    spans.extend(sub_spans)
                    continue

        spans.append((s_start, s_end))
    return spans


def pack_spans_into_ranges(
    spans: Sequence[tuple[int, int]],
    *,
    outer_start_line: int,
    outer_end_line: int,
    max_lines: int,
) -> list[tuple[int, int]]:
    """Partition `[outer_start_line, outer_end_line]` contiguously respecting AST spans."""
    packed: list[tuple[int, int]] = []
    chunk_start = outer_start_line
    chunk_end = outer_start_line
    has_content = False

    for s_start, s_end in spans:
        if s_end < chunk_start:
            continue
        candidate_end = max(chunk_end, s_end)
        if candidate_end - chunk_start + 1 <= max_lines:
            chunk_end = candidate_end
            has_content = True
            continue

        span_len = s_end - s_start + 1
        current_len = chunk_end - chunk_start + 1
        if has_content and (span_len <= max_lines or current_len >= MIN_CHUNK_LINES):
            packed.append((chunk_start, chunk_end))
            chunk_start = chunk_end + 1
            chunk_end = chunk_start
            has_content = False

        while s_end - chunk_start + 1 > max_lines:
            win_end = chunk_start + max_lines - 1
            packed.append((chunk_start, win_end))
            chunk_start = win_end + 1

        chunk_end = max(chunk_start, s_end)
        has_content = True

    final_end = max(chunk_end, outer_end_line)
    while final_end - chunk_start + 1 > max_lines:
        win_end = chunk_start + max_lines - 1
        packed.append((chunk_start, win_end))
        chunk_start = win_end + 1
    if chunk_start <= final_end:
        packed.append((chunk_start, final_end))
    return packed
