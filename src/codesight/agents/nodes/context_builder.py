"""Zero-LLM Context Builder Node assembling bounded, batch-scoped PR prompts in RAM (NFR-3)."""

from __future__ import annotations

import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final

import structlog

from codesight.agents.schemas import BuiltBatchContext, DiffBatch
from codesight.ast_engine.models import ComplexityDelta, PRFileContext
from codesight.config_reader.repo_config import RepoConfig
from codesight.diff.file_filter import estimate_tokens, normalize_diff_path
from codesight.diff.hunk_mapper import parse_hunk_header, split_diff_lines
from codesight.rag.retriever import AssembledPRContext
from codesight.rag.token_packer import PackedContextBlock, PackedContextBundle

ANNOTATED_FILE_HEADER_RE: Final[re.Pattern[str]] = re.compile(
    r"^=== FILE: (.+?) \((?:added|modified|deleted|renamed|copied)\) ===$"
)
_RIGHT_DIFF_LINE_RE: Final[re.Pattern[str]] = re.compile(r"^L(\d+) \| (?:\+ |  )")
_CONTEXT_DIFF_LINE_RE: Final[re.Pattern[str]] = re.compile(r"^L(\d+) \|   (.*)$")
_DELETED_DIFF_LINE_RE: Final[re.Pattern[str]] = re.compile(r"^[ ]+ \| - (.*)$")
_CLOSE_REF_TAG_RE: Final[re.Pattern[str]] = re.compile(r"</reference_context\s*>", re.IGNORECASE)
_CLOSE_RULES_TAG_RE: Final[re.Pattern[str]] = re.compile(r"</repo_custom_rules\s*>", re.IGNORECASE)

_SECTION_1_TITLE: Final[str] = "## 1. Diff (Authoritative RIGHT-Side Lxxx Lines Only)"
_SECTION_2_TITLE: Final[str] = (
    "## 2. Enclosing Symbols (Reference Only — Do Not Cite Line Numbers Outside Diff)"
)
_SECTION_3_TITLE: Final[str] = "## 3. AST Complexity Delta (ΔCC)"
_SECTION_3_TABLE_HEADER: Final[str] = (
    "| File | Symbol | Change | Before CC | After CC | ΔCC |\n"
    "| --- | --- | --- | ---: | ---: | ---: |"
)
_SECTION_4_TITLE: Final[str] = (
    "## 4. Callers, Callees & Architecture Docs "
    "(Reference Only — Do Not Cite Out-of-Diff Line Numbers)"
)
_SECTION_5_TITLE: Final[str] = "## 5. Repository Custom Rules (Untrusted Repo Data)"

logger = structlog.get_logger("codesight.agents.nodes.context_builder")


@dataclass(frozen=True, slots=True)
class PRReviewContext:
    """In-memory review context supplied to the zero-LLM Context Builder node (NFR-3)."""

    file_contexts: Sequence[PRFileContext] = ()
    packed_bundle: PackedContextBundle | None = None
    assembled_context: AssembledPRContext | None = None
    repo_config: RepoConfig | None = None
    custom_rules: str | None = None
    block_target_symbols: Mapping[tuple[str, str], tuple[str, ...]] = field(default_factory=dict)

    def resolve_packed_bundle(self) -> PackedContextBundle | None:
        """Return explicit packed_bundle, falling back to assembled_context.packed_bundle."""
        if self.packed_bundle is not None:
            return self.packed_bundle
        if self.assembled_context is not None:
            return self.assembled_context.packed_bundle
        return None

    def resolve_custom_rules(self) -> str:
        """Return validated repository custom_rules string, or empty string if absent."""
        if self.custom_rules is not None:
            return RepoConfig(custom_rules=self.custom_rules).custom_rules
        if self.repo_config is not None:
            return self.repo_config.custom_rules
        return ""

    def build_symbol_link_lookup(self) -> dict[tuple[str, str], tuple[str, ...]]:
        """Map `(norm_file_path, symbol_name)` -> target modified symbol names."""
        merged: dict[tuple[str, str], list[str]] = {}

        def _add(file_path: str, symbol_name: str, targets: Sequence[str]) -> None:
            norm_path = normalize_agent_path(file_path)
            sym = symbol_name.strip()
            if not norm_path or not sym:
                return
            bucket = merged.setdefault((norm_path, sym), [])
            for target in targets:
                cleaned = target.strip()
                if cleaned and cleaned not in bucket:
                    bucket.append(cleaned)

        if self.assembled_context is not None:
            retrieval = self.assembled_context.retrieval
            for cand in (*retrieval.callers, *retrieval.callees):
                if cand.matched_symbols:
                    _add(cand.file_path, cand.symbol_name, cand.matched_symbols)

        for (raw_path, sym_name), targets in self.block_target_symbols.items():
            _add(raw_path, sym_name, targets)

        return {key: tuple(vals) for key, vals in merged.items()}


