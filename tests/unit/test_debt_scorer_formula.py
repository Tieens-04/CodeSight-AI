"""Unit tests for TechnicalDebtScorer (0-100), ReportComposer, and SmartQualityGate (CA-21)."""

from __future__ import annotations

import random

import pytest

from codesight.agents.schemas import CodeSuggestion, VerifiedFinding
from codesight.ast_engine.models import (
    ComplexityDelta,
    ComplexityDiffSummary,
    FunctionComplexity,
    TestCompanionReport,
)
from codesight.config_reader.repo_config import RepoConfig
from codesight.diff.models import ParsedPullRequestDiff, PatchFileEntry
from codesight.diff.smart_diff_parser import SmartDiffParser
from codesight.scoring import (
    DebtScoreBreakdown,
    FileComplexityInput,
    ReportComposer,
    TechnicalDebtScorer,
    evaluate_smart_quality_gate,
)


def _make_finding(
    cid: str,
    *,
    file_path: str = "src/service.py",
    start_line: int = 10,
    end_line: int = 10,
    severity: str = "WARNING",
    confidence: float = 0.85,
    title: str = "Sample finding",
    with_suggestion: bool = False,
) -> VerifiedFinding:
    suggestion = (
        CodeSuggestion(
            start_line=start_line,
            end_line=end_line,
            replacement_code="    return safe_value()",
            explanation="Use safe_value() helper.",
        )
        if with_suggestion
        else None
    )
    return VerifiedFinding(
        candidate_id=cid,
        role="security_auditor" if severity == "CRITICAL" else "logic_performance_auditor",
        batch_index=0,
        file_path=file_path,
        start_line=start_line,
        end_line=end_line,
        severity=severity,
        category="sql_injection" if severity == "CRITICAL" else "n_plus_one",
        title=title,
        evidence="Observed in diff line L10",
        confidence=confidence,
        rationale="Verified against AST and diff hunks.",
        suggestion=suggestion,
    )


def _make_diff() -> tuple[ParsedPullRequestDiff, str]:
    entries = [
        PatchFileEntry(
            filename="src/auth/login.py",
            status="modified",
            blob_sha="a" * 40,
            patch=(
                "@@ -10,2 +10,3 @@ def login():\n"
                "     user = fetch()\n+    check(user)\n     return user\n"
            ),
        ),
        PatchFileEntry(
            filename="src/api/orders.py",
            status="modified",
            blob_sha="b" * 40,
            patch="@@ -1,1 +1,2 @@\n def list_orders():\n+    return []\n",
        ),
    ]
    return SmartDiffParser().parse_patch_entries(entries), "deadbeef1234567890"


