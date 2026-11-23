# Sprint 2 Remediation & Milestone 2 Hardening (CA-16) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate all High/Medium/selected-Low defects found in the Sprint 2 audit (`H1`–`H3`, `N1`–`N5`, `M1`–`M5`, `L3`–`L4`, formatting debt), execute the live Dual-Language AST-RAG evaluation on real Python/FastAPI and TypeScript/TSX repositories, and formally close `CA-16` plus Epics `CA-1` and `CA-2` before starting Sprint 3 (`CA-17`).

**Architecture:** Python AST extraction strips default parameter literals before `sanitize_signature()`, string literals are masked before `INLINE_BODY_LEAK_RE` checks across runtime and auditor scripts, and wave indexing isolates `ZeroCodeViolationError` per file so a single malformed header can never wedge a repository. By default (`INDEX_EMBED_CODE_CHUNKS=false`), `code` chunks receive a canonical unit vector `(1.0, 0.0, ...)` with `0` Gemini API calls while `architecture_doc` chunks use `gemini-embedding-001` behind a 60s-window `RetryInfo` + TPM limiter; `HybridContextRetriever` resolves receiver-qualified calls and enforces an import/class structural affinity gate on ambiguous method names (`get`, `update`, `save`, `create`, `run`). Mutual exclusion (`AsyncRepoIndexLock` with Lua TTL renewal), non-default branch guards, cursor rollback guards, and dedicated Celery indexing timeouts protect concurrent CLI and `push` webhook runs.

**Tech Stack:** Python `3.12` (`uv`), `tree-sitter` (`tree-sitter-python`, `tree-sitter-typescript`), `qdrant-client` (gRPC `6334`), `google-genai` (`gemini-embedding-001`, 768-dim Cosine), Celery `5.4+` + Redis `7` (`6381`), PostgreSQL `16` + SQLAlchemy `2.0` Async (`5433`), `httpx`, `typer`, `ruff`, `mypy --strict`, `pytest` + `pytest-cov`.

**Spec:** Sprint 2 Audit Report (`sprint2_review.md`) & Jira Tickets `CA-13`, `CA-14`, `CA-15`, `CA-16`, `CA-2` (`AGENTS.md` §1–§4).

## Global Constraints

- Active Git branch MUST be `feat/ca-16-sprint2-remediation` branched from `main` (`0ae909c`); NEVER commit directly to `main`, and NEVER stage or leave untracked `.env`, `*.pem`, or `*.key` files in `git status --porcelain`.
- Package management strictly via `uv` (`uv run ...`); never run global `pip install`; zero production `assert` statements inside `src/codesight/`.
- Container & Port Coexistence: NEVER stop, restart, or touch `flintflow-*` (`3001`, `6379`, `8080`) or `documind-*` (`3002`, `5432`, `6380`, `8081`, `9000`, `9001`); scope all Compose commands with `-p codesight` (`5433`, `6381`, `6333/6334`, `8082`).
- Zero-Code Persistence (`NFR-3`): Keep Qdrant payload keys strictly inside the 15-key allowlist in `.agents/skills/zerocode-ast-rag/scripts/audit_qdrant_zerocode.py` (`ALLOWED_KEYS`); never add unapproved top-level payload keys, never store parameter default literals or raw code bodies in Qdrant, and always release transient RAM snippets inside `finally`.
- Mechanical Quality Gates required before closing `CA-16`: `uv run ruff check .` (`0` errors), `uv run ruff format --check .` (`0` reformats), `uv run mypy src` (`0` issues), `uv run pytest --cov=src --cov-fail-under=80` (`>= 80%` coverage across core modules), `audit_qdrant_zerocode.py --self-test`, and `preclose_audit.py --ticket CA-16`.

## Review Focus

- **Complex Python parameter defaults (`DSN` credentials, nested calls `typer.Option("x", help="Fallback: if unset...")`, lambdas, `*args/**kwargs`):** Stripping defaults in `python_parser.py` must keep parameter names and type annotations intact while removing `0` characters of return annotations (`-> ReturnType`) and leaking `0` secret characters into `signature` (`Task 1`).
- **TypeScript string-literal union types containing colons/semicolons + keywords (`msg: "Note: if set" | 'Error: raise now'`):** Masking quoted literals in `_has_inline_body_leak()` must allow valid TS string-literal types while still rejecting genuine single-line arrow/function body leaks (`const f = () => { return 1 }`, `def f(): raise ValueError`) in both `src/codesight/rag/models.py` and `audit_qdrant_zerocode.py` (`Task 1`).
- **Mixed wave containing unchanged points, new `code` chunks, and new `architecture_doc` chunks under `embed_code_chunks=False`:** `code` chunks must emit `0` Gemini API requests, preserve Qdrant UUIDv5 deduplication (`skipped_existing`), store normalized 768-dim unit vectors `(1.0, 0.0, ...)`, and still embed new `architecture_doc` chunks in the same wave via `GeminiEmbedder` (`Task 2`).
- **Concurrent CLI `codesight index` vs long-running Celery `push` sync (> 90s) & out-of-order commit SHA:** `AsyncRepoIndexLock` renewal must extend the Redis lock only if the holder's token still matches, CLI must contend on the same lock key, and a stale run finishing late must not overwrite `Repository.last_indexed_sha` backwards (`Task 3`).
- **Modified `UserRepository.update()` vs unrelated `SessionStore.save()` across modules (`N3` + `M5`):** `HybridContextRetriever` must find callers that import `user_repository` / `UserRepository` or invoke `UserRepository.update` / `user_repo.update` (`Recall`), while rejecting `SessionStore.save()` in an unrelated package with zero import/class overlap (`Precision`) (`Task 4`).

