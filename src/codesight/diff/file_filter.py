"""Smart PR file filter, linear-time POSIX glob matcher, and token-efficiency calculator."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Final

import structlog

from codesight.diff.models import (
    FilterDecision,
    FilterMetrics,
    FilterReason,
    SkippedFileRecord,
)

if TYPE_CHECKING:
    from codesight.config_reader.repo_config import RepoConfig

logger = structlog.get_logger("codesight.diff")

_BINARY_FILES_DIFFER_RE: Final[re.Pattern[str]] = re.compile(
    r"^Binary files .+ and .+ differ\r?$",
    re.MULTILINE,
)
_GIT_BINARY_PATCH_RE: Final[re.Pattern[str]] = re.compile(
    r"^GIT binary patch(?:\s|$)",
    re.MULTILINE,
)

_CASE_INSENSITIVE_REASONS: Final[frozenset[FilterReason]] = frozenset(
    {
        FilterReason.ASSET,
        FilterReason.MINIFIED,
    }
)

DEFAULT_EXCLUSION_RULES: Final[tuple[tuple[FilterReason, tuple[str, ...]], ...]] = (
    (
        FilterReason.LOCKFILE,
        (
            "package-lock.json",
            "npm-shrinkwrap.json",
            "pnpm-lock.yaml",
            "yarn.lock",
            "bun.lock",
            "bun.lockb",
            "uv.lock",
            "poetry.lock",
            "pdm.lock",
            "Pipfile.lock",
            "Cargo.lock",
            "go.sum",
            "composer.lock",
            "Gemfile.lock",
            "packages.lock.json",
        ),
    ),
    (
        FilterReason.ASSET,
        (
            "*.svg",
            "*.png",
            "*.jpg",
            "*.jpeg",
            "*.gif",
            "*.webp",
            "*.ico",
            "*.bmp",
            "*.pdf",
            "*.woff",
            "*.woff2",
            "*.ttf",
            "*.eot",
            "*.otf",
            "*.mp3",
            "*.mp4",
            "*.mov",
            "*.zip",
            "*.tar",
            "*.gz",
            "*.7z",
        ),
    ),
    (
        FilterReason.MINIFIED,
        (
            "*.min.js",
            "*.min.mjs",
            "*.min.cjs",
            "*.min.css",
            "*.bundle.js",
            "*.chunk.js",
            "*.map",
        ),
    ),
    (
        FilterReason.BUILD_OR_VENDOR,
        (
            "**/dist/**",
            "**/build/**",
            "**/out/**",
            "**/.next/**",
            "**/coverage/**",
            "**/node_modules/**",
            "**/vendor/**",
            "**/.venv/**",
            "**/venv/**",
            "**/__pycache__/**",
            "*.pyc",
            "*.pyo",
            "*.so",
            "*.dll",
            "*.dylib",
            "*.exe",
            "*.whl",
        ),
    ),
    (
        FilterReason.MIGRATION,
        ("**/migrations/**",),
    ),
    (
        FilterReason.GENERATED,
        (
            "*_pb2.py",
            "*_pb2.pyi",
            "*_pb2_grpc.py",
            "*.generated.*",
            "*.g.dart",
            "*.snap",
        ),
    ),
)


def normalize_diff_path(raw_path: str) -> str:
    """Normalize a path to clean POSIX form for glob matching without leading './' or '/'."""
    normalized = raw_path.strip().replace("\\", "/")
    normalized = re.sub(r"/+", "/", normalized)
    while normalized.startswith(("./", "/")):
        if normalized.startswith("./"):
            normalized = normalized[2:]
        else:
            normalized = normalized[1:]
    return normalized


def resolve_after_source_for_path(
    after_sources_by_file: Mapping[str, str | bytes] | None,
    file_path: str,
) -> str | bytes | None:
    """Resolve an in-memory post-PR file source by exact or normalized repository path."""
    if not after_sources_by_file:
        return None
    if file_path in after_sources_by_file:
        return after_sources_by_file[file_path]
    target_norm = normalize_diff_path(file_path).rstrip("/")
    if not target_norm:
        return None
    if target_norm in after_sources_by_file:
        return after_sources_by_file[target_norm]
    for raw_path, src in after_sources_by_file.items():
        if normalize_diff_path(raw_path).rstrip("/") == target_norm:
            return src
    return None


def estimate_tokens(content: str | bytes) -> int:
    """Estimate token count deterministically offline via ceil(utf8_bytes / 4)."""
    raw_bytes = content.encode("utf-8") if isinstance(content, str) else content
    if not raw_bytes:
        return 0
    return (len(raw_bytes) + 3) // 4


def is_binary_patch(patch_text: str | None) -> bool:
    """Return True when diff/patch text exhibits binary indicators or NUL bytes."""
    if not patch_text:
        return False
    if "\x00" in patch_text:
        return True
    return bool(
        _GIT_BINARY_PATCH_RE.search(patch_text) or _BINARY_FILES_DIFFER_RE.search(patch_text)
    )


def count_text_lines(text: str) -> int:
    """Count LF/CRLF lines consistently without splitting on Unicode line separators."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


