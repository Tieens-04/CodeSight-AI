"""Unified diff hunk mapper tracking LEFT/RIGHT coordinates and LLM line prefixes."""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Final

from codesight.diff.models import (
    DiffHunk,
    DiffLine,
    DiffLineKind,
    FileChangeType,
    ParsedFileDiff,
)

HUNK_HEADER_RE: Final[re.Pattern[str]] = re.compile(
    r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: ?(.*))?$"
)


def split_diff_lines(text: str) -> list[str]:
    """Split diff text strictly on LF without splitting on Unicode line separators."""
    if not text:
        return []
    lines = [line.rstrip("\r") for line in text.split("\n")]
    if lines[-1] == "":
        lines.pop()
    return lines


def parse_hunk_header(raw_line: str) -> tuple[int, int, int, int, str] | None:
    """Parse `@@ -old_start[,old_count] +new_start[,new_count] @@ [section]` header."""
    match = HUNK_HEADER_RE.match(raw_line)
    if match is None:
        return None
    old_start = int(match.group(1))
    old_count = int(match.group(2)) if match.group(2) is not None else 1
    new_start = int(match.group(3))
    new_count = int(match.group(4)) if match.group(4) is not None else 1
    section_header = (match.group(5) or "").strip()
    return old_start, old_count, new_start, new_count, section_header


def classify_hunk_line(
    raw_line: str,
    old_remaining: int,
    new_remaining: int,
) -> tuple[DiffLineKind, int, int] | None:
    """Classify a hunk body line while enforcing remaining LEFT/RIGHT line counts."""
    if raw_line.startswith("\\"):
        return "no_newline", old_remaining, new_remaining
    if raw_line.startswith("+") and new_remaining > 0:
        return "add", old_remaining, new_remaining - 1
    if raw_line.startswith("-") and old_remaining > 0:
        return "delete", old_remaining - 1, new_remaining
    if (raw_line.startswith(" ") or raw_line == "") and old_remaining > 0 and new_remaining > 0:
        return "context", old_remaining - 1, new_remaining - 1
    return None


type _PendingLine = tuple[DiffLineKind, str, int | None, int | None]
type _PendingHunk = tuple[int, int, int, int, int, str, str, list[_PendingLine]]


class DiffHunkMapper:
    """Map unified diff hunks into explicit `L<num> |` LLM lines and RIGHT coordinates."""

    def map_hunks(
        self,
        hunk_lines: Sequence[str],
        *,
        file_path: str,
        old_path: str | None = None,
        new_path: str | None = None,
        change_type: FileChangeType = "modified",
    ) -> ParsedFileDiff:
        """Parse hunk lines and build GitHub RIGHT-side commentability metadata."""
        is_deleted_file = change_type == "deleted"
        pending_hunks: list[_PendingHunk] = []
        current_entries: list[_PendingLine] | None = None
        in_hunk = False
        hunk_id = 0
        left_cursor = 0
        right_cursor = 0
        old_remaining = 0
        new_remaining = 0
        max_right_line = 0
        valid_commentable_lines: set[int] = set()
        right_line_to_hunk: dict[int, int] = {}

        for raw_line in hunk_lines:
            parsed_header = parse_hunk_header(raw_line)
            if parsed_header is not None:
                old_start, old_count, new_start, new_count, section_header = parsed_header
                hunk_id += 1
                left_cursor = old_start
                right_cursor = new_start
                old_remaining = old_count
                new_remaining = new_count
                in_hunk = True
                current_entries = []
                pending_hunks.append(
                    (
                        hunk_id,
                        old_start,
                        old_count,
                        new_start,
                        new_count,
                        section_header,
                        raw_line,
                        current_entries,
                    )
                )
                continue

            if not in_hunk or current_entries is None:
                continue

            classified = classify_hunk_line(raw_line, old_remaining, new_remaining)
            if classified is None:
                in_hunk = False
                continue

            kind, old_remaining, new_remaining = classified
            if kind == "no_newline":
                current_entries.append(("no_newline", raw_line[1:].lstrip(), None, None))
            elif kind == "add":
                r_line = None if is_deleted_file else right_cursor
                if not is_deleted_file:
                    valid_commentable_lines.add(right_cursor)
                    right_line_to_hunk[right_cursor] = hunk_id
                    if right_cursor > max_right_line:
                        max_right_line = right_cursor
                    right_cursor += 1
                current_entries.append(("add", raw_line[1:], None, r_line))
            elif kind == "delete":
                current_entries.append(("delete", raw_line[1:], left_cursor, None))
                left_cursor += 1
            else:
                r_line = None if is_deleted_file else right_cursor
                if not is_deleted_file:
                    valid_commentable_lines.add(right_cursor)
                    right_line_to_hunk[right_cursor] = hunk_id
                    if right_cursor > max_right_line:
                        max_right_line = right_cursor
                    right_cursor += 1
                content = raw_line[1:] if raw_line.startswith(" ") else ""
                current_entries.append(("context", content, left_cursor, r_line))
                left_cursor += 1

        num_width = max(3, len(str(max_right_line)))
        blank_label = " " * (1 + num_width)
        formatted_hunks: list[DiffHunk] = []

        for (
            h_id,
            old_start,
            old_count,
            new_start,
            new_count,
            section_header,
            raw_header_line,
            entries,
        ) in pending_hunks:
            formatted_lines: list[DiffLine] = []
            text_lines: list[str] = [raw_header_line]
            for kind, content, l_line, r_line in entries:
                if kind == "context" and r_line is not None:
                    llm_text = f"L{r_line:0{num_width}d} |   {content}"
                elif kind == "add" and r_line is not None:
                    llm_text = f"L{r_line:0{num_width}d} | + {content}"
                elif kind == "no_newline":
                    llm_text = f"{blank_label} | \\ {content}"
                else:
                    llm_text = f"{blank_label} | - {content}"

                text_lines.append(llm_text)
                formatted_lines.append(
                    DiffLine(
                        kind=kind,
                        content=content,
                        hunk_id=h_id,
                        left_line=l_line,
                        right_line=r_line,
                        llm_text=llm_text,
                    )
                )

            formatted_hunks.append(
                DiffHunk(
                    hunk_id=h_id,
                    old_start=old_start,
                    old_count=old_count,
                    new_start=new_start,
                    new_count=new_count,
                    section_header=section_header,
                    lines=tuple(formatted_lines),
                    annotated_text="\n".join(text_lines),
                )
            )

        file_header = f"=== FILE: {file_path} ({change_type}) ==="
        if formatted_hunks:
            hunk_body = "\n".join(h.annotated_text for h in formatted_hunks)
            annotated_diff = f"{file_header}\n{hunk_body}"
        else:
            annotated_diff = file_header

        return ParsedFileDiff(
            file_path=file_path,
            old_path=old_path,
            new_path=new_path,
            change_type=change_type,
            hunks=tuple(formatted_hunks),
            annotated_diff=annotated_diff,
            valid_commentable_lines=valid_commentable_lines,
            right_line_to_hunk=right_line_to_hunk,
        )