---

## File Structure

- `.agents/skills/codesight-jira-workflow/scripts/preclose_audit.py`: Enforce `uv run ruff format --check .` alongside `ruff check`, `mypy`, and `pytest`.
- `.agents/skills/zerocode-ast-rag/scripts/audit_qdrant_zerocode.py`: Synchronize quote-masked `_has_inline_body_leak()` with `src/codesight/rag/models.py` and add canary coverage for stripped Python parameter defaults.
- `src/codesight/ast_engine/_ast_utils.py`: Add `count_syntax_error_nodes(root_node: Node) -> int` for baseline-relative syntax validation (`N4` / `CA-20`).
- `src/codesight/ast_engine/python_parser.py`: Strip `default_parameter` and `typed_default_parameter` values in `_extract_signature()` (`N2`, `H1`) and emit receiver-qualified call symbols (`ClassName.method`, `receiver.method`) alongside bare names (`N3`, `M5`).
- `src/codesight/ast_engine/typescript_parser.py`: Emit receiver-qualified call symbols (`ClassName.method` for `this.method()`, `receiver.method` for identifier member calls) alongside bare names (`N3`, `M5`).
- `src/codesight/ast_engine/registry.py`: Allow `_collect_removed_symbols()` to extract valid declarations when baseline TS files have localized `tree-sitter-typescript` grammar error nodes (`N4`).
- `src/codesight/ast_engine/models.py`: Remove `imports:` line from `CodeSymbolChunk.build_embedding_text()` (`H3`).
- `src/codesight/ast_engine/complexity.py`: Restrict `detect_missing_test_companion()` source line counting to code file extensions and standard multi-layout source globs (`L3`).
- `src/codesight/core/config.py`, `.env.example`, `docker-compose.yml`: Add `index_embed_code_chunks`, `gemini_embed_tpm_budget`, `gemini_embed_concurrency`, `index_task_soft_time_limit_seconds`, and `index_task_hard_time_limit_seconds`.
- `src/codesight/rag/models.py`: Add `CODE_CHUNK_PLACEHOLDER_VECTOR` and `mask_quoted_literals()` inside `_has_inline_body_leak()`.
- `src/codesight/rag/embedder.py`: Parse Google RPC `RetryInfo.retryDelay`, clamp `Retry-After` to `retry_cap_seconds=65.0` (`max_retries=5`), and enforce `_AsyncTPMLimiter` (`N5`, `L4`).
- `src/codesight/rag/indexer.py`, `src/codesight/cli/commands/index.py`, `src/codesight/db/repositories.py`: Per-file `ZeroCodeViolationError` isolation (`H1`), `embed_code_chunks=False` fast path (`N1`), `allow_non_default_branch=False` guard (`M1`), Lua heartbeat renewal in `AsyncRepoIndexLock` + CLI lock wiring + cursor rollback guard (`M2`).
- `src/codesight/worker/celery_app.py`, `src/codesight/worker/tasks/indexing_tasks.py`: Dedicated time limits (`300s`/`360s`) for `handle_push_event`, retryable `SoftTimeLimitExceeded` classification (`M4`), and explicit warning log when indexing credentials are absent (`N6`).
- `src/codesight/rag/retriever.py`: Catch `EmbeddingConfigError`, `ValueError`, and gRPC/Qdrant errors in `_search_architecture_docs()` (`M3`); split `BUILTIN_NOISE_SYMBOLS` vs `AMBIGUOUS_METHOD_SYMBOLS` with priority qualified lookup and structural import/class affinity gate (`N3`, `M5`).
- `src/codesight/rag/live_eval.py`, `evals/run_m2_eval.py`, `tests/integration/test_week2_dual_lang_rag_e2e.py`: Real-world FastAPI + TypeScript/TSX evaluation runner (`mypy --strict` checked) and live pytest gate asserting `Recall@5 >= 0.85`, `< 0.85s` retrieval latency, `tokens_saved_pct >= 45%`, and `0` Qdrant storage leaks (`H2`).

---

### Task 1: Branch Setup, Format Gate, Signature Sanitization & Per-File Guard Isolation (`H1`, `N2`, `N4`, `ruff format`)

**Files:**
- Modify: `.agents/skills/codesight-jira-workflow/scripts/preclose_audit.py:90-125`
- Modify: `.agents/skills/zerocode-ast-rag/scripts/audit_qdrant_zerocode.py:67-88`
- Modify: `src/codesight/ast_engine/_ast_utils.py`
- Modify: `src/codesight/ast_engine/python_parser.py:276-295`
- Modify: `src/codesight/ast_engine/registry.py:115-145`
- Modify: `src/codesight/rag/models.py:95-116`
- Modify: `src/codesight/rag/indexer.py:830-865, 1255-1325, 1840-1920`
- Test: `tests/unit/test_python_ast_parser.py`, `tests/unit/test_typescript_ast_parser.py`, `tests/unit/test_repo_indexer_and_cli.py`

**Interfaces:**
- Consumes: `tree_sitter.Node`, `ZeroCodeViolationError`
- Produces:
  - `count_syntax_error_nodes(root_node: Node) -> int` in `codesight.ast_engine._ast_utils`
  - `mask_quoted_literals(text: str) -> str` in `codesight.rag.models`
  - `_strip_python_header_span(definition: Node, start_byte: int, end_byte: int, source_bytes: bytes, *, skip_node: Node | None = None) -> str` in `codesight.ast_engine.python_parser`
  - `InitialIndexReport.skipped_guard_files: int = 0` and `IncrementalSyncReport.skipped_guard_files: int = 0` in `codesight.rag.indexer`

