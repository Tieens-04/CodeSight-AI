# Sprint 2 Review Bugs (`S2-01`–`S2-07`) Remediation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remediate all 7 open Sprint 2 defects (`S2-01`–`S2-05` P1 and `S2-06`–`S2-07` P2) across `InitialRepoIndexer`, `IncrementalBranchSyncer`, `evals/run_m2_eval.py`, `live_eval.py`, and `complexity.py` with strict TDD coverage and zero regressions before Sprint 3 (`CA-17`).

**Architecture:** `AsyncRepoIndexLock` becomes strictly fail-closed on Redis acquisition failures and yields a composable `RepoIndexLockLease` that tracks lease expiry/heartbeat loss and verifies Redis token ownership via Lua before every Qdrant or PostgreSQL mutation (`S2-01`), while `GitHubRepoIndexClient.fetch_git_tree()` walks non-recursive subtrees whenever `recursive=1` returns `truncated=true` and withholds PostgreSQL `last_indexed_sha` updates if truncation remains unresolved (`S2-06`). `evals/run_m2_eval.py` provisions an isolated temporary PostgreSQL database per run (`S2-02`), rejects pre-existing `--collection` names up front, and deletes a collection in `finally` only when created by the current run (`S2-03`). `live_eval.py` decouples ground truth from Qdrant `called_symbols` via curated `evals/m2_ground_truth.json` manifests plus an independent raw-source AST/lexical/Markdown oracle, enforces separate `>= 0.85` Recall@5 gates on callers, callees, and non-degraded architecture docs (`S2-04`), extracts function-body NFR-3 canaries purely from source spans without filtering against Qdrant payloads and feeds the exact same marker set to both payload and container-disk audits (`S2-05`), and restricts `detect_missing_test_companion()` test-line counting to supported non-ambient code extensions (`S2-07`).

**Tech Stack:** Python `3.12` (`uv`), `tree-sitter` (`tree-sitter-python`, `tree-sitter-typescript`), Python `ast` stdlib, `qdrant-client` (gRPC `6334` / HTTP `6333`), `redis.asyncio` (`6381`, Lua CAS), PostgreSQL `16` (`asyncpg` + SQLAlchemy `2.0` Async on `5433`), `httpx`, `typer`, `ruff`, `mypy --strict`, `pytest` + `pytest-cov`.

**Spec:** `docs/sprint-2-bugs.md` & Jira Tickets `CA-11`, `CA-13`, `CA-14`, `CA-15`, `CA-16` (`AGENTS.md` §1–§4).

## Global Constraints

- Active Git branch MUST remain `feat/ca-16-sprint2-remediation`; NEVER commit directly to `main`, and NEVER stage or leave untracked `.env`, `*.pem`, or `*.key` files in `git status --porcelain`.
- Package management strictly via `uv` (`uv run ...`); never run global `pip install`; avoid `assert` in production `src/codesight/` code where runtime validation is required.
- Container & Port Coexistence: NEVER stop, restart, or touch `flintflow-*` (`3001`, `6379`, `8080`) or `documind-*` (`3002`, `5432`, `6380`, `8081`, `9000`, `9001`); scope all operations to `codesight-*` (`5433`, `6381`, `6333/6334`, `8082`).
- Zero-Code Persistence (`NFR-3`): Keep Qdrant payload keys strictly inside the 15-key `ALLOWED_KEYS` allowlist in `.agents/skills/zerocode-ast-rag/scripts/audit_qdrant_zerocode.py`; never persist raw source bodies in Qdrant or on disk.
- Mechanical Quality Gates required before completion: `uv run ruff check .` (`0` errors), `uv run ruff format --check .` (`0` reformats), `uv run mypy src evals` (`0` issues), `uv run pytest --cov=src --cov-fail-under=80` (`>= 80%` core coverage), `uv run python .agents/skills/zerocode-ast-rag/scripts/audit_qdrant_zerocode.py --self-test`, and `uv run python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket CA-16`.

## Review Focus

