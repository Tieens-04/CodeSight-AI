"""Mechanical gate checker for CodeSight-AI benchmark results (`evals/results/latest_benchmark.json`)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def evaluate_report(report: dict[str, Any], require_live: bool = False) -> list[str]:
    errors: list[str] = []
    mode = report.get("mode")
    pr_count = report.get("golden_pr_count", 0)
    clean_count = report.get("clean_pr_count", 0)
    metrics = report.get("metrics") or {}

    if pr_count < 24:
        errors.append(f"`golden_pr_count` is {pr_count} (minimum required: 24 Golden PRs).")
    if clean_count < 4:
        errors.append(f"`clean_pr_count` is {clean_count} (minimum required: 4 zero-defect PRs to guard FP).")

    if require_live and mode != "live":
        errors.append(f"Latency/release sign-off requires `mode == 'live'`, got {mode!r} (mock/cassette ineligible).")

    precision = float(metrics.get("precision", 0.0))
    if precision < 0.85:
        errors.append(f"Precision {precision:.2%} < 85.00% target.")

    syntax_rate = float(metrics.get("suggestion_syntax_pass_rate", 0.0))
    if syntax_rate < 1.0:
        errors.append(f"Suggestion Syntax Pass Rate {syntax_rate:.2%} < 100.00% target.")

    token_red = float(metrics.get("token_reduction", 0.0))
    if token_red < 0.40:
        errors.append(f"Token Reduction {token_red:.2%} < 40.00% target.")

    if mode == "live" or require_live:
        avg_lat = float(metrics.get("avg_e2e_latency_seconds", 999.0))
        if avg_lat >= 25.0 or avg_lat <= 0.0:
            errors.append(f"Average E2E Latency {avg_lat:.2f}s violates `< 25.0s` (> 0s) live SLA.")

    return errors


def run_self_test() -> int:
    valid_live = {
        "mode": "live",
        "golden_pr_count": 24,
        "clean_pr_count": 4,
        "metrics": {
            "precision": 0.88,
            "suggestion_syntax_pass_rate": 1.0,
            "token_reduction": 0.51,
            "avg_e2e_latency_seconds": 17.6,
        },
    }
    assert evaluate_report(valid_live, require_live=True) == []

    mocked_cheat = {
        "mode": "cassette",
        "golden_pr_count": 24,
        "clean_pr_count": 4,
        "metrics": {
            "precision": 0.90,
            "suggestion_syntax_pass_rate": 1.0,
            "token_reduction": 0.55,
            "avg_e2e_latency_seconds": 0.4,
        },
    }
    assert evaluate_report(mocked_cheat, require_live=False) == []
    cheat_errs = evaluate_report(mocked_cheat, require_live=True)
    assert len(cheat_errs) == 1 and "live" in cheat_errs[0]

    weak_metrics = {
        "mode": "live",
        "golden_pr_count": 10,
        "clean_pr_count": 0,
        "metrics": {
            "precision": 0.80,
            "suggestion_syntax_pass_rate": 0.95,
            "token_reduction": 0.35,
            "avg_e2e_latency_seconds": 28.2,
        },
    }
    assert len(evaluate_report(weak_metrics, require_live=True)) == 6
    print("[PASS] codesight-kpi-benchmark self-test passed (live gate, cassette anti-cheat, 5 KPI thresholds).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify CodeSight-AI benchmark KPI gates.")
    parser.add_argument("--self-test", action="store_true", help="Run built-in assertions")
    parser.add_argument("--report", type=Path, help="Path to benchmark JSON report")
    parser.add_argument("--require-live", action="store_true", help="Require `mode == 'live'` for latency sign-off")
    args = parser.parse_args()

    if args.self_test:
        return run_self_test()
    if not args.report or not args.report.exists():
        print(f"[FAIL] Benchmark report not found: {args.report}")
        return 1

    data = json.loads(args.report.read_text(encoding="utf-8"))
    errors = evaluate_report(data, require_live=args.require_live)
    if errors:
        print("[FAIL] Benchmark KPI gate check failed:")
        for err in errors:
            print(f"  - {err}")
        return 1

    print("[PASS] All CodeSight-AI KPI gates satisfied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