- [ ] **Step 1: Transition `CA-16` to `In Progress` (`21`), create branch `feat/ca-16-sprint2-remediation`, format existing drift, and add format check to `preclose_audit.py`**

Transition `CA-16` via `jira_transition_issue(issue_key="CA-16", transition="21")`. Checkout `git checkout -b feat/ca-16-sprint2-remediation`, run `uv run ruff format .` on the 10 unformatted files, and add `uv run ruff format --check .` to `check_python_gates()` in `.agents/skills/codesight-jira-workflow/scripts/preclose_audit.py`.

Run: `uv run ruff format --check .`
Expected: `104 files already formatted` (exit `0`).

- [ ] **Step 2: Write failing unit tests for `N2` (Python secret default stripping), `H1` (TS string-literal types + per-file indexer isolation), and `N4` (`count_syntax_error_nodes` + TS removed symbol resilience)**

Add tests in `tests/unit/test_python_ast_parser.py`, `tests/unit/test_typescript_ast_parser.py`, and `tests/unit/test_repo_indexer_and_cli.py`:

```python
def test_python_signature_strips_default_values_and_credentials() -> None:
    src = (
        "def connect(\n"
        "    dsn: str = 'postgresql://admin:S3cr3tP4ss@db/app',\n"
        "    api_key = 'sk-live-secret',\n"
        "    help_msg: str = typer.Option('x', help='Search: if empty, raise error'),\n"
        "    *args: int,\n"
        "    retries: int = 3,\n"
        ") -> bool:\n"
        "    return True\n"
    )
    chunks = PythonASTParser().parse(src, file_path="src/db.py").chunks
    assert len(chunks) == 1
    sig = chunks[0].signature
    assert sig == "def connect(dsn: str, api_key, help_msg: str, *args: int, retries: int) -> bool"
    assert "S3cr3tP4ss" not in sig
    assert "sk-live-secret" not in sig
    item = TransientEmbeddingItem.from_code_chunk(
        chunks[0], repo_full_name="acme/app", blob_sha="a1" * 20
    )
    assert "S3cr3tP4ss" not in str(item.payload)


def test_ts_string_literal_type_with_colon_keyword_passes_zerocode_guard() -> None:
    src = 'export const notify = (mode: "Note: if set" | "Error: return null"): string => mode;\n'
    chunks = TypeScriptASTParser().parse(src, file_path="src/notify.ts").chunks
    item = TransientEmbeddingItem.from_code_chunk(
        chunks[0], repo_full_name="acme/web", blob_sha="b2" * 20
    )
    assert item.payload["symbol_name"] == "notify"
```

Plus an indexer wave test in `test_repo_indexer_and_cli.py` injecting a monkeypatched chunk with a raw body leak in 1 of 3 files, asserting the other 2 files are indexed, `report.skipped_guard_files == 1`, and `report.persisted_cursor is True`.

- [ ] **Step 3: Run targeted tests to verify they fail**

Run: `uv run pytest tests/unit/test_python_ast_parser.py tests/unit/test_typescript_ast_parser.py tests/unit/test_repo_indexer_and_cli.py -k "strips_default_values or string_literal_type_with_colon or skipped_guard_files" -v`
Expected: FAIL (`S3cr3tP4ss` present in signature; `ZeroCodeViolationError` raised on TS string-literal type and unisolated indexer wave).

- [ ] **Step 4: Implement Python default stripping, quote-masked leak detection, per-file `ZeroCodeViolationError` isolation, and `count_syntax_error_nodes`**

1. In `src/codesight/ast_engine/python_parser.py`: implement `_strip_python_header_span()` removing comments and `= <default>` byte spans inside `default_parameter` and `typed_default_parameter` nodes within `definition.child_by_field_name("parameters")` while preserving parameter names, annotations, `*`/`**` markers, and return annotations (`-> ...`).
2. In `src/codesight/rag/models.py` and `.agents/skills/zerocode-ast-rag/scripts/audit_qdrant_zerocode.py`: add `mask_quoted_literals(text: str) -> str` (replacing characters inside `'...'`, `"..."`, and `` `...` `` with spaces while honoring backslash escapes) and apply it inside `_has_inline_body_leak()` prior to `INLINE_BODY_LEAK_RE` / `_PY_ONLY_INLINE_BODY_LEAK_RE`.
3. In `src/codesight/rag/indexer.py`: catch `ZeroCodeViolationError` per file in both `InitialRepoIndexer.index_repository` and `IncrementalBranchSynchronizer._run_incremental_compare`, log `repo_index_file_skipped_zerocode_guard` (`WARNING`), increment `skipped_guard_files`, remove the skipped path from `current_blob_by_path` so stale points are not retained silently, and continue the wave.
4. In `src/codesight/ast_engine/_ast_utils.py` and `registry.py`: add `count_syntax_error_nodes(root_node: Node) -> int` and update `_collect_removed_symbols()` so localized TS syntax error nodes do not blank out valid sibling symbol diffs when `before_parsed.chunks` is non-empty.

- [ ] **Step 5: Run tests, self-audit script, and commit**

Run: `uv run pytest tests/unit/test_python_ast_parser.py tests/unit/test_typescript_ast_parser.py tests/unit/test_ast_registry_and_pr_slicing.py tests/unit/test_repo_indexer_and_cli.py -v && uv run python .agents/skills/zerocode-ast-rag/scripts/audit_qdrant_zerocode.py --self-test && uv run ruff check . && uv run mypy src`
Expected: All PASS (`0` errors).

