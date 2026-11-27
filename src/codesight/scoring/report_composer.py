"""Zero-Noise Markdown/Mermaid PR Report Composer and Smart Quality Gate."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass

import structlog

from codesight.agents.schemas import VerifiedFinding, neutralize_suggestion_fences
from codesight.config_reader.repo_config import RepoConfig
from codesight.diff.file_filter import normalize_diff_path
from codesight.diff.models import ParsedPullRequestDiff
from codesight.scoring.debt_scorer import DebtScoreBreakdown, FileComplexityInput
from codesight.vcs.github_publisher import (
    CheckConclusion,
    ReviewCommentDraft,
    ReviewEvent,
    format_markdown_code_span,
)

_CLOSING_DETAILS_RE = re.compile(r"</details", re.IGNORECASE)

logger = structlog.get_logger("codesight.scoring.report_composer")


@dataclass(frozen=True, slots=True)
class SmartGateDecision:
    """GitHub PR Review event and Check Run conclusion derived from Debt Score and RepoConfig."""

    passed: bool
    review_event: ReviewEvent
    check_conclusion: CheckConclusion
    reasons: tuple[str, ...]
    min_debt_score: int
    debt_score: int
    critical_count: int


def evaluate_smart_quality_gate(
    breakdown: DebtScoreBreakdown,
    repo_config: RepoConfig,
) -> SmartGateDecision:
    """Evaluate Smart Quality Gate policy against DebtScoreBreakdown and RepoConfig."""
    reasons: list[str] = []
    if breakdown.critical_count > 0:
        reasons.append(f"{breakdown.critical_count} CRITICAL finding(s) detected")
    if breakdown.score < repo_config.min_debt_score:
        reasons.append(
            f"Debt score {breakdown.score}/100 is below minimum threshold "
            f"{repo_config.min_debt_score}/100"
        )

    if not reasons:
        decision = SmartGateDecision(
            passed=True,
            review_event="COMMENT",
            check_conclusion="success",
            reasons=(),
            min_debt_score=repo_config.min_debt_score,
            debt_score=breakdown.score,
            critical_count=breakdown.critical_count,
        )
    elif repo_config.review_action == "REQUEST_CHANGES":
        decision = SmartGateDecision(
            passed=False,
            review_event="REQUEST_CHANGES",
            check_conclusion="failure",
            reasons=tuple(reasons),
            min_debt_score=repo_config.min_debt_score,
            debt_score=breakdown.score,
            critical_count=breakdown.critical_count,
        )
    else:
        conclusion: CheckConclusion = "failure" if breakdown.critical_count > 0 else "neutral"
        decision = SmartGateDecision(
            passed=False,
            review_event="COMMENT",
            check_conclusion=conclusion,
            reasons=tuple(reasons),
            min_debt_score=repo_config.min_debt_score,
            debt_score=breakdown.score,
            critical_count=breakdown.critical_count,
        )

    logger.info(
        "smart_gate_evaluated",
        passed=decision.passed,
        review_event=decision.review_event,
        check_conclusion=decision.check_conclusion,
        debt_score=decision.debt_score,
        min_debt_score=decision.min_debt_score,
        critical_count=decision.critical_count,
    )
    return decision


def _score_badge_color(score: int) -> str:
    if score >= 80:
        return "brightgreen"
    if score >= 60:
        return "yellow"
    if score >= 40:
        return "orange"
    return "red"


def _gate_badge(gate: SmartGateDecision) -> tuple[str, str]:
    if gate.passed:
        return "PASS", "brightgreen"
    if gate.review_event == "REQUEST_CHANGES":
        return "CHANGES_REQUESTED", "red"
    if gate.check_conclusion == "failure":
        return "FAIL", "red"
    return "NEUTRAL", "yellow"


def _escape_table_cell(text: str) -> str:
    return text.replace("|", "\\|")


def _format_finding_location(finding: VerifiedFinding) -> str:
    if finding.start_line == finding.end_line:
        return f"L{finding.end_line}"
    return f"L{finding.start_line}-L{finding.end_line}"


def _escape_details_html(text: str) -> str:
    return _CLOSING_DETAILS_RE.sub("&lt;/details", text)


def _render_info_summary_body(item: VerifiedFinding) -> str:
    parts = [neutralize_suggestion_fences(item.evidence)]
    rationale = neutralize_suggestion_fences(item.rationale).strip()
    if rationale and rationale != parts[0].strip():
        parts.append(f"**Verification:** {rationale}")
    explanation = (
        item.suggestion.explanation
        if item.suggestion is not None and item.suggestion.explanation
        else item.suggestion_explanation
    )
    if explanation and explanation.strip():
        parts.append(neutralize_suggestion_fences(explanation.strip()))
    return _escape_details_html("\n\n".join(parts))


class ReportComposer:
    """Renders PR Summary Markdown/Mermaid and enforces Zero-Noise inline/INFO partitioning."""

    def partition_findings_for_github(
        self,
        findings: Sequence[VerifiedFinding],
    ) -> tuple[tuple[ReviewCommentDraft, ...], tuple[VerifiedFinding, ...]]:
        """Keep CRITICAL/WARNING as inline drafts and route INFO to collapsible PR Summary."""
        inline_drafts = tuple(
            finding.to_review_comment_draft()
            for finding in findings
            if finding.severity in {"CRITICAL", "WARNING"}
        )
        info_findings = tuple(finding for finding in findings if finding.severity == "INFO")
        return inline_drafts, info_findings

    def compose_pr_summary(
        self,
        *,
        breakdown: DebtScoreBreakdown,
        gate: SmartGateDecision,
        findings: Sequence[VerifiedFinding],
        parsed_diff: ParsedPullRequestDiff,
        head_sha: str,
        config_hash: str,
        trace_id: str,
        file_complexities: Sequence[FileComplexityInput] = (),
        cached_file_paths: AbstractSet[str] = frozenset(),
        tokens_saved_pct: float | None = None,
        latency_ms: int | None = None,
        diff_source: str = "diff",
    ) -> str:
        """Build the PR Summary Markdown with score badge, module table, Mermaid pie, and INFO."""
        score_color = _score_badge_color(breakdown.score)
        gate_label, gate_color = _gate_badge(gate)
        score_badge = (
            f"![Debt Score](https://img.shields.io/badge/Technical_Debt_Score-"
            f"{breakdown.score}%2F100-{score_color})"
        )
        gate_badge = (
            f"![Quality Gate](https://img.shields.io/badge/Smart_Gate-{gate_label}-{gate_color})"
        )

        cached_norms = {normalize_diff_path(p).rstrip("/") for p in cached_file_paths if p}
        cc_by_file: dict[str, FileComplexityInput] = {}
        for fc in file_complexities:
            norm_path = normalize_diff_path(fc.file_path).rstrip("/") or fc.file_path
            cc_by_file[norm_path] = fc
            if fc.from_cache:
                cached_norms.add(norm_path)

        total_kept = len(parsed_diff.files)
        cache_hits = sum(
            1
            for pf in parsed_diff.files
            if normalize_diff_path(pf.file_path).rstrip("/") in cached_norms
        )

        effective_tokens_pct = (
            tokens_saved_pct
            if tokens_saved_pct is not None
            else parsed_diff.metrics.tokens_saved_pct
        )

        lines: list[str] = [
            "## CodeSight-AI — Autonomous PR Review",
            "",
            f"{score_badge} {gate_badge}",
            "",
            f"- **Technical Debt Score:** **{breakdown.score}/100** "
            f"(Findings: `-{breakdown.weighted_findings_penalty:.2f}`, "
            f"Complexity: `-{breakdown.complexity_penalty:.2f}`, "
            f"Untested: `-{breakdown.untested_penalty:.2f}`)",
            f"- **Findings:** `CRITICAL={breakdown.critical_count}` | "
            f"`WARNING={breakdown.warning_count}` | `INFO={breakdown.info_count}`",
            f"- **Smart Quality Gate:** `{gate.review_event}` / Check `{gate.check_conclusion}` "
            f"(min threshold `{gate.min_debt_score}/100`)"
            + (f" — {'; '.join(gate.reasons)}" if gate.reasons else ""),
            f"- **Token Reduction:** **{effective_tokens_pct:.2f}%** "
            "| **Files Analyzed:** "
            f"`{parsed_diff.metrics.kept_files}/{parsed_diff.metrics.total_files}`"
            + (f" | **SHA Cache:** `{cache_hits}/{total_kept}` hit(s)" if total_kept > 0 else ""),
            f"- **Commit:** `{head_sha[:12] or 'unknown'}` | **Trace ID:** `{trace_id}` "
            f"| **Config Hash:** `{config_hash}` | **Diff Source:** `{diff_source}`"
            + (f" | **Latency:** `{latency_ms}ms`" if latency_ms is not None else ""),
        ]

        if breakdown.is_untested:
            lines.append(
                f"- **Test Coverage Guard:** Added `{breakdown.added_source_logic_lines}` "
                "source logic lines with `0` companion test lines (`-15` penalty)."
            )

        counts_by_file: dict[str, dict[str, int]] = defaultdict(
            lambda: {"CRITICAL": 0, "WARNING": 0, "INFO": 0}
        )
        for finding in findings:
            norm_path = normalize_diff_path(finding.file_path).rstrip("/") or finding.file_path
            counts_by_file[norm_path][finding.severity] += 1

        change_by_file: dict[str, str] = {}
        ordered_paths: list[str] = []
        for pf in parsed_diff.files:
            norm_path = normalize_diff_path(pf.file_path).rstrip("/") or pf.file_path
            change_by_file[norm_path] = pf.change_type
            if norm_path not in ordered_paths:
                ordered_paths.append(norm_path)
        for norm_path in sorted(counts_by_file):
            if norm_path not in ordered_paths:
                ordered_paths.append(norm_path)

        if ordered_paths:
            lines.extend(
                [
                    "",
                    "### Module & File Breakdown",
                    "",
                    "| File | Change | CRITICAL | WARNING | INFO | ΔCC | Source |",
                    "| --- | --- | ---: | ---: | ---: | ---: | --- |",
                ]
            )
            for norm_path in ordered_paths:
                sev_counts = counts_by_file[norm_path]
                cc_item = cc_by_file.get(norm_path)
                delta_cc = cc_item.net_delta_cc if cc_item is not None else 0
                delta_str = f"+{delta_cc}" if delta_cc > 0 else str(delta_cc)
                source_label = "⚡ SHA Cache" if norm_path in cached_norms else "🤖 AI"
                safe_path = _escape_table_cell(format_markdown_code_span(norm_path))
                change_type = change_by_file.get(norm_path, "modified")
                lines.append(
                    f"| {safe_path} | {change_type} | {sev_counts['CRITICAL']} | "
                    f"{sev_counts['WARNING']} | {sev_counts['INFO']} | "
                    f"{delta_str} | {source_label} |"
                )

        total_findings_count = (
            breakdown.critical_count + breakdown.warning_count + breakdown.info_count
        )
        lines.extend(["", "### Risk Distribution", ""])
        if total_findings_count > 0:
            pie_lines = ["```mermaid", "pie showData", "    title Risk Distribution by Severity"]
            for label, count in (
                ("CRITICAL", breakdown.critical_count),
                ("WARNING", breakdown.warning_count),
                ("INFO", breakdown.info_count),
            ):
                if count > 0:
                    pie_lines.append(f'    "{label}" : {count}')
            pie_lines.append("```")
            lines.extend(pie_lines)
        else:
            # GitHub fails to render Mermaid pie charts when every slice is zero.
            lines.append("> ✅ **Clean PR** — No verified issues detected.")

        info_findings = tuple(finding for finding in findings if finding.severity == "INFO")
        if info_findings:
            lines.extend(
                [
                    "",
                    "<details>",
                    f"<summary>Informational Findings ({len(info_findings)})</summary>",
                    "",
                ]
            )
            for idx, item in enumerate(info_findings, start=1):
                safe_path = format_markdown_code_span(item.file_path)
                loc = _format_finding_location(item)
                safe_title = _escape_details_html(neutralize_suggestion_fences(item.title))
                safe_body = _render_info_summary_body(item)
                lines.append(f"{idx}. {safe_path} ({loc}) — **{safe_title}**")
                lines.append("")
                lines.append(safe_body)
                lines.append("")
            lines.append("</details>")

        return "\n".join(lines).strip()

    def compose_check_summary(
        self,
        breakdown: DebtScoreBreakdown,
        gate: SmartGateDecision,
        *,
        inline_count: int,
        downgraded_count: int,
        cache_hits: int = 0,
        total_files: int = 0,
    ) -> tuple[str, str]:
        """Build `(title, summary)` strings for GitHub Check Run finalization."""
        if gate.passed:
            title = f"CodeSight-AI Quality Gate Passed ({breakdown.score}/100)"
        elif gate.review_event == "REQUEST_CHANGES":
            title = f"CodeSight-AI Changes Requested ({breakdown.score}/100)"
        elif gate.check_conclusion == "failure":
            title = f"CodeSight-AI Quality Gate Failed ({breakdown.score}/100)"
        else:
            title = f"CodeSight-AI Quality Gate Warning ({breakdown.score}/100)"

        parts = [
            f"Technical Debt Score: {breakdown.score}/100 (min {gate.min_debt_score}/100).",
            (
                f"Findings: CRITICAL={breakdown.critical_count}, "
                f"WARNING={breakdown.warning_count}, INFO={breakdown.info_count}."
            ),
            f"Published {inline_count} inline and {downgraded_count} summary finding(s).",
        ]
        if total_files > 0:
            parts.append(f"SHA cache reused {cache_hits}/{total_files} file(s).")
        if gate.reasons:
            parts.append("Gate reasons: " + "; ".join(gate.reasons) + ".")
        return title, " ".join(parts)