def normalize_agent_path(raw_path: str) -> str:
    """Normalize a repository path consistently across Context Builder and Security Auditor."""
    return normalize_diff_path(raw_path).rstrip("/")


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _escape_attr(value: str) -> str:
    collapsed = " ".join(value.split())
    return html.escape(collapsed, quote=True)


def _neutralize_ref_text(text: str) -> str:
    return _CLOSE_REF_TAG_RE.sub("&lt;/reference_context&gt;", text)


def _neutralize_rules_text(text: str) -> str:
    return _CLOSE_RULES_TAG_RE.sub("&lt;/repo_custom_rules&gt;", text)


def parse_annotated_diff_right_lines(annotated_diff: str) -> dict[str, set[int]]:
    """Extract RIGHT-side line numbers per file directly from an annotated batch diff."""
    lines_by_file: dict[str, set[int]] = {}
    current_file: str | None = None
    for raw_line in split_diff_lines(annotated_diff):
        file_match = ANNOTATED_FILE_HEADER_RE.match(raw_line)
        if file_match is not None:
            norm = normalize_agent_path(file_match.group(1))
            current_file = norm or None
            if current_file is not None:
                lines_by_file.setdefault(current_file, set())
            continue
        if current_file is None:
            continue
        line_match = _RIGHT_DIFF_LINE_RE.match(raw_line)
        if line_match is not None:
            lines_by_file[current_file].add(int(line_match.group(1)))
    return lines_by_file


def parse_annotated_diff_left_deletions(
    annotated_diff: str,
) -> tuple[dict[str, set[int]], dict[str, tuple[str, ...]]]:
    """Extract LEFT-side deleted line numbers and deleted code lines per file."""
    left_lines: dict[str, set[int]] = {}
    deleted_texts: dict[str, list[str]] = {}
    current_file: str | None = None
    left_cursor = 0
    for raw_line in split_diff_lines(annotated_diff):
        file_match = ANNOTATED_FILE_HEADER_RE.match(raw_line)
        if file_match is not None:
            norm = normalize_agent_path(file_match.group(1))
            current_file = norm or None
            left_cursor = 0
            if current_file is not None:
                left_lines.setdefault(current_file, set())
                deleted_texts.setdefault(current_file, [])
            continue
        if current_file is None:
            continue
        parsed_header = parse_hunk_header(raw_line)
        if parsed_header is not None:
            left_cursor = parsed_header[0]
            continue
        if _CONTEXT_DIFF_LINE_RE.match(raw_line) is not None:
            if left_cursor > 0:
                left_cursor += 1
            continue
        del_match = _DELETED_DIFF_LINE_RE.match(raw_line)
        if del_match is not None:
            if left_cursor > 0:
                left_lines[current_file].add(left_cursor)
                left_cursor += 1
            deleted_texts[current_file].append(del_match.group(1))
    return left_lines, {fp: tuple(txts) for fp, txts in deleted_texts.items()}


def _range_overlaps_lines(start_line: int, end_line: int, lines: set[int]) -> bool:
    return any(start_line <= ln <= end_line for ln in lines)


