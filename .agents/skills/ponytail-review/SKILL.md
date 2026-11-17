---
name: ponytail-review
description: Use when reviewing a code change for logic bugs, security risks, concurrency/load failures, missing high-risk tests, latency regressions, or unnecessary bloat/over-engineering across the diff and its connected callers.
---

# Ponytail Review — Pragmatic Correctness, Load & Anti-Bloat Audit

Review a change like the senior engineer who will be paged at 3 AM when it breaks in production.
Order of importance: **correct, safe, holds under load, tested, fast, lean**.
Lean still matters: every extra line must be read, tested, and maintained later.

## 1. Understand first

- Review what is in scope: uncommitted working-tree changes (`git diff HEAD` + untracked `??` files), a branch diff, or named files.
- Read the diff, then read the **connected code**: callers of every changed function, the functions it calls, Pydantic/SQLAlchemy/Qdrant schemas, and tests.
- Trace the real runtime flow: where webhook/API payloads enter, what touches Redis/PostgreSQL/Qdrant/RAM, and what leaves toward GitHub or Gemini APIs.
- When a function signature, return shape, exception type, or lock contract changes, grep **every caller** across `src/` and `tests/`.
- Judge scale against CodeSight-AI's real concurrency & SLA targets (`< 100ms` webhook ACK, `< 25s` E2E review, concurrent `pull_request.synchronize` bursts, Gemini Free Tier RPM limits).

## 2. Look for

1. **Bug:** wrong result, crash, missed edge case (empty diff, renamed/deleted binary file, `\ No newline at end of file`, `0`-line hunk `@@ -1,5 +0,0 @@`, timezone/UTC drift, out-of-order SHA delivery), or a fix applied in one caller while the shared helper stays broken.
2. **Risk:** `NFR-3` Zero-Code Persistence leak (raw code written to Qdrant payload, DB, logs, or uncleaned temp dir), `HMAC-SHA256` / auth bypass, secret exposure in `structlog`, swallowed exceptions, or uncommitted DB state before Celery dispatch.
3. **Scale & Concurrency:** TOCTOU check-then-write races on Redis locks or idempotency keys, blocking synchronous I/O inside `async def` FastAPI/worker coroutines, unbounded memory growth when streaming GitHub blobs/tarballs, unbatched Gemini calls exceeding RPM limits, or N+1 SQL/Qdrant queries.
4. **Missing test:** risky new logic (diff line mapper, Lua CAS lock, `tree-sitter` splice validator, retry/backoff, auth cache) without a test that genuinely fails if the logic breaks.
5. **Speed:** needless round-trips to GitHub/Qdrant/Gemini or repeated AST parsing inside hot loops that threaten the `< 25s` E2E budget.
6. **Lean (`YAGNI`):** code that should not exist or should be smaller:
   - **delete**: dead functions, unused config knobs, unreferenced types/branches
   - **reuse**: `src/codesight/` already has a helper/exception/client — name its exact path
   - **stdlib / native**: Python `3.12+` stdlib (`hmac`, `uuid`, `asyncio`, `contextlib`, `pathlib`) already solves it cleanly
   - **yagni**: abstract base classes with a single implementation, speculative hooks nobody calls
   - **merge**: near-duplicate helpers or regexes that must change together

## 3. Check before you report

- Every finding **must have a concrete failure scenario**: *"Given input/timing X, line Y produces wrong outcome Z."* No concrete case -> no finding.
- Re-read the exact `file:line` and callers to confirm the bug is reachable and the dead code is truly unreferenced.
- Propose the **smallest safe fix**; prefer fixes that delete code over fixes that add layers.
- No subjective style nitpicks or vague `"consider..."` remarks.

## 4. Output format

Start with `What this change does:` in 2–3 plain sentences.

Group numbered findings into (skip empty groups):
- **Must fix:** correctness bug, `NFR-3` / security breach, data loss, race condition, `HTTP 422` diff coordinate bug, SLA breaker.
- **Should fix:** risky logic without a real test, latency bottleneck, code duplication, dead/bloated code.
- **Nice to have:** small simplifications or minor speed-ups.

Each finding includes:
1. **Title & Location** (`src/codesight/...:L40-52`)
2. **Problem:** Concrete input/race and exact failure behavior.
3. **Fix:** Minimal concrete change.
4. **Impact if skipped:** Why it matters in production.

End with `Verdict: Ship.` or `Verdict: Fix <IDs> first.` and `Lean: -<N> lines possible.` (when applicable). Lists findings only; **never modifies files**.