```bash
git add .agents/skills/ src/codesight/ast_engine/ src/codesight/rag/ tests/unit/ docs/plans/2026-11-22-ca-16-sprint2-remediation-plan.md
git commit -m "fix(ast,rag): strip python param defaults, mask string literals, and isolate per-file guard errors"
```

---

### Task 2: Zero-API Code-Chunk Vector Default, Lean Embedding Text & Quota-Resilient `GeminiEmbedder` (`N1`, `H3`, `N5`, `L4`)

**Files:**
- Modify: `src/codesight/ast_engine/models.py:89-106`
- Modify: `src/codesight/core/config.py:44-65`, `.env.example`, `docker-compose.yml:165-185`
- Modify: `src/codesight/rag/models.py:50-75`
- Modify: `src/codesight/rag/embedder.py:40-140, 250-365`
- Modify: `src/codesight/rag/indexer.py:970-1060, 1270-1290, 1520-1600, 1860-1875`
- Modify: `src/codesight/cli/commands/index.py:60-190`
- Modify: `src/codesight/worker/tasks/indexing_tasks.py:345-410`
- Test: `tests/unit/test_gemini_embedder_batching.py`, `tests/unit/test_repo_indexer_and_cli.py`, `tests/integration/test_incremental_branch_sync.py`

**Interfaces:**
- Consumes: `EMBEDDING_DIMENSIONS` (`768`), `Settings`
- Produces:
  - `CODE_CHUNK_PLACEHOLDER_VECTOR: Final[tuple[float, ...]] = (1.0,) + (0.0,) * 767` in `codesight.rag.models`
  - `Settings.index_embed_code_chunks: bool = False`, `Settings.gemini_embed_tpm_budget: int = 25_000`, `Settings.gemini_embed_concurrency: int = 2`
  - `parse_gemini_retry_delay(exc: BaseException, headers: Mapping[str, str]) -> float | None` in `codesight.rag.embedder`
  - `GeminiEmbedder(..., max_retries: int = 5, retry_cap_seconds: float = 65.0, tpm_budget: int | None = None)`
  - `InitialRepoIndexer(..., embed_code_chunks: bool = False)` and `IncrementalBranchSynchronizer(..., embed_code_chunks: bool = False)`

- [ ] **Step 1: Write failing unit tests for `H3` (no `imports:` in embedding text), `N1` (`embed_code_chunks=False` skips Gemini for code while embedding `architecture_doc`), and `N5`/`L4` (`RetryInfo.retryDelay`, capped `Retry-After`, TPM pacing)**

Add tests in `tests/unit/test_gemini_embedder_batching.py` and `tests/unit/test_repo_indexer_and_cli.py`:
- Assert `chunk.build_embedding_text()` contains `file:`, `symbol:`, `signature:`, `calls:` and `"imports:" not in chunk.build_embedding_text()`.
- Assert `InitialRepoIndexer` with default `embed_code_chunks=False` on a repo with 5 Python/TS files and 1 `docs/adr/001.md` sends **only** the `architecture_doc` items to `fake_genai`, upserts all code + doc points into Qdrant with `CODE_CHUNK_PLACEHOLDER_VECTOR` on code points, and makes `0` Gemini calls on a code-only repo.
- Assert `parse_gemini_retry_delay()` extracts `42.5` from a `ClientError(429)` carrying `details={"error": {"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "42.5s"}]}}`, `_compute_backoff_delay()` caps a `300s` `Retry-After` header at `retry_cap_seconds` (`65.0`), and `_AsyncTPMLimiter` sleeps via `sleep_fn` when cumulative estimated tokens (`chars // 4`) exceed `tpm_budget` within 60s.

- [ ] **Step 2: Run targeted tests to verify they fail**

Run: `uv run pytest tests/unit/test_gemini_embedder_batching.py tests/unit/test_repo_indexer_and_cli.py -k "imports or placeholder or retry_delay or tpm" -v`
Expected: FAIL (`imports:` still present; code chunks still embedded by default; `parse_gemini_retry_delay` undefined).

- [ ] **Step 3: Implement lean `build_embedding_text`, `embed_code_chunks=False` wave bypass, `RetryInfo` + 65s backoff cap, and `_AsyncTPMLimiter`**

1. Remove `if self.imports: lines.append(...)` from `CodeSymbolChunk.build_embedding_text()` in `src/codesight/ast_engine/models.py` (update any unit assertions in `test_gemini_embedder_batching.py` / `test_python_ast_parser.py` expecting `imports:` in embedding text; payload `imports` list remains untouched).
2. Add `index_embed_code_chunks: bool = False`, `gemini_embed_tpm_budget: int = Field(default=25_000, ge=1_000)`, and `gemini_embed_concurrency: int = Field(default=2, ge=1, le=10)` to `Settings` (`config.py`, `.env.example`, `docker-compose.yml`).
3. In `src/codesight/rag/embedder.py`:
   - Raise `DEFAULT_EMBED_MAX_RETRIES = 5` and `DEFAULT_EMBED_RETRY_CAP_SECONDS = 65.0`; clamp `retry_after` in `_compute_backoff_delay` to `[1.0, self._retry_cap_seconds]`.
   - Implement `parse_gemini_retry_delay(exc, headers)` inspecting `getattr(exc, "details", None)` for `google.rpc.RetryInfo` (`retryDelay` `"Ns"` / `"N.Ms"`) before falling back to `parse_retry_after_header(headers)`.
   - Add `_AsyncTPMLimiter(tpm_budget: int | None, sleep_fn=..., clock_fn=time.monotonic)` gating each batch inside `_embed_batch_with_retry` (enabled when `tpm_budget` is passed or `settings` is provided without a test `client`).