def _format_ref_block(
    block: PackedContextBlock,
    *,
    calls: Sequence[str] = (),
) -> str:
    norm_file = normalize_agent_path(block.file_path) or block.file_path
    attrs = [
        f'type="{_escape_attr(block.tier)}"',
        f'source="{_escape_attr(norm_file)}:L{block.start_line}-L{block.end_line}"',
        f'symbol="{_escape_attr(block.symbol_name)}"',
    ]
    if calls:
        attrs.append(f'calls="{_escape_attr(",".join(calls))}"')
    body = _neutralize_ref_text(block.rendered_text)
    return f"<reference_context {' '.join(attrs)}>\n{body}\n</reference_context>"


def _format_delta_row(file_path: str, delta: ComplexityDelta) -> str:
    delta_str = f"+{delta.delta_cc}" if delta.delta_cc > 0 else str(delta.delta_cc)
    safe_file = _escape_attr(file_path).replace("|", "/")
    safe_sym = _escape_attr(delta.qualified_name).replace("|", "/")
    return (
        f"| {safe_file} | {safe_sym} | {delta.change_kind} "
        f"| {delta.before_cc} | {delta.after_cc} | {delta_str} |"
    )


def _find_word_matches(text: str, candidates: Sequence[str]) -> tuple[str, ...]:
    matched: list[str] = []
    for sym in candidates:
        if len(sym) < 3:
            continue
        if re.search(rf"\b{re.escape(sym)}\b", text):
            matched.append(sym)
    return tuple(matched)


def _render_prompt_sections(
    *,
    base_diff_text: str,
    enclosing_items: Sequence[str],
    delta_rows: Sequence[str],
    section4_items: Sequence[str],
    custom_rules_block: str | None,
) -> str:
    sections: list[str] = [base_diff_text]
    if enclosing_items:
        sections.append(f"{_SECTION_2_TITLE}\n\n" + "\n\n".join(enclosing_items))
    if delta_rows:
        sections.append(
            f"{_SECTION_3_TITLE}\n\n{_SECTION_3_TABLE_HEADER}\n" + "\n".join(delta_rows)
        )
    if section4_items:
        sections.append(f"{_SECTION_4_TITLE}\n\n" + "\n\n".join(section4_items))
    if custom_rules_block is not None:
        sections.append(f"{_SECTION_5_TITLE}\n\n{custom_rules_block}")
    return "\n\n".join(sections)