class _WildcardToken(Enum):
    STAR = "star"
    ANY = "any"


@dataclass(frozen=True, slots=True)
class _CharClass:
    chars: frozenset[str]
    ranges: tuple[tuple[int, int], ...]
    negated: bool
    ignore_case: bool = False

    def matches(self, ch: str) -> bool:
        candidates = (ch, ch.upper()) if self.ignore_case and ch != ch.upper() else (ch,)
        in_set = any(
            c in self.chars or (len(c) == 1 and any(lo <= ord(c) <= hi for lo, hi in self.ranges))
            for c in candidates
        )
        return not in_set if self.negated else in_set


type _SegmentToken = _WildcardToken | _CharClass | str


def _segment_token_matches(tok: _SegmentToken, ch: str) -> bool:
    if tok is _WildcardToken.ANY:
        return True
    if isinstance(tok, str):
        return tok == ch
    return isinstance(tok, _CharClass) and tok.matches(ch)


@dataclass(frozen=True, slots=True)
class _SegmentMatcher:
    tokens: tuple[_SegmentToken, ...]
    ignore_case: bool = False
    exact_literal: str | None = None
    suffix_literal: str | None = None

    def matches(self, raw_segment: str) -> bool:
        text = raw_segment.lower() if self.ignore_case else raw_segment
        if self.exact_literal is not None:
            return text == self.exact_literal
        if self.suffix_literal is not None:
            return text.endswith(self.suffix_literal)

        tokens = self.tokens
        t_len = len(tokens)
        s_len = len(text)
        ti = 0
        si = 0
        star_ti = -1
        star_si = 0

        while si < s_len:
            tok = tokens[ti] if ti < t_len else None
            if tok is _WildcardToken.STAR:
                star_ti = ti
                star_si = si
                ti += 1
            elif tok is not None and _segment_token_matches(tok, text[si]):
                ti += 1
                si += 1
            elif star_ti != -1:
                ti = star_ti + 1
                star_si += 1
                si = star_si
            else:
                return False

        while ti < t_len and tokens[ti] is _WildcardToken.STAR:
            ti += 1
        return ti == t_len


def _compile_segment(segment: str, *, ignore_case: bool = False) -> _SegmentMatcher:
    tokens: list[_SegmentToken] = []
    idx = 0
    length = len(segment)

    while idx < length:
        ch = segment[idx]
        if ch == "*":
            if not tokens or tokens[-1] is not _WildcardToken.STAR:
                tokens.append(_WildcardToken.STAR)
            idx += 1
        elif ch == "?":
            tokens.append(_WildcardToken.ANY)
            idx += 1
        elif ch == "[":
            close_idx = idx + 1
            negated = False
            if close_idx < length and segment[close_idx] == "!":
                negated = True
                close_idx += 1
            content_start = close_idx
            if close_idx < length and segment[close_idx] == "]":
                close_idx += 1
            while close_idx < length and segment[close_idx] != "]":
                close_idx += 1
            if close_idx >= length:
                tokens.append("[")
                idx += 1
            else:
                body = segment[content_start:close_idx]
                chars: set[str] = set()
                ranges: list[tuple[int, int]] = []
                b_idx = 0
                b_len = len(body)
                while b_idx < b_len:
                    c1 = body[b_idx]
                    if b_idx + 2 < b_len and body[b_idx + 1] == "-":
                        c2 = body[b_idx + 2]
                        if ord(c1) > ord(c2):
                            raise ValueError("Glob pattern contains an invalid character range.")
                        ranges.append((ord(c1), ord(c2)))
                        b_idx += 3
                    else:
                        chars.add(c1)
                        b_idx += 1
                tokens.append(
                    _CharClass(frozenset(chars), tuple(ranges), negated, ignore_case=ignore_case)
                )
                idx = close_idx + 1
        else:
            tokens.append(ch.lower() if ignore_case else ch)
            idx += 1

    tok_tuple = tuple(tokens)
    if all(isinstance(t, str) for t in tok_tuple):
        return _SegmentMatcher(
            tokens=tok_tuple,
            ignore_case=ignore_case,
            exact_literal="".join(t for t in tok_tuple if isinstance(t, str)),
        )
    if (
        len(tok_tuple) >= 2
        and tok_tuple[0] is _WildcardToken.STAR
        and all(isinstance(t, str) for t in tok_tuple[1:])
    ):
        return _SegmentMatcher(
            tokens=tok_tuple,
            ignore_case=ignore_case,
            suffix_literal="".join(t for t in tok_tuple[1:] if isinstance(t, str)),
        )
    return _SegmentMatcher(tokens=tok_tuple, ignore_case=ignore_case)