4. In `src/codesight/rag/indexer.py` (`_prepare_and_embed_wave_items` shared by `InitialRepoIndexer` and `IncrementalBranchSynchronizer`) & `src/codesight/cli/commands/index.py`:
   - Add `embed_code_chunks: bool = False` (and CLI `--embed-code-chunks/--no-embed-code-chunks`).
   - When `embed_code_chunks=False`: deduplicate & filter all wave items via `scoped_id_checker` once; wrap missing `chunk_type == "code"` items directly with `CODE_CHUNK_PLACEHOLDER_VECTOR` (`0` API calls) and clear their transient text immediately; invoke `self._embedder.embed_items(...)` **only** when missing `architecture_doc` items exist! Allow `embedder: GeminiEmbedder | None = None` when `GEMINI_API_KEY` is unset so code-only push syncs still index AST call graphs in dev/CI, raising `EmbeddingConfigError` only if `embed_code_chunks=True` or `architecture_doc` chunks require Gemini without a key.
   - Update existing unit tests in `test_repo_indexer_and_cli.py` that specifically assert code-chunk `context:\n` embedding (`test_transient_context_snippet...`) to pass `embed_code_chunks=True`.

- [ ] **Step 4: Run unit & integration suites to verify all pass**

Run: `uv run pytest tests/unit/test_gemini_embedder_batching.py tests/unit/test_repo_indexer_and_cli.py tests/integration/test_incremental_branch_sync.py -v && uv run ruff check . && uv run mypy src`
Expected: PASS (`0` errors).

- [ ] **Step 5: Commit**

```bash
git add src/codesight/ .env.example docker-compose.yml tests/
git commit -m "perf(rag): skip code-chunk Gemini calls by default, drop imports bloat, and harden 429 TPM backoff"
```

---

### Task 3: Branch Scope Guard, Lock Heartbeat Renewal, CLI Lock, Cursor Guard & Indexing Timeouts (`M1`, `M2`, `M4`, `N6`)

**Files:**
- Modify: `src/codesight/core/config.py`
- Modify: `src/codesight/db/repositories.py:18-135`
- Modify: `src/codesight/rag/indexer.py:660-740, 770-820, 1090-1350`
- Modify: `src/codesight/cli/commands/index.py:65-195`
- Modify: `src/codesight/worker/celery_app.py:60-75, 245-280, 560-660`
- Modify: `src/codesight/worker/tasks/indexing_tasks.py:240-410`
- Test: `tests/unit/test_repo_indexer_and_cli.py`, `tests/unit/test_worker_and_cli.py`, `tests/integration/test_incremental_branch_sync.py`

**Interfaces:**
- Consumes: `AsyncRepoIndexLock`, `billiard.exceptions.SoftTimeLimitExceeded`
- Produces:
  - `InitialRepoIndexer(..., repo_lock: AsyncRepoIndexLock | None = None)`
  - `InitialRepoIndexer.index_repository(..., allow_non_default_branch: bool = False) -> InitialIndexReport`
  - `upsert_repository_state(..., expected_previous_sha: str | None | object = _UNSET) -> tuple[str, bool]`
  - `Settings.index_task_soft_time_limit_seconds: int = 300`, `Settings.index_task_hard_time_limit_seconds: int = 360`

- [ ] **Step 1: Write failing tests for `M1` (non-default branch guard), `M2` (lock renewal + CLI lock contention + stale cursor rollback protection), and `M4` (`SoftTimeLimitExceeded` retryable + dedicated push task limits)**

Add tests in `tests/unit/test_repo_indexer_and_cli.py` and `tests/unit/test_worker_and_cli.py`:
- `M1`: Calling `indexer.index_repository("acme/branch", ref="feat/demo")` raises `ValueError("non-default branch")` without writing points to Qdrant unless `allow_non_default_branch=True`.
- `M2`:
  - `AsyncRepoIndexLock(..., ttl_seconds=3).hold("repo:1")` renews its TTL via Lua CAS while held past `2.2s`, and never renews if the lock token was replaced.
  - `run_cli_index()` contends on `AsyncRepoIndexLock` when a concurrent push sync holds the lock.
  - `upsert_repository_state(..., last_indexed_sha="old_sha", expected_previous_sha="prior_sha")` refuses to overwrite `Repository.last_indexed_sha` when DB already advanced to `"newer_sha"`, returning `(repo_id, False)`.
- `M4`:
  - `classify_retryable_exception(SoftTimeLimitExceeded()) == (True, 5.0)`.
  - `handle_push_event` task metadata has `soft_time_limit == 300` and `time_limit == 360`, and `PushSyncRunner` re-raises `SoftTimeLimitExceeded` as retryable without marking `WebhookDelivery.status = "failed"` prematurely.

- [ ] **Step 2: Run targeted tests to verify they fail**

Run: `uv run pytest tests/unit/test_repo_indexer_and_cli.py tests/unit/test_worker_and_cli.py -k "non_default_branch or lock_renew or expected_previous_sha or soft_time_limit" -v`
Expected: FAIL.

- [ ] **Step 3: Implement `M1`, `M2`, `M4`, and `N6` credential warning**

