"""Pydantic v2 structured output schemas and LangGraph review state models."""

from __future__ import annotations

import math
import operator
import re
from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from codesight.vcs.github_publisher import ReviewCommentDraft

type LLMRole = Literal["security_auditor", "logic_performance_auditor", "critic"]
type AuditorRole = Literal["security_auditor", "logic_performance_auditor"]
type FindingSeverity = Literal["CRITICAL", "WARNING", "INFO"]
type CriticDecision = Literal["APPROVE", "MODIFY", "REJECT"]
type BatchingMode = Literal["empty", "single_batch", "map_reduce"]
type ReviewRunStatus = Literal["pending", "empty_diff", "unreviewable_only", "completed"]
type TelemetryState = Literal["disabled", "active", "degraded"]


_BACKTICK_RUN_RE = re.compile(r"`+")
_SUGGESTION_FENCE_RE = re.compile(
    r"^([ \t]*(?:`{3,}|~{3,}))[ \t]*suggestion\b",
    re.MULTILINE | re.IGNORECASE,
)


def _neutralize_suggestion_fences(text: str) -> str:
    return _SUGGESTION_FENCE_RE.sub(r"\1", text)


def _reject_bool_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Line number or integer index must be a strict integer, not bool.")
    return value


def _validate_finite_float(value: Any, *, allow_none: bool = False) -> float | None:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Confidence must be a finite numeric value, not bool.")
    num = float(value)
    if not math.isfinite(num):
        raise ValueError("Confidence must be a finite number.")
    return num


def _format_suggestion_fence(replacement_code: str) -> str:
    cleaned = replacement_code.replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    max_ticks = max((len(m.group(0)) for m in _BACKTICK_RUN_RE.finditer(cleaned)), default=0)
    fence = "`" * max(3, max_ticks + 1)
    return f"{fence}suggestion\n{cleaned}\n{fence}"