def test_debt_scorer_exact_formula_caps_and_clamping() -> None:
    scorer = TechnicalDebtScorer()

    # 1. Clean PR => 100
    clean = scorer.score(())
    assert clean.score == 100
    assert clean.weighted_findings_penalty == 0.0
    assert clean.complexity_penalty == 0.0
    assert clean.untested_penalty == 0.0

    # 2. Mixed formula:
    # findings: CRITICAL (25 * 0.8 = 20) + WARNING (10 * 0.9 = 9) + INFO (2 * 0.8 = 1.6) => 30.6
    # complexity: net_delta_cc=4 * 1.5 (=6.0) + high_cc=1 * 3 (=3.0) => 9.0
    # untested: 15.0
    # total penalty = 54.6 => 100 - 54.6 = 45.4 => round => 45
    findings = [
        _make_finding("c1", severity="CRITICAL", confidence=0.8),
        _make_finding("c2", severity="WARNING", confidence=0.9),
        _make_finding("c3", severity="INFO", confidence=0.8),
    ]
    fn_metric = FunctionComplexity(
        symbol_name="big_fn",
        qualified_name="big_fn",
        symbol_kind="function",
        start_line=1,
        end_line=40,
        line_count=40,
        cyclomatic_complexity=14,
        max_nesting_depth=4,
        parameter_count=3,
    )
    cc_summary = ComplexityDiffSummary(
        deltas=(
            ComplexityDelta(
                symbol_name="big_fn",
                qualified_name="big_fn",
                change_kind="modified",
                before_cc=10,
                after_cc=14,
                delta_cc=4,
            ),
        ),
        net_delta_cc=4,
        max_delta_cc=4,
        max_after_cc=14,
        high_complexity_functions=(fn_metric,),
    )
    fc_inputs = [
        FileComplexityInput.from_summary("src/service.py", cc_summary),
        FileComplexityInput.from_summary("src/clean.py", None),
    ]
    companion = TestCompanionReport(
        added_source_logic_lines=75,
        added_test_logic_lines=0,
        threshold=50,
        is_untested=True,
        source_files=("src/service.py",),
    )

    breakdown = scorer.score(findings, file_complexities=fc_inputs, test_companion=companion)
    assert breakdown.weighted_findings_penalty == 30.6
    assert breakdown.complexity_penalty == 9.0
    assert breakdown.untested_penalty == 15.0
    assert breakdown.score == 45
    assert breakdown.critical_count == 1
    assert breakdown.warning_count == 1
    assert breakdown.info_count == 1
    assert breakdown.is_untested is True

    # 3. Penalty caps (70 / 20 / 15) => raw = -5 => clamped to 0
    extreme_findings = [
        _make_finding(f"x{i}", severity="CRITICAL", confidence=1.0) for i in range(6)
    ]
    extreme_cc = [FileComplexityInput("src/huge.py", net_delta_cc=30, high_cc_count=5)]
    extreme = scorer.score(extreme_findings, file_complexities=extreme_cc, test_companion=companion)
    assert extreme.weighted_findings_penalty == 70.0
    assert extreme.complexity_penalty == 20.0
    assert extreme.untested_penalty == 15.0
    assert extreme.score == 0


def test_debt_scorer_is_100_percent_deterministic_across_runs_and_permutations() -> None:
    scorer = TechnicalDebtScorer()
    base_findings = [
        _make_finding("f1", severity="CRITICAL", confidence=0.81),
        _make_finding("f2", severity="WARNING", confidence=0.77),
        _make_finding("f3", severity="WARNING", confidence=0.93),
        _make_finding("f4", severity="INFO", confidence=0.76),
    ]
    complexities = [
        FileComplexityInput("src/a.py", net_delta_cc=3, high_cc_count=1),
        FileComplexityInput("src/b.py", net_delta_cc=-2, high_cc_count=0),
    ]
    companion = TestCompanionReport(
        added_source_logic_lines=20,
        added_test_logic_lines=10,
        threshold=50,
        is_untested=False,
    )

    baseline = scorer.score(
        base_findings,
        file_complexities=complexities,
        test_companion=companion,
    )
    rng = random.Random(2026)
    for _ in range(100):
        shuffled_f = list(base_findings)
        rng.shuffle(shuffled_f)
        shuffled_c = list(complexities)
        rng.shuffle(shuffled_c)
        cur = scorer.score(shuffled_f, file_complexities=shuffled_c, test_companion=companion)
        assert cur == baseline