1. `M1` (`indexer.py`, `cli/commands/index.py`): Add `allow_non_default_branch: bool = False` (`--allow-non-default-branch` in CLI). Reject `not is_default_target and not allow_non_default_branch` with `ValueError` before Qdrant writes; update `test_repo_indexer_and_cli.py:1793` to assert default rejection plus explicit `allow_non_default_branch=True` behavior.
2. `M2` (`indexer.py`, `cli/commands/index.py`, `db/repositories.py`):
   - Add `_RENEW_LOCK_LUA` (`if redis.call("get", KEYS[1]) == ARGV[1] then return redis.call("expire", KEYS[1], tonumber(ARGV[2])) else return 0 end`) and spawn an `asyncio.Task` inside `AsyncRepoIndexLock.hold()` sleeping `max(0.5, self._ttl_seconds / 3.0)` to renew TTL until `finally` cancels it prior to `_release()`.
   - Accept `repo_lock: AsyncRepoIndexLock | None = None` on `InitialRepoIndexer`, acquire `repo_lock.hold(scope_key)` inside `index_repository()` (with an internal `_skip_repo_lock: bool = False` flag used when `IncrementalBranchSynchronizer` already holds `repo_lock` during full bootstrap/fallback so it never deadlocks!), and pass `AsyncRepoIndexLock(active_redis)` from `run_cli_index()`.
   - Add optimistic `expected_previous_sha` check in `apply_repo_upsert` / `upsert_repository_state`: read `initial_last_indexed_sha` at start of `index_repository()`, and only advance `last_indexed_sha` if DB still holds `initial_last_indexed_sha` (or `recreate=True`).
3. `M4` & `N6` (`config.py`, `celery_app.py`, `indexing_tasks.py`):
   - Add `index_task_soft_time_limit_seconds = 300` and `index_task_hard_time_limit_seconds = 360` to `Settings`; pass `soft_time_limit` and `time_limit` to `_typed_task` for `codesight.handle_push_event`.
   - Classify `billiard.exceptions.SoftTimeLimitExceeded` as `(True, 5.0)` in `classify_retryable_exception()` and treat it as retryable (`deferred` / retry) in `indexing_tasks.py`; emit `logger.warning("push_sync_skipped_missing_credentials", ...)` when push sync falls back to stub mode in dev (`N6`).

- [ ] **Step 4: Run unit & integration suites to verify all pass**

Run: `uv run pytest tests/unit/test_repo_indexer_and_cli.py tests/unit/test_worker_and_cli.py tests/integration/test_incremental_branch_sync.py -v && uv run ruff check . && uv run mypy src`
Expected: PASS (`0` errors).

- [ ] **Step 5: Commit**

```bash
git add src/codesight/ tests/
git commit -m "fix(indexer,worker): enforce branch guard, lock renewal, monotonic cursor, and push task timeouts"
```

---

### Task 4: Fault-Tolerant Doc Search, Receiver-Qualified Caller/Callee Precision & `P_untested` Filter (`M3`, `N3`, `M5`, `L3`)

**Files:**
- Modify: `src/codesight/ast_engine/python_parser.py:180-255, 345-415`
- Modify: `src/codesight/ast_engine/typescript_parser.py:340-430, 490-545`
- Modify: `src/codesight/rag/retriever.py:45-185, 385-510, 595-840`
- Modify: `src/codesight/ast_engine/complexity.py:95-120, 944-990`
- Test: `tests/unit/test_python_ast_parser.py`, `tests/unit/test_typescript_ast_parser.py`, `tests/unit/test_complexity_analyzer.py`, `tests/integration/test_hybrid_retriever.py`

**Interfaces:**
- Consumes: `QdrantZeroCodeStore.find_by_called_symbols`, `QdrantZeroCodeStore.find_by_symbol_names`
- Produces:
  - `BUILTIN_NOISE_SYMBOLS: Final[frozenset[str]]` and `AMBIGUOUS_METHOD_SYMBOLS: Final[frozenset[str]]` in `codesight.rag.retriever`
  - Receiver-qualified tokens (`"<ClassName>.<method>"`, `"<receiver>.<method>"`) appended alongside bare `"<method>"` in `CodeSymbolChunk.called_symbols` for both Python and TypeScript/TSX

- [ ] **Step 1: Write failing unit & integration tests for `M3`, `N3` + `M5`, and `L3`**

Add tests in `tests/integration/test_hybrid_retriever.py` and `tests/unit/test_complexity_analyzer.py`:
- `M3`: When `embedder.embed_query` raises `EmbeddingConfigError`, `ValueError`, or `store.search_similar` raises a gRPC `RpcError` / `RuntimeError`, `retriever.retrieve()` returns `doc_search_degraded is True` while still returning valid `callers` and `callees`.
- `N3` + `M5` (Python & TS):
  - Index `UserRepository.update` (`src/repos/user_repository.py`), `UserRouter.patch_user` (`from src.repos.user_repository import UserRepository; repo.update(u)`), `AuditStore.update` (`src/audit/store.py`), and `BillingWorker.save` / `SessionCache.save` (unrelated packages with disjoint imports).
  - Modifying `UserRepository.update` returns `UserRouter.patch_user` in `result.callers` (`N3` fixed: `update` is no longer dropped!) and excludes unrelated `CronJob.run_cleanup` calling `cache.update()` without importing `user_repository` (`M5` fixed!).
  - Modifying `BillingWorker.save` excludes `SessionCache` callers that only call `session_cache.save()` with zero import/class/file overlap (`M5` fixed!).
- `L3`: `detect_missing_test_companion()` ignores `src/data/seed.json` and `src/config/schema.yaml` (> 100 added lines) so `added_source_logic_lines == 0`, while detecting > 50 added Python lines in `app/services/order.py` (non-`src/` layout) as `is_untested is True`.

