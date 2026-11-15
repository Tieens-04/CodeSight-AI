---
name: codesight-jira-workflow
description: Use when starting, branching, testing, or closing any Jira CA-* ticket in CodeSight-AI, or when asked to fast-track a ticket to Done, commit directly to main, or touch Docker containers.
---

# CodeSight Jira Ticket Lifecycle & Gate Enforcement

## Overview

Every `CA-*` ticket in Jira Project `CA` (Board `74`) enforces strict state ordering, branch isolation, port coexistence (`FlintFlow` / `DocuMind`), and real verification evidence before `Done`. **Violating the letter of the rules is violating the spirit of the rules.**

**REQUIRED SUB-SKILL:** Use `superpowers:verification-before-completion` before posting Jira evidence.

## Quick Reference — Mandatory Lifecycle

| Phase | Jira Transition (`jira-cwb`) | Required Gate / Artifact |
| :--- | :--- | :--- |
| **1. Start** | `21` (`In Progress`) | Inspect `CA-X` ACs; create branch `feat/ca-<X>-<short-slug>` |
| **2. Verify** | `31` (`Verify / Test`) | Run `python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py` |
| **3. Evidence** | `jira_add_comment` on `CA-X` | Fill every slot of the **Verification Evidence Template** below |
| **4. Close** | `41` (`Done`) | Allowed ONLY after step 2 exits `0` and step 3 succeeds |

## Verification Evidence Template (Required Structure)

Every `jira_add_comment` before transition `41` MUST contain all five sections in order:

```markdown
## Verification Evidence — CA-<X>
- **Branch / Commit:** `feat/ca-<X>-<slug>` @ `<short-sha>`
- **Acceptance Criteria:**
  - [x] AC-1: <measurable result>
  - [x] AC-2: <measurable result>
- **Quality Gates (`uv run`):**
  - `ruff check .`: PASS (`0` errors)
  - `mypy src`: PASS (`0` issues)
  - `pytest --cov=src --cov-fail-under=80`: PASS (`<passed>` passed, coverage `<pct>%`)
- **Port & NFR Invariants:** Coexistence verified (`5433`, `6381`, `6333/6334`, `8082`); Zero-Code Persistence (`NFR-3`) intact.
- **Verification Output Summary:**
  ```text
  <paste real command summary lines from preclose_audit.py>
  ```
```

## Pre-Close Mechanical Gate

Always execute before transitioning `31` -> `41`:

```powershell
python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket CA-5
```

If `preclose_audit.py` exits non-zero, **keep the ticket in `31` (`Verify / Test`)** until every failure is resolved.

## Rationalization Counters

| Excuse | Reality |
| :--- | :--- |
| "Demo is in 2 minutes, close `CA-X` now and fix lint later" | Leave status at `31` (`Verify / Test`) for the demo. Never transition `41` without passing gates. |
| "Unit tests passed 12/12, that's enough" | Passing tests without `>= 80%` core coverage, `ruff`, and `mypy` fails the Definition of Done. |
| "I worked on `main` because the repo was just initialized" | Create `feat/ca-<X>-<slug>` and move commits there before closing `CA-X`. |
| "Let me run `docker compose down` globally to free ports" | Forbidden. Never stop or touch `flintflow-*` or `documind-*` containers; always scope with `-p codesight`. |

## Red Flags — STOP Immediately

- Calling `jira_transition_issue` with `"41"` before `jira_add_comment`
- Referencing or mutating issues in `DA` or `CWB` Jira projects
- Running global `pip install` instead of `uv add` / `uv run`
- Pasting fabricated or truncated-away error outputs into Jira comments