def test_smart_quality_gate_matrix() -> None:
    cfg_comment = RepoConfig(min_debt_score=60, review_action="COMMENT")
    cfg_block = RepoConfig(min_debt_score=60, review_action="REQUEST_CHANGES")

    passing = DebtScoreBreakdown(
        score=82,
        weighted_findings_penalty=10.0,
        complexity_penalty=8.0,
        untested_penalty=0.0,
        critical_count=0,
        warning_count=1,
        info_count=1,
    )
    dec_pass = evaluate_smart_quality_gate(passing, cfg_block)
    assert dec_pass.passed is True
    assert dec_pass.review_event == "COMMENT"
    assert dec_pass.check_conclusion == "success"
    assert dec_pass.reasons == ()

    low_score_only = DebtScoreBreakdown(
        score=55,
        weighted_findings_penalty=30.0,
        complexity_penalty=15.0,
        untested_penalty=0.0,
        critical_count=0,
        warning_count=3,
        info_count=0,
    )
    dec_neutral = evaluate_smart_quality_gate(low_score_only, cfg_comment)
    assert dec_neutral.passed is False
    assert dec_neutral.review_event == "COMMENT"
    assert dec_neutral.check_conclusion == "neutral"

    dec_block_low = evaluate_smart_quality_gate(low_score_only, cfg_block)
    assert dec_block_low.passed is False
    assert dec_block_low.review_event == "REQUEST_CHANGES"
    assert dec_block_low.check_conclusion == "failure"

    critical_even_high_score = DebtScoreBreakdown(
        score=75,
        weighted_findings_penalty=25.0,
        complexity_penalty=0.0,
        untested_penalty=0.0,
        critical_count=1,
        warning_count=0,
        info_count=0,
    )
    dec_crit_comment = evaluate_smart_quality_gate(critical_even_high_score, cfg_comment)
    assert dec_crit_comment.passed is False
    assert dec_crit_comment.review_event == "COMMENT"
    assert dec_crit_comment.check_conclusion == "failure"

    dec_crit_block = evaluate_smart_quality_gate(critical_even_high_score, cfg_block)
    assert dec_crit_block.passed is False
    assert dec_crit_block.review_event == "REQUEST_CHANGES"
    assert dec_crit_block.check_conclusion == "failure"


def test_report_composer_zero_noise_mermaid_and_info_details() -> None:
    parsed_diff, head_sha = _make_diff()
    assert isinstance(parsed_diff, type(SmartDiffParser().parse_patch_entries([])))
    composer = ReportComposer()

    findings = [
        _make_finding(
            "crit-1",
            file_path="src/auth/login.py",
            start_line=11,
            end_line=11,
            severity="CRITICAL",
            confidence=0.95,
            title="SQL Injection in login",
            with_suggestion=True,
        ),
        _make_finding(
            "warn-1",
            file_path="src/api/orders.py",
            start_line=2,
            end_line=2,
            severity="WARNING",
            confidence=0.82,
            title="Unbounded query",
        ),
        _make_finding(
            "info-1",
            file_path="src/auth/login.py",
            start_line=10,
            end_line=10,
            severity="INFO",
            confidence=0.78,
            title="Consider helper extraction",
            with_suggestion=True,
        ),
    ]
    inline_drafts, info_findings = composer.partition_findings_for_github(findings)
    assert len(inline_drafts) == 2
    assert {d.severity for d in inline_drafts} == {"CRITICAL", "WARNING"}
    assert len(info_findings) == 1
    assert info_findings[0].candidate_id == "info-1"

    breakdown = TechnicalDebtScorer().score(
        findings,
        file_complexities=[
            FileComplexityInput("src/auth/login.py", net_delta_cc=3, high_cc_count=0),
            FileComplexityInput(
                "src/api/orders.py", net_delta_cc=0, high_cc_count=0, from_cache=True
            ),
        ],
    )
    gate = evaluate_smart_quality_gate(breakdown, RepoConfig(min_debt_score=60))
    summary = composer.compose_pr_summary(
        breakdown=breakdown,
        gate=gate,
        findings=findings,
        parsed_diff=parsed_diff,
        head_sha=head_sha,
        config_hash=RepoConfig().config_hash,
        trace_id="trace-ca21-01",
        file_complexities=[
            FileComplexityInput("src/auth/login.py", net_delta_cc=3),
            FileComplexityInput("src/api/orders.py", net_delta_cc=0, from_cache=True),
        ],
        cached_file_paths={"src/api/orders.py"},
        tokens_saved_pct=84.5,
        latency_ms=1420,
    )

    assert "Technical_Debt_Score-" in summary
    assert "| `src/auth/login.py` | modified | 1 | 0 | 1 | +3 | 🤖 AI |" in summary
    assert "| `src/api/orders.py` | modified | 0 | 1 | 0 | 0 | ⚡ SHA Cache |" in summary
    assert "```mermaid\npie showData" in summary
    assert '"CRITICAL" : 1' in summary
    assert '"WARNING" : 1' in summary
    assert '"INFO" : 1' in summary
    assert "<details>\n<summary>Informational Findings (1)</summary>" in summary
    # Zero-Noise: suggestion fences inside <details> prose must be neutralized
    assert "```suggestion" not in summary

    # Clean PR (0 findings): omit empty Mermaid pie chart so GitHub renders cleanly
    clean_breakdown = TechnicalDebtScorer().score(())
    clean_gate = evaluate_smart_quality_gate(clean_breakdown, RepoConfig())
    clean_summary = composer.compose_pr_summary(
        breakdown=clean_breakdown,
        gate=clean_gate,
        findings=(),
        parsed_diff=parsed_diff,
        head_sha=head_sha,
        config_hash=RepoConfig().config_hash,
        trace_id="trace-clean",
    )
    assert "pie showData" not in clean_summary
    assert "Clean PR" in clean_summary

    title, check_body = composer.compose_check_summary(
        breakdown,
        gate,
        inline_count=2,
        downgraded_count=0,
        cache_hits=1,
        total_files=2,
    )
    assert "Failed" in title
    assert "SHA cache reused 1/2" in check_body