- **Lock loss between wave fetch and `delete_by_file_paths` before heartbeat tick (`S2-01`):** A worker whose Redis lock key expired or was stolen by a newer push 10ms before `upsert_points`, `delete_by_file_paths`, or `upsert_repository_state` (well before the `ttl / 3` background heartbeat wakes up) must still fail closed with `RetryableTaskError` via pre-write Redis Lua verification without deleting the newer worker's Qdrant points (`Task 1`).
- **Fallback full reconcile inside `IncrementalBranchSyncer` (`_skip_repo_lock=True`) losing parent lock (`S2-01`):** Passing the parent `CompositeRepoLockLease` into `InitialRepoIndexer.index_repository(..., _lock_lease=...)` must fence every wave write, stale-point delete, and PostgreSQL cursor update during `bootstrap_full_index` / `fallback_full_reconcile` (`Task 1`).
- **Nested truncated subtrees & unresolvable GitHub directory truncation (`S2-06`):** Non-recursive subtree BFS must reconstruct nested paths (`pkg/sub/mod.py`), ignore gitlink submodules (`160000`), guard against cyclic tree SHAs, and if any directory still reports `truncated=true` (or exceeds subtree budget), keep `truncated_tree=True`, skip pruning absent paths, and refuse to advance `Repository.last_indexed_sha` (`Task 1`).
- **Pre-indexing failure in `evals/run_m2_eval.py` when `--collection` collides with main index (`S2-02`, `S2-03`):** Passing an existing collection name or failing before `QdrantZeroCodeStore.ensure_collection()` creates the collection must leave existing Qdrant collections and main PostgreSQL `Repository.last_indexed_sha` rows 100% untouched (`Task 2`).
- **Multi-line function headers vs leaked body lines inside `payload["imports"]` (`S2-04`, `S2-05`):** Body canary extraction must skip signature header lines using syntax body boundaries (not payload filtering!) so multi-line parameter lists never cause false positives while a genuine body line planted inside `payload["imports"]` or `payload["called_symbols"]` is caught by `zerocode_violations > 0` (`Task 3`).

---

## File Structure

- `src/codesight/rag/indexer.py`: Add `RepoIndexLockLease` & `CompositeRepoLockLease`, fail-closed `AsyncRepoIndexLock.hold()`, pre-write `ensure_held()` fencing across `InitialRepoIndexer` and `IncrementalBranchSyncer`, non-recursive subtree walk in `GitHubRepoIndexClient.fetch_git_tree()`, and cursor-update block when `tree.truncated` remains `True` (`S2-01`, `S2-06`).
- `src/codesight/cli/commands/index.py`: Render distinct incomplete-index status when `report.truncated_tree` is `True` and exit `1` after rendering so CLI callers cannot mistake a truncated run for complete (`S2-06`).
- `src/codesight/rag/qdrant_store.py`: Track `created_collection: bool` on `QdrantZeroCodeStore` when `ensure_collection()` actually creates a new Qdrant collection (`S2-03`).
- `src/codesight/rag/live_eval.py`: Add `isolated_eval_session_factory()`, `M2ProbeGroundTruth` / `M2RepoGroundTruth` + JSON loader + raw-source independent ground-truth oracle (`ast` stdlib for Python, lexical declaration/call/JSX scanner for TS/TSX, Markdown section matcher for docs), per-group `caller_recall_at_5` / `callee_recall_at_5` / `doc_recall_at_5` + `degraded_doc_cases` KPI validation, and payload-independent `_extract_body_canaries()` passed to both `audit_live_collection` and `scan_container_storage_for_canaries` (`S2-02`, `S2-04`, `S2-05`).
- `evals/m2_ground_truth.json`: Curated, independently verified ground-truth probes (callers, callees, architecture docs) for `Tieens-04/CodeSight-AI` (`S2-04`).
- `evals/run_m2_eval.py`: Reject existing `--collection` names before running, delete in `finally` only when `eval_store.created_collection` is `True`, run against `isolated_eval_session_factory()`, and wire `--ground-truth` (`S2-02`, `S2-03`, `S2-04`).
- `src/codesight/ast_engine/complexity.py`: Gate companion test file detection in `detect_missing_test_companion()` on `_CODE_SOURCE_EXTENSIONS` and `not is_ambient_declaration_path()` (`S2-07`).
- `tests/unit/test_repo_indexer_and_cli.py`, `tests/integration/test_incremental_branch_sync.py`, `tests/unit/test_m2_live_eval_metrics.py`, `tests/integration/test_week2_dual_lang_rag_e2e.py`, `tests/unit/test_complexity_metrics.py`: Comprehensive unit and live Docker integration tests for `S2-01`–`S2-07`.

