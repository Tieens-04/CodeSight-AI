---
name: code-review
description: Use when the user asks to review a branch, PR, or uncommitted working-tree changes against documented repository standards and the originating Jira issue or spec using parallel subagents.
---

# Two-Axis Independent Code Review (Standards + Spec)

Two-axis review of either uncommitted working-tree changes (`git status --short` + `git diff HEAD` + untracked `??` files) or committed branch changes (`git diff main...HEAD`):

- **Standards**: Does the code conform to `AGENTS.md`, `.ai-workflow/rules/domain-review.md`, and the Fowler smell baseline?
- **Spec**: Does the code faithfully implement every requirement in the originating Jira `CA-*` issue (`jira.md`) and approved plan/spec (`plan.md` / `docs/specs/`)?

Both axes run as **parallel read-only subagents** (`invoke_subagent` with `TypeName: "research"`) so they do not pollute each other's context, then the main reviewer verifies `file:line` evidence and aggregates findings.

## Process

### 1. Pin the change set

- **Uncommitted working-tree mode** (default in `.ai-workflow` Step 3 & Step 5 before `git commit`):
  - Run `git status --short` to capture modified/deleted files (`M`, `A`, `D`) AND newly created untracked files (`??`), ignoring `.ai-workflow/tasks/`.
  - Run `git diff HEAD` for tracked changes, and explicitly pass the full list of untracked `??` file paths so subagents read every new file in full.
  - Never rely on three-dot `git diff main...HEAD` while changes are still uncommitted (`HEAD` equals `main`, so three-dot diff would be empty).
- **Committed branch mode**:
  - Confirm `git rev-parse <fixed-point>` (default `main`), capture `git diff <fixed-point>...HEAD` and `git log <fixed-point>..HEAD --oneline`.

### 2. Identify the spec source

Look for the originating spec in this order:
1. `.ai-workflow/tasks/<TICKET_ID>/jira.md` and `.ai-workflow/tasks/<TICKET_ID>/plan.md` (when running inside `.ai-workflow`).
2. Issue key (`CA-XX`) in the branch name or user prompt, fetched via `jira-cwb` (`jira_get_issue`).
3. Matching design/plan files under `docs/specs/` or `docs/plans/`.
4. If none exist, ask the user where the spec is.

### 3. Identify the standards sources

Always include `AGENTS.md`, `.ai-workflow/rules/domain-review.md` (if present), and `pyproject.toml`.

On top of documented repo standards, the Standards axis carries the **Fowler code smell baseline** (_Refactoring_, ch.3):
- **The repo overrides.** Documented repo rules in `AGENTS.md` always win.
- **Always a judgement call.** Baseline smells are heuristics, never mechanical lint duplicates; skip anything `ruff` or `mypy` already enforces.

Smell baseline (*what it is* -> *how to fix*):
- **Mysterious Name**: function, variable, or type whose name hides what it does or holds -> rename clearly.
- **Duplicated Code**: same logic shape repeated across hunks/modules -> extract shared helper/validator and call from both.
- **Feature Envy**: function reaches into another module/object's internals more than its own -> move behavior onto the owner.
- **Data Clumps**: same group of parameters keeps travelling together -> bundle into a `dataclass` or Pydantic model.
- **Primitive Obsession**: raw `dict`/`str`/`int` standing in for a domain concept (e.g., diff coordinates, SHA lock token, repo config) -> use a typed domain object/enum.
- **Repeated Switches**: same `if`/`match` cascade repeated across files -> consolidate into one dispatch map or polymorphic interface.
- **Shotgun Surgery**: one logical change requires scattered edits across unrelated modules -> co-locate responsibilities.
- **Divergent Change**: one module changes for several unrelated reasons -> split by responsibility.
- **Speculative Generality**: hooks, config flags, or abstractions not required by `jira.md` / `AGENTS.md` -> delete (`YAGNI`).
- **Message Chains**: brittle deep attribute/dict chains across layers -> expose a clear boundary method.
- **Middle Man**: wrapper function/class that only delegates without adding domain value -> call the target directly.

### 4. Spawn subagents in parallel

Dispatch read-only `research` subagents with:
- **Standards subagent**: diff + untracked file list + `AGENTS.md` + `.ai-workflow/rules/domain-review.md` + smell baseline above. Ask for concrete `file:line` violations of repo invariants vs baseline smell judgement calls.
- **Spec subagent**: diff + untracked file list + `jira.md` + `plan.md` / `docs/specs/`. Ask for: (a) missing or partial Acceptance Criteria, (b) unrequested scope creep, (c) tests that mock away the real bug/logic instead of exercising actual behavior.

### 5. Aggregate without masking

Keep **Standards** and **Spec** findings clearly attributed with `file:line` evidence. Code that follows every standard can still fail the spec, and code that satisfies the spec can still violate `NFR-3` or concurrency invariants.