- [ ] **Step 2: Run targeted tests to verify they fail**

Run: `uv run pytest tests/integration/test_hybrid_retriever.py tests/unit/test_complexity_analyzer.py -k "degraded or ambiguous or update or seed_json" -v`
Expected: FAIL.

- [ ] **Step 3: Implement receiver-qualified calls, `M3` doc degradation guard, `N3`/`M5` ambiguity affinity gate, and `L3` code-extension filter**

1. **AST Qualified Call Extraction (`python_parser.py`, `typescript_parser.py`):**
   - Pass `enclosing_class: str | None` (`event.scope_stack[-1]` when inside a class) into `_extract_called_symbols` / `_extract_ts_called_symbols`.
   - For `self.m()` / `cls.m()` (Python) and `this.m()` / `super.m()` (TS) when `enclosing_class` is known, emit both `"m"` and `f"{enclosing_class}.m"`.
   - For simple identifier receiver calls `obj.m()` (excluding chained calls), emit both `"m"` and `"obj.m"`. Keep Qdrant payload keys strictly unchanged (`called_symbols: list[str]`).
2. **Doc Search Isolation (`M3` in `retriever.py`):**
   - In `_search_architecture_docs()`, catch `(EmbeddingConfigError, RetryableTaskError, RateLimitTaskError, ValueError, TimeoutError, ConnectionError, OSError, RuntimeError)` and any non-base `Exception` except `ZeroCodeViolationError` (so gRPC `RpcError` and Qdrant client errors never tear down `asyncio.gather`), logging `hybrid_retriever_doc_search_degraded` (`error_type=type(exc).__name__`) and returning `((), True)`.
3. **Caller/Callee Precision & Recall (`N3` + `M5` in `retriever.py`):**
   - Separate `BUILTIN_NOISE_SYMBOLS` (language/stdlib builtins never defined as domain targets: `len`, `print`, `range`, `isinstance`, `super`, `dict`, `list`, `str`, `int`, `console`, `Math`, `JSON`, `Promise`, `Array`, `Object`, etc.) from `AMBIGUOUS_METHOD_SYMBOLS` (`get`, `set`, `update`, `delete`, `remove`, `find`, `parse`, `read`, `write`, `open`, `close`, `match`, `resolve`, `filter`, `format`, `test`, `save`, `create`, `run`, `execute`, `handle`, `process`, `build`, `load`, `init`, `validate`, `send`, `fetch`).
   - For modified chunks: emit `<ClassName>.<method>` (when `qualified_name` has a class prefix) into **priority specific symbols**, and allow `AMBIGUOUS_METHOD_SYMBOLS` into a secondary lookup (never dropped!). Run Qdrant `find_by_called_symbols` / `find_by_symbol_names` separately for specific symbols vs ambiguous symbols so ambiguous hits never crowd out specific hits inside the scroll window.
   - Enforce **Ambiguous Match Affinity Gate**: if a candidate's `matched_symbols` consists solely of bare names in `AMBIGUOUS_METHOD_SYMBOLS`, keep it ONLY when at least one holds: (a) same file (`cand_file_path in footprint.modified_file_paths`), (b) qualified receiver match (`f"{enclosing_class}.{sym}"` or `f"{snake_class}.{sym}"` in `cand_called_symbols`), or (c) direct import token overlap between caller `imports` and `{modified_stems, modified_enclosing_classes}` (for callers) or `callee_stem` in `modified_import_sets` (for callees).
4. **`P_untested` Filter (`L3` in `complexity.py`):**
   - Add `_CODE_SOURCE_EXTENSIONS = frozenset({".py", ".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"})` check in `detect_missing_test_companion()` before counting `added_source_logic`, and expand `DEFAULT_SOURCE_GLOBS` to `("src/**", "app/**", "backend/**", "frontend/src/**", "packages/*/src/**")`.

- [ ] **Step 4: Run full unit + integration retriever/AST tests**

Run: `uv run pytest tests/unit/test_python_ast_parser.py tests/unit/test_typescript_ast_parser.py tests/unit/test_complexity_analyzer.py tests/integration/test_hybrid_retriever.py tests/integration/test_week2_dual_lang_rag_e2e.py -v && uv run ruff check . && uv run mypy src`
Expected: All PASS (`0` errors).

- [ ] **Step 5: Commit**

```bash
git add src/codesight/ tests/
git commit -m "fix(rag,ast): isolate doc search errors, add receiver-aware caller matching, and refine P_untested filter"
```

---

### Task 5: Real-World FastAPI + TS/TSX Live Evaluation Harness & Sprint 2 Jira Closure (`H2`, `CA-16`, `CA-1`, `CA-2`)

**Files:**
- Create: `src/codesight/rag/live_eval.py`, `evals/run_m2_eval.py`
- Modify: `tests/integration/test_week2_dual_lang_rag_e2e.py:1734-1853`
- Test: `tests/unit/test_m2_live_eval_metrics.py`, `tests/integration/test_week2_dual_lang_rag_e2e.py`

**Interfaces:**
- Consumes: `run_cli_index`, `HybridContextRetriever`, `GitHubBlobFetcher`, `TokenBudgetPacker`, `audit_qdrant_zerocode`
- Produces:
  - `M2EvalCase`, `M2RepoEvalSummary`, `M2LiveEvaluationReport` dataclasses in `codesight.rag.live_eval`
  - `compute_recall_at_k(retrieved_items: Sequence[str], expected_targets: AbstractSet[str], *, k: int = 5) -> float`
  - `validate_m2_kpi_thresholds(report: M2LiveEvaluationReport) -> list[str]`
  - `async def run_milestone2_live_evaluation(*, settings: Settings, py_repo: str, ts_repo: str, collection_name: str, session_factory: async_sessionmaker[AsyncSession]) -> M2LiveEvaluationReport`

