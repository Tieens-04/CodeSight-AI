"""Centralized Zero-Noise Critic Agent node with confidence gate, dedup, and AST guard."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from typing import Any, Final

import structlog
from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import ValidationError

from codesight.agents.llm_factory import LLMFactory
from codesight.agents.nodes._auditor_common import (
    BatchDiffIndex,
    extract_diff_secret_literals,
    is_evidence_grounded,
    mask_sensitive_values,
    metrics_from_invocation,
    normalize_auditor_suggestion,
    parse_batch_diff_index,
    parse_reference_code_blocks,
    select_reference_code_for_finding,
)
from codesight.agents.nodes.context_builder import (
    PRReviewContext,
    neutralize_rules_text,
    normalize_agent_path,
)
from codesight.agents.prompts.critic_prompt import CRITIC_SYSTEM_PROMPT
from codesight.agents.schemas import (
    AuditorRole,
    BuiltBatchContext,
    CodeSuggestion,
    CriticVerdict,
    DiffBatch,
    FindingCandidate,
    FindingSeverity,
    LLMCallMetrics,
    LLMCodeSuggestionDraft,
    LLMCriticDraftOutput,
    LLMCriticVerdictDraft,
    VerifiedFinding,
)
from codesight.ast_engine.suggestion_validator import TreeSitterSuggestionValidator
from codesight.diff.file_filter import estimate_tokens

MIN_CRITIC_CONFIDENCE: Final[float] = 0.75

_ROLE_ORDER: Final[dict[AuditorRole, int]] = {
    "security_auditor": 0,
    "logic_performance_auditor": 1,
}
_SEVERITY_RANK: Final[dict[FindingSeverity, int]] = {
    "CRITICAL": 0,
    "WARNING": 1,
    "INFO": 2,
}
_STYLE_NITPICK_CATEGORIES: Final[frozenset[str]] = frozenset(
    {
        "style",
        "style_nitpick",
        "formatting",
        "formatting_style",
        "naming",
        "naming_convention",
        "whitespace",
        "lint_style",
        "cosmetic",
    }
)
_STYLE_NITPICK_TITLE_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:naming preference|minor naming|whitespace|line length|trailing comma|"
    r"prefer single quotes|prefer double quotes|consider renaming)\b",
    re.IGNORECASE,
)
_ISSUE_GROUP_ALIASES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    ("sql_injection", ("sql_injection", "sqli", "unparameterized_sql")),
    ("command_injection", ("command_injection", "cmd_injection", "os_command")),
    ("xss", ("xss", "cross_site_scripting")),
    ("ssrf", ("ssrf",)),
    ("path_traversal", ("path_traversal", "directory_traversal")),
    ("hardcoded_secret", ("hardcoded_secret", "secret", "credential")),
    ("broken_access_control", ("broken_access_control", "idor", "authz", "authorization")),
    ("n_plus_one", ("n_plus_one", "n+1")),
    ("async_blocking", ("blocking_io", "event_loop_blocking", "sync_io", "sequential_await")),
    ("quadratic_complexity", ("quadratic", "o(n")),
    (
        "resource_leak",
        (
            "listener_leak",
            "timer_leak",
            "resource_leak",
            "memory_leak",
            "connection_leak",
            "handle_leak",
        ),
    ),
    ("broken_caller", ("caller", "signature", "contract")),
)
_GENERIC_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"security", "performance", "logic", "architecture", "bug", "general", "other"}
)
_DEDUP_STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "for",
        "the",
        "with",
        "from",
        "and",
        "not",
        "via",
        "into",
        "over",
        "this",
        "that",
        "use",
        "using",
        "user",
        "users",
        "input",
        "line",
        "lines",
        "code",
        "query",
        "call",
        "calls",
        "loop",
        "data",
        "inside",
        "missing",
        "check",
        "checks",
        "unsafe",
        "potential",
        "possible",
        "error",
        "warning",
        "issue",
        "bug",
        "file",
        "function",
        "method",
        "value",
    }
)

logger = structlog.get_logger("codesight.agents.nodes.critic_agent")


@dataclass(frozen=True, slots=True)
class CriticNodeResult:
    """Output bundle produced by `run_critic_agent` for a single diff batch."""

    verdicts: list[CriticVerdict]
    verified_findings: list[VerifiedFinding]
    metrics: LLMCallMetrics | None
    truncated_ids: tuple[str, ...]
    dropped_verdicts: int
    stripped_suggestions_count: int


def _truncate_text_to_bytes(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    if max_bytes <= 3:
        return "..."[:max_bytes]
    clipped = encoded[: max_bytes - 3].decode("utf-8", errors="ignore")
    return f"{clipped}..."


def validate_suggestion_draft(
    raw_suggestion: CodeSuggestion | LLMCodeSuggestionDraft | None,
) -> CodeSuggestion | None:
    """Convert a lenient suggestion draft into a strict `CodeSuggestion` or `None`."""
    if raw_suggestion is None:
        return None
    if isinstance(raw_suggestion, CodeSuggestion):
        return raw_suggestion
    try:
        return CodeSuggestion.model_validate(
            {
                "start_line": raw_suggestion.start_line,
                "end_line": raw_suggestion.end_line,
                "replacement_code": raw_suggestion.replacement_code,
                "explanation": raw_suggestion.explanation,
            }
        )
    except ValidationError:
        return None


_DECISION_ALIASES: Final[dict[str, str]] = {
    "APPROVE": "APPROVE",
    "APPROVED": "APPROVE",
    "ACCEPT": "APPROVE",
    "ACCEPTED": "APPROVE",
    "KEEP": "APPROVE",
    "MODIFY": "MODIFY",
    "MODIFIED": "MODIFY",
    "REJECT": "REJECT",
    "REJECTED": "REJECT",
    "DROP": "REJECT",
}


def normalize_critic_verdicts(
    verdicts: Sequence[CriticVerdict | LLMCriticVerdictDraft],
    *,
    batch_index: int,
    valid_candidate_ids: AbstractSet[str],
) -> tuple[list[CriticVerdict], int]:
    """Validate Critic verdicts per item, dropping orphans, duplicates, and invalid entries."""
    normalized: list[CriticVerdict] = []
    seen_ids: set[str] = set()
    dropped_count = 0

    for item in verdicts:
        if isinstance(item, CriticVerdict):
            cid = item.candidate_id
            if cid not in valid_candidate_ids or cid in seen_ids:
                dropped_count += 1
                continue
            seen_ids.add(cid)
            normalized.append(item.model_copy(update={"batch_index": batch_index}))
            continue

        valid_suggestion = validate_suggestion_draft(item.suggestion)
        adjusted_severity: str | None = None
        if item.adjusted_severity is not None:
            cleaned_sev = item.adjusted_severity.strip().upper()
            if cleaned_sev in _SEVERITY_RANK:
                adjusted_severity = cleaned_sev
        raw_decision = item.decision.strip().upper()
        canonical_decision = _DECISION_ALIASES.get(raw_decision, raw_decision)
        try:
            verdict = CriticVerdict.model_validate(
                {
                    "candidate_id": item.candidate_id,
                    "batch_index": batch_index,
                    "decision": canonical_decision,
                    "rationale": item.rationale,
                    "confidence": item.confidence,
                    "adjusted_severity": adjusted_severity,
                    "suggestion": valid_suggestion,
                }
            )
        except ValidationError:
            dropped_count += 1
            continue

        if verdict.candidate_id not in valid_candidate_ids or verdict.candidate_id in seen_ids:
            dropped_count += 1
            continue
        seen_ids.add(verdict.candidate_id)
        normalized.append(verdict)

    return normalized, dropped_count


def build_critic_messages_within_budget(
    *,
    batch: DiffBatch,
    candidates: Sequence[FindingCandidate],
    available_user_tokens: int,
    custom_rules: str = "",
) -> tuple[list[BaseMessage], tuple[str, ...]]:
    """Pack Critic prompt strictly inside `available_user_tokens` and return truncated IDs."""
    ordered = sorted(
        candidates,
        key=lambda f: (
            _SEVERITY_RANK[f.severity],
            -f.confidence,
            _ROLE_ORDER[f.role],
            f.file_path,
            f.start_line,
            f.end_line,
            f.candidate_id,
        ),
    )

    header = f"Batch: {batch.batch_id} (index={batch.batch_index})\nCandidate Findings:\n"
    diff_section_header = "\n\nDiff Excerpt:\n"

    candidates_token_cap = max(1, int(available_user_tokens * 0.6)) if ordered else 0
    selected_serialized: list[str] = []
    truncated_ids: list[str] = []

    for cand in ordered:
        compact: dict[str, Any] = {
            "candidate_id": cand.candidate_id,
            "role": cand.role,
            "file_path": cand.file_path,
            "start_line": cand.start_line,
            "end_line": cand.end_line,
            "severity": cand.severity,
            "category": cand.category,
            "title": _truncate_text_to_bytes(cand.title, 160),
            "evidence": _truncate_text_to_bytes(cand.evidence, 320),
            "confidence": cand.confidence,
        }
        if cand.suggestion is not None:
            compact["suggestion_span"] = (
                f"L{cand.suggestion.start_line}-L{cand.suggestion.end_line}"
            )
            compact["suggestion_code"] = _truncate_text_to_bytes(
                cand.suggestion.replacement_code, 160
            )
        entry_line = json.dumps(compact, separators=(",", ":"))
        trial_text = header + "\n".join([*selected_serialized, entry_line])
        if estimate_tokens(trial_text) <= candidates_token_cap:
            selected_serialized.append(entry_line)
        else:
            truncated_ids.append(cand.candidate_id)

    candidates_block = (
        header + "\n".join(selected_serialized) if selected_serialized else f"{header}(none)"
    )

    sections: list[str] = [candidates_block]
    used_tokens = estimate_tokens(candidates_block)

    cleaned_rules = custom_rules.strip()
    if cleaned_rules:
        safe_rules = neutralize_rules_text(cleaned_rules)
        rules_budget = max(0, min(320, available_user_tokens - used_tokens - 64))
        if rules_budget >= 24:
            clipped_rules = _truncate_text_to_bytes(safe_rules, rules_budget * 4)
            rules_block = (
                '\n\n<repo_custom_rules untrusted_data="true">\n'
                f"{clipped_rules}\n"
                "</repo_custom_rules>"
            )
            if used_tokens + estimate_tokens(rules_block) <= available_user_tokens - 24:
                sections.append(rules_block)
                used_tokens += estimate_tokens(rules_block)

    remaining_tokens = available_user_tokens - used_tokens - estimate_tokens(diff_section_header)
    if remaining_tokens > 16:
        diff_excerpt = _truncate_text_to_bytes(batch.annotated_diff, remaining_tokens * 4)
        sections.append(f"{diff_section_header}{diff_excerpt}")

    user_content = "".join(sections)
    if estimate_tokens(user_content) > available_user_tokens:
        user_content = _truncate_text_to_bytes(user_content, available_user_tokens * 4)

    return [
        SystemMessage(content=CRITIC_SYSTEM_PROMPT),
        HumanMessage(content=user_content),
    ], tuple(truncated_ids)


def _validate_min_confidence(min_confidence: float) -> float:
    if (
        isinstance(min_confidence, bool)
        or not isinstance(min_confidence, (int, float))
        or not math.isfinite(min_confidence)
        or min_confidence < MIN_CRITIC_CONFIDENCE
        or min_confidence > 1.0
    ):
        raise ValueError(
            f"min_confidence must be a finite float within [{MIN_CRITIC_CONFIDENCE}, 1.0]."
        )
    return float(min_confidence)


def _extract_batch_secret_literals(index: BatchDiffIndex) -> tuple[str, ...]:
    all_batch_lines = [
        *[ln for contents in index.line_contents_by_file.values() for ln in contents.values()],
        *[ln for del_lines in index.deleted_lines_by_file.values() for ln in del_lines],
    ]
    return extract_diff_secret_literals(all_batch_lines)


def _is_style_nitpick(
    candidate: FindingCandidate,
    *,
    effective_severity: FindingSeverity | None = None,
) -> bool:
    sev = effective_severity if effective_severity is not None else candidate.severity
    if sev == "CRITICAL":
        return False
    cat = candidate.category.strip().lower().replace("-", "_").replace(" ", "_")
    if cat in _STYLE_NITPICK_CATEGORIES:
        return True
    return sev == "INFO" and bool(_STYLE_NITPICK_TITLE_RE.search(candidate.title))


def _normalize_issue_group(category: str) -> tuple[str, bool]:
    lowered = category.strip().lower().replace("-", "_").replace(" ", "_")
    for group_name, aliases in _ISSUE_GROUP_ALIASES:
        if any(alias in lowered for alias in aliases):
            return group_name, True
    return lowered, False


def _are_duplicate_findings(a: VerifiedFinding, b: VerifiedFinding) -> bool:
    if normalize_agent_path(a.file_path) != normalize_agent_path(b.file_path):
        return False
    overlaps = max(a.start_line, b.start_line) <= min(a.end_line, b.end_line)
    if not overlaps:
        return False
    group_a, canon_a = _normalize_issue_group(a.category)
    group_b, canon_b = _normalize_issue_group(b.category)
    if group_a == group_b:
        return True
    if canon_a and canon_b:
        return False
    if group_a not in _GENERIC_CATEGORIES and group_b not in _GENERIC_CATEGORIES:
        return False
    # Catch cross-role overlap only when at least one side uses a generic category.
    tokens_a = {
        t
        for t in re.findall(r"[a-z0-9_]{3,}", f"{a.category} {a.title}".lower())
        if t not in _DEDUP_STOPWORDS
    }
    tokens_b = {
        t
        for t in re.findall(r"[a-z0-9_]{3,}", f"{b.category} {b.title}".lower())
        if t not in _DEDUP_STOPWORDS
    }
    return len(tokens_a & tokens_b) >= 2


def _verified_sort_key(finding: VerifiedFinding) -> tuple[int, float, int, int, int, str]:
    return (
        _SEVERITY_RANK[finding.severity],
        -finding.confidence,
        _ROLE_ORDER[finding.role],
        0 if finding.suggestion is not None else 1,
        finding.batch_index,
        finding.candidate_id,
    )


def deduplicate_verified_findings(findings: Sequence[VerifiedFinding]) -> list[VerifiedFinding]:
    """Deduplicate overlapping findings deterministically while preserving highest-ranked entry."""
    ordered = sorted(findings, key=_verified_sort_key)
    kept: list[VerifiedFinding] = []
    for cand in ordered:
        if any(_are_duplicate_findings(existing, cand) for existing in kept):
            continue
        kept.append(cand)
    return sorted(
        kept,
        key=lambda f: (
            f.batch_index,
            _SEVERITY_RANK[f.severity],
            f.file_path,
            f.start_line,
            f.end_line,
            f.candidate_id,
        ),
    )


def _is_candidate_grounded(
    candidate: FindingCandidate,
    *,
    index: BatchDiffIndex,
    ref_blocks: tuple[Any, ...],
    secret_literals: Sequence[str] = (),
) -> bool:
    norm_path = normalize_agent_path(candidate.file_path)
    file_hunks = index.line_hunks_by_file.get(norm_path)
    if not norm_path or not file_hunks:
        return False
    start_hunk = file_hunks.get(candidate.start_line)
    if start_hunk is None or any(
        file_hunks.get(ln) != start_hunk
        for ln in range(candidate.start_line, candidate.end_line + 1)
    ):
        return False

    file_contents = index.line_contents_by_file.get(norm_path, {})
    deleted_lines = index.deleted_lines_by_file.get(norm_path, ())
    anchored_lines = [
        file_contents[ln]
        for ln in range(candidate.start_line, candidate.end_line + 1)
        if ln in file_contents
    ]
    ref_text = select_reference_code_for_finding(
        ref_blocks,
        file_path=norm_path,
        start_line=candidate.start_line,
        end_line=candidate.end_line,
        file_contents=file_contents,
        deleted_lines=(deleted_lines if candidate.role == "logic_performance_auditor" else ()),
    )
    if is_evidence_grounded(
        candidate.evidence,
        anchored_lines=anchored_lines,
        start_line=candidate.start_line,
        end_line=candidate.end_line,
        reference_context_text=ref_text,
    ):
        return True
    if "***REDACTED***" in candidate.evidence:
        masked_anchored = [
            mask_sensitive_values(ln, extra_literals=secret_literals) for ln in anchored_lines
        ]
        masked_ref = (
            mask_sensitive_values(ref_text, extra_literals=secret_literals) if ref_text else ""
        )
        return is_evidence_grounded(
            candidate.evidence,
            anchored_lines=masked_anchored,
            start_line=candidate.start_line,
            end_line=candidate.end_line,
            reference_context_text=masked_ref,
        )
    return False


def reconcile_critic_findings(
    *,
    batch: DiffBatch,
    candidates: Sequence[FindingCandidate],
    verdicts: Sequence[CriticVerdict],
    batch_context: BuiltBatchContext | None = None,
    review_context: PRReviewContext | None = None,
    suggestion_validator: TreeSitterSuggestionValidator | None = None,
    min_confidence: float = MIN_CRITIC_CONFIDENCE,
    diff_index: BatchDiffIndex | None = None,
    secret_literals: Sequence[str] | None = None,
) -> tuple[list[VerifiedFinding], int]:
    """Filter candidates fail-closed by verdict & confidence >= 0.75, dedup, and validate AST."""
    validated_min_conf = _validate_min_confidence(min_confidence)
    if not candidates or not verdicts:
        return [], 0

    index = diff_index if diff_index is not None else parse_batch_diff_index(batch)
    ref_blocks = (
        parse_reference_code_blocks(batch_context.rendered_prompt)
        if batch_context is not None
        else ()
    )
    resolved_secrets = (
        tuple(secret_literals)
        if secret_literals is not None
        else _extract_batch_secret_literals(index)
    )
    verdicts_by_id = {v.candidate_id: v for v in verdicts}
    validator = (
        suggestion_validator
        if suggestion_validator is not None
        else TreeSitterSuggestionValidator()
    )

    approved: list[VerifiedFinding] = []
    stripped_suggestions = 0

    for cand in candidates:
        verdict = verdicts_by_id.get(cand.candidate_id)
        if verdict is None or verdict.decision == "REJECT":
            continue

        effective_conf = verdict.confidence if verdict.confidence is not None else cand.confidence
        if effective_conf < validated_min_conf:
            continue

        final_severity: FindingSeverity = (
            verdict.adjusted_severity if verdict.adjusted_severity is not None else cand.severity
        )
        if _is_style_nitpick(cand, effective_severity=final_severity):
            continue

        if not _is_candidate_grounded(
            cand, index=index, ref_blocks=ref_blocks, secret_literals=resolved_secrets
        ):
            continue

        norm_path = normalize_agent_path(cand.file_path)
        file_hunks = index.line_hunks_by_file.get(norm_path, {})
        file_contents = index.line_contents_by_file.get(norm_path, {})
        expected_hunk = file_hunks.get(cand.start_line)

        raw_suggestion: CodeSuggestion | None = cand.suggestion
        preserved_explanation: str | None = (
            raw_suggestion.explanation if raw_suggestion is not None else None
        )
        stripped_reason: str | None = None

        if verdict.suggestion is not None:
            if (
                cand.suggestion is not None
                and verdict.suggestion.replacement_code == cand.suggestion.replacement_code
                and verdict.suggestion.start_line == cand.suggestion.start_line
                and verdict.suggestion.end_line == cand.suggestion.end_line
            ):
                if verdict.suggestion.explanation is not None:
                    raw_suggestion = cand.suggestion.model_copy(
                        update={"explanation": verdict.suggestion.explanation}
                    )
                    preserved_explanation = raw_suggestion.explanation
            else:
                normalized_verdict_sugg = normalize_auditor_suggestion(
                    verdict.suggestion,
                    file_path=norm_path,
                    file_line_hunks=file_hunks,
                    file_line_contents=file_contents,
                    extra_secret_literals=resolved_secrets,
                    expected_hunk_id=expected_hunk,
                )
                if normalized_verdict_sugg is not None:
                    raw_suggestion = normalized_verdict_sugg
                    preserved_explanation = raw_suggestion.explanation
                else:
                    # Do not revert to superseded cand.suggestion when Critic MODIFY fails checks.
                    raw_suggestion = None
                    stripped_suggestions += 1
                    stripped_reason = "invalid_critic_suggestion"
                    preserved_explanation = verdict.suggestion.explanation
                    if preserved_explanation is not None:
                        preserved_explanation = mask_sensitive_values(
                            preserved_explanation,
                            extra_literals=resolved_secrets,
                        )

        final_suggestion: CodeSuggestion | None = None
        if raw_suggestion is not None:
            masked_code = mask_sensitive_values(
                raw_suggestion.replacement_code,
                extra_literals=resolved_secrets,
            )
            masked_expl = (
                mask_sensitive_values(
                    raw_suggestion.explanation,
                    extra_literals=resolved_secrets,
                )
                if raw_suggestion.explanation is not None
                else None
            )
            masked_candidate_sugg = CodeSuggestion(
                start_line=raw_suggestion.start_line,
                end_line=raw_suggestion.end_line,
                replacement_code=masked_code,
                explanation=masked_expl,
            )
            preserved_explanation = masked_expl
            after_source = (
                review_context.resolve_after_source(norm_path)
                if review_context is not None
                else None
            )
            val_res = validator.validate_suggestion(
                norm_path,
                masked_candidate_sugg,
                after_source=after_source,
                right_line_hunks=file_hunks,
            )
            if val_res.is_safe and val_res.suggestion is not None:
                final_suggestion = masked_candidate_sugg
            else:
                stripped_suggestions += 1
                stripped_reason = val_res.reason

        approved.append(
            VerifiedFinding(
                candidate_id=cand.candidate_id,
                role=cand.role,
                batch_index=batch.batch_index,
                file_path=norm_path,
                start_line=cand.start_line,
                end_line=cand.end_line,
                severity=final_severity,
                category=mask_sensitive_values(cand.category, extra_literals=resolved_secrets),
                title=mask_sensitive_values(cand.title, extra_literals=resolved_secrets),
                evidence=mask_sensitive_values(cand.evidence, extra_literals=resolved_secrets),
                confidence=round(float(effective_conf), 4),
                rationale=mask_sensitive_values(verdict.rationale, extra_literals=resolved_secrets),
                suggestion=final_suggestion,
                suggestion_explanation=preserved_explanation,
                suggestion_stripped_reason=stripped_reason,
            )
        )

    return deduplicate_verified_findings(approved), stripped_suggestions


def _mask_verdicts(
    verdicts: Sequence[CriticVerdict],
    *,
    secret_literals: Sequence[str],
) -> list[CriticVerdict]:
    if not verdicts:
        return []
    masked: list[CriticVerdict] = []
    for v in verdicts:
        masked_sugg = v.suggestion
        if masked_sugg is not None:
            masked_sugg = CodeSuggestion(
                start_line=masked_sugg.start_line,
                end_line=masked_sugg.end_line,
                replacement_code=mask_sensitive_values(
                    masked_sugg.replacement_code, extra_literals=secret_literals
                ),
                explanation=(
                    mask_sensitive_values(masked_sugg.explanation, extra_literals=secret_literals)
                    if masked_sugg.explanation is not None
                    else None
                ),
            )
        masked.append(
            v.model_copy(
                update={
                    "rationale": mask_sensitive_values(v.rationale, extra_literals=secret_literals),
                    "suggestion": masked_sugg,
                }
            )
        )
    return masked


async def run_critic_agent(
    *,
    batch: DiffBatch,
    candidates: Sequence[FindingCandidate],
    llm_factory: LLMFactory,
    max_user_tokens: int,
    batch_context: BuiltBatchContext | None = None,
    review_context: PRReviewContext | None = None,
    suggestion_validator: TreeSitterSuggestionValidator | None = None,
    config: RunnableConfig | None = None,
    min_confidence: float = MIN_CRITIC_CONFIDENCE,
) -> CriticNodeResult:
    """Verify Auditor candidates in one batch call, short-circuiting empty batches."""
    validated_min_conf = _validate_min_confidence(min_confidence)
    if not candidates:
        logger.info(
            "critic_agent_short_circuited",
            batch_id=batch.batch_id,
            batch_index=batch.batch_index,
        )
        return CriticNodeResult(
            verdicts=[],
            verified_findings=[],
            metrics=None,
            truncated_ids=(),
            dropped_verdicts=0,
            stripped_suggestions_count=0,
        )

    custom_rules = review_context.resolve_custom_rules() if review_context is not None else ""
    messages, truncated_ids = build_critic_messages_within_budget(
        batch=batch,
        candidates=candidates,
        available_user_tokens=max_user_tokens,
        custom_rules=custom_rules,
    )
    valid_candidate_ids = {c.candidate_id for c in candidates} - set(truncated_ids)
    if not valid_candidate_ids:
        logger.info(
            "critic_agent_short_circuited_truncated",
            batch_id=batch.batch_id,
            batch_index=batch.batch_index,
            truncated_count=len(truncated_ids),
        )
        return CriticNodeResult(
            verdicts=[],
            verified_findings=[],
            metrics=None,
            truncated_ids=truncated_ids,
            dropped_verdicts=0,
            stripped_suggestions_count=0,
        )

    inv = await llm_factory.invoke_structured(
        role="critic",
        schema=LLMCriticDraftOutput,
        messages=messages,
        config=config,
    )
    verdicts, dropped_verdicts = normalize_critic_verdicts(
        inv.parsed.verdicts,
        batch_index=batch.batch_index,
        valid_candidate_ids=valid_candidate_ids,
    )
    diff_index = parse_batch_diff_index(batch)
    secret_literals = _extract_batch_secret_literals(diff_index)
    masked_verdicts = _mask_verdicts(verdicts, secret_literals=secret_literals)
    verified, stripped_count = reconcile_critic_findings(
        batch=batch,
        candidates=candidates,
        verdicts=masked_verdicts,
        batch_context=batch_context,
        review_context=review_context,
        suggestion_validator=suggestion_validator,
        min_confidence=validated_min_conf,
        diff_index=diff_index,
        secret_literals=secret_literals,
    )
    metrics = metrics_from_invocation("critic", batch.batch_index, inv)
    logger.info(
        "critic_agent_completed",
        batch_id=batch.batch_id,
        batch_index=batch.batch_index,
        candidate_count=len(candidates),
        verdict_count=len(masked_verdicts),
        verified_count=len(verified),
        stripped_suggestions_count=stripped_count,
        truncated_count=len(truncated_ids),
        dropped_verdicts_count=dropped_verdicts,
        latency_ms=inv.latency_ms,
    )
    return CriticNodeResult(
        verdicts=masked_verdicts,
        verified_findings=verified,
        metrics=metrics,
        truncated_ids=truncated_ids,
        dropped_verdicts=dropped_verdicts,
        stripped_suggestions_count=stripped_count,
    )
