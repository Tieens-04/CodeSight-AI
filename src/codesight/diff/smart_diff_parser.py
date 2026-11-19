"""Multi-file unified diff parser combining FileFilter and DiffHunkMapper."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Final

import structlog

from codesight.diff.file_filter import (
    FileFilter,
    is_binary_patch,
)
from codesight.diff.hunk_mapper import (
    DiffHunkMapper,
    classify_hunk_line,
    parse_hunk_header,
    split_diff_lines,
)
from codesight.diff.models import (
    FileChangeType,
    FilterDecision,
    FilterReason,
    ParsedFileDiff,
    ParsedPullRequestDiff,
    PatchFileEntry,
    SkippedFileRecord,
)

logger = structlog.get_logger("codesight.diff")

_BINARY_DIFF_PATHS_RE: Final[re.Pattern[str]] = re.compile(r"^Binary files (.+) and (.+) differ$")

_C_ESCAPE_BYTES: Final[dict[str, int]] = {
    "a": 0x07,
    "b": 0x08,
    "f": 0x0C,
    "n": 0x0A,
    "r": 0x0D,
    "t": 0x09,
    "v": 0x0B,
    "\\": ord("\\"),
    '"': ord('"'),
}


def _decode_c_quoted(inner: str) -> str:
    buf = bytearray()
    idx = 0
    length = len(inner)
    while idx < length:
        ch = inner[idx]
        if ch != "\\" or idx + 1 >= length:
            buf.extend(ch.encode("utf-8"))
            idx += 1
            continue

        nxt = inner[idx + 1]
        if nxt in _C_ESCAPE_BYTES:
            buf.append(_C_ESCAPE_BYTES[nxt])
            idx += 2
        elif "0" <= nxt <= "7":
            end = idx + 2
            while end < length and end < idx + 4 and "0" <= inner[end] <= "7":
                end += 1
            buf.append(int(inner[idx + 1 : end], 8) & 0xFF)
            idx = end
        else:
            buf.extend(nxt.encode("utf-8"))
            idx += 2
    return buf.decode("utf-8", errors="replace")


def _strip_git_ab_prefix(path: str) -> str:
    if path == "/dev/null":
        return path
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def unquote_git_path(raw: str, *, strip_ab_prefix: bool = True) -> str:
    """Decode Git C-quoted/octal UTF-8 paths and optionally strip a/ b/ prefixes."""
    stripped_lead = raw.lstrip()
    if stripped_lead.startswith('"'):
        end_idx = 1
        while end_idx < len(stripped_lead):
            if stripped_lead[end_idx] == "\\" and end_idx + 1 < len(stripped_lead):
                end_idx += 2
                continue
            if stripped_lead[end_idx] == '"':
                break
            end_idx += 1
        decoded = _decode_c_quoted(stripped_lead[1:end_idx])
        return _strip_git_ab_prefix(decoded) if strip_ab_prefix else decoded

    text = raw.split("\t", 1)[0].strip() if "\t" in raw else raw.strip()
    return _strip_git_ab_prefix(text) if strip_ab_prefix else text


def _consume_header_token(text: str, start: int) -> tuple[str, int]:
    length = len(text)
    while start < length and text[start] == " ":
        start += 1
    if start >= length:
        return "", start

    if text[start] == '"':
        idx = start + 1
        while idx < length:
            if text[idx] == "\\" and idx + 1 < length:
                idx += 2
                continue
            if text[idx] == '"':
                idx += 1
                break
            idx += 1
        return text[start:idx], idx

    end = text.find(" ", start)
    if end == -1:
        end = length
    return text[start:end], end


def _parse_diff_git_header(raw_line: str) -> tuple[str, str]:
    rest = raw_line[len("diff --git ") :].strip()
    if '"' in rest:
        tok1, pos = _consume_header_token(rest, 0)
        tok2, _ = _consume_header_token(rest, pos)
        if tok1 and tok2:
            return unquote_git_path(tok1), unquote_git_path(tok2)

    parts = rest.split()
    if len(parts) == 2:
        return unquote_git_path(parts[0]), unquote_git_path(parts[1])

    idx = rest.find(" b/")
    while idx != -1:
        left = rest[:idx]
        right = rest[idx + 1 :]
        left_clean = unquote_git_path(left)
        right_clean = unquote_git_path(right)
        if left_clean == right_clean:
            return left_clean, right_clean
        idx = rest.find(" b/", idx + 1)

    if " b/" in rest:
        left, right = rest.split(" b/", 1)
        return unquote_git_path(left), unquote_git_path(f"b/{right}")

    cleaned = unquote_git_path(rest)
    return cleaned, cleaned


def _extract_binary_diff_paths(raw_line: str) -> tuple[str | None, str | None] | None:
    match = _BINARY_DIFF_PATHS_RE.match(raw_line)
    if match is None:
        return None
    left_raw = unquote_git_path(match.group(1))
    right_raw = unquote_git_path(match.group(2))
    old_p = None if left_raw == "/dev/null" else left_raw
    new_p = None if right_raw == "/dev/null" else right_raw
    return old_p, new_p


@dataclass(slots=True)
class _RawFileBlock:
    has_git_header: bool
    old_path: str | None = None
    new_path: str | None = None
    change_type: FileChangeType = "modified"
    is_binary: bool = False
    seen_hunk: bool = False
    raw_lines: list[str] = field(default_factory=list)
    hunk_lines: list[str] = field(default_factory=list)

    @property
    def effective_path(self) -> str:
        if self.new_path is not None:
            return self.new_path
        return self.old_path if self.old_path is not None else "<unknown>"


def _split_raw_diff_blocks(raw_diff: str) -> list[_RawFileBlock]:
    blocks: list[_RawFileBlock] = []
    current: _RawFileBlock | None = None
    in_hunk = False
    in_trailer = False
    old_remaining = 0
    new_remaining = 0

    for raw_line in split_diff_lines(raw_diff):
        if raw_line.startswith("diff --git "):
            old_p, new_p = _parse_diff_git_header(raw_line)
            current = _RawFileBlock(
                has_git_header=True,
                old_path=old_p,
                new_path=new_p,
            )
            current.raw_lines.append(raw_line)
            blocks.append(current)
            in_hunk = False
            in_trailer = False
            old_remaining = 0
            new_remaining = 0
            continue

        if current is not None and in_hunk:
            classified = classify_hunk_line(raw_line, old_remaining, new_remaining)
            if classified is not None:
                _, old_remaining, new_remaining = classified
                current.raw_lines.append(raw_line)
                current.hunk_lines.append(raw_line)
                continue
            in_hunk = False

        if raw_line == "-- ":
            in_trailer = True
            continue
        if in_trailer:
            continue

        if current is None or not current.has_git_header:
            bin_paths = _extract_binary_diff_paths(raw_line)
            if bin_paths is not None:
                old_b, new_b = bin_paths
                b_change: FileChangeType = (
                    "added" if old_b is None else ("deleted" if new_b is None else "modified")
                )
                current = _RawFileBlock(
                    has_git_header=False,
                    old_path=old_b,
                    new_path=new_b,
                    change_type=b_change,
                    is_binary=True,
                    seen_hunk=True,
                    raw_lines=[raw_line],
                )
                blocks.append(current)
                continue

        parsed_header = parse_hunk_header(raw_line)
        if current is None:
            if parsed_header is None and not raw_line.startswith(
                ("--- ", "+++ ", "GIT binary patch")
            ):
                continue
            current = _RawFileBlock(has_git_header=False)
            blocks.append(current)
        elif (
            not current.has_git_header
            and (current.seen_hunk or current.is_binary)
            and raw_line.startswith("--- ")
        ):
            current = _RawFileBlock(has_git_header=False)
            blocks.append(current)

        if parsed_header is not None:
            _, old_remaining, _, new_remaining, _ = parsed_header
            current.seen_hunk = True
            in_hunk = True
            current.raw_lines.append(raw_line)
            current.hunk_lines.append(raw_line)
            continue

        if current.seen_hunk or (not current.has_git_header and current.is_binary):
            # Post-hunk / post-classic-binary trailer (or `diff -ruN ...` preamble) — ignore.
            continue

        current.raw_lines.append(raw_line)
        if raw_line.startswith("new file mode "):
            current.change_type = "added"
            current.old_path = None
        elif raw_line.startswith("deleted file mode "):
            current.change_type = "deleted"
            current.new_path = None
        elif raw_line.startswith("rename from "):
            current.change_type = "renamed"
            current.old_path = unquote_git_path(
                raw_line[len("rename from ") :],
                strip_ab_prefix=False,
            )
        elif raw_line.startswith("rename to "):
            current.change_type = "renamed"
            current.new_path = unquote_git_path(
                raw_line[len("rename to ") :],
                strip_ab_prefix=False,
            )
        elif raw_line.startswith("copy from "):
            current.change_type = "copied"
            current.old_path = unquote_git_path(
                raw_line[len("copy from ") :],
                strip_ab_prefix=False,
            )
        elif raw_line.startswith("copy to "):
            current.change_type = "copied"
            current.new_path = unquote_git_path(
                raw_line[len("copy to ") :],
                strip_ab_prefix=False,
            )
        elif raw_line.startswith("--- "):
            path = unquote_git_path(raw_line[4:])
            if path == "/dev/null":
                current.old_path = None
                current.change_type = "added"
            else:
                current.old_path = path
        elif raw_line.startswith("+++ "):
            path = unquote_git_path(raw_line[4:])
            if path == "/dev/null":
                current.new_path = None
                current.change_type = "deleted"
            else:
                current.new_path = path
        elif is_binary_patch(raw_line):
            current.is_binary = True

    return [b for b in blocks if b.has_git_header or b.seen_hunk or b.is_binary]


def _map_github_status(
    status: str,
    *,
    filename: str,
    previous_filename: str | None,
) -> tuple[FileChangeType, str | None, str | None]:
    normalized_status = status.strip().lower()
    if normalized_status == "added":
        return "added", None, filename
    if normalized_status in {"removed", "deleted"}:
        return "deleted", filename, None
    if normalized_status == "renamed":
        return "renamed", previous_filename, filename
    if normalized_status == "copied":
        return "copied", previous_filename, filename
    return "modified", filename, filename


class SmartDiffParser:
    """Filter PR noise files and map unified diffs into line-annotated LLM context."""

    def __init__(
        self,
        *,
        file_filter: FileFilter | None = None,
        hunk_mapper: DiffHunkMapper | None = None,
    ) -> None:
        self._file_filter = file_filter if file_filter is not None else FileFilter()
        self._hunk_mapper = hunk_mapper if hunk_mapper is not None else DiffHunkMapper()

    def parse_diff(self, raw_diff: str) -> ParsedPullRequestDiff:
        """Parse a full multi-file unified `git diff` string on RAM."""
        blocks = _split_raw_diff_blocks(raw_diff)
        kept_files: list[ParsedFileDiff] = []
        kept_raw_patches: list[str] = []
        skipped_records: list[SkippedFileRecord] = []

        for block in blocks:
            raw_text = "\n".join(block.raw_lines) + ("\n" if block.raw_lines else "")
            effective_path = block.effective_path
            decision = self._file_filter.evaluate(
                effective_path,
                raw_text,
                is_binary=block.is_binary,
            )
            if decision.should_skip:
                skipped_records.append(
                    self._file_filter.build_skipped_record(effective_path, decision, raw_text)
                )
                continue

            parsed_file = self._hunk_mapper.map_hunks(
                block.hunk_lines,
                file_path=effective_path,
                old_path=block.old_path,
                new_path=block.new_path,
                change_type=block.change_type,
            )
            kept_files.append(parsed_file)
            kept_raw_patches.append(raw_text)

        return self._finalize_result(kept_files, kept_raw_patches, skipped_records)

    def parse_patch_entries(
        self,
        entries: Sequence[PatchFileEntry],
    ) -> ParsedPullRequestDiff:
        """Parse GitHub REST `/pulls/{number}/files` entries into a ParsedPullRequestDiff."""
        kept_files: list[ParsedFileDiff] = []
        kept_raw_patches: list[str] = []
        skipped_records: list[SkippedFileRecord] = []

        for entry in entries:
            effective_path = entry.filename
            prev_path = entry.previous_filename
            change_type, old_p, new_p = _map_github_status(
                entry.status,
                filename=effective_path,
                previous_filename=prev_path,
            )
            patch_text = entry.patch or ""
            decision = self._file_filter.evaluate(
                effective_path,
                patch_text,
                is_binary=entry.is_binary,
            )
            if (
                not decision.should_skip
                and entry.patch is None
                and change_type not in {"renamed", "copied", "deleted"}
                and entry.status.strip().lower() != "unchanged"
            ):
                if entry.changes is not None and entry.changes > 0 and not entry.is_binary:
                    decision = FilterDecision(
                        should_skip=True,
                        reason=FilterReason.PATCH_TRUNCATED,
                        matched_pattern="<patch_truncated>",
                    )
                else:
                    decision = FilterDecision(
                        should_skip=True,
                        reason=FilterReason.BINARY,
                        matched_pattern="<binary>",
                    )
            if decision.should_skip:
                skipped_records.append(
                    self._file_filter.build_skipped_record(
                        effective_path,
                        decision,
                        patch_text,
                    )
                )
                continue

            parsed_file = self._hunk_mapper.map_hunks(
                split_diff_lines(patch_text),
                file_path=effective_path,
                old_path=old_p,
                new_path=new_p,
                change_type=change_type,
                blob_sha=entry.blob_sha,
            )
            kept_files.append(parsed_file)
            kept_raw_patches.append(patch_text)

        return self._finalize_result(kept_files, kept_raw_patches, skipped_records)

    def _finalize_result(
        self,
        kept_files: list[ParsedFileDiff],
        kept_raw_patches: list[str],
        skipped_records: list[SkippedFileRecord],
    ) -> ParsedPullRequestDiff:
        metrics = self._file_filter.compute_metrics(
            kept_patches=kept_raw_patches,
            skipped_records=skipped_records,
        )
        annotated_diff = "\n\n".join(f.annotated_diff for f in kept_files if f.annotated_diff)
        logger.debug(
            "diff_parsed",
            total_files=metrics.total_files,
            kept_files=metrics.kept_files,
            skipped_files=metrics.skipped_files,
            saved_estimated_tokens=metrics.saved_estimated_tokens,
            tokens_saved_pct=metrics.tokens_saved_pct,
        )
        return ParsedPullRequestDiff(
            files=tuple(kept_files),
            skipped_files=tuple(skipped_records),
            metrics=metrics,
            annotated_diff=annotated_diff,
        )