def _strip_non_empty_str(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("Field value must be a string.")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError("Field value must not be empty or whitespace.")
    return cleaned


class CodeSuggestion(BaseModel):
    """Structured inline fix suggestion bounded to an ordered RIGHT-side line range."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    replacement_code: str
    explanation: str | None = None

    @field_validator("start_line", "end_line", mode="before")
    @classmethod
    def _validate_strict_lines(cls, value: Any) -> int:
        return _reject_bool_int(value)

    @model_validator(mode="after")
    def _validate_line_order(self) -> Self:
        if self.start_line > self.end_line:
            raise ValueError("start_line must be <= end_line.")
        return self

    @property
    def is_single_line(self) -> bool:
        """Return True when the suggestion targets a single line on the RIGHT side."""
        return self.start_line == self.end_line


class FindingCandidate(BaseModel):
    """Candidate review finding emitted by an Auditor agent for Critic verification."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(default="pending", min_length=1)
    role: AuditorRole
    batch_index: int = Field(default=0, ge=0)
    file_path: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    severity: FindingSeverity
    category: str = Field(min_length=1)
    title: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    confidence: float = Field(default=0.8, ge=0.0, le=1.0)
    suggestion: CodeSuggestion | None = None

    @field_validator("batch_index", "start_line", "end_line", mode="before")
    @classmethod
    def _validate_strict_ints(cls, value: Any) -> int:
        return _reject_bool_int(value)

    @field_validator("candidate_id", "file_path", "category", "title", "evidence", mode="before")
    @classmethod
    def _validate_non_empty_strings(cls, value: Any) -> str:
        return _strip_non_empty_str(value)

    @model_validator(mode="after")
    def _validate_line_order(self) -> Self:
        if self.start_line > self.end_line:
            raise ValueError("start_line must be <= end_line.")
        return self

    def to_github_line_coords(self) -> tuple[int | None, int]:
        """Return GitHub Review API `(start_line, line)` omitting `start_line` on single-line."""
        if self.start_line == self.end_line:
            return None, self.end_line
        return self.start_line, self.end_line


class CriticVerdict(BaseModel):
    """Verification verdict emitted by the Critic agent referencing a FindingCandidate."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(min_length=1)
    batch_index: int = Field(default=0, ge=0)
    decision: CriticDecision
    rationale: str = Field(min_length=1)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    adjusted_severity: FindingSeverity | None = None
    suggestion: CodeSuggestion | None = None

    @field_validator("batch_index", mode="before")
    @classmethod
    def _validate_batch_index(cls, value: Any) -> int:
        return _reject_bool_int(value)

    @field_validator("candidate_id", "rationale", mode="before")
    @classmethod
    def _validate_text_fields(cls, value: Any) -> str:
        return _strip_non_empty_str(value)

    @field_validator("confidence", mode="before")
    @classmethod
    def _validate_confidence(cls, value: Any) -> float | None:
        return _validate_finite_float(value, allow_none=True)


class VerifiedFinding(BaseModel):
    """Zero-noise finding approved by Critic with syntax-validated suggestion (CA-20)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: str = Field(min_length=1)
    role: AuditorRole
    batch_index: int = Field(default=0, ge=0)
    file_path: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    severity: FindingSeverity
    category: str = Field(min_length=1)
    title: str = Field(min_length=1)
    evidence: str = Field(min_length=1)
    confidence: float = Field(ge=0.75, le=1.0)
    rationale: str = Field(min_length=1)
    suggestion: CodeSuggestion | None = None
    suggestion_explanation: str | None = None
    suggestion_stripped_reason: str | None = None

    @field_validator("batch_index", "start_line", "end_line", mode="before")
    @classmethod
    def _validate_strict_ints(cls, value: Any) -> int:
        return _reject_bool_int(value)

    @field_validator(
        "candidate_id",
        "file_path",
        "category",
        "title",
        "evidence",
        "rationale",
        mode="before",
    )
    @classmethod
    def _validate_non_empty_strings(cls, value: Any) -> str:
        return _strip_non_empty_str(value)

    @field_validator("confidence", mode="before")
    @classmethod
    def _validate_confidence(cls, value: Any) -> float:
        num = _validate_finite_float(value, allow_none=False)
        assert num is not None
        return num

    @model_validator(mode="after")
    def _validate_line_order(self) -> Self:
        if self.start_line > self.end_line:
            raise ValueError("start_line must be <= end_line.")
        return self

    def to_github_line_coords(self) -> tuple[int | None, int]:
        """Align GitHub inline `(start_line, line)` to validated suggestion span when present."""
        target_start = (
            self.suggestion.start_line if self.suggestion is not None else self.start_line
        )
        target_end = self.suggestion.end_line if self.suggestion is not None else self.end_line
        if target_start == target_end:
            return None, target_end
        return target_start, target_end

    def render_markdown_body(self) -> str:
        """Render PR comment Markdown keeping explanation even when suggestion was stripped."""
        safe_title = _neutralize_suggestion_fences(self.title)
        safe_evidence = _neutralize_suggestion_fences(self.evidence)
        safe_rationale = _neutralize_suggestion_fences(self.rationale)
        parts: list[str] = [f"**[{self.severity}] {safe_title}**", safe_evidence]
        if safe_rationale.strip() and safe_rationale.strip() != safe_evidence.strip():
            parts.append(f"**Verification:** {safe_rationale}")
        explanation = (
            self.suggestion.explanation
            if self.suggestion is not None and self.suggestion.explanation
            else self.suggestion_explanation
        )
        if explanation and explanation.strip():
            parts.append(_neutralize_suggestion_fences(explanation.strip()))
        if self.suggestion is not None:
            parts.append(_format_suggestion_fence(self.suggestion.replacement_code))
        return "\n\n".join(parts)

    def to_review_comment_draft(self) -> ReviewCommentDraft:
        """Convert to GitHub `ReviewCommentDraft` anchored on the validated RIGHT-side span."""
        from codesight.vcs.github_publisher import ReviewCommentDraft

        start_line, line = self.to_github_line_coords()
        return ReviewCommentDraft(
            path=self.file_path,
            body=self.render_markdown_body(),
            line=line,
            start_line=start_line,
            severity=self.severity,
        )


class AuditorOutput(BaseModel):
    """Structured output contract returned by a Security or Logic/Performance Auditor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    findings: list[FindingCandidate] = Field(default_factory=list)


class CriticOutput(BaseModel):
    """Structured output contract returned by the Critic agent."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    verdicts: list[CriticVerdict] = Field(default_factory=list)


class LLMCodeSuggestionDraft(BaseModel):
    """Lenient LLM-facing suggestion draft validated per-item before domain conversion."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    start_line: int | None = None
    end_line: int | None = None
    replacement_code: str = ""
    explanation: str | None = None


class LLMFindingDraft(BaseModel):
    """Lenient LLM-facing finding draft validated per-item so one bad entry cannot fail a batch."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    file_path: str = ""
    start_line: int | None = None
    end_line: int | None = None
    severity: str = Field(default="", description="Severity level: CRITICAL, WARNING, or INFO.")
    category: str = ""
    title: str = ""
    evidence: str = ""
    confidence: float = 0.8
    suggestion: LLMCodeSuggestionDraft | None = None


class LLMAuditorDraftOutput(BaseModel):
    """Lenient structured output schema bound to Auditor LLMs."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    findings: list[LLMFindingDraft] = Field(default_factory=list)


class LLMCriticVerdictDraft(BaseModel):
    """Lenient LLM-facing Critic verdict draft validated and reconciled per-item."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    candidate_id: str = ""
    decision: str = Field(
        default="",
        description="Critic verdict decision: must be APPROVE, MODIFY, or REJECT.",
    )
    rationale: str = ""
    confidence: float | None = Field(
        default=None,
        description="Confidence score between 0.0 and 1.0 (findings < 0.75 are dropped).",
    )
    adjusted_severity: str | None = Field(
        default=None,
        description="Canonical severity: CRITICAL, WARNING, or INFO.",
    )
    suggestion: LLMCodeSuggestionDraft | None = None


class LLMCriticDraftOutput(BaseModel):
    """Lenient structured output schema bound to the Critic LLM."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    verdicts: list[LLMCriticVerdictDraft] = Field(default_factory=list)


class DiffBatch(BaseModel):
    """Bounded, line-annotated diff slice routed to a single-batch or Map-Reduce branch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_index: int = Field(ge=0)
    batch_id: str = Field(min_length=1)
    file_paths: tuple[str, ...] = ()
    annotated_diff: str = Field(min_length=1)
    estimated_tokens: int = Field(ge=0)
    right_line_hunks_by_file: dict[str, dict[int, int]] = Field(default_factory=dict)
    left_deleted_hunks_by_file: dict[str, dict[int, int]] = Field(default_factory=dict)

    @field_validator("batch_index", "estimated_tokens", mode="before")
    @classmethod
    def _validate_ints(cls, value: Any) -> int:
        return _reject_bool_int(value)


class BuiltBatchContext(BaseModel):
    """Zero-LLM structured prompt context assembled for one DiffBatch within token budget."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str = Field(min_length=1)
    batch_index: int = Field(ge=0)
    rendered_prompt: str = Field(min_length=1)
    diff_tokens: int = Field(ge=0)
    aux_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    max_user_tokens: int = Field(ge=1)
    included_sections: tuple[str, ...] = ()
    enclosing_count: int = Field(default=0, ge=0)
    delta_cc_count: int = Field(default=0, ge=0)
    caller_count: int = Field(default=0, ge=0)
    callee_count: int = Field(default=0, ge=0)
    adr_count: int = Field(default=0, ge=0)
    has_custom_rules: bool = False
    dropped_aux_blocks: int = Field(default=0, ge=0)
    truncated_sections: tuple[str, ...] = ()

    @field_validator(
        "batch_index",
        "diff_tokens",
        "aux_tokens",
        "total_tokens",
        "max_user_tokens",
        "enclosing_count",
        "delta_cc_count",
        "caller_count",
        "callee_count",
        "adr_count",
        "dropped_aux_blocks",
        mode="before",
    )
    @classmethod
    def _validate_strict_counts(cls, value: Any) -> int:
        return _reject_bool_int(value)


class UnreviewableDiffSegment(BaseModel):
    """Zero-code metadata describing a single diff line that exceeded the batch token budget."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_path: str = Field(min_length=1)
    hunk_id: int | None = Field(default=None, ge=1)
    right_line: int | None = Field(default=None, ge=1)
    left_line: int | None = Field(default=None, ge=1)
    estimated_tokens: int = Field(ge=1)
    reason: str = Field(min_length=1)


class BatchReviewResult(BaseModel):
    """Aggregated findings and Critic verdicts produced for a single diff batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_index: int = Field(ge=0)
    batch_id: str = Field(min_length=1)
    findings: tuple[FindingCandidate, ...] = ()
    verdicts: tuple[CriticVerdict, ...] = ()
    verified_findings: tuple[VerifiedFinding, ...] = ()
    stripped_suggestions_count: int = Field(default=0, ge=0)
    truncated_candidates_count: int = Field(default=0, ge=0)
    truncated_candidate_ids: tuple[str, ...] = ()
    dropped_invalid_findings_count: int = Field(default=0, ge=0)
    dropped_invalid_verdicts_count: int = Field(default=0, ge=0)


class LLMCallMetrics(BaseModel):
    """Per-call token, latency, and estimated cost telemetry for a single LLM invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: LLMRole
    batch_index: int = Field(ge=0)
    model_name: str = Field(min_length=1)
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float | None = Field(default=None, ge=0.0)
    latency_ms: float = Field(default=0.0, ge=0.0)
    attempts: int = Field(default=1, ge=1)


class TelemetryStatus(BaseModel):
    """Outcome of Langfuse observability export, isolated from PR review business status."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    active: bool = False
    trace_id: str | None = None
    trace_url: str | None = None
    status: TelemetryState = "disabled"
    error_type: str | None = None
    flushed: bool = False


class PRReviewState(BaseModel):
    """LangGraph in-memory orchestration state for a single Pull Request review."""

    model_config = ConfigDict(extra="forbid")

    pr_id: str = Field(default="pr-unknown", min_length=1)
    head_sha: str = Field(default="", max_length=64)
    trace_id: str = Field(default="-", min_length=1)
    total_diff_tokens: int = Field(default=0, ge=0)
    batching_mode: BatchingMode = "empty"
    batches: list[DiffBatch] = Field(default_factory=list)
    batch_contexts: dict[str, BuiltBatchContext] = Field(default_factory=dict)
    unreviewable_segments: list[UnreviewableDiffSegment] = Field(default_factory=list)
    security_findings: Annotated[list[FindingCandidate], operator.add] = Field(default_factory=list)
    logic_findings: Annotated[list[FindingCandidate], operator.add] = Field(default_factory=list)
    batch_results: Annotated[list[BatchReviewResult], operator.add] = Field(default_factory=list)
    merged_findings: list[FindingCandidate] = Field(default_factory=list)
    merged_verdicts: list[CriticVerdict] = Field(default_factory=list)
    verified_findings: list[VerifiedFinding] = Field(default_factory=list)
    stripped_suggestions_count: int = Field(default=0, ge=0)
    dropped_invalid_findings_count: Annotated[int, operator.add] = 0
    llm_metrics: Annotated[list[LLMCallMetrics], operator.add] = Field(default_factory=list)
    telemetry: TelemetryStatus = Field(default_factory=TelemetryStatus)
    status: ReviewRunStatus = "pending"