@dataclass(frozen=True, slots=True)
class CompiledGlob:
    """Linear-time POSIX glob matcher supporting recursive `**` without regex backtracking."""

    basename_only: bool
    segments: tuple[_SegmentMatcher | None, ...]

    def matches(self, normalized_path: str) -> bool:
        """Match a non-empty normalized POSIX path without leading or trailing slashes."""
        if self.basename_only:
            basename = normalized_path.rsplit("/", 1)[-1]
            seg = self.segments[0]
            return seg is not None and seg.matches(basename)

        parts = normalized_path.split("/")
        segs = self.segments
        p_len = len(segs)
        dp = [False] * (p_len + 1)
        dp[0] = True
        for i in range(p_len - 1):
            if dp[i] and segs[i] is None:
                dp[i + 1] = True

        for part in parts:
            next_dp = [False] * (p_len + 1)
            for i in range(p_len):
                if not dp[i]:
                    continue
                seg = segs[i]
                if seg is None:
                    next_dp[i] = True
                    next_dp[i + 1] = True
                elif seg.matches(part):
                    next_dp[i + 1] = True
            dp = next_dp
            for i in range(p_len - 1):
                if dp[i] and segs[i] is None:
                    dp[i + 1] = True

        return dp[p_len]


def compile_posix_glob(raw_pattern: str, *, ignore_case: bool = False) -> CompiledGlob:
    """Compile a POSIX glob pattern into a backtracking-free segment matcher."""
    if "\x00" in raw_pattern:
        raise ValueError("Glob pattern must not contain NUL bytes.")

    cleaned = normalize_diff_path(raw_pattern)
    if not cleaned:
        raise ValueError("Glob pattern must not be empty.")
    if cleaned.endswith("/"):
        cleaned = f"{cleaned}**"

    if "/" not in cleaned:
        if cleaned == "**":
            return CompiledGlob(basename_only=False, segments=(None,))
        return CompiledGlob(
            basename_only=True,
            segments=(_compile_segment(cleaned, ignore_case=ignore_case),),
        )

    raw_segments = [seg for seg in cleaned.split("/") if seg]
    compiled_segments: list[_SegmentMatcher | None] = []
    for seg in raw_segments:
        if seg == "**":
            if compiled_segments and compiled_segments[-1] is None:
                continue
            compiled_segments.append(None)
        else:
            compiled_segments.append(_compile_segment(seg, ignore_case=ignore_case))

    return CompiledGlob(basename_only=False, segments=tuple(compiled_segments))


@dataclass(frozen=True, slots=True)
class _CompiledRule:
    reason: FilterReason
    pattern: str
    matcher: CompiledGlob


def _compile_builtin_rules() -> tuple[_CompiledRule, ...]:
    rules: list[_CompiledRule] = []
    for reason, patterns in DEFAULT_EXCLUSION_RULES:
        ignore_case = reason in _CASE_INSENSITIVE_REASONS
        for pat in patterns:
            rules.append(
                _CompiledRule(
                    reason=reason,
                    pattern=pat,
                    matcher=compile_posix_glob(pat, ignore_case=ignore_case),
                )
            )
    return tuple(rules)


_BUILTIN_COMPILED_RULES: Final[tuple[_CompiledRule, ...]] = _compile_builtin_rules()