---

### Task 1: Fail-Closed Redis Index Lock, Pre-Write Lease Fencing & Truncated Git Tree Resolution (`S2-01`, `S2-06`)

**Files:**
- Modify: `src/codesight/rag/indexer.py:459-508, 673-818, 1268-1634, 1764-2194`
- Modify: `src/codesight/cli/commands/index.py:221-253, 362-377`
- Test: `tests/unit/test_repo_indexer_and_cli.py`
- Test: `tests/integration/test_incremental_branch_sync.py`

**Interfaces:**
- Consumes: `RetryableTaskError`, `QdrantZeroCodeStore`, `upsert_repository_state`
- Produces:
  - `class RepoIndexLockLease` with `is_lost: bool` and `async def ensure_held(self) -> None` in `codesight.rag.indexer`
  - `class CompositeRepoLockLease` with `async def ensure_held(self) -> None` in `codesight.rag.indexer`
  - `AsyncRepoIndexLock.hold(self, scope_key: str) -> AsyncIterator[RepoIndexLockLease]` raising `RetryableTaskError` on Redis acquisition error (`S2-01`)
  - `GitHubRepoIndexClient.fetch_git_tree(...) -> GitTreeSnapshot` walking non-recursive `/git/trees/{sha}` subtrees when `?recursive=1` returns `truncated=True` (`S2-06`)
  - `IncrementalSyncReport.truncated_tree: bool = False` in `codesight.rag.indexer`

- [ ] **Step 1: Write failing unit tests for `S2-01` (Redis error on acquire + lock loss mid-sync/mid-index before writes) and `S2-06` (subtree traversal on truncated recursive tree + cursor preservation when still truncated)**

Update `_BrokenRedis` expectation in `tests/unit/test_repo_indexer_and_cli.py` and add dedicated tests:
1. `test_s2_01_repo_index_lock_fails_closed_on_redis_acquire_error`: Redis `ConnectionError` on `set(..., nx=True)` raises `RetryableTaskError` and never enters critical section in `hold()`, `index_repository()`, or `sync_push()`.
2. `test_s2_01_sync_push_and_initial_indexer_abort_writes_when_lock_lost_mid_run`: Simulate Push A (`sha_old`) fetching blobs while Push B (`sha_new`) replaces `repo_index_lock:acme/race` in Redis and writes new Qdrant points + Postgres SHA `sha_new`; assert Push A raises `RetryableTaskError` before `upsert_points` / `delete_by_file_paths` / `upsert_repository_state`, leaving Push B's Qdrant points and `sha_new` cursor intact (covering both fast-path `incremental_compare` and `_run_full_fallback`). Also test Redis error during pre-write verification and lease expiry after repeated renewal failures.
3. `test_s2_06_fetch_git_tree_walks_subtrees_when_recursive_truncated_and_guards_cursor`:
   - Case A: `/git/trees/{root}?recursive=1` returns `truncated=True` (omitting `pkg/sub/deep.py`), while non-recursive `/git/trees/{root}`, `/git/trees/{pkg}`, `/git/trees/{sub}` return `truncated=False` -> `InitialRepoIndexer` indexes 100% of files including `pkg/sub/deep.py`, sets `report.truncated_tree is False`, and persists `last_indexed_sha`.
   - Case B: Even non-recursive `/git/trees/{root}` returns `truncated=True` -> `InitialRepoIndexer` keeps `report.truncated_tree is True`, sets `report.persisted_cursor is False`, leaves PostgreSQL `last_indexed_sha` unchanged, and CLI `codesight index` reports incomplete truncated tree and exits with code `1`.

- [ ] **Step 2: Run targeted tests to verify RED**

