"""Deterministic Technical Debt Scorer (0-100) using exact Decimal arithmetic."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

import structlog
from pydantic import BaseModel, ConfigDict, Field

from codesight.agents.schemas import FindingSeverity, VerifiedFinding
from codesight.ast_engine.complexity import detect_missing_test_companion
from codesight.ast_engine.models import ComplexityDiffSummary, TestCompanionReport
from codesight.diff.models import ParsedPullRequestDiff

SEVERITY_WEIGHTS: Final[dict[FindingSeverity, Decimal]] = {
    "CRITICAL": Decimal("25"),
    "WARNING": Decimal("10"),
    "INFO": Decimal("2"),
}
MAX_FINDINGS_PENALTY: Final[Decimal] = Decimal("70")
MAX_COMPLEXITY_PENALTY: Final[Decimal] = Decimal("20")
UNTESTED_PENALTY: Final[Decimal] = Decimal("15")
DELTA_CC_PENALTY_FACTOR: Final[Decimal] = Decimal("1.5")
HIGH_CC_FUNCTION_PENALTY: Final[Decimal] = Decimal("3")

logger = structlog.get_logger("codesight.scoring.debt_scorer")


@dataclass(frozen=True, slots=True)
class FileComplexityInput:
    """Static complexity penalties extracted for one reviewed file."""

    file_path: str
    net_delta_cc: int = 0
    high_cc_count: int = 0
    from_cache: bool = False

    @classmethod
    def from_summary(
        cls,
        file_path: str,
        summary: ComplexityDiffSummary | None,
        *,
        from_cache: bool = False,
    ) -> FileComplexityInput:
        """Build FileComplexityInput from an optional ComplexityDiffSummary."""
        if summary is None:
            return cls(file_path=file_path, from_cache=from_cache)
        return cls(
            file_path=file_path,
            net_delta_cc=summary.net_delta_cc,
            high_cc_count=len(summary.high_complexity_functions),
            from_cache=from_cache,
        )


class DebtScoreBreakdown(BaseModel):
    """Immutable Technical Debt Score (0-100) and component penalty breakdown."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    score: int = Field(ge=0, le=100)
    weighted_findings_penalty: float = Field(ge=0.0, le=70.0)
    complexity_penalty: float = Field(ge=0.0, le=20.0)
    untested_penalty: float = Field(ge=0.0, le=15.0)
    critical_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    info_count: int = Field(ge=0)
    is_untested: bool = False
    added_source_logic_lines: int = Field(default=0, ge=0)
    added_test_logic_lines: int = Field(default=0, ge=0)


class TechnicalDebtScorer:
    """Computes a deterministic 0-100 Technical Debt Score from findings, CC, and test coverage."""

    def score(
        self,
        findings: Sequence[VerifiedFinding],
        *,
        file_complexities: Sequence[FileComplexityInput] = (),
        test_companion: TestCompanionReport | None = None,
        parsed_diff: ParsedPullRequestDiff | None = None,
        source_globs: Sequence[str] | None = None,
    ) -> DebtScoreBreakdown:
        """Compute `100 - weighted_findings - complexity - untested` clamped to [0, 100]."""
        effective_companion = (
            test_companion
            if test_companion is not None
            else (
                detect_missing_test_companion(parsed_diff, source_globs=source_globs)
                if parsed_diff is not None
                else None
            )
        )

        # Decimal accumulation avoids float addition order sensitivity across shuffled inputs.
        weighted_raw = Decimal("0")
        critical_count = 0
        warning_count = 0
        info_count = 0
        for finding in findings:
            weighted_raw += SEVERITY_WEIGHTS[finding.severity] * Decimal(
                f"{finding.confidence:.6f}"
            )
            if finding.severity == "CRITICAL":
                critical_count += 1
            elif finding.severity == "WARNING":
                warning_count += 1
            else:
                info_count += 1

        weighted_penalty = min(MAX_FINDINGS_PENALTY, weighted_raw)

        complexity_raw = Decimal("0")
        for fc in file_complexities:
            complexity_raw += (
                Decimal(max(0, fc.net_delta_cc)) * DELTA_CC_PENALTY_FACTOR
                + Decimal(max(0, fc.high_cc_count)) * HIGH_CC_FUNCTION_PENALTY
            )
        complexity_penalty = min(MAX_COMPLEXITY_PENALTY, complexity_raw)

        is_untested = bool(effective_companion is not None and effective_companion.is_untested)
        untested_penalty = UNTESTED_PENALTY if is_untested else Decimal("0")

        raw_score = Decimal("100") - weighted_penalty - complexity_penalty - untested_penalty
        bounded_score = max(
            0,
            min(100, int(raw_score.quantize(Decimal("1"), rounding=ROUND_HALF_UP))),
        )

        breakdown = DebtScoreBreakdown(
            score=bounded_score,
            weighted_findings_penalty=float(
                weighted_penalty.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            ),
            complexity_penalty=float(
                complexity_penalty.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            ),
            untested_penalty=float(untested_penalty),
            critical_count=critical_count,
            warning_count=warning_count,
            info_count=info_count,
            is_untested=is_untested,
            added_source_logic_lines=(
                effective_companion.added_source_logic_lines
                if effective_companion is not None
                else 0
            ),
            added_test_logic_lines=(
                effective_companion.added_test_logic_lines if effective_companion is not None else 0
            ),
        )
        logger.info(
            "debt_score_computed",
            score=breakdown.score,
            critical_count=breakdown.critical_count,
            warning_count=breakdown.warning_count,
            info_count=breakdown.info_count,
            weighted_findings_penalty=breakdown.weighted_findings_penalty,
            complexity_penalty=breakdown.complexity_penalty,
            untested_penalty=breakdown.untested_penalty,
            is_untested=breakdown.is_untested,
        )
        return breakdown