@pytest.mark.parametrize("bad_score", [-1, 101])
def test_debt_score_breakdown_validates_bounds(bad_score: int) -> None:
    with pytest.raises(ValueError):
        DebtScoreBreakdown(
            score=bad_score,
            weighted_findings_penalty=0.0,
            complexity_penalty=0.0,
            untested_penalty=0.0,
            critical_count=0,
            warning_count=0,
            info_count=0,
        )


def test_debt_scorer_parsed_diff_untested_and_report_composer_hygiene() -> None:
    # 1. TechnicalDebtScorer.score(..., parsed_diff=...) invokes detect_missing_test_companion
    added_lines = "\n".join(f"+x_{i} = {i}" for i in range(55))
    large_untested_diff = SmartDiffParser().parse_patch_entries(
        [
            PatchFileEntry(
                filename="src/core/engine.py",
                status="added",
                blob_sha="a" * 40,
                patch=f"@@ -0,0 +1,55 @@\n{added_lines}\n",
            )
        ]
    )
    breakdown = TechnicalDebtScorer().score((), parsed_diff=large_untested_diff)
    assert breakdown.is_untested is True
    assert breakdown.untested_penalty == 15.0
    assert breakdown.added_source_logic_lines >= 51
    assert breakdown.score == 85

    # 2. ReportComposer derives cache_hits from FileComplexityInput(from_cache=True),
    # escapes </details>, and avoids repeating the INFO title in the collapsible body.
    parsed_diff, head_sha = _make_diff()
    info_item = VerifiedFinding(
        candidate_id="info-escape",
        role="logic_performance_auditor",
        batch_index=0,
        file_path="src/auth/login.py",
        start_line=11,
        end_line=11,
        severity="INFO",
        category="readability",
        title="Unique Info Heading",
        evidence="Contains </details> tag inside prose.",
        confidence=0.80,
        rationale="Contains </details> tag inside prose.",
    )
    scored = TechnicalDebtScorer().score([info_item])
    gate = evaluate_smart_quality_gate(scored, RepoConfig())
    summary = ReportComposer().compose_pr_summary(
        breakdown=scored,
        gate=gate,
        findings=[info_item],
        parsed_diff=parsed_diff,
        head_sha=head_sha,
        config_hash=RepoConfig().config_hash,
        trace_id="trace-hygiene",
        file_complexities=[FileComplexityInput("src/api/orders.py", from_cache=True)],
    )
    assert "**SHA Cache:** `1/2` hit(s)" in summary
    assert "&lt;/details>" in summary
    assert summary.count("</details>") == 1
    assert summary.count("Unique Info Heading") == 1
