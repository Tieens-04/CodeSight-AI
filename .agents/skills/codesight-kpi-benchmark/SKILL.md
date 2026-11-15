---
name: codesight-kpi-benchmark
description: Use when building Golden PR ground-truth fixtures, implementing DeepEval or evals/run_benchmark.py, measuring precision, token savings, or E2E latency, or signing off CA-23 through CA-28.
---

# CodeSight KPI Benchmark & Anti-Gaming Gate

## Overview

Defines the non-negotiable measurement contracts for `evals/run_benchmark.py`, DeepEval scoring, and `/radar` telemetry (`CA-23`..`CA-28`). **Never claim `< 25s` latency from mocked/cached LLM stubs.**

## Four Hard Release Gates

| Metric | Formula | Release Threshold | Valid Mode |
| :--- | :--- | :--- | :--- |
| **Precision** | `TP / (TP + FP)` across `>= 24` Golden PRs | `>= 0.85` (`85%`) | `cassette` or `live` |
| **Suggestion Syntax Pass Rate** | `valid_suggestions / total_suggestions` (`tree-sitter` splice) | `== 1.00` (`100%`) | `cassette` or `live` |
| **Token Reduction** | `(baseline_tokens - codesight_tokens) / baseline_tokens` | `>= 0.40` (`40%`) | `cassette` or `live` |
| **Average E2E Latency** | Mean wall-clock `perf_counter` across live PR runs | `< 25.0` seconds | **`live` ONLY** |

## Golden PR Ground-Truth Contract (`>= 24` PRs)

1. **Composition:** Minimum `12` Python PRs + `12` TypeScript/TSX PRs, including **at least 4 clean (zero-defect) PRs** so noisy reviewers are penalized via False Positives (`FP`).
2. **True Positive (`TP`) Match Rule:** A predicted finding matches a ground-truth defect only when all three hold:
   - Exact `file_path` match
   - Line window overlap within `±3` lines (`pred_start <= gt_end + 3` and `pred_end >= gt_start - 3`)
   - Matching category (`security` | `performance` | `architecture` | `correctness`) + DeepEval rubric score `>= 0.70`
3. **Token Accounting:** `baseline_tokens` = full text of all modified files in the PR (pre-`FileFilter`) + naive imported files; `codesight_tokens` = total input tokens across all LangGraph LLM calls for that PR. Count offline or via `count_tokens` (0 generation quota).

## Canonical Benchmark Artifact (`evals/results/latest_benchmark.json`)

`evals/run_benchmark.py` MUST write this JSON structure (consumed by `GET /radar` and CI):

```json
{
  "run_id": "2026-11-15T15:00:00Z",
  "mode": "live",
  "golden_pr_count": 24,
  "clean_pr_count": 4,
  "metrics": {
    "precision": 0.88,
    "suggestion_syntax_pass_rate": 1.0,
    "token_reduction": 0.52,
    "avg_e2e_latency_seconds": 18.4,
    "p95_e2e_latency_seconds": 23.1
  }
}
```

## Mechanical KPI Gate Verification

```powershell
# CI / deterministic prompt tuning check:
python .agents/skills/codesight-kpi-benchmark/scripts/check_kpi_gates.py --report evals/results/latest_benchmark.json

# Milestone 4 sign-off (CA-26 / CA-28 — enforces mode == "live" and >= 24 PRs):
python .agents/skills/codesight-kpi-benchmark/scripts/check_kpi_gates.py --report evals/results/latest_benchmark.json --require-live
```

## Rationalization Counters

| Excuse | Reality |
| :--- | :--- |
| "Mocking Gemini proves pipeline overhead is `< 1s`, so E2E is `< 25s`" | LLM + Qdrant network latency IS 90% of E2E time. Mocked runs fail `--require-live`. |
| "Dataset only has buggy PRs, no need for clean PRs" | Without clean PRs, a noisy bot easily games Precision. Include `>= 4` zero-defect PRs. |
| "98% suggestion syntax pass rate is basically 100%" | `TreeSitterSuggestionValidator` strips invalid fences before publish — anything `< 100%` means the validator was bypassed. |