class FileFilter:
    """Filter non-logic PR files (lockfiles, assets, migrations, binary, .codesight.yaml globs)."""

    def __init__(self, *, ignore_paths: Sequence[str] = ()) -> None:
        custom_rules: list[_CompiledRule] = []
        for pattern in ignore_paths:
            custom_rules.append(
                _CompiledRule(
                    reason=FilterReason.CUSTOM_IGNORE,
                    pattern=pattern,
                    matcher=compile_posix_glob(pattern),
                )
            )
        self._rules: tuple[_CompiledRule, ...] = (*_BUILTIN_COMPILED_RULES, *custom_rules)

    @classmethod
    def from_repo_config(cls, repo_config: RepoConfig) -> FileFilter:
        """Build a FileFilter initialized with RepoConfig.ignore_paths, skipping corrupt globs."""
        valid_patterns: list[str] = []
        for idx, pattern in enumerate(repo_config.ignore_paths):
            try:
                compile_posix_glob(pattern)
            except ValueError:
                logger.warning(
                    "file_filter_invalid_ignore_pattern_skipped",
                    pattern_index=idx,
                )
                continue
            valid_patterns.append(pattern)
        return cls(ignore_paths=valid_patterns)

    def evaluate(
        self,
        file_path: str,
        patch_text: str | None = None,
        *,
        is_binary: bool = False,
    ) -> FilterDecision:
        """Evaluate whether a file should be excluded from LLM review."""
        if is_binary or is_binary_patch(patch_text):
            return FilterDecision(
                should_skip=True,
                reason=FilterReason.BINARY,
                matched_pattern="<binary>",
            )

        normalized = normalize_diff_path(file_path).rstrip("/")
        if not normalized:
            return FilterDecision(should_skip=False, reason=FilterReason.KEPT)

        for rule in self._rules:
            if rule.matcher.matches(normalized):
                return FilterDecision(
                    should_skip=True,
                    reason=rule.reason,
                    matched_pattern=rule.pattern,
                )

        return FilterDecision(should_skip=False, reason=FilterReason.KEPT)

    def build_skipped_record(
        self,
        file_path: str,
        decision: FilterDecision,
        patch_text: str | None = None,
    ) -> SkippedFileRecord:
        """Construct a SkippedFileRecord capturing line, byte, and token savings."""
        text = patch_text or ""
        raw_bytes = len(text.encode("utf-8"))
        return SkippedFileRecord(
            file_path=file_path,
            reason=decision.reason,
            matched_pattern=decision.matched_pattern,
            raw_lines=count_text_lines(text),
            raw_bytes=raw_bytes,
            estimated_tokens=estimate_tokens(text),
        )

    def compute_metrics(
        self,
        *,
        kept_patches: Sequence[str],
        skipped_records: Sequence[SkippedFileRecord],
    ) -> FilterMetrics:
        """Aggregate FilterMetrics across kept patches and skipped file records."""
        kept_files = len(kept_patches)
        skipped_files = len(skipped_records)
        total_files = kept_files + skipped_files

        kept_lines = sum(count_text_lines(p) for p in kept_patches)
        kept_bytes = sum(len(p.encode("utf-8")) for p in kept_patches)
        kept_tokens = sum(estimate_tokens(p) for p in kept_patches)

        skipped_lines = sum(r.raw_lines for r in skipped_records)
        skipped_bytes = sum(r.raw_bytes for r in skipped_records)
        saved_tokens = sum(r.estimated_tokens for r in skipped_records)

        by_reason: dict[str, int] = {}
        for rec in skipped_records:
            key = str(rec.reason)
            by_reason[key] = by_reason.get(key, 0) + 1

        raw_lines = kept_lines + skipped_lines
        raw_bytes = kept_bytes + skipped_bytes
        raw_tokens = kept_tokens + saved_tokens
        tokens_saved_pct = round((saved_tokens / raw_tokens) * 100.0, 2) if raw_tokens > 0 else 0.0

        return FilterMetrics(
            total_files=total_files,
            kept_files=kept_files,
            skipped_files=skipped_files,
            skipped_by_reason=by_reason,
            raw_lines=raw_lines,
            kept_lines=kept_lines,
            skipped_lines=skipped_lines,
            raw_bytes=raw_bytes,
            kept_bytes=kept_bytes,
            skipped_bytes=skipped_bytes,
            raw_estimated_tokens=raw_tokens,
            kept_estimated_tokens=kept_tokens,
            saved_estimated_tokens=saved_tokens,
            tokens_saved_pct=tokens_saved_pct,
        )