- [ ] **Step 1: Write unit tests for `codesight.rag.live_eval` metric calculations (`Recall@5`, warm latency `< 850ms`, `tokens_saved_pct` band, non-empty `ts_repo` enforcement)**

Create `tests/unit/test_m2_live_eval_metrics.py` testing `compute_recall_at_k(retrieved_items, expected_targets, k=5) -> float` and `validate_m2_kpi_thresholds(report)` asserting violations when `ts_repo` is blank, `recall_at_5 < 0.85`, `p50_retrieval_ms >= 850.0`, `tokens_saved_pct < 45.0`, or `zerocode_violations > 0`.

- [ ] **Step 2: Run unit test to verify it fails**

Run: `uv run pytest tests/unit/test_m2_live_eval_metrics.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'codesight.rag.live_eval'`).

- [ ] **Step 3: Implement `src/codesight/rag/live_eval.py`, CLI `evals/run_m2_eval.py`, and upgrade `test_week2_opt_in_live_github_and_gemini_rag_smoke`**

1. Create `src/codesight/rag/live_eval.py` (with thin CLI entrypoint `evals/run_m2_eval.py`):
   - Require **both** `py_repo` (default `Tieens-04/CodeSight-AI` or `CODESIGHT_LIVE_PY_REPO`) and `ts_repo` (`CODESIGHT_LIVE_TS_REPO`, auto-discovering a real TypeScript/TSX repo via GitHub API user repos or explicit env slug — failing loudly instead of silently skipping if empty).
   - Index both repos via `run_cli_index` into an isolated Qdrant collection with production `index_embed_code_chunks=False` and live `GeminiEmbedder` (`gemini-embedding-001`, 768-dim).
   - Construct deterministic cross-file modification probes from live indexed Qdrant graph edges (selecting service/helper symbols that have real cross-file callers in Qdrant + repository architecture docs `README.md`/`docs/**`), execute `retriever.retrieve_and_pack(...)` using **production `DEFAULT_DOC_SCORE_THRESHOLD`** (recording raw live `gemini-embedding-001` cosine similarity scores; if 768-dim `RETRIEVAL_QUERY` vs `RETRIEVAL_DOCUMENT` cosine scores empirically cluster below `0.65`, calibrate `DEFAULT_DOC_SCORE_THRESHOLD` based on measured data and document exact scores), and fetch slices into RAM via live `GitHubBlobFetcher`.
   - Measure warm retrieval + RAM blob fetch + token pack latency (`< 850ms`), `Recall@5 >= 0.85`, `tokens_saved_pct >= 45.0%`, and run `audit_live_collection(..., scan_all=True, strict_indexes=True) == 0` plus `/qdrant/storage` disk check.
2. Update `test_week2_opt_in_live_github_and_gemini_rag_smoke` in `tests/integration/test_week2_dual_lang_rag_e2e.py` to call `run_milestone2_live_evaluation(...)` and assert every Milestone 2 KPI threshold (`Recall@5 >= 0.85`, `retrieval_ms < 850.0`, `tokens_saved_pct >= 45.0`, `zerocode_violations == 0`).

- [ ] **Step 4: Run offline unit + integration test suite & verify `.env` readiness for live execution**

Run: `uv run pytest --cov=src --cov-fail-under=80 && uv run ruff check . && uv run ruff format --check . && uv run mypy src`
Expected: 100% offline tests PASS, coverage `>= 80%`.

Then pause and request user to populate `GEMINI_API_KEY` and `GITHUB_TOKEN` (and optional `CODESIGHT_LIVE_PY_REPO` / `CODESIGHT_LIVE_TS_REPO`) in `.env` (confirming `.env` is ignored by Git), and execute the live Milestone 2 gate:

Run: `CODESIGHT_RUN_LIVE_M2_EVAL=1 uv run pytest tests/integration/test_week2_dual_lang_rag_e2e.py -k live -v -s`
Expected: `1 passed` with printed `M2LiveEvaluationReport` JSON metrics (`Recall@5 >= 85%`, `< 850ms`, `tokens_saved_pct >= 45%`, `0` NFR-3 leaks).

- [ ] **Step 5: Run `preclose_audit.py --ticket CA-16`, commit, open/merge PR, and close `CA-16`, `CA-1`, and `CA-2` on Jira**

1. Transition `CA-16` to `Verify / Test` (`31`) and run:
   `uv run python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket CA-16`
   `uv run python .agents/skills/zerocode-ast-rag/scripts/audit_qdrant_zerocode.py --self-test`
   Expected: `[PASS] All CodeSight-AI pre-close gates passed.`
2. Commit `evals/` + test updates, push `feat/ca-16-sprint2-remediation`, create and merge GitHub PR into `main`.
3. Post structured **Verification Evidence** comment (following `codesight-jira-workflow` template with real commit SHA, test counts, coverage `%`, and live Milestone 2 telemetry) on `CA-16` via `jira_add_comment`, then transition `CA-16` to `Done` (`41`).
4. Post Milestone 1 & Milestone 2 completion summary comments on Epics `CA-1` and `CA-2` (`100%` child tickets `Done`), and transition both `CA-1` and `CA-2` through `21` -> `31` -> `41` (`Done`).