Run: `uv run pytest tests/unit/test_repo_indexer_and_cli.py -k "s2_01 or s2_06 or lock_fallback" -v`
Expected: FAIL (`_BrokenRedis` enters critical section instead of raising `RetryableTaskError`; Push A deletes Push B's points; truncated tree skips subtrees and still persists cursor).

- [ ] **Step 3: Implement `RepoIndexLockLease`, fail-closed `AsyncRepoIndexLock`, pre-write fencing, and non-recursive subtree traversal**

In `src/codesight/rag/indexer.py`:
- Implement `RepoIndexLockLease` tracking `lease_deadline` (`monotonic + ttl_seconds`), `_lost_reason: str | None`, and `async def ensure_held(self) -> None` which checks `not is_lost` and `time.monotonic() < lease_deadline`, then calls `AsyncRepoIndexLock._verify_and_renew(key, token)` (raising `RetryableTaskError(..., retry_after_seconds=DEFAULT_LOCK_RETRY_AFTER_SECONDS)` on token mismatch, key expiry, or `RedisError`/`OSError`).
- In `AsyncRepoIndexLock.hold(scope_key)`: raise `RetryableTaskError` on `(redis.exceptions.RedisError, OSError)` during `set(..., nx=True)` instead of `break`ing fail-open; update `_renew` so `RedisError`/`OSError` returns `False` without falsely refreshing `lease_deadline`, while `_heartbeat` keeps retrying until `time.monotonic() >= lease_deadline` (marking lease lost on token mismatch or lease expiry) and skips `_release` if ownership was already lost.
- Wire `CompositeRepoLockLease.ensure_held()` before every Qdrant/PostgreSQL mutation in `InitialRepoIndexer._index_repository_unlocked` and `IncrementalBranchSyncer.sync_push` (forwarding `_lock_lease` into `_run_full_fallback` -> `index_repository`).
- In `GitHubRepoIndexClient.fetch_git_tree`: when `?recursive=1` returns `truncated=True`, run bounded BFS over non-recursive `/repos/{slug}/git/trees/{sha}` (`MAX_SUBTREE_REQUESTS = 1024`, `MAX_TREE_DEPTH = 32`, `DEFAULT_BLOB_CONCURRENCY`, skipping gitlink mode `160000` and visited `(prefix, sha)` pairs). If all visited directory nodes have `truncated=False`, return complete `GitTreeSnapshot(sha=..., truncated=False, entries=...)`; otherwise merge discovered entries and return `truncated=True`.
- In `InitialRepoIndexer._index_repository_unlocked`: skip `upsert_repository_state(..., last_indexed_sha=commit_sha)` when `tree.truncated` is `True` (`cursor_persisted = False`).
- In `src/codesight/cli/commands/index.py`: render `"not updated (incomplete: truncated Git tree)"` in `render_index_report()` and raise `typer.Exit(code=1)` after rendering when `report.truncated_tree` is `True`.

- [ ] **Step 4: Run unit and integration indexer tests to verify GREEN**

Run: `uv run pytest tests/unit/test_repo_indexer_and_cli.py tests/integration/test_incremental_branch_sync.py -v`
Expected: PASS (`0` failures).

- [ ] **Step 5: Commit Task 1**

```bash
git add src/codesight/rag/indexer.py src/codesight/cli/commands/index.py tests/unit/test_repo_indexer_and_cli.py tests/integration/test_incremental_branch_sync.py
git commit -m "fix(rag): enforce fail-closed Redis lock fencing and walk truncated Git subtrees"
```

---

### Task 2: Isolated PostgreSQL Database & Safe Qdrant Collection Lifecycle in `evals/run_m2_eval.py` (`S2-02`, `S2-03`)

**Files:**
- Modify: `src/codesight/rag/qdrant_store.py:82-197`
- Modify: `src/codesight/rag/live_eval.py`
- Modify: `evals/run_m2_eval.py:51-180`
- Test: `tests/unit/test_m2_live_eval_metrics.py`
- Test: `tests/integration/test_incremental_branch_sync.py`

**Interfaces:**
- Consumes: `Settings`, `asyncpg`, `QdrantZeroCodeStore`, `create_db_engine`, `create_session_factory`
- Produces:
  - `QdrantZeroCodeStore.created_collection -> bool` in `codesight.rag.qdrant_store` (`True` iff `ensure_collection()` executed `create_collection` successfully on this store instance)
  - `isolated_eval_session_factory(settings: Settings) -> AsyncIterator[tuple[Settings, async_sessionmaker[AsyncSession]]]` in `codesight.rag.live_eval`

- [ ] **Step 1: Write failing unit & integration tests for `S2-02` (eval CLI preserves main Postgres `last_indexed_sha` cursor) and `S2-03` (reject existing `--collection` and delete only collections created by the run)**

Add tests in `tests/unit/test_m2_live_eval_metrics.py` and `tests/integration/test_incremental_branch_sync.py`:
1. `test_s2_03_eval_cli_rejects_existing_collection_and_never_deletes_unowned_collection`:
   - Existing collection (`collection_exists` returns `True` pre-flight): `main()` returns `1`, prints error to `stderr`, and calls `delete_collection` `0` times even without `--keep-collection`.
   - Error before indexing (`isolated_eval_session_factory` or `run_milestone2_live_evaluation` raises before `store.ensure_collection()` creates collection): `delete_collection` is called `0` times.
   - Collection created by run (`store.created_collection` is `True`): `delete_collection` IS called once on both passing (`rc == 0`) and KPI-failing (`rc == 1`) runs when `--keep-collection` is omitted, and `0` times when `--keep-collection` is set.
2. `test_s2_02_eval_cli_uses_isolated_postgres_and_preserves_worker_cursor_live` (integration on `codesight-postgres` `5433` + `codesight-qdrant` `6333/6334`):
   - Seed a "main" database with `Repository(full_name=py_slug, last_indexed_sha=None)` (or an older SHA `sha_v0`).
   - Run `evals/run_m2_eval.py` pointing `Settings` at that "main" database while indexing `py_slug @ sha_v1`.
   - Assert main database still has `last_indexed_sha == sha_v0` (not `sha_v1`!), temporary `codesight_m2_eval_*` database was dropped cleanly, and a subsequent `IncrementalBranchSyncer.sync_push(after_sha=sha_v1)` against the main DB + main collection indexes `sha_v1` instead of returning `noop_already_indexed`.

- [ ] **Step 2: Run targeted tests to verify RED**

Run: `uv run pytest tests/unit/test_m2_live_eval_metrics.py tests/integration/test_incremental_branch_sync.py -k "s2_02 or s2_03" -v`
Expected: FAIL (existing collection is deleted in `finally`; main PostgreSQL `last_indexed_sha` is overwritten by `run_m2_eval.py`).

- [ ] **Step 3: Implement `QdrantZeroCodeStore.created_collection`, `isolated_eval_session_factory`, and hardened `evals/run_m2_eval.py`**

- In `src/codesight/rag/qdrant_store.py`: initialize `self._created_collection = False`; inside `ensure_collection()`, set `self._created_collection = True` immediately after `await self._client.create_collection(...)` succeeds; expose `@property def created_collection(self) -> bool`.
- In `src/codesight/rag/live_eval.py`: implement `@asynccontextmanager async def isolated_eval_session_factory(settings: Settings)` creating a temporary `codesight_m2_eval_<8hex>` database on `codesight-postgres` via `asyncpg.connect(dsn=settings.postgres_dsn)`, running `Base.metadata.create_all`, yielding `(eval_db_settings, session_factory)`, and in `finally` disposing the SQLAlchemy engine, terminating backends on `temp_db_name`, and executing `DROP DATABASE IF EXISTS "{temp_db_name}"`.
- In `evals/run_m2_eval.py`:
  - Validate `args.collection` format (`SAFE_IDENT_RE` and no `".."`).
  - Pre-check `if await qdrant_cleanup.collection_exists(args.collection): raise ValueError(...)` BEFORE creating the isolated DB or running evaluation.
  - Construct `eval_store = QdrantZeroCodeStore(settings=eval_db_settings, client=qdrant_cleanup, collection_name=args.collection)` inside `isolated_eval_session_factory(settings)` and pass `store=eval_store` to `run_milestone2_live_evaluation`.
  - In `finally`, call `delete_collection(args.collection)` ONLY when `eval_store is not None and eval_store.created_collection and not args.keep_collection`.

- [ ] **Step 4: Run targeted tests to verify GREEN**

Run: `uv run pytest tests/unit/test_m2_live_eval_metrics.py tests/integration/test_incremental_branch_sync.py -k "s2_02 or s2_03 or eval_cli" -v`
Expected: PASS (`0` failures).

- [ ] **Step 5: Commit Task 2**

```bash
git add src/codesight/rag/qdrant_store.py src/codesight/rag/live_eval.py evals/run_m2_eval.py tests/unit/test_m2_live_eval_metrics.py tests/integration/test_incremental_branch_sync.py
git commit -m "fix(eval): isolate eval CLI Postgres DB and guard pre-existing Qdrant collections"
```

---

### Task 3: Independent Multi-Tier Ground Truth, Per-Group KPI Gates & Payload-Independent NFR-3 Canaries (`S2-04`, `S2-05`)

**Files:**
- Create: `evals/m2_ground_truth.json`
- Modify: `src/codesight/rag/live_eval.py`
- Modify: `evals/run_m2_eval.py`
- Modify: `tests/integration/test_week2_dual_lang_rag_e2e.py:1750-1794`
- Test: `tests/unit/test_m2_live_eval_metrics.py`

**Interfaces:**
- Consumes: `HybridContextRetriever`, `GitHubBlobFetcher`, `audit_qdrant_zerocode.py` (`audit_live_collection`, `audit_payloads`, `scan_container_storage_for_canaries`)
- Produces:
  - `M2ProbeGroundTruth(file_path, symbol_name, expected_callers, expected_callees, expected_docs)` and `M2RepoGroundTruth(repo_full_name, language, probes)` in `codesight.rag.live_eval`
  - `load_m2_ground_truth(path: Path | str) -> dict[str, M2RepoGroundTruth]` and `build_independent_repo_ground_truth(...) -> M2RepoGroundTruth` in `codesight.rag.live_eval`
  - Per-group fields on `M2EvalCase`, `M2RepoEvalSummary`, and `M2LiveEvaluationReport`: `caller_recall_at_5`, `callee_recall_at_5`, `doc_recall_at_5`, `caller_cases_evaluated`, `callee_cases_evaluated`, `doc_cases_evaluated`, `degraded_doc_cases`
  - Updated `_extract_body_canaries(source_text, *, language, start_line, end_line, collected, max_canaries)` with ZERO payload filtering, passing `canary_substrings=scan_markers` to both payload audit and disk scan (`S2-05`)

- [ ] **Step 1: Write failing unit tests for `S2-04` (per-group caller/callee/doc Recall@5, `doc_search_degraded` rejection, AST-parser-miss detection via independent ground truth, curated JSON manifest loading) and `S2-05` (body line leaked into `payload["imports"]` caught by canary selection + payload auditor)**

Add tests in `tests/unit/test_m2_live_eval_metrics.py`:
1. `test_s2_04_validator_rejects_zero_callee_recall_low_doc_recall_and_degraded_doc_search`: Construct simulated Python + TypeScript reports/runs with `caller_recall_at_5=1.0`, `callee_recall_at_5=0.0`, and `doc_search_degraded=True`; assert `validate_m2_kpi_thresholds` flags callee recall, doc recall, and degraded doc search for both repos and overall.
2. `test_s2_04_independent_ground_truth_catches_missing_ast_called_symbols_edge`: Seed Qdrant points where `orders.py::submit_order_route` calls `compute_invoice_total(...)` in raw source bytes but Qdrant `payload["called_symbols"]` is `[]` (simulating a missed AST edge); assert `run_milestone2_live_evaluation` still puts `src/routers/orders.py::submit_order_route` into `expected_callers`, measures `caller_recall_at_5 < 0.85`, and fails `validate_m2_kpi_thresholds`.
3. `test_s2_04_curated_ground_truth_manifest_loads_and_verifies_real_codesight_targets`: Load `evals/m2_ground_truth.json`, verify every file/symbol/caller/callee/doc target exists in `CodeSight-AI` source files on disk, and verify `run_milestone2_live_evaluation(..., ground_truth=...)` enforces exact curated targets.
4. `test_s2_05_canary_selector_ignores_payload_and_catches_body_leak_inside_imports`: Plant a real function body line (`normalized_fee = (subtotal_cents * fee_bps) // 10_000`) inside valid metadata field `payload["imports"]`; assert `_extract_body_canaries` retains the canary, passes the identical `scan_markers` tuple to both `auditor_fn(..., canary_substrings=scan_markers)` and `disk_scanner_fn(..., scan_markers)`, and sets `report.zerocode_violations >= 1` when audited with real `audit_qdrant_zerocode.audit_payloads`. Also test multi-line function headers so parameter lines are never misclassified as body canaries.

- [ ] **Step 2: Run targeted tests to verify RED**

Run: `uv run pytest tests/unit/test_m2_live_eval_metrics.py -k "s2_04 or s2_05" -v`
Expected: FAIL (validator returns `[]` on `callee_recall_at_5=0.0` + `doc_search_degraded=True`; missed `called_symbols` disappears from `expected_callers`; body line in `imports` is filtered out of `body_canaries` and omitted from `auditor_fn`).

- [ ] **Step 3: Create `evals/m2_ground_truth.json` and implement `S2-04` + `S2-05` in `src/codesight/rag/live_eval.py` and `evals/run_m2_eval.py`**

- Author `evals/m2_ground_truth.json` with 3 human-verified cross-file probes for `Tieens-04/CodeSight-AI` (`src/codesight/rag/qdrant_store.py::upsert_points`, `src/codesight/diff/smart_diff_parser.py::parse_diff`, `src/codesight/vcs/blob_fetcher.py::fetch_slices`), each specifying verified cross-file callers, cross-file callees, and `AGENTS.md` architecture doc sections.
- In `src/codesight/rag/live_eval.py`:
  - Add `M2ProbeGroundTruth`, `M2RepoGroundTruth`, `load_m2_ground_truth()`, and `build_independent_repo_ground_truth()` (parsing raw `.py` blobs with Python stdlib `ast` respecting nested function/class scope boundaries, scanning raw `.ts`/`.tsx` blobs with comment/string-stripped declaration + call/JSX lexical analysis, and matching raw architecture `.md` headings/sections by domain/identifier overlap — zero reads of Qdrant `called_symbols` or `imports`).
  - Compute `caller_recall_at_5`, `callee_recall_at_5`, and `doc_recall_at_5` separately across non-empty target cases, track `caller_cases_evaluated`, `callee_cases_evaluated`, `doc_cases_evaluated`, and `degraded_doc_cases`, and enforce all three groups (`>= 0.85`, `>= 1` case per group per repo, `0` degraded doc cases) in `validate_m2_kpi_thresholds()`.
  - Rewrite `_extract_body_canaries()` to locate the function's true body start line from syntax (`ast` body start for Python, body `{` / `=>` for TS/TSX) without inspecting Qdrant payloads; pass `canary_substrings=scan_markers` to `active_auditor` AND also run `resolved_auditor.audit_payloads` over scrolled `repo_points` when using an in-memory/injected `auditor_fn` vs real auditor, ensuring identical `scan_markers` feed both payload and disk audits.
- Wire `--ground-truth` in `evals/run_m2_eval.py` and load `evals/m2_ground_truth.json` in `test_week2_dual_lang_rag_e2e.py` opt-in live test when matching configured repos.

- [ ] **Step 4: Run `test_m2_live_eval_metrics.py` and `test_week2_dual_lang_rag_e2e.py` to verify GREEN**

Run: `uv run pytest tests/unit/test_m2_live_eval_metrics.py tests/integration/test_week2_dual_lang_rag_e2e.py -v`
Expected: PASS (`0` failures; opt-in live test cleanly skipped only when `.env` lacks credentials).

- [ ] **Step 5: Commit Task 3**

```bash
git add evals/m2_ground_truth.json evals/run_m2_eval.py src/codesight/rag/live_eval.py tests/unit/test_m2_live_eval_metrics.py tests/integration/test_week2_dual_lang_rag_e2e.py
git commit -m "fix(eval): enforce independent ground-truth per-tier gates and payload-independent NFR-3 canaries"
```

---

### Task 4: Restrict Companion Test Line Counting to Supported Code Extensions (`S2-07`)

**Files:**
- Modify: `src/codesight/ast_engine/complexity.py:972-998`
- Test: `tests/unit/test_complexity_metrics.py`

**Interfaces:**
- Consumes: `_CODE_SOURCE_EXTENSIONS`, `is_ambient_declaration_path`, `ParsedPullRequestDiff`
- Produces: Hardened `detect_missing_test_companion(...) -> TestCompanionReport` ignoring documentation/fixture/snapshot/ambient files inside `test_globs`

- [ ] **Step 1: Write failing unit tests for `S2-07` (`tests/README.md` regression + `tests/test_service.py` Python control)**

Add `test_s2_07_docs_and_fixtures_in_tests_dir_do_not_suppress_p_untested` in `tests/unit/test_complexity_metrics.py`:
- Diff 1: `+51` Python logic lines in `src/service.py` + `+1` text line in `tests/README.md` (plus `+10` JSON lines in `tests/fixtures/data.json`, `+5` snapshot lines in `src/__tests__/App.test.tsx.snap`, and `+3` ambient lines in `tests/types.test.d.ts`) -> assert `report.added_source_logic_lines == 51`, `report.added_test_logic_lines == 0`, `report.test_files == ()`, and `report.is_untested is True`.
- Diff 2 (positive Python test control): Same `+51` lines in `src/service.py` + `+1` line in `tests/README.md` + `+3` Python test logic lines in `tests/test_service.py` -> assert `report.added_test_logic_lines == 3`, `report.test_files == ("tests/test_service.py",)`, and `report.is_untested is False`.

- [ ] **Step 2: Run targeted test to verify RED**

Run: `uv run pytest tests/unit/test_complexity_metrics.py -k "s2_07" -v`
Expected: FAIL (`report.added_test_logic_lines >= 1` and `report.is_untested is False` when `tests/README.md` is modified).

- [ ] **Step 3: Filter `detect_missing_test_companion` on `_CODE_SOURCE_EXTENSIONS` and `not is_ambient_declaration_path` before matching `test_matchers`**

In `src/codesight/ast_engine/complexity.py`:
Check `Path(norm_path).suffix.lower() in _CODE_SOURCE_EXTENSIONS and not is_ambient_declaration_path(norm_path)` before branching into `if _matches_any(norm_path, test_matchers): ... elif _matches_any(norm_path, source_matchers): ...`.

- [ ] **Step 4: Run complexity unit tests to verify GREEN**

Run: `uv run pytest tests/unit/test_complexity_metrics.py -v`
Expected: PASS (`0` failures).

- [ ] **Step 5: Commit Task 4**

```bash
git add src/codesight/ast_engine/complexity.py tests/unit/test_complexity_metrics.py
git commit -m "fix(ast): count only supported code extensions in detect_missing_test_companion"
```

---

### Task 5: Independent Whole-Branch Code Review & Full Mechanical Quality Gates

**Files:**
- Modify: `docs/sprint-2-bugs.md`, `docs/plans/2026-11-23-sprint2-bugs-remediation-plan.md` (commit tracked docs if clean)

**Interfaces:**
- Consumes: All changes from Tasks 1–4
- Produces: Clean `ponytail-review` audit, passing `ruff`, `mypy`, `pytest --cov`, `audit_qdrant_zerocode.py --self-test`, and `preclose_audit.py --ticket CA-16`

- [ ] **Step 1: Dispatch fresh `research` subagent running `ponytail-review` across `5d4c057..HEAD`**

Verify zero Must-fix / Important defects across lock fencing, subtree traversal, isolated Postgres/Qdrant cleanup, independent ground truth, NFR-3 canaries, and `P_untested` filtering. Apply any required fixes via RED->GREEN TDD.

- [ ] **Step 2: Execute all mechanical quality gates**

Run:
- `uv run ruff check .`
- `uv run ruff format --check .`
- `uv run mypy src evals`
- `uv run python .agents/skills/zerocode-ast-rag/scripts/audit_qdrant_zerocode.py --self-test`
- `uv run python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket CA-16`
Expected: All exit `0` with `0` lint/type issues, `0` failed tests, and `>= 80%` coverage across `src/codesight`.
