"""Markdown architecture document classifier and heading-aware RAM chunker (CA-14)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

import yaml

from codesight.ast_engine.models import MAX_SIGNATURE_CHARS, sanitize_signature
from codesight.config_reader.repo_config import _NoAliasSafeLoader
from codesight.diff.file_filter import CompiledGlob, compile_posix_glob, normalize_diff_path
from codesight.rag.models import (
    MAX_TRANSIENT_SNIPPET_CHARS,
    TransientEmbeddingItem,
    validate_positive_int,
)

MAX_DOC_SECTION_CHARS: Final[int] = MAX_TRANSIENT_SNIPPET_CHARS
ARCHITECTURE_DOC_BASENAMES: Final[frozenset[str]] = frozenset(
    {
        "readme.md",
        "contributing.md",
        "agents.md",
        "claude.md",
        "architecture.md",
    }
)
ARCHITECTURE_DOC_GLOBS: Final[tuple[str, ...]] = (
    "**/adr/**/*.md",
    "**/adrs/**/*.md",
    "docs/architecture/**/*.md",
    "docs/specs/**/*.md",
)

_COMPILED_ARCH_DOC_GLOBS: Final[tuple[CompiledGlob, ...]] = tuple(
    compile_posix_glob(pattern, ignore_case=True) for pattern in ARCHITECTURE_DOC_GLOBS
)
_ATX_HEADING_RE: Final[re.Pattern[str]] = re.compile(
    r"^ {0,3}(#{1,4})(?:[ \t]+(.*?))?[ \t]*#*[ \t]*$"
)
_FENCE_OPEN_RE: Final[re.Pattern[str]] = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_HORIZONTAL_RULE_RE: Final[re.Pattern[str]] = re.compile(
    r"^ {0,3}(?:-\s*-\s*-[- \t]*|\*\s*\*\s*\*[* \t]*|_\s*_\s*_[_ \t]*)$"
)
_FRONTMATTER_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_.-]+$")
MAX_FRONTMATTER_LINES: Final[int] = 80


def is_architecture_doc_path(file_path: str) -> bool:
    """Return True when `file_path` matches the curated architecture Markdown allowlist."""
    normalized = normalize_diff_path(file_path).rstrip("/")
    if not normalized or not normalized.lower().endswith(".md"):
        return False

    first_segment = normalized.split("/", 1)[0].lower()
    if first_segment == ".github":
        return False

    basename = normalized.rsplit("/", 1)[-1].lower()
    if basename in ARCHITECTURE_DOC_BASENAMES:
        return True

    return any(matcher.matches(normalized) for matcher in _COMPILED_ARCH_DOC_GLOBS)


def _format_part_title(base_title: str, part_index: int) -> str:
    suffix = f" (part {part_index})"
    max_base = max(1, MAX_SIGNATURE_CHARS - len(suffix))
    trimmed_base = sanitize_signature(base_title, max_chars=max_base)
    return f"{trimmed_base}{suffix}"


def _skip_yaml_frontmatter(lines: list[str]) -> int:
    """Return the 0-based index after validated YAML frontmatter, or 0 if absent."""
    if not lines or lines[0].rstrip(" \t") != "---":
        return 0
    upper = min(len(lines), MAX_FRONTMATTER_LINES + 1)
    for idx in range(1, upper):
        marker = lines[idx].rstrip(" \t")
        if marker in {"---", "..."}:
            block_lines = lines[1:idx]
            if any(
                _ATX_HEADING_RE.match(line) or _FENCE_OPEN_RE.match(line) for line in block_lines
            ):
                return 0
            inner = "\n".join(block_lines)
            if not inner.strip():
                return idx + 1
            try:
                parsed = yaml.load(inner, Loader=_NoAliasSafeLoader)
            except yaml.YAMLError:
                return 0
            if (
                isinstance(parsed, dict)
                and bool(parsed)
                and all(
                    isinstance(k, str) and bool(_FRONTMATTER_KEY_RE.fullmatch(k)) for k in parsed
                )
            ):
                return idx + 1
            return 0
    return 0


def _split_oversized_lines(
    raw_lines: list[tuple[int, str]],
    *,
    max_chars: int,
) -> list[tuple[int, int, str]]:
    """Split `(line_no, text)` entries into `<= max_chars` excerpts with exact line spans."""
    trimmed = _trim_blank_edges(raw_lines)
    if not trimmed:
        return []

    full_text = "\n".join(text for _, text in trimmed)
    if len(full_text) <= max_chars:
        return [(trimmed[0][0], trimmed[-1][0], full_text)]

    chunks: list[tuple[int, int, str]] = []
    bucket: list[tuple[int, str]] = []
    bucket_len = 0

    def _flush_bucket() -> None:
        nonlocal bucket, bucket_len
        cleaned = _trim_blank_edges(bucket)
        if cleaned:
            chunks.append(
                (
                    cleaned[0][0],
                    cleaned[-1][0],
                    "\n".join(line_text for _, line_text in cleaned),
                )
            )
        bucket = []
        bucket_len = 0

    for line_no, line_text in trimmed:
        if len(line_text) > max_chars:
            offset = 0
            if bucket:
                available = max_chars - bucket_len - 1
                if available >= 64:
                    prefix = line_text[:available].rstrip()
                    if prefix:
                        bucket.append((line_no, prefix))
                        offset = available
                _flush_bucket()
            while offset < len(line_text):
                piece = line_text[offset : offset + max_chars].strip()
                if piece:
                    chunks.append((line_no, line_no, piece))
                offset += max_chars
            continue

        added_len = len(line_text) if not bucket else len(line_text) + 1
        if bucket and bucket_len + added_len > max_chars:
            _flush_bucket()
            # Drop leading blank lines on the newly opened part
            if not line_text.strip():
                continue
            added_len = len(line_text)

        if not bucket and not line_text.strip():
            continue
        bucket.append((line_no, line_text))
        bucket_len += added_len

    _flush_bucket()
    return chunks


def _trim_blank_edges(lines: list[tuple[int, str]]) -> list[tuple[int, str]]:
    start = 0
    end = len(lines)
    while start < end and not lines[start][1].strip():
        start += 1
    while end > start and not lines[end - 1][1].strip():
        end -= 1
    return lines[start:end]


@dataclass(frozen=True, slots=True)
class MarkdownDocSection:
    """Transient RAM Markdown section preserving 1-based line bounds for CA-15 blob slicing."""

    file_path: str
    section_title: str
    heading_level: int
    start_line: int
    end_line: int
    transient_excerpt: str

    def to_transient_embedding_item(
        self,
        *,
        repo_full_name: str,
        blob_sha: str,
        repo_id: str | None = None,
        indexed_at: str | None = None,
    ) -> TransientEmbeddingItem:
        """Convert this section into an NFR-3 safe TransientEmbeddingItem."""
        return TransientEmbeddingItem.from_architecture_doc(
            repo_full_name=repo_full_name,
            blob_sha=blob_sha,
            file_path=self.file_path,
            section_title=self.section_title,
            start_line=self.start_line,
            end_line=self.end_line,
            transient_snippet=self.transient_excerpt,
            repo_id=repo_id,
            indexed_at=indexed_at,
        )


class MarkdownDocChunker:
    """Heading- and fence-aware Markdown chunker producing NFR-3 compliant sections in RAM."""

    def __init__(self, *, max_section_chars: int = MAX_DOC_SECTION_CHARS) -> None:
        self._max_section_chars = validate_positive_int(
            max_section_chars,
            name="max_section_chars",
            maximum=MAX_TRANSIENT_SNIPPET_CHARS,
        )

    def chunk_markdown(
        self,
        source: str | bytes,
        *,
        file_path: str,
    ) -> tuple[MarkdownDocSection, ...]:
        """Split a Markdown document in RAM by `#`..`####` headings while ignoring code fences."""
        norm_path = normalize_diff_path(file_path).rstrip("/") or "README.md"
        if isinstance(source, bytes):
            if b"\x00" in source:
                return ()
            text = source.decode("utf-8", errors="replace")
        else:
            if "\x00" in source:
                return ()
            text = source

        if text.startswith("\ufeff"):
            text = text[1:]

        if not text.strip():
            return ()

        raw_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        start_idx = _skip_yaml_frontmatter(raw_lines)
        if start_idx >= len(raw_lines):
            return ()

        basename = norm_path.rsplit("/", 1)[-1]
        default_title = sanitize_signature(f"Document Overview ({basename})")

        sections: list[MarkdownDocSection] = []
        heading_stack: list[tuple[int, str]] = []
        current_title = default_title
        current_level = 0
        current_lines: list[tuple[int, str]] = []
        current_has_body = False

        fence_char: str | None = None
        fence_len = 0

        def _flush_current() -> None:
            nonlocal current_lines, current_has_body
            if not current_lines or not current_has_body:
                current_lines = []
                current_has_body = False
                return
            parts = _split_oversized_lines(current_lines, max_chars=self._max_section_chars)
            if len(parts) == 1:
                s_line, e_line, excerpt = parts[0]
                sections.append(
                    MarkdownDocSection(
                        file_path=norm_path,
                        section_title=sanitize_signature(current_title),
                        heading_level=current_level,
                        start_line=s_line,
                        end_line=e_line,
                        transient_excerpt=excerpt,
                    )
                )
            else:
                for part_idx, (s_line, e_line, excerpt) in enumerate(parts, start=1):
                    sections.append(
                        MarkdownDocSection(
                            file_path=norm_path,
                            section_title=_format_part_title(current_title, part_idx),
                            heading_level=current_level,
                            start_line=s_line,
                            end_line=e_line,
                            transient_excerpt=excerpt,
                        )
                    )
            current_lines = []
            current_has_body = False

        for idx in range(start_idx, len(raw_lines)):
            line_no = idx + 1
            line = raw_lines[idx]

            if fence_char is not None:
                current_lines.append((line_no, line))
                if line.strip():
                    current_has_body = True
                stripped_left = line.lstrip(" ")
                indent = len(line) - len(stripped_left)
                closing_body = stripped_left.rstrip(" \t")
                if (
                    indent <= 3
                    and len(closing_body) >= fence_len
                    and set(closing_body) == {fence_char}
                ):
                    fence_char = None
                    fence_len = 0
                continue

            fence_match = _FENCE_OPEN_RE.match(line)
            if fence_match is not None:
                marker = fence_match.group(1)
                trailing = fence_match.group(2)
                # Backtick fences cannot contain backticks in their info string (CommonMark)
                if marker[0] == "~" or "`" not in trailing:
                    fence_char = marker[0]
                    fence_len = len(marker)
                    current_lines.append((line_no, line))
                    current_has_body = True
                    continue

            heading_match = _ATX_HEADING_RE.match(line)
            if heading_match is not None:
                hashes = heading_match.group(1)
                collapsed_heading = " ".join((heading_match.group(2) or "").split()).rstrip(": ")
                clean_heading = sanitize_signature(collapsed_heading) if collapsed_heading else ""
                if clean_heading:
                    _flush_current()
                    level = len(hashes)
                    while heading_stack and heading_stack[-1][0] >= level:
                        heading_stack.pop()
                    heading_stack.append((level, clean_heading))
                    breadcrumb = " > ".join(title for _, title in heading_stack)
                    current_title = sanitize_signature(breadcrumb)
                    current_level = level
                    current_lines = [(line_no, line)]
                    current_has_body = False
                    continue

            current_lines.append((line_no, line))
            if line.strip() and _HORIZONTAL_RULE_RE.match(line) is None:
                current_has_body = True

        _flush_current()
        return tuple(sections)
