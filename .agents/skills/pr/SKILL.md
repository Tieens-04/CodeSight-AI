---
name: pr
description: Use when drafting a GitHub Pull Request description with concise architecture/flow visuals, concrete before/after verification evidence, and merge blast-radius analysis.
---

# Pull Request Description Authoring (`pr`)

Use this concise, high-signal structure when writing a PR body for **CodeSight-AI** (aligned with domain terminology in `AGENTS.md`):

```markdown
## Summary

<1-2 sentence intent + smallest diagram, call-tree, file-tree, or diff sketch that makes the change obvious>

## Acceptance Criteria & Invariants

- [x] AC-1 (`CA-XX`): <what is satisfied and where>
- [x] Zero-Code Persistence (`NFR-3`) & Port Coexistence (`5433` / `6381` / `6333-6334` / `8082`) preserved

## Evidence

- **Before:** <failing scenario, missing capability, or baseline behavior>
- **After:** <exact `uv run` test/audit output, latency/coverage metrics, or `/radar` screenshot>

## Merge Danger

**Door:** <two-way | one-way (e.g., irreversible Alembic schema drop or Qdrant collection re-creation)>

**Blast Radius:** <Low | Medium | High — concise scope of affected components: webhook gateway, Celery worker, Qdrant RAG, GitHub publisher, `/radar`>
```

## Section Guidelines

### 1. Summary Visuals (Pick ONE compact view that fits best)

- **Runtime control flow / async pipeline (Call Tree):**
  ```text
  POST /api/v1/webhooks/github
    verify_github_signature (HMAC-SHA256)
    claim_delivery_idempotency (PostgreSQL / Redis)
    acquire_supersede_slot (Lua CAS) -> enqueue review_pr_task
  ```

- **State / protocol interaction (Mermaid):**
  ```mermaid
  sequenceDiagram
      participant GH as GitHub Webhook
      participant API as FastAPI Gateway
      participant R as Redis (Lua CAS)
      participant W as Celery Worker
      GH->>API: pull_request.synchronize (SHA_2)
      API->>R: supersede(pr_key, SHA_2)
      API-->>GH: 202 Accepted (<100ms)
      W->>R: verify active SHA == SHA_2 before publish
  ```

- **Module layout change (Shallow File Tree):**
  ```text
  src/codesight/
  ├── api/webhooks.py        # <100ms HMAC + idempotency + enqueue
  ├── worker/locks.py        # Redis Lua CAS Cancel-on-Supersede
  └── config/repo_config.py  # resilient .codesight.yaml parser
  ```

### 2. Evidence

Prefer real execution output from the current branch:
- `uv run ruff check .` -> `All checks passed!`
- `uv run mypy src` -> `Success: no issues found`
- `uv run pytest --cov=src --cov-fail-under=80` -> exact passed count & coverage `%`
- `python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket CA-XX` -> `[PASS]`
- Screenshots when `/radar` UI changes.

### 3. Merge Danger

- **Two-way door:** Additive modules, stateless worker logic, feature-flagged behavior, or reversible Alembic migrations (`downgrade()` cleanly restores state).
- **One-way door:** Destructive DB column drops, Qdrant vector dimension/schema breaking changes requiring full re-indexing, or webhook contract changes affecting live GitHub App deliveries.
