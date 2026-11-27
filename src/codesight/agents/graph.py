"""LangGraph Batch-Grouped Parallel (~3 LLM calls/PR) & Map-Reduce Send() PR review skeleton."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

import structlog
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import BaseModel, ConfigDict

from codesight.agents.llm_factory import LLMFactory
from codesight.agents.nodes.context_builder import PRReviewContext, build_batch_context
from codesight.agents.nodes.critic_agent import (
    CriticNodeResult,
    deduplicate_verified_findings,
    run_critic_agent,
)
from codesight.agents.nodes.perf_arch_auditor import run_perf_arch_auditor
from codesight.agents.nodes.security_auditor import run_security_auditor
from codesight.agents.prompts.critic_prompt import CRITIC_SYSTEM_PROMPT
from codesight.agents.prompts.perf_arch_prompt import PERF_ARCH_AUDITOR_SYSTEM_PROMPT
from codesight.agents.prompts.security_prompt import SECURITY_AUDITOR_SYSTEM_PROMPT
from codesight.agents.schemas import (
    AuditorRole,
    BatchReviewResult,
    BuiltBatchContext,
    CriticVerdict,
    DiffBatch,
    FindingCandidate,
    LLMAuditorDraftOutput,
    LLMCallMetrics,
    LLMCriticDraftOutput,
    PRReviewState,
    UnreviewableDiffSegment,
    VerifiedFinding,
)
from codesight.core.config import Settings
from codesight.core.logging import get_trace_id, sanitize_trace_id
from codesight.core.observability import create_langfuse_trace_session
from codesight.diff.file_filter import estimate_tokens, normalize_diff_path
from codesight.diff.models import DiffHunk, ParsedFileDiff, ParsedPullRequestDiff

DEFAULT_BATCH_DIFF_TOKEN_LIMIT: Final[int] = 12_000
DEFAULT_MAX_REQUEST_TOKENS: Final[int] = 24_000
DEFAULT_OUTPUT_RESERVE_TOKENS: Final[int] = 4_096

_ROLE_ORDER: Final[dict[AuditorRole, int]] = {
    "security_auditor": 0,
    "logic_performance_auditor": 1,
}

_AUDITOR_SCHEMA_TOKENS: Final[int] = estimate_tokens(
    json.dumps(LLMAuditorDraftOutput.model_json_schema())
)
_DIFF_WRAPPER_RESERVE_TOKENS: Final[int] = 32
_CRITIC_SCHEMA_TOKENS: Final[int] = estimate_tokens(
    json.dumps(LLMCriticDraftOutput.model_json_schema(), separators=(",", ":"))
)

logger = structlog.get_logger("codesight.agents.graph")


@dataclass(frozen=True, slots=True)
class ReviewGraphConfig:
    """Token-envelope configuration governing single-batch vs Map-Reduce Send() splitting."""

    batch_diff_token_limit: int = DEFAULT_BATCH_DIFF_TOKEN_LIMIT
    max_request_tokens: int = DEFAULT_MAX_REQUEST_TOKENS
    output_reserve_tokens: int = DEFAULT_OUTPUT_RESERVE_TOKENS

    def __post_init__(self) -> None:
        if self.batch_diff_token_limit < 1:
            raise ValueError("batch_diff_token_limit must be >= 1.")
        if self.max_request_tokens < 1:
            raise ValueError("max_request_tokens must be >= 1.")
        if self.output_reserve_tokens < 0:
            raise ValueError("output_reserve_tokens must be >= 0.")
        if self.output_reserve_tokens >= self.max_request_tokens:
            raise ValueError("output_reserve_tokens must be < max_request_tokens.")

    def available_auditor_diff_tokens(self) -> int:
        """Compute max diff tokens per batch after reserving instructions, schema, and output."""
        available = self.available_auditor_user_tokens() - _DIFF_WRAPPER_RESERVE_TOKENS
        if available < 1:
            raise ValueError(
                "max_request_tokens is too small to fit auditor instructions, schema, and output."
            )
        return min(self.batch_diff_token_limit, available)

    def available_auditor_user_tokens(
        self,
        *,
        system_prompt: str | None = None,
    ) -> int:
        """Compute max auditor user-message tokens after system prompt, JSON schema, and output."""
        system_tokens = (
            max(
                estimate_tokens(SECURITY_AUDITOR_SYSTEM_PROMPT),
                estimate_tokens(PERF_ARCH_AUDITOR_SYSTEM_PROMPT),
            )
            if system_prompt is None
            else estimate_tokens(system_prompt)
        )
        available = (
            self.max_request_tokens
            - self.output_reserve_tokens
            - system_tokens
            - _AUDITOR_SCHEMA_TOKENS
            - 16
        )
        if available < 1:
            raise ValueError(
                "max_request_tokens is too small to fit auditor instructions, schema, and output."
            )
        return available

    def available_critic_payload_tokens(self) -> int:
        """Compute max Critic user-message tokens after reserving instructions, schema, output."""
        prompt_overhead = estimate_tokens(CRITIC_SYSTEM_PROMPT) + _CRITIC_SCHEMA_TOKENS + 64
        available = self.max_request_tokens - self.output_reserve_tokens - prompt_overhead
        if available < 1:
            raise ValueError(
                "max_request_tokens is too small to fit critic instructions, schema, and output."
            )
        return available


# ---------------------------------------------------------------------------
# Hierarchical File -> Hunk -> Line Diff Splitter
# ---------------------------------------------------------------------------


def _file_header(parsed_file: ParsedFileDiff) -> str:
    return f"=== FILE: {parsed_file.file_path} ({parsed_file.change_type}) ==="


def _hunk_left_deleted_hunks(hunk: DiffHunk) -> dict[int, int]:
    return {
        dl.left_line: hunk.hunk_id
        for dl in hunk.lines
        if dl.kind == "delete" and dl.left_line is not None
    }


def _file_left_deleted_hunks(parsed_file: ParsedFileDiff) -> dict[int, int]:
    merged: dict[int, int] = {}
    for hunk in parsed_file.hunks:
        merged.update(_hunk_left_deleted_hunks(hunk))
    return merged


class _BatchAccumulator:
    """Mutable accumulator packing ordered (file_path, file_header, chunk_text) pieces in O(1)."""

    def __init__(self, max_batch_tokens: int) -> None:
        self._max_batch_tokens = max_batch_tokens
        self._groups: list[tuple[str, str, list[str]]] = []
        self._line_hunks_by_file: dict[str, dict[int, int]] = {}
        self._left_deleted_hunks_by_file: dict[str, dict[int, int]] = {}
        self._total_bytes: int = 0
        self._batches: list[DiffBatch] = []

    def _render_groups(self, groups: Sequence[tuple[str, str, Sequence[str]]]) -> str:
        blocks = [f"{hdr}\n" + "\n".join(chunks) for _, hdr, chunks in groups]
        return "\n\n".join(blocks)

    def has_content(self) -> bool:
        return bool(self._groups)

    def try_add_chunk(
        self,
        file_path: str,
        file_header: str,
        chunk_text: str,
        *,
        line_hunks: Mapping[int, int] | None = None,
        left_deleted_hunks: Mapping[int, int] | None = None,
    ) -> bool:
        chunk_bytes = len(chunk_text.encode("utf-8"))
        same_file = bool(self._groups and self._groups[-1][0] == file_path)
        if not self._groups:
            hdr_bytes = len(file_header.encode("utf-8"))
            new_total_bytes = hdr_bytes + 1 + chunk_bytes
        elif same_file:
            new_total_bytes = self._total_bytes + 1 + chunk_bytes
        else:
            hdr_bytes = len(file_header.encode("utf-8"))
            new_total_bytes = self._total_bytes + 2 + hdr_bytes + 1 + chunk_bytes

        new_tokens = (new_total_bytes + 3) // 4 if new_total_bytes > 0 else 0
        if new_tokens <= self._max_batch_tokens:
            if same_file:
                self._groups[-1][2].append(chunk_text)
            else:
                self._groups.append((file_path, file_header, [chunk_text]))
            norm_fp = normalize_diff_path(file_path).rstrip("/")
            if norm_fp:
                if line_hunks:
                    self._line_hunks_by_file.setdefault(norm_fp, {}).update(line_hunks)
                if left_deleted_hunks:
                    self._left_deleted_hunks_by_file.setdefault(norm_fp, {}).update(
                        left_deleted_hunks
                    )
            self._total_bytes = new_total_bytes
            return True
        return False

    def flush(self) -> None:
        if not self._groups:
            return
        rendered = self._render_groups(self._groups)
        file_paths = tuple(dict.fromkeys(fp for fp, _, _ in self._groups))
        idx = len(self._batches)
        self._batches.append(
            DiffBatch(
                batch_index=idx,
                batch_id=f"batch-{idx:03d}",
                file_paths=file_paths,
                annotated_diff=rendered,
                estimated_tokens=estimate_tokens(rendered),
                right_line_hunks_by_file={
                    fp: dict(lh) for fp, lh in self._line_hunks_by_file.items() if lh
                },
                left_deleted_hunks_by_file={
                    fp: dict(lh) for fp, lh in self._left_deleted_hunks_by_file.items() if lh
                },
            )
        )
        self._groups = []
        self._line_hunks_by_file = {}
        self._left_deleted_hunks_by_file = {}
        self._total_bytes = 0

    def finalize(self) -> tuple[DiffBatch, ...]:
        self.flush()
        return tuple(self._batches)


def _split_oversized_hunk_by_lines(
    *,
    parsed_file: ParsedFileDiff,
    hunk: DiffHunk,
    accumulator: _BatchAccumulator,
    max_batch_tokens: int,
    unreviewable: list[UnreviewableDiffSegment],
) -> None:
    hdr = _file_header(parsed_file)
    hunk_header = hunk.annotated_text.split("\n", 1)[0]
    prefix_bytes = len(f"{hdr}\n{hunk_header}".encode())

    pending_lines: list[str] = []
    pending_line_hunks: dict[int, int] = {}
    pending_left_deleted_hunks: dict[int, int] = {}
    pending_bytes = 0

    def _flush_pending_lines() -> None:
        nonlocal pending_lines, pending_line_hunks, pending_left_deleted_hunks, pending_bytes
        if not pending_lines:
            return
        chunk = hunk_header + "\n" + "\n".join(pending_lines)
        if not accumulator.try_add_chunk(
            parsed_file.file_path,
            hdr,
            chunk,
            line_hunks=pending_line_hunks,
            left_deleted_hunks=pending_left_deleted_hunks,
        ):
            accumulator.flush()
            added = accumulator.try_add_chunk(
                parsed_file.file_path,
                hdr,
                chunk,
                line_hunks=pending_line_hunks,
                left_deleted_hunks=pending_left_deleted_hunks,
            )
            if not added:  # pragma: no cover - guarded by standalone check below
                raise RuntimeError("Line-split chunk unexpectedly exceeded batch token budget.")
        pending_lines = []
        pending_line_hunks = {}
        pending_left_deleted_hunks = {}
        pending_bytes = 0

    for diff_line in hunk.lines:
        line_bytes = len(diff_line.llm_text.encode("utf-8"))
        standalone_bytes = prefix_bytes + 1 + line_bytes
        standalone_tokens = (standalone_bytes + 3) // 4
        if standalone_tokens > max_batch_tokens:
            _flush_pending_lines()
            unreviewable.append(
                UnreviewableDiffSegment(
                    file_path=parsed_file.file_path,
                    hunk_id=hunk.hunk_id,
                    right_line=diff_line.right_line,
                    left_line=diff_line.left_line,
                    estimated_tokens=standalone_tokens,
                    reason="single_line_exceeds_batch_token_budget",
                )
            )
            continue

        trial_bytes = (
            prefix_bytes + 1 + line_bytes if not pending_lines else pending_bytes + 1 + line_bytes
        )
        if ((trial_bytes + 3) // 4) <= max_batch_tokens:
            pending_lines.append(diff_line.llm_text)
            if diff_line.right_line is not None:
                pending_line_hunks[diff_line.right_line] = hunk.hunk_id
            if diff_line.kind == "delete" and diff_line.left_line is not None:
                pending_left_deleted_hunks[diff_line.left_line] = hunk.hunk_id
            pending_bytes = trial_bytes
        else:
            _flush_pending_lines()
            pending_lines = [diff_line.llm_text]
            pending_line_hunks = (
                {diff_line.right_line: hunk.hunk_id} if diff_line.right_line is not None else {}
            )
            pending_left_deleted_hunks = (
                {diff_line.left_line: hunk.hunk_id}
                if diff_line.kind == "delete" and diff_line.left_line is not None
                else {}
            )
            pending_bytes = standalone_bytes

    _flush_pending_lines()


def split_parsed_diff_into_batches(
    parsed_diff: ParsedPullRequestDiff,
    *,
    max_batch_tokens: int = DEFAULT_BATCH_DIFF_TOKEN_LIMIT,
) -> tuple[tuple[DiffBatch, ...], tuple[UnreviewableDiffSegment, ...]]:
    """Partition a filtered ParsedPullRequestDiff hierarchically (file -> hunk -> line)."""
    if max_batch_tokens < 1:
        raise ValueError("max_batch_tokens must be >= 1.")

    reviewable_files = [
        f
        for f in parsed_diff.files
        if f.change_type != "deleted" and f.hunks and f.annotated_diff.strip()
    ]
    if not reviewable_files:
        return (), ()

    full_diff = "\n\n".join(f.annotated_diff for f in reviewable_files)
    total_tokens = estimate_tokens(full_diff)
    if total_tokens <= max_batch_tokens:
        left_deleted_by_file: dict[str, dict[int, int]] = {}
        for f in reviewable_files:
            left_map = _file_left_deleted_hunks(f)
            norm_fp = normalize_diff_path(f.file_path).rstrip("/")
            if norm_fp and left_map:
                left_deleted_by_file[norm_fp] = left_map
        single_batch = DiffBatch(
            batch_index=0,
            batch_id="batch-000",
            file_paths=tuple(f.file_path for f in reviewable_files),
            annotated_diff=full_diff,
            estimated_tokens=total_tokens,
            right_line_hunks_by_file={
                normalize_diff_path(f.file_path).rstrip("/"): dict(f.right_line_to_hunk)
                for f in reviewable_files
                if f.right_line_to_hunk
            },
            left_deleted_hunks_by_file=left_deleted_by_file,
        )
        return (single_batch,), ()

    accumulator = _BatchAccumulator(max_batch_tokens=max_batch_tokens)
    unreviewable: list[UnreviewableDiffSegment] = []

    for parsed_file in reviewable_files:
        hdr = _file_header(parsed_file)
        file_hunks_body = "\n".join(h.annotated_text for h in parsed_file.hunks)
        file_left_deleted = _file_left_deleted_hunks(parsed_file)

        # 1. File-level packing: keep whole files together whenever possible.
        if accumulator.try_add_chunk(
            parsed_file.file_path,
            hdr,
            file_hunks_body,
            line_hunks=parsed_file.right_line_to_hunk,
            left_deleted_hunks=file_left_deleted,
        ):
            continue
        if estimate_tokens(parsed_file.annotated_diff) <= max_batch_tokens:
            accumulator.flush()
            accumulator.try_add_chunk(
                parsed_file.file_path,
                hdr,
                file_hunks_body,
                line_hunks=parsed_file.right_line_to_hunk,
                left_deleted_hunks=file_left_deleted,
            )
            continue

        # 2. Hunk-level packing: split oversized file at hunk boundaries.
        for hunk in parsed_file.hunks:
            hunk_line_hunks = {
                dl.right_line: hunk.hunk_id for dl in hunk.lines if dl.right_line is not None
            }
            hunk_left_deleted = _hunk_left_deleted_hunks(hunk)
            if accumulator.try_add_chunk(
                parsed_file.file_path,
                hdr,
                hunk.annotated_text,
                line_hunks=hunk_line_hunks,
                left_deleted_hunks=hunk_left_deleted,
            ):
                continue
            if estimate_tokens(f"{hdr}\n{hunk.annotated_text}") <= max_batch_tokens:
                accumulator.flush()
                accumulator.try_add_chunk(
                    parsed_file.file_path,
                    hdr,
                    hunk.annotated_text,
                    line_hunks=hunk_line_hunks,
                    left_deleted_hunks=hunk_left_deleted,
                )
                continue

            # 3. Line-level packing: split oversized hunk at line boundaries preserving L-numbers.
            accumulator.flush()
            _split_oversized_hunk_by_lines(
                parsed_file=parsed_file,
                hunk=hunk,
                accumulator=accumulator,
                max_batch_tokens=max_batch_tokens,
                unreviewable=unreviewable,
            )

    return accumulator.finalize(), tuple(unreviewable)


# ---------------------------------------------------------------------------
# Prompt Builders, Candidate Normalization & Critic Budget Guard
# ---------------------------------------------------------------------------


def _sort_findings(findings: Sequence[FindingCandidate]) -> list[FindingCandidate]:
    return sorted(
        findings,
        key=lambda f: (
            f.batch_index,
            _ROLE_ORDER[f.role],
            f.file_path,
            f.start_line,
            f.end_line,
            f.candidate_id,
        ),
    )


def _sort_verdicts(verdicts: Sequence[CriticVerdict]) -> list[CriticVerdict]:
    return sorted(verdicts, key=lambda v: (v.batch_index, v.candidate_id))


def _node_config(
    config: RunnableConfig | None,
    *,
    role: str,
    batch: DiffBatch,
) -> RunnableConfig:
    base: dict[str, Any] = dict(config) if config is not None else {}
    existing_meta = dict(cast(Mapping[str, Any], base.get("metadata") or {}))
    existing_meta.update(
        {
            "role": role,
            "batch_index": batch.batch_index,
            "batch_id": batch.batch_id,
        }
    )
    base["metadata"] = existing_meta
    base["run_name"] = f"{role}_{batch.batch_id}"
    return cast(RunnableConfig, base)


# ---------------------------------------------------------------------------
# LangGraph Construction & Async Entry Point
# ---------------------------------------------------------------------------


class _BatchWorkerInput(BaseModel):
    """Input payload dispatched to each Map-Reduce `Send('process_diff_batch', ...)` branch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    batch: DiffBatch
    batch_context: BuiltBatchContext | None = None


