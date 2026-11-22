"""Priority-tiered TokenBudgetPacker with overlap merging, line truncation & KPI accounting."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

import structlog

from codesight.diff.file_filter import estimate_tokens, normalize_diff_path
from codesight.rag.models import validate_positive_int

DEFAULT_AUX_TOKEN_BUDGET: Final[int] = 3500
MIN_TRUNCATED_SLICE_TOKENS: Final[int] = 120
TRUNCATION_MARKER: Final[str] = "... [truncated to fit token budget]"

type ContextTier = Literal["enclosing_symbol", "caller", "callee", "architecture_doc"]

_TIER_GROUPS: Final[tuple[tuple[ContextTier, ...], ...]] = (
    ("enclosing_symbol",),
    ("caller", "callee"),
    ("architecture_doc",),
)
_TIER_LABELS: Final[dict[ContextTier, str]] = {
    "enclosing_symbol": "Enclosing Symbol",
    "caller": "Direct Caller",
    "callee": "Direct Callee",
    "architecture_doc": "Architecture Doc",
}
_TIER_SUB_ORDER: Final[dict[ContextTier, int]] = {
    "enclosing_symbol": 0,
    "caller": 1,
    "callee": 2,
    "architecture_doc": 3,
}

logger = structlog.get_logger("codesight.rag.token_packer")


def _split_candidate_lines(content: str) -> list[str]:
    if not content:
        return []
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if normalized.endswith("\n") and lines:
        lines.pop()
    return lines


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class ContextCandidateSlice:
    """In-memory resolved context slice ready for priority ranking and token packing."""

    tier: ContextTier
    file_path: str
    source_key: str
    start_line: int
    end_line: int
    content: str
    symbol_name: str
    symbol_kind: str
    signature: str
    relevance_score: float = 0.0
    full_file_tokens: int = 0
    focus_lines: tuple[int, ...] = ()
    use_line_numbers: bool = True

    def __post_init__(self) -> None:
        if isinstance(self.start_line, bool) or not isinstance(self.start_line, int):
            raise ValueError("start_line must be an integer >= 1.")
        if isinstance(self.end_line, bool) or not isinstance(self.end_line, int):
            raise ValueError("end_line must be an integer >= start_line.")
        if self.start_line < 1 or self.end_line < self.start_line:
            raise ValueError("start_line and end_line must satisfy 1 <= start_line <= end_line.")
        if (
            isinstance(self.full_file_tokens, bool)
            or not isinstance(self.full_file_tokens, int)
            or self.full_file_tokens < 0
        ):
            raise ValueError("full_file_tokens must be a non-negative integer.")


@dataclass(frozen=True, slots=True)
class PackedContextBlock:
    """One formatted context slice accepted within the hard auxiliary token budget."""

    tier: ContextTier
    file_path: str
    start_line: int
    end_line: int
    symbol_name: str
    symbol_kind: str
    signature: str
    rendered_text: str
    token_count: int
    truncated: bool


@dataclass(frozen=True, slots=True)
class TokenBudgetMetrics:
    """Deterministic anti-gaming token accounting comparing naive full-file vs packed review."""

    budget_tokens: int
    packed_aux_tokens: int
    diff_tokens: int
    skipped_noise_tokens: int
    baseline_tokens: int
    codesight_tokens: int
    saved_tokens: int
    tokens_saved_pct: float
    aux_tokens_saved_pct: float
    unique_baseline_files: int
    packed_blocks_count: int
    truncated_blocks_count: int
    dropped_blocks_count: int


@dataclass(frozen=True, slots=True)
class PackedContextBundle:
    """Complete packed auxiliary context ready for Context Builder Node (CA-18)."""

    blocks: tuple[PackedContextBlock, ...]
    rendered_context: str
    metrics: TokenBudgetMetrics


@dataclass(slots=True)
class _NormalizedSpan:
    tier: ContextTier
    file_path: str
    source_key: str
    line_map: dict[int, str]
    start_line: int
    end_line: int
    symbol_name: str
    symbol_kind: str
    signature: str
    relevance_score: float
    full_file_tokens: int
    focus_lines: tuple[int, ...]
    use_line_numbers: bool


def _format_line(line_no: int, text: str, *, use_line_numbers: bool) -> str:
    return f"L{line_no}: {text}" if use_line_numbers else text


def _build_block_header(span: _NormalizedSpan) -> str:
    label = _TIER_LABELS[span.tier]
    sig = " ".join(span.signature.split()).strip()
    if sig:
        return f"### [{label}] {span.file_path} (L{span.start_line}-L{span.end_line}) — {sig}"
    return f"### [{label}] {span.file_path} (L{span.start_line}-L{span.end_line})"


def _render_full_span(span: _NormalizedSpan) -> str:
    header = _build_block_header(span)
    body_lines = [
        _format_line(ln, span.line_map[ln], use_line_numbers=span.use_line_numbers)
        for ln in sorted(span.line_map)
    ]
    return f"{header}\n" + "\n".join(body_lines)


def _render_truncated_span(
    span: _NormalizedSpan,
    *,
    max_bytes: int,
) -> tuple[str, int, int, frozenset[int]] | None:
    """Truncate a span cleanly on whole-line boundaries within `max_bytes` UTF-8 bytes."""
    header = _build_block_header(span)
    ordered_lines = [
        (ln, _format_line(ln, span.line_map[ln], use_line_numbers=span.use_line_numbers))
        for ln in sorted(span.line_map)
    ]
    if not ordered_lines:
        return None

    marker_overhead = _utf8_len(f"\n{TRUNCATION_MARKER}")
    header_bytes = _utf8_len(header)
    if header_bytes + marker_overhead >= max_bytes:
        return None

    valid_focus = sorted({ln for ln in span.focus_lines if ln in span.line_map})

    if span.tier == "enclosing_symbol" and valid_focus:
        return _truncate_centered_on_focus(
            header=header,
            ordered_lines=ordered_lines,
            focus_lines=valid_focus,
            max_bytes=max_bytes,
        )

    return _truncate_from_top(
        header=header,
        ordered_lines=ordered_lines,
        max_bytes=max_bytes,
    )


def _truncate_from_top(
    *,
    header: str,
    ordered_lines: Sequence[tuple[int, str]],
    max_bytes: int,
) -> tuple[str, int, int, frozenset[int]] | None:
    marker_suffix = f"\n{TRUNCATION_MARKER}"
    budget_for_body = max_bytes - _utf8_len(header) - _utf8_len(marker_suffix)
    if budget_for_body <= 0:
        return None

    chosen: list[tuple[int, str]] = []
    used = 0
    for ln, formatted in ordered_lines:
        addition = _utf8_len(formatted) + 1  # leading '\n'
        if used + addition > budget_for_body:
            break
        chosen.append((ln, formatted))
        used += addition

    if not chosen:
        return None

    rendered = header + "\n" + "\n".join(text for _, text in chosen) + marker_suffix
    if _utf8_len(rendered) > max_bytes:
        return None
    return rendered, chosen[0][0], chosen[-1][0], frozenset(ln for ln, _ in chosen)


def _truncate_centered_on_focus(
    *,
    header: str,
    ordered_lines: Sequence[tuple[int, str]],
    focus_lines: Sequence[int],
    max_bytes: int,
) -> tuple[str, int, int, frozenset[int]] | None:
    """Keep leading signature lines + symmetric whole-line windows around all focus lines."""
    total = len(ordered_lines)
    line_to_idx = {ln: idx for idx, (ln, _) in enumerate(ordered_lines)}
    focus_indices = [line_to_idx[ln] for ln in focus_lines if ln in line_to_idx]
    if not focus_indices:
        return _truncate_from_top(
            header=header,
            ordered_lines=ordered_lines,
            max_bytes=max_bytes,
        )

    def _assemble(selected_indices: set[int]) -> str:
        sorted_idx = sorted(selected_indices)
        parts: list[str] = [header]
        prev_idx: int | None = None
        for idx in sorted_idx:
            if prev_idx is not None and idx > prev_idx + 1:
                parts.append("...")
            parts.append(ordered_lines[idx][1])
            prev_idx = idx
        parts.append(TRUNCATION_MARKER)
        return "\n".join(parts)

    selected: set[int] = set()
    # 1. Seed with leading signature/docstring lines (up to 2 lines) if they fit
    for lead_idx in range(min(2, total)):
        trial = selected | {lead_idx}
        if _utf8_len(_assemble(trial)) <= max_bytes:
            selected = trial

    # 2. Add focus lines directly
    for f_idx in focus_indices:
        trial = selected | {f_idx}
        if _utf8_len(_assemble(trial)) <= max_bytes:
            selected = trial

    if not selected:
        return _truncate_from_top(
            header=header,
            ordered_lines=ordered_lines,
            max_bytes=max_bytes,
        )

    # 3. Expand symmetrically around every focus line, preferring interior lines between focus edits
    min_focus = min(focus_indices)
    max_focus = max(focus_indices)
    max_dist = max(max(f_idx, total - 1 - f_idx) for f_idx in focus_indices)
    for radius in range(1, max_dist + 1):
        if len(selected) == total:
            break
        interior_cands: list[int] = []
        outer_cands: list[int] = []
        seen_radius: set[int] = set()
        for f_idx in focus_indices:
            for cand_idx in (f_idx - radius, f_idx + radius):
                if not (0 <= cand_idx < total) or cand_idx in seen_radius:
                    continue
                seen_radius.add(cand_idx)
                if min_focus < cand_idx < max_focus:
                    interior_cands.append(cand_idx)
                else:
                    outer_cands.append(cand_idx)
        if not seen_radius:
            break
        attempted_new = False
        added_any = False
        for cand_idx in (*interior_cands, *outer_cands):
            if cand_idx in selected:
                continue
            attempted_new = True
            trial = selected | {cand_idx}
            if _utf8_len(_assemble(trial)) <= max_bytes:
                selected = trial
                added_any = True
        if attempted_new and not added_any:
            break

    rendered = _assemble(selected)
    if _utf8_len(rendered) > max_bytes:
        return None
    chosen_lines = [ordered_lines[idx][0] for idx in sorted(selected)]
    return rendered, chosen_lines[0], chosen_lines[-1], frozenset(chosen_lines)


def _subtract_owned_lines(
    span: _NormalizedSpan,
    owned_lines: set[int],
) -> list[_NormalizedSpan]:
    """Split `span` into contiguous sub-spans excluding any line numbers in `owned_lines`."""
    if not owned_lines:
        return [span]
    remaining_nums = sorted(ln for ln in span.line_map if ln not in owned_lines)
    if not remaining_nums:
        return []

    segments: list[list[int]] = [[remaining_nums[0]]]
    for ln in remaining_nums[1:]:
        if ln == segments[-1][-1] + 1:
            segments[-1].append(ln)
        else:
            segments.append([ln])

    out: list[_NormalizedSpan] = []
    for seg in segments:
        seg_start = seg[0]
        seg_end = seg[-1]
        seg_map = {ln: span.line_map[ln] for ln in seg}
        seg_focus = tuple(ln for ln in span.focus_lines if seg_start <= ln <= seg_end)
        out.append(
            _NormalizedSpan(
                tier=span.tier,
                file_path=span.file_path,
                source_key=span.source_key,
                line_map=seg_map,
                start_line=seg_start,
                end_line=seg_end,
                symbol_name=span.symbol_name,
                symbol_kind=span.symbol_kind,
                signature=span.signature,
                relevance_score=span.relevance_score,
                full_file_tokens=span.full_file_tokens,
                focus_lines=seg_focus,
                use_line_numbers=span.use_line_numbers,
            )
        )
    return out


def _merge_same_tier_spans(spans: Sequence[_NormalizedSpan]) -> list[_NormalizedSpan]:
    """Merge overlapping or adjacent spans sharing `(file_path, source_key)` within one tier."""
    grouped: dict[tuple[str, str], list[_NormalizedSpan]] = {}
    for span in spans:
        grouped.setdefault((span.file_path, span.source_key), []).append(span)

    merged_all: list[_NormalizedSpan] = []
    for group in grouped.values():
        group.sort(key=lambda s: (s.start_line, s.end_line))
        current = group[0]
        for nxt in group[1:]:
            if nxt.start_line <= current.end_line + 1:
                combined_map = dict(current.line_map)
                combined_map.update(nxt.line_map)
                prefer_nxt = (nxt.relevance_score, -_TIER_SUB_ORDER[nxt.tier]) > (
                    current.relevance_score,
                    -_TIER_SUB_ORDER[current.tier],
                )
                winner = nxt if prefer_nxt else current
                merged_focus = tuple(sorted(set(current.focus_lines) | set(nxt.focus_lines)))
                current = _NormalizedSpan(
                    tier=winner.tier,
                    file_path=current.file_path,
                    source_key=current.source_key,
                    line_map=combined_map,
                    start_line=min(current.start_line, nxt.start_line),
                    end_line=max(current.end_line, nxt.end_line),
                    symbol_name=winner.symbol_name,
                    symbol_kind=winner.symbol_kind,
                    signature=winner.signature,
                    relevance_score=max(current.relevance_score, nxt.relevance_score),
                    full_file_tokens=max(current.full_file_tokens, nxt.full_file_tokens),
                    focus_lines=merged_focus,
                    use_line_numbers=current.use_line_numbers and nxt.use_line_numbers,
                )
            else:
                merged_all.append(current)
                current = nxt
        merged_all.append(current)

    return merged_all


class TokenBudgetPacker:
    """Packs prioritized PR context within a strict token ceiling and measures token savings."""

    def __init__(
        self,
        *,
        budget_tokens: int = DEFAULT_AUX_TOKEN_BUDGET,
        min_truncated_tokens: int = MIN_TRUNCATED_SLICE_TOKENS,
    ) -> None:
        self._budget_tokens = validate_positive_int(budget_tokens, name="budget_tokens")
        self._min_truncated_tokens = validate_positive_int(
            min_truncated_tokens,
            name="min_truncated_tokens",
        )

    @property
    def budget_tokens(self) -> int:
        """Return configured hard auxiliary token ceiling per batch."""
        return self._budget_tokens

    def pack(
        self,
        candidates: Sequence[ContextCandidateSlice],
        *,
        diff_text: str | None = None,
        diff_tokens: int | None = None,
        modified_file_tokens: Mapping[str, int] | None = None,
        skipped_noise_tokens: int = 0,
    ) -> PackedContextBundle:
        """Deduplicate overlapping spans, pack by tier under hard budget, and compute KPIs."""
        if (
            isinstance(skipped_noise_tokens, bool)
            or not isinstance(skipped_noise_tokens, int)
            or skipped_noise_tokens < 0
        ):
            raise ValueError("skipped_noise_tokens must be a non-negative integer.")
        if diff_tokens is not None and (
            isinstance(diff_tokens, bool) or not isinstance(diff_tokens, int) or diff_tokens < 0
        ):
            raise ValueError("diff_tokens must be a non-negative integer when provided.")

        eff_diff_tokens = (
            diff_tokens
            if diff_tokens is not None
            else (estimate_tokens(diff_text) if diff_text else 0)
        )

        # Anti-gaming baseline accounting: deduplicate full_file_tokens strictly once per file_path
        unique_total_file_tokens: dict[str, int] = {}
        unique_aux_file_tokens: dict[str, int] = {}
        if modified_file_tokens:
            for raw_path, raw_toks in modified_file_tokens.items():
                if isinstance(raw_toks, bool) or not isinstance(raw_toks, int) or raw_toks < 0:
                    continue
                norm_p = normalize_diff_path(raw_path).rstrip("/")
                if norm_p and raw_toks > 0:
                    unique_total_file_tokens[norm_p] = max(
                        unique_total_file_tokens.get(norm_p, 0),
                        raw_toks,
                    )

        normalized_by_tier_group: list[list[_NormalizedSpan]] = [[] for _ in _TIER_GROUPS]
        for cand in candidates:
            norm_path = normalize_diff_path(cand.file_path).rstrip("/")
            if not norm_path:
                continue
            lines = _split_candidate_lines(cand.content)
            if not lines:
                continue
            if cand.full_file_tokens > 0:
                unique_aux_file_tokens[norm_path] = max(
                    unique_aux_file_tokens.get(norm_path, 0),
                    cand.full_file_tokens,
                )
                unique_total_file_tokens[norm_path] = max(
                    unique_total_file_tokens.get(norm_path, 0),
                    cand.full_file_tokens,
                )

            line_map = {
                cand.start_line + offset: line_text for offset, line_text in enumerate(lines)
            }
            actual_end = (
                cand.start_line + len(lines) - 1 if cand.use_line_numbers else cand.end_line
            )
            span = _NormalizedSpan(
                tier=cand.tier,
                file_path=norm_path,
                source_key=cand.source_key.strip() or norm_path,
                line_map=line_map,
                start_line=cand.start_line,
                end_line=actual_end,
                symbol_name=cand.symbol_name,
                symbol_kind=cand.symbol_kind,
                signature=cand.signature,
                relevance_score=cand.relevance_score,
                full_file_tokens=cand.full_file_tokens,
                focus_lines=cand.focus_lines,
                use_line_numbers=cand.use_line_numbers,
            )
            for group_idx, group_tiers in enumerate(_TIER_GROUPS):
                if cand.tier in group_tiers:
                    normalized_by_tier_group[group_idx].append(span)
                    break

        # Exact UTF-8 byte ceiling guarantees estimate_tokens(rendered_context) <= budget_tokens
        max_total_bytes = self._budget_tokens * 4
        sep_bytes = 2  # len("\n\n".encode("utf-8"))
        used_bytes = 0
        packed_blocks: list[PackedContextBlock] = []
        truncated_count = 0
        dropped_count = 0
        owned_lines_by_file_ver: dict[tuple[str, str], set[int]] = {}

        for tier_spans in normalized_by_tier_group:
            if not tier_spans:
                continue

            # 1. Subtract/merge only real line-numbered spans; keep raw diff hunks intact
            trimmed_numbered: list[_NormalizedSpan] = []
            raw_hunk_spans: list[_NormalizedSpan] = []
            for span in tier_spans:
                if not span.use_line_numbers:
                    raw_hunk_spans.append(span)
                    continue
                key = (span.file_path, span.source_key)
                owned = owned_lines_by_file_ver.get(key, set())
                trimmed_numbered.extend(_subtract_owned_lines(span, owned))
            if not trimmed_numbered and not raw_hunk_spans:
                continue

            # 2. Merge overlapping/adjacent line-numbered spans within this tier
            merged = (
                _merge_same_tier_spans(trimmed_numbered) if trimmed_numbered else []
            ) + raw_hunk_spans

            # 3. Deterministic priority sort inside this tier
            merged.sort(
                key=lambda s: (
                    -s.relevance_score,
                    _TIER_SUB_ORDER[s.tier],
                    s.file_path,
                    s.start_line,
                )
            )

            # 4. Pass 1 (Greedy complete): fit complete blocks in rank order
            oversized: list[_NormalizedSpan] = []
            for span in merged:
                rendered = _render_full_span(span)
                block_bytes = _utf8_len(rendered)
                prefix_sep = sep_bytes if packed_blocks else 0
                if used_bytes + prefix_sep + block_bytes <= max_total_bytes:
                    packed_blocks.append(
                        PackedContextBlock(
                            tier=span.tier,
                            file_path=span.file_path,
                            start_line=span.start_line,
                            end_line=span.end_line,
                            symbol_name=span.symbol_name,
                            symbol_kind=span.symbol_kind,
                            signature=span.signature,
                            rendered_text=rendered,
                            token_count=estimate_tokens(rendered),
                            truncated=False,
                        )
                    )
                    used_bytes += prefix_sep + block_bytes
                    if span.use_line_numbers:
                        owned_lines_by_file_ver.setdefault(
                            (span.file_path, span.source_key), set()
                        ).update(span.line_map.keys())
                else:
                    oversized.append(span)

            # 5. Pass 2 (Line-safe truncation): spend remaining budget >= min_truncated_tokens
            for span in oversized:
                prefix_sep = sep_bytes if packed_blocks else 0
                avail_bytes = max_total_bytes - used_bytes - prefix_sep
                avail_tokens = avail_bytes // 4
                if avail_tokens < self._min_truncated_tokens:
                    dropped_count += 1
                    continue

                trunc_result = _render_truncated_span(span, max_bytes=avail_bytes)
                if trunc_result is None:
                    dropped_count += 1
                    continue
                rendered_trunc, eff_start, eff_end, rendered_lines = trunc_result
                block_bytes = _utf8_len(rendered_trunc)
                if used_bytes + prefix_sep + block_bytes > max_total_bytes:
                    dropped_count += 1
                    continue

                packed_blocks.append(
                    PackedContextBlock(
                        tier=span.tier,
                        file_path=span.file_path,
                        start_line=eff_start,
                        end_line=eff_end,
                        symbol_name=span.symbol_name,
                        symbol_kind=span.symbol_kind,
                        signature=span.signature,
                        rendered_text=rendered_trunc,
                        token_count=estimate_tokens(rendered_trunc),
                        truncated=True,
                    )
                )
                used_bytes += prefix_sep + block_bytes
                truncated_count += 1
                if span.use_line_numbers:
                    owned_lines_by_file_ver.setdefault(
                        (span.file_path, span.source_key), set()
                    ).update(rendered_lines)

        rendered_context = "\n\n".join(block.rendered_text for block in packed_blocks)
        packed_aux_tokens = estimate_tokens(rendered_context)
        baseline_tokens = sum(unique_total_file_tokens.values()) + skipped_noise_tokens
        aux_baseline_tokens = sum(unique_aux_file_tokens.values())
        codesight_tokens = eff_diff_tokens + packed_aux_tokens
        saved_tokens = max(0, baseline_tokens - codesight_tokens)
        tokens_saved_pct = (
            round((saved_tokens / baseline_tokens) * 100.0, 2) if baseline_tokens > 0 else 0.0
        )
        aux_saved = max(0, aux_baseline_tokens - packed_aux_tokens)
        aux_tokens_saved_pct = (
            round((aux_saved / aux_baseline_tokens) * 100.0, 2) if aux_baseline_tokens > 0 else 0.0
        )

        metrics = TokenBudgetMetrics(
            budget_tokens=self._budget_tokens,
            packed_aux_tokens=packed_aux_tokens,
            diff_tokens=eff_diff_tokens,
            skipped_noise_tokens=skipped_noise_tokens,
            baseline_tokens=baseline_tokens,
            codesight_tokens=codesight_tokens,
            saved_tokens=saved_tokens,
            tokens_saved_pct=tokens_saved_pct,
            aux_tokens_saved_pct=aux_tokens_saved_pct,
            unique_baseline_files=len(unique_total_file_tokens),
            packed_blocks_count=len(packed_blocks),
            truncated_blocks_count=truncated_count,
            dropped_blocks_count=dropped_count,
        )
        logger.info(
            "token_budget_packed",
            budget_tokens=self._budget_tokens,
            packed_aux_tokens=packed_aux_tokens,
            baseline_tokens=baseline_tokens,
            codesight_tokens=codesight_tokens,
            tokens_saved_pct=tokens_saved_pct,
            unique_baseline_files=metrics.unique_baseline_files,
            packed_blocks=metrics.packed_blocks_count,
            truncated_blocks=truncated_count,
            dropped_blocks=dropped_count,
        )
        return PackedContextBundle(
            blocks=tuple(packed_blocks),
            rendered_context=rendered_context,
            metrics=metrics,
        )