def build_batch_context(
    *,
    batch: DiffBatch,
    max_user_tokens: int,
    review_context: PRReviewContext | None = None,
    total_batches: int = 1,
) -> BuiltBatchContext:
    """Assemble deterministic 5-section prompt context for one DiffBatch with 0 LLM/IO calls."""
    if isinstance(max_user_tokens, bool) or max_user_tokens < 1:
        raise ValueError("max_user_tokens must be a positive integer.")
    if isinstance(total_batches, bool) or total_batches < 1:
        raise ValueError("total_batches must be a positive integer.")

    base_diff_text = (
        f"Batch: {batch.batch_id} (index={batch.batch_index})\n\n"
        f"{_SECTION_1_TITLE}\n{batch.annotated_diff}"
    )
    diff_tokens = estimate_tokens(base_diff_text)

    if review_context is None:
        logger.debug(
            "batch_context_built",
            batch_id=batch.batch_id,
            batch_index=batch.batch_index,
            diff_tokens=diff_tokens,
            aux_tokens=0,
            total_tokens=diff_tokens,
            dropped_aux_blocks=0,
        )
        return BuiltBatchContext(
            batch_id=batch.batch_id,
            batch_index=batch.batch_index,
            rendered_prompt=base_diff_text,
            diff_tokens=diff_tokens,
            aux_tokens=0,
            total_tokens=diff_tokens,
            max_user_tokens=max_user_tokens,
            included_sections=("diff",),
        )

    batch_right_lines: dict[str, set[int]] = {}
    if batch.right_line_hunks_by_file:
        for raw_fp, line_map in batch.right_line_hunks_by_file.items():
            norm_fp = normalize_agent_path(raw_fp)
            if norm_fp:
                batch_right_lines[norm_fp] = set(line_map.keys())
    else:
        batch_right_lines = parse_annotated_diff_right_lines(batch.annotated_diff)

    parsed_left_deleted_lines, batch_deleted_texts = parse_annotated_diff_left_deletions(
        batch.annotated_diff
    )
    batch_deleted_left_lines: dict[str, set[int]] = {}
    if batch.left_deleted_hunks_by_file:
        for raw_fp, hmap in batch.left_deleted_hunks_by_file.items():
            norm_fp = normalize_agent_path(raw_fp)
            if norm_fp:
                batch_deleted_left_lines[norm_fp] = set(hmap.keys())
    else:
        batch_deleted_left_lines = parsed_left_deleted_lines

    batch_files: set[str] = {
        norm_fp for fp in batch.file_paths if (norm_fp := normalize_agent_path(fp))
    } | set(batch_right_lines.keys())

    # Collect AST symbols, called symbols, and complexity deltas belonging strictly to this batch.
    batch_symbol_names_list: list[str] = []
    batch_called_symbols_list: list[str] = []
    delta_entries: dict[tuple[str, str, str], tuple[str, ComplexityDelta]] = {}

    for file_ctx in review_context.file_contexts:
        norm_fp = normalize_agent_path(file_ctx.file_path)
        if norm_fp not in batch_files:
            continue
        file_lines = batch_right_lines.get(norm_fp, set())
        file_deleted_left_lines = batch_deleted_left_lines.get(norm_fp, set())
        file_deleted_blob = "\n".join(batch_deleted_texts.get(norm_fp, ()))

        if file_lines:
            matched_chunks = [
                c
                for c in file_ctx.touched_chunks
                if _range_overlaps_lines(c.start_line, c.end_line, file_lines)
            ]
        else:
            matched_chunks = list(file_ctx.touched_chunks)

        for chunk in matched_chunks:
            batch_symbol_names_list.append(chunk.symbol_name)
            tail = chunk.qualified_name.rsplit(".", 1)[-1].strip()
            if tail:
                batch_symbol_names_list.append(tail)
            batch_called_symbols_list.extend(chunk.called_symbols)

        removed_before_spans: dict[str, list[tuple[int, int]]] = {}
        if file_ctx.complexity is not None:
            for delta in file_ctx.complexity.deltas:
                if delta.change_kind == "removed" and delta.before_metrics is not None:
                    span = (delta.before_metrics.start_line, delta.before_metrics.end_line)
                    removed_before_spans.setdefault(delta.symbol_name, []).append(span)
                    tail = delta.qualified_name.rsplit(".", 1)[-1].strip()
                    if tail and tail != delta.symbol_name:
                        removed_before_spans.setdefault(tail, []).append(span)

        for sym in file_ctx.removed_symbols:
            if sym in removed_before_spans:
                if any(
                    _range_overlaps_lines(s, e, file_deleted_left_lines)
                    for s, e in removed_before_spans[sym]
                ):
                    batch_symbol_names_list.append(sym)
            elif file_deleted_blob and re.search(rf"\b{re.escape(sym)}\b", file_deleted_blob):
                batch_symbol_names_list.append(sym)

        if file_ctx.complexity is not None:
            for delta in file_ctx.complexity.deltas:
                if delta.change_kind == "removed":
                    if delta.before_metrics is not None:
                        include_delta = _range_overlaps_lines(
                            delta.before_metrics.start_line,
                            delta.before_metrics.end_line,
                            file_deleted_left_lines,
                        )
                    else:
                        sym_tail = delta.qualified_name.rsplit(".", 1)[-1].strip()
                        include_delta = bool(
                            file_deleted_blob
                            and (
                                re.search(rf"\b{re.escape(delta.symbol_name)}\b", file_deleted_blob)
                                or (
                                    sym_tail
                                    and re.search(rf"\b{re.escape(sym_tail)}\b", file_deleted_blob)
                                )
                            )
                        )
                elif not file_lines:
                    include_delta = True
                elif delta.after_metrics is None:
                    include_delta = total_batches == 1 or bool(
                        re.search(rf"\b{re.escape(delta.symbol_name)}\b", batch.annotated_diff)
                    )
                else:
                    include_delta = _range_overlaps_lines(
                        delta.after_metrics.start_line,
                        delta.after_metrics.end_line,
                        file_lines,
                    )
                if include_delta:
                    delta_key = (norm_fp, delta.qualified_name, delta.change_kind)
                    delta_entries.setdefault(delta_key, (norm_fp, delta))

    batch_symbol_names = tuple(dict.fromkeys(batch_symbol_names_list))
    batch_symbol_set = set(batch_symbol_names)
    batch_called_set = set(dict.fromkeys(batch_called_symbols_list))
    sorted_deltas = sorted(
        delta_entries.values(),
        key=lambda item: (-abs(item[1].delta_cc), item[0], item[1].qualified_name),
    )

    # Match packed RAG blocks to this batch with caller/callee symbol linking.
    packed_bundle = review_context.resolve_packed_bundle()
    symbol_links = review_context.build_symbol_link_lookup()
    has_file_contexts = bool(review_context.file_contexts)

    enclosing_candidates: list[str] = []
    caller_candidates: list[str] = []
    callee_candidates: list[str] = []
    adr_candidates: list[str] = []

    if packed_bundle is not None:
        for block in packed_bundle.blocks:
            norm_fp = normalize_agent_path(block.file_path)
            if block.tier == "enclosing_symbol":
                if norm_fp not in batch_files:
                    continue
                file_lines = batch_right_lines.get(norm_fp, set())
                if not file_lines or _range_overlaps_lines(
                    block.start_line, block.end_line, file_lines
                ):
                    enclosing_candidates.append(_format_ref_block(block))
            elif block.tier == "caller":
                linked = symbol_links.get((norm_fp, block.symbol_name), ())
                matched_links = tuple(sym for sym in linked if sym in batch_symbol_set)
                include = False
                calls: tuple[str, ...] = ()
                if matched_links:
                    include = True
                    calls = matched_links
                elif norm_fp in batch_files:
                    include = True
                else:
                    text_matches = _find_word_matches(block.rendered_text, batch_symbol_names)
                    if text_matches:
                        include = True
                        calls = text_matches
                    elif total_batches == 1 and not has_file_contexts:
                        include = True
                        calls = linked
                if include:
                    caller_candidates.append(_format_ref_block(block, calls=calls))
            elif block.tier == "callee":
                linked = symbol_links.get((norm_fp, block.symbol_name), ())
                include = False
                if norm_fp in batch_files or block.symbol_name in batch_called_set:
                    include = True
                elif any(sym in (batch_called_set | batch_symbol_set) for sym in linked):
                    include = True
                elif len(block.symbol_name) >= 3 and re.search(
                    rf"\b{re.escape(block.symbol_name)}\b",
                    batch.annotated_diff,
                ):
                    include = True
                elif total_batches == 1 and not has_file_contexts:
                    include = True
                if include:
                    callee_candidates.append(_format_ref_block(block))
            elif block.tier == "architecture_doc":
                adr_candidates.append(_format_ref_block(block))

    rules_text = review_context.resolve_custom_rules()
    rules_block = (
        f'<repo_custom_rules source=".codesight.yaml">\n'
        f"{_neutralize_rules_text(rules_text)}\n</repo_custom_rules>"
        if rules_text
        else None
    )

    # Priority admission at block/row boundaries in O(1) incremental UTF-8 byte accounting.
    max_user_bytes = max_user_tokens * 4
    current_bytes = _utf8_len(base_diff_text)
    sec2_open_bytes = 2 + _utf8_len(_SECTION_2_TITLE) + 2
    sec3_open_bytes = 2 + _utf8_len(_SECTION_3_TITLE) + 2 + _utf8_len(_SECTION_3_TABLE_HEADER) + 1
    sec4_open_bytes = 2 + _utf8_len(_SECTION_4_TITLE) + 2
    sec5_open_bytes = 2 + _utf8_len(_SECTION_5_TITLE) + 2

    accepted_enclosing: list[str] = []
    accepted_delta_rows: list[str] = []
    accepted_sec4: list[str] = []
    accepted_rules: str | None = None
    accepted_callers = 0
    accepted_callees = 0
    accepted_adrs = 0
    dropped_aux_blocks = 0
    truncated_sections: list[str] = []

    def _try_admit(delta_bytes: int) -> bool:
        nonlocal current_bytes
        if current_bytes + delta_bytes <= max_user_bytes:
            current_bytes += delta_bytes
            return True
        return False

    def _mark_dropped(section_name: str) -> None:
        nonlocal dropped_aux_blocks
        dropped_aux_blocks += 1
        if section_name not in truncated_sections:
            truncated_sections.append(section_name)

    for enc_block in enclosing_candidates:
        delta_bytes = (sec2_open_bytes if not accepted_enclosing else 2) + _utf8_len(enc_block)
        if _try_admit(delta_bytes):
            accepted_enclosing.append(enc_block)
        else:
            _mark_dropped("enclosing_symbol")

    for caller_block in caller_candidates:
        delta_bytes = (sec4_open_bytes if not accepted_sec4 else 2) + _utf8_len(caller_block)
        if _try_admit(delta_bytes):
            accepted_sec4.append(caller_block)
            accepted_callers += 1
        else:
            _mark_dropped("callers_callees_adr")

    for callee_block in callee_candidates:
        delta_bytes = (sec4_open_bytes if not accepted_sec4 else 2) + _utf8_len(callee_block)
        if _try_admit(delta_bytes):
            accepted_sec4.append(callee_block)
            accepted_callees += 1
        else:
            _mark_dropped("callers_callees_adr")

    for norm_fp, delta in sorted_deltas:
        row_text = _format_delta_row(norm_fp, delta)
        delta_bytes = (sec3_open_bytes if not accepted_delta_rows else 1) + _utf8_len(row_text)
        if _try_admit(delta_bytes):
            accepted_delta_rows.append(row_text)
        else:
            _mark_dropped("delta_cc")

    if rules_block is not None:
        if _try_admit(sec5_open_bytes + _utf8_len(rules_block)):
            accepted_rules = rules_block
        else:
            _mark_dropped("custom_rules")

    for adr_block in adr_candidates:
        delta_bytes = (sec4_open_bytes if not accepted_sec4 else 2) + _utf8_len(adr_block)
        if _try_admit(delta_bytes):
            accepted_sec4.append(adr_block)
            accepted_adrs += 1
        else:
            _mark_dropped("callers_callees_adr")

    rendered_prompt = _render_prompt_sections(
        base_diff_text=base_diff_text,
        enclosing_items=accepted_enclosing,
        delta_rows=accepted_delta_rows,
        section4_items=accepted_sec4,
        custom_rules_block=accepted_rules,
    )
    total_tokens = estimate_tokens(rendered_prompt)
    aux_tokens = max(0, total_tokens - diff_tokens)

    included: list[str] = ["diff"]
    if accepted_enclosing:
        included.append("enclosing_symbol")
    if accepted_delta_rows:
        included.append("delta_cc")
    if accepted_sec4:
        included.append("callers_callees_adr")
    if accepted_rules is not None:
        included.append("custom_rules")

    logger.debug(
        "batch_context_built",
        batch_id=batch.batch_id,
        batch_index=batch.batch_index,
        diff_tokens=diff_tokens,
        aux_tokens=aux_tokens,
        total_tokens=total_tokens,
        enclosing_count=len(accepted_enclosing),
        delta_cc_count=len(accepted_delta_rows),
        caller_count=accepted_callers,
        callee_count=accepted_callees,
        adr_count=accepted_adrs,
        has_custom_rules=accepted_rules is not None,
        dropped_aux_blocks=dropped_aux_blocks,
    )

    return BuiltBatchContext(
        batch_id=batch.batch_id,
        batch_index=batch.batch_index,
        rendered_prompt=rendered_prompt,
        diff_tokens=diff_tokens,
        aux_tokens=aux_tokens,
        total_tokens=total_tokens,
        max_user_tokens=max_user_tokens,
        included_sections=tuple(included),
        enclosing_count=len(accepted_enclosing),
        delta_cc_count=len(accepted_delta_rows),
        caller_count=accepted_callers,
        callee_count=accepted_callees,
        adr_count=accepted_adrs,
        has_custom_rules=accepted_rules is not None,
        dropped_aux_blocks=dropped_aux_blocks,
        truncated_sections=tuple(truncated_sections),
    )