def build_pr_review_graph(
    llm_factory: LLMFactory,
    *,
    graph_config: ReviewGraphConfig | None = None,
    review_context: PRReviewContext | None = None,
) -> Any:
    """Compile the in-memory LangGraph StateGraph supporting single-batch and Send() Map-Reduce."""
    cfg = graph_config if graph_config is not None else ReviewGraphConfig()
    auditor_user_budget = cfg.available_auditor_user_tokens()
    critic_user_budget = cfg.available_critic_payload_tokens()

    async def _run_auditor(
        role: AuditorRole,
        batch: DiffBatch,
        batch_context: BuiltBatchContext | None,
        config: RunnableConfig | None,
    ) -> tuple[list[FindingCandidate], LLMCallMetrics, int]:
        effective_ctx = (
            batch_context
            if batch_context is not None
            else build_batch_context(
                batch=batch,
                max_user_tokens=auditor_user_budget,
                review_context=review_context,
                total_batches=1,
            )
        )
        node_cfg = _node_config(config, role=role, batch=batch)
        if role == "security_auditor":
            return await run_security_auditor(
                batch=batch,
                batch_context=effective_ctx,
                llm_factory=llm_factory,
                config=node_cfg,
            )
        return await run_perf_arch_auditor(
            batch=batch,
            batch_context=effective_ctx,
            llm_factory=llm_factory,
            config=node_cfg,
        )

    async def _run_critic(
        batch: DiffBatch,
        candidates: Sequence[FindingCandidate],
        batch_context: BuiltBatchContext | None,
        config: RunnableConfig | None,
    ) -> CriticNodeResult:
        return await run_critic_agent(
            batch=batch,
            candidates=candidates,
            llm_factory=llm_factory,
            max_user_tokens=critic_user_budget,
            batch_context=batch_context,
            review_context=review_context,
            config=_node_config(config, role="critic", batch=batch),
        )

    async def plan_batches(state: PRReviewState) -> dict[str, Any]:
        if not state.batches:
            return {"batching_mode": "empty"}
        if len(state.batches) == 1:
            return {"batching_mode": "single_batch"}
        return {"batching_mode": "map_reduce"}

    def route_after_plan(state: PRReviewState) -> str:
        if state.batching_mode == "empty" or not state.batches:
            return "finalize_review"
        return "context_builder"

    async def context_builder(state: PRReviewState) -> dict[str, Any]:
        total_batches = max(1, len(state.batches))
        built = {
            batch.batch_id: build_batch_context(
                batch=batch,
                max_user_tokens=auditor_user_budget,
                review_context=review_context,
                total_batches=total_batches,
            )
            for batch in state.batches
        }
        return {"batch_contexts": built}

    def route_after_context(state: PRReviewState) -> list[str] | list[Send]:
        if state.batching_mode == "single_batch":
            # Parallel superstep fan-out -> fan-in to critic_synthesis (3 LLM calls total).
            return ["security_auditor", "logic_performance_auditor"]
        return [
            Send(
                "process_diff_batch",
                _BatchWorkerInput(
                    batch=batch,
                    batch_context=state.batch_contexts.get(batch.batch_id),
                ),
            )
            for batch in state.batches
        ]

    async def security_auditor(
        state: PRReviewState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        batch = state.batches[0]
        findings, metrics, dropped = await _run_auditor(
            "security_auditor",
            batch,
            state.batch_contexts.get(batch.batch_id),
            config,
        )
        return {
            "security_findings": findings,
            "dropped_invalid_findings_count": dropped,
            "llm_metrics": [metrics],
        }

    async def logic_performance_auditor(
        state: PRReviewState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        batch = state.batches[0]
        findings, metrics, dropped = await _run_auditor(
            "logic_performance_auditor",
            batch,
            state.batch_contexts.get(batch.batch_id),
            config,
        )
        return {
            "logic_findings": findings,
            "dropped_invalid_findings_count": dropped,
            "llm_metrics": [metrics],
        }

    async def critic_synthesis(
        state: PRReviewState,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        batch = state.batches[0]
        combined = _sort_findings([*state.security_findings, *state.logic_findings])
        critic_res = await _run_critic(
            batch,
            combined,
            state.batch_contexts.get(batch.batch_id),
            config,
        )
        ordered_verdicts = _sort_verdicts(critic_res.verdicts)
        batch_result = BatchReviewResult(
            batch_index=batch.batch_index,
            batch_id=batch.batch_id,
            findings=tuple(combined),
            verdicts=tuple(ordered_verdicts),
            verified_findings=tuple(critic_res.verified_findings),
            stripped_suggestions_count=critic_res.stripped_suggestions_count,
            truncated_candidates_count=len(critic_res.truncated_ids),
            truncated_candidate_ids=critic_res.truncated_ids,
            dropped_invalid_findings_count=state.dropped_invalid_findings_count,
            dropped_invalid_verdicts_count=critic_res.dropped_verdicts,
        )
        return {
            "batch_results": [batch_result],
            "merged_findings": combined,
            "merged_verdicts": ordered_verdicts,
            "verified_findings": critic_res.verified_findings,
            "stripped_suggestions_count": critic_res.stripped_suggestions_count,
            "llm_metrics": [critic_res.metrics] if critic_res.metrics is not None else [],
        }

    async def process_diff_batch(
        state: _BatchWorkerInput,
        config: RunnableConfig,
    ) -> dict[str, Any]:
        batch = state.batch
        sec_task = asyncio.create_task(
            _run_auditor("security_auditor", batch, state.batch_context, config)
        )
        log_task = asyncio.create_task(
            _run_auditor("logic_performance_auditor", batch, state.batch_context, config)
        )
        try:
            (
                (sec_findings, sec_metrics, sec_dropped),
                (log_findings, log_metrics, log_dropped),
            ) = await asyncio.gather(sec_task, log_task)
        except BaseException:
            for task in (sec_task, log_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sec_task, log_task, return_exceptions=True)
            raise
        combined = _sort_findings([*sec_findings, *log_findings])
        critic_res = await _run_critic(batch, combined, state.batch_context, config)
        ordered_verdicts = _sort_verdicts(critic_res.verdicts)
        total_dropped_findings = sec_dropped + log_dropped
        batch_result = BatchReviewResult(
            batch_index=batch.batch_index,
            batch_id=batch.batch_id,
            findings=tuple(combined),
            verdicts=tuple(ordered_verdicts),
            verified_findings=tuple(critic_res.verified_findings),
            stripped_suggestions_count=critic_res.stripped_suggestions_count,
            truncated_candidates_count=len(critic_res.truncated_ids),
            truncated_candidate_ids=critic_res.truncated_ids,
            dropped_invalid_findings_count=total_dropped_findings,
            dropped_invalid_verdicts_count=critic_res.dropped_verdicts,
        )
        llm_metrics: list[LLMCallMetrics] = [sec_metrics, log_metrics]
        if critic_res.metrics is not None:
            llm_metrics.append(critic_res.metrics)
        return {
            "security_findings": sec_findings,
            "logic_findings": log_findings,
            "dropped_invalid_findings_count": total_dropped_findings,
            "batch_results": [batch_result],
            "llm_metrics": llm_metrics,
        }

    async def reduce_batches(state: PRReviewState) -> dict[str, Any]:
        ordered_batches = sorted(state.batch_results, key=lambda b: b.batch_index)
        merged_findings = _sort_findings(
            [finding for br in ordered_batches for finding in br.findings]
        )
        merged_verdicts = _sort_verdicts(
            [verdict for br in ordered_batches for verdict in br.verdicts]
        )
        raw_verified: list[VerifiedFinding] = [
            vf for br in ordered_batches for vf in br.verified_findings
        ]
        deduped_verified = deduplicate_verified_findings(raw_verified)
        stripped_total = sum(br.stripped_suggestions_count for br in ordered_batches)
        return {
            "merged_findings": merged_findings,
            "merged_verdicts": merged_verdicts,
            "verified_findings": deduped_verified,
            "stripped_suggestions_count": stripped_total,
        }

    async def finalize_review(state: PRReviewState) -> dict[str, Any]:
        if state.batching_mode == "empty":
            if state.unreviewable_segments:
                return {"status": "unreviewable_only"}
            return {"status": "empty_diff"}
        return {"status": "completed"}

    graph: StateGraph[PRReviewState] = StateGraph(PRReviewState)
    graph.add_node("plan_batches", plan_batches)
    graph.add_node("context_builder", context_builder)
    graph.add_node("security_auditor", security_auditor)
    graph.add_node("logic_performance_auditor", logic_performance_auditor)
    graph.add_node("critic_synthesis", critic_synthesis)
    graph.add_node("process_diff_batch", process_diff_batch, input_schema=_BatchWorkerInput)
    graph.add_node("reduce_batches", reduce_batches)
    graph.add_node("finalize_review", finalize_review)

    graph.add_edge(START, "plan_batches")
    graph.add_conditional_edges(
        "plan_batches",
        route_after_plan,
        ["context_builder", "finalize_review"],
    )
    graph.add_conditional_edges(
        "context_builder",
        route_after_context,
        ["security_auditor", "logic_performance_auditor", "process_diff_batch"],
    )
    graph.add_edge("security_auditor", "critic_synthesis")
    graph.add_edge("logic_performance_auditor", "critic_synthesis")
    graph.add_edge("critic_synthesis", "finalize_review")
    graph.add_edge("process_diff_batch", "reduce_batches")
    graph.add_edge("reduce_batches", "finalize_review")
    graph.add_edge("finalize_review", END)

    # In-memory execution only — no persistent checkpointer (NFR-3 Zero-Code Persistence).
    return graph.compile()


async def run_pr_review_graph(
    *,
    parsed_diff: ParsedPullRequestDiff,
    llm_factory: LLMFactory,
    graph_config: ReviewGraphConfig | None = None,
    review_context: PRReviewContext | None = None,
    pr_id: str = "pr-unknown",
    head_sha: str = "",
    trace_id: str | None = None,
    settings: Settings | None = None,
    span_exporter: Any = None,
    telemetry_flush_timeout_seconds: float = 2.0,
) -> PRReviewState:
    """Execute the full in-memory LangGraph PR review pipeline on a ParsedPullRequestDiff."""
    cfg = graph_config if graph_config is not None else ReviewGraphConfig()
    effective_settings = settings if settings is not None else llm_factory.settings
    raw_trace = trace_id if trace_id is not None else get_trace_id()
    if raw_trace is not None and raw_trace.strip() in {"", "-"}:
        raw_trace = None
    effective_trace_id = sanitize_trace_id(raw_trace)

    effective_batch_limit = cfg.available_auditor_diff_tokens()
    batches, unreviewable = split_parsed_diff_into_batches(
        parsed_diff,
        max_batch_tokens=effective_batch_limit,
    )
    total_diff_tokens = estimate_tokens(parsed_diff.annotated_diff)

    initial_state = PRReviewState(
        pr_id=pr_id,
        head_sha=head_sha[:64],
        trace_id=effective_trace_id,
        total_diff_tokens=total_diff_tokens,
        batching_mode=(
            "empty" if not batches else ("single_batch" if len(batches) == 1 else "map_reduce")
        ),
        batches=list(batches),
        unreviewable_segments=list(unreviewable),
    )

    compiled = build_pr_review_graph(
        llm_factory,
        graph_config=cfg,
        review_context=review_context,
    )
    telemetry_session = create_langfuse_trace_session(
        settings=effective_settings,
        trace_id=effective_trace_id,
        span_exporter=span_exporter,
    )

    run_config: RunnableConfig = {
        "run_name": "codesight_pr_review",
        "metadata": {
            "pr_id": pr_id,
            "head_sha": head_sha[:64],
            "trace_id": effective_trace_id,
            "batching_mode": initial_state.batching_mode,
            "provider": llm_factory.provider,
        },
        "callbacks": list(telemetry_session.callbacks),
    }

    was_cancelled = False
    final_telemetry = telemetry_session.telemetry_status
    try:
        raw_final = await compiled.ainvoke(initial_state, config=run_config)
        final_state = (
            raw_final
            if isinstance(raw_final, PRReviewState)
            else PRReviewState.model_validate(raw_final)
        )
    except asyncio.CancelledError:
        was_cancelled = True
        raise
    finally:
        if not was_cancelled:
            final_telemetry = await telemetry_session.aflush_and_close(
                timeout_seconds=telemetry_flush_timeout_seconds
            )

    final_state = final_state.model_copy(update={"telemetry": final_telemetry})
    truncated_total = sum(br.truncated_candidates_count for br in final_state.batch_results)
    dropped_findings_total = sum(
        br.dropped_invalid_findings_count for br in final_state.batch_results
    )
    dropped_verdicts_total = sum(
        br.dropped_invalid_verdicts_count for br in final_state.batch_results
    )
    logger.info(
        "pr_review_graph_completed",
        trace_id=effective_trace_id,
        pr_id=pr_id,
        batching_mode=final_state.batching_mode,
        batch_count=len(final_state.batches),
        unreviewable_count=len(final_state.unreviewable_segments),
        finding_count=len(final_state.merged_findings),
        verdict_count=len(final_state.merged_verdicts),
        verified_finding_count=len(final_state.verified_findings),
        stripped_suggestions_count=final_state.stripped_suggestions_count,
        truncated_candidates_count=truncated_total,
        dropped_invalid_findings_count=dropped_findings_total,
        dropped_invalid_verdicts_count=dropped_verdicts_total,
        llm_call_count=len(final_state.llm_metrics),
        telemetry_status=final_telemetry.status,
    )
    return final_state
