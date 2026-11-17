# CA-7 Design Specification — Day 3 Celery + Redis Reliability, Cancel-on-Supersede Lock & `.codesight.yaml` Reader

- **Jira Ticket:** `CA-7` (*[Day 3] Hàng đợi Celery + Redis, Khóa Cancel-on-Supersede & Trình đọc .codesight.yaml*)
- **Parent Epic / Sprint:** `CA-1` — `Sprint 1` (Board `74`)
- **Date:** 2026-11-17
- **Target Branch:** `feat/ca-7-day3-celery-supersede-config` (branched from `main` @ `cf90b0e`)
- **Requirements Covered:** `FR-01` (Async PR Review Queue & Debounce), `FR-02` (Custom `ignore_paths` input for `FileFilter`), `NFR-1` (`0s` delay on `pull_request.opened` for `< 25s` E2E SLA; `5s` debounce on `synchronize`), `NFR-2` (`acks_late=True` + Exponential Backoff + Jitter + `Retry-After` resilience), `NFR-3` (Zero-Code Persistence via `ephemeral_workspace()` & zero raw config logging), `NFR-4` (Structured JSON logging with `trace_id`).

---

## 1. Purpose & Scope Boundaries

`CA-7` hardens CodeSight-AI's asynchronous Celery + Redis execution layer (`6381`), unifies the `Cancel-on-Supersede` lock contract across the FastAPI Webhook Gateway and Celery Worker pipeline, delivers the cross-platform `ephemeral_workspace()` guard guaranteeing 100% temporary directory destruction (`NFR-3`), and implements the fault-tolerant `.codesight.yaml` repository configuration reader (`Pydantic v2` + `PyYAML`) over the GitHub Contents API.

### In Scope (`CA-7`)
1. Add `pyyaml>=6.0.2` (runtime) and `types-pyyaml>=6.0.12` (dev group for `mypy --strict`) via `uv` (`pyproject.toml` + `uv.lock`).
2. Implement `src/codesight/worker/supersede_lock.py` as the single source of truth for `pr_active:{repo_id}:{pr_number}` and `pr_active_task:{repo_id}:{pr_number}` (`TTL = 86400s`, `synchronize` debounce `5s`, `opened`/`reopened` delay `0s`), providing atomic Lua Compare-And-Set scripts (`AsyncSupersedeLock`), synchronous worker helpers (`SyncSupersedeLock`, `is_pr_event_superseded`), `PRSupersededError`, and fail-open Redis degradation; refactor `src/codesight/api/routes/webhooks.py` to delegate supersede state mutations to `AsyncSupersedeLock`.
3. Upgrade `src/codesight/worker/celery_app.py` with `task_acks_late=True`, `task_reject_on_worker_lost=True`, `worker_prefetch_multiplier=1`, `task_soft_time_limit=60`, `task_time_limit=90`, custom `CodeSightTask(celery.Task)` (`max_retries=3`, Exponential Backoff `2s -> 4s -> 8s` + jitter, `Retry-After <= 60s` handling on `HTTP 429`, supersede-before-retry short-circuit, zero retries on permanent `4xx`), and the `execute_pr_review_lifecycle()` orchestration spine enforcing a mandatory pre-publish supersede check.
4. Implement `src/codesight/core/ephemeral.py` (`ephemeral_workspace()`, `async_ephemeral_workspace()`, and `resolve_safe_subpath()`) targeting `/tmp` (`128m` RAM `tmpfs` in Docker) with `0o700` POSIX permissions and Python 3.12 `shutil.rmtree(..., onexc=...)` read-only bit recovery on Windows.
5. Implement `src/codesight/config_reader/repo_config.py` (`RepoConfig`, `parse_repo_config_yaml()`, `RepoConfigLoader`) with **Trust-on-Default-Branch** ref resolution, `.codesight.yaml` -> `.codesight.yml` fallback, `64 KiB` size ceiling, `yaml.safe_load`, deterministic `config_hash` (`SHA-256` 16-hex prefix for `CA-21`), atomic fallback to defaults on malformed YAML/schema errors, and transient `429`/`5xx` propagation to Celery retry; provide documented `.codesight.example.yaml` at repo root.
6. Deliver comprehensive unit & Redis-backed tests (`tests/unit/test_supersede_lock.py`, `tests/unit/test_repo_config.py`, and expanded `tests/unit/test_worker_and_cli.py`) maintaining `>= 80%` branch coverage and keeping all 33 existing `CA-5`/`CA-6` tests green.

### Explicitly Out of Scope (Deferred to Subsequent Tickets)
- `FileFilter` default exclusion rules, `SmartDiffParser`, and `DiffHunkMapper` (`CA-8`).
- `GitHubReviewPublisher` (`Checks API` & `POST .../pulls/{pr}/reviews`) and `codesight review --dry-run` CLI command (`CA-9`).
- Multi-agent LangGraph nodes (`CA-17..CA-20`), `TechnicalDebtScorer`, and `sha_cache.py` (`CA-21`).

---

## 2. `Cancel-on-Supersede` Lock Architecture (`src/codesight/worker/supersede_lock.py`)

### 2.1 Redis Key Contract & Timing Constants
- **`pr_active_sha_key(repo_id: str, pr_number: int) -> str`:** Returns `"pr_active:{repo_id}:{pr_number}"`.
- **`pr_active_task_key(repo_id: str, pr_number: int) -> str`:** Returns `"pr_active_task:{repo_id}:{pr_number}"`.
- **`PR_SUPERSEDE_TTL_SECONDS: Final[int] = 86_400`** (`24` hours).
- **`PR_SYNCHRONIZE_DEBOUNCE_SECONDS: Final[int] = 5`** (`countdown=0` for `opened` and `reopened`; `countdown=5` for `synchronize`).
- **Stale Delivery Ack Contract:** When an out-of-order stale PR webhook is skipped at dispatch time, `CeleryWebhookDispatcher.dispatch_pull_request()` returns `None` and `POST /api/v1/webhooks/github` returns `HTTP 200` `WebhookAckResponse(status="superseded", task_id=None)` while persisting `webhook_events.status = 'ignored'` (preventing duplicate redeliveries without conflating skipped deliveries with queued tasks).
- **`PRSupersededError(Exception)`:** Carries `repo_id: str`, `pr_number: int`, `head_sha: str`, `active_head_sha: str`, and `stage: str` (`"entry"`, `"pre_retry"`, `"pre_llm"`, `"pre_publish"`). Treated as a clean cooperative cancellation — never retried.

### 2.2 Multi-Checkpoint Lifecycle & Atomic Lua CAS Scripts
```text
Developer pushes SHA_1 (opened)          Developer pushes SHA_2 (synchronize, +1.5s)
        │                                                │
        ▼                                                ▼
Gateway claims pr_active = SHA_1 (1 RTT Lua)     Gateway claims pr_active = SHA_2 (1 RTT Lua)
Enqueues Task T1 (countdown = 0s)                Revokes T1 (terminate=False)
        │                                        Enqueues Task T2 (countdown = 5s)
        ▼                                                │
T1 starts execution:                                     │
  ├─ Checkpoint 1 (Entry): GET pr_active ────────────────┤ (if already SHA_2 -> exit "superseded" in <2ms)
  ├─ Ephemeral Workspace + Load RepoConfig               │
  ├─ [Downstream Diff / RAG / LLM Pipeline]              │
  ├─ Retry Guard (if 429/timeout): GET pr_active ────────┤ (if SHA_2 -> cancel retry, return "superseded")
  └─ Checkpoint N (Mandatory Pre-Publish Gate):          │
       ensure_active(stage="pre_publish") ───────────────┘ (sees SHA_2 != SHA_1 -> raises PRSupersededError,
                                                            0 stale comments posted to GitHub!)
```

- **`AsyncSupersedeLock` (used by `webhooks.py` and async worker stages):**
  - `claim_active_sha(repo_id, pr_number, head_sha, *, ttl_seconds=86400, before_sha=None, event_ts_ms=None) -> SupersedeClaim`:
    - Executes a 1-RTT Lua script reading `(GET sha_key, GET task_key, GET ts_key)` and writing `SET sha_key head_sha EX ttl` atomically while guarding against out-of-order GitHub webhook delivery:
      - Records superseded `before_sha` values in `pr_active_ancestors:{repo_id}:{pr_number}` (`Redis SET`, `TTL = 86400s`) and monotonic `pull_request.updated_at` epoch-ms in `pr_active_ts:{repo_id}:{pr_number}`.
      - If an incoming non-direct-successor commit (`before_sha != active_head_sha`) is already in `pr_active_ancestors` or carries `event_ts_ms < active_ts_ms`, the Lua script returns `claimed=False, stale=True` so `CeleryWebhookDispatcher` skips publishing/revoking and logs `webhook_pr_out_of_order_superseded`.
      - Intentional force-push reverts (`before_sha == active_head_sha`) bypass the stale rejection so reverting back to a prior SHA succeeds cleanly.
      - **Known limitation:** 3+ rapid non-adjacent force-pushes reordered within the same 1-second GitHub `updated_at` timestamp resolution remain ambiguous without external commit-graph ancestry queries.
    - If an injected unit-test Redis double lacks `.eval`, gracefully falls back to sequential `get`/`set` plus matching Redis Set (`sismember`/`srem`/`sadd`/`scard`/`expire`) operations on `pr_active_ancestors` so lightweight test doubles remain compatible without `WRONGTYPE` divergence.
  - `rollback_active_sha(repo_id, pr_number, *, attempted_sha, prev_sha, ttl_seconds=86400, added_ancestor=None, removed_ancestor=None, prev_ts_ms=None) -> None`:
    - Executes a Lua Compare-And-Set (`CAS`) script: restores `prev_sha` (or `DEL sha_key` when `prev_sha` is `None`), reverses only the exact `added_ancestor` / `removed_ancestor` Set delta recorded on `SupersedeClaim` (preserving pre-existing ancestors and restoring reverted SHAs), and restores `pr_active_ts` **only if** `GET(sha_key) == attempted_sha`. Prevents a failed broker publish on `SHA_2` from clobbering `SHA_3` if `SHA_3` arrived concurrently.
  - `record_active_task(repo_id, pr_number, *, head_sha, task_id, ttl_seconds=86400) -> None`:
    - Executes a Lua CAS script setting `task_key = task_id` (`EX ttl`) **only if** `GET(sha_key) == head_sha`.
  - `check_superseded(repo_id, pr_number, head_sha) -> tuple[bool, str | None]` and `ensure_active(repo_id, pr_number, head_sha, *, stage: str = "pre_publish") -> None`:
    - Returns `(True, active_sha)` / raises `PRSupersededError` iff `active_sha` is non-empty and `active_sha != head_sha`.
    - When key is absent (`None`) or Redis raises `RedisError`/`OSError`, logs `"supersede_check_unavailable"` (`WARNING`, `error_type=type(exc).__name__`) on errors and **fails open** (`(False, None)`), consistent with `CA-6` Redis cache resilience.
- **`SyncSupersedeLock` & `is_pr_event_superseded(payload, *, redis_client=None, redis_from_url=None) -> bool`:**
  - Synchronous counterpart over `redis.Redis` (`socket_timeout=0.5`) for Celery worker task entry and retry guards; owned clients are always closed in `finally`. `celery_app.py` preserves its `redis_sync` import and passes `redis_from_url=redis_sync.Redis.from_url` so existing `CA-6` monkeypatch hooks remain 100% compatible.

---

## 3. Celery Reliability, Retry Policy & Lifecycle Spine (`src/codesight/worker/celery_app.py`)

### 3.1 Worker & Broker Configuration (`create_celery_app`)
| Setting | Value | Rationale |
| :--- | :--- | :--- |
| `task_acks_late` | `True` | Broker retains message until task finishes (`NFR-2` crash recovery) |
| `task_reject_on_worker_lost` | `True` | Requeues unacked task if worker process is killed (`SIGKILL` / OOM) |
| `worker_prefetch_multiplier` | `1` | Prevents one worker slot from hoarding debounced tasks |
| `task_soft_time_limit` / `task_time_limit` | `60` / `90` (seconds) | Bounds hung network calls while staying far below Redis `visibility_timeout` (`3600s`) |
| `task_default_retry_delay` | `2` (seconds) | Base delay for exponential backoff |

### 3.2 Explicit Retry Classification & Backoff + Jitter (`CodeSightTask`)
- **Domain Retry Exceptions (`src/codesight/core/errors.py`, re-exported by `celery_app.py` so CLI consumers like `repo_config.py` stay decoupled from Celery):**
  - `class RetryableTaskError(RuntimeError)`: Base transient failure (network timeout, connection reset, upstream `HTTP 500/502/503/504`).
  - `class RateLimitTaskError(RetryableTaskError)`: Carries `retry_after_seconds: float | None = None` extracted via `parse_retry_after_header()` from `Retry-After` (delta-seconds or RFC 7231 HTTP-date) or `X-RateLimit-Reset`, clamped to `[1.0, 60.0]`.
- **Classification Rules (`classify_retryable_exception(exc: BaseException) -> tuple[bool, float | None]`):**
  - **Retryable (`True`):** `RateLimitTaskError`, `RetryableTaskError`, `httpx.TimeoutException`, `httpx.TransportError`, `redis.exceptions.ConnectionError`, `redis.exceptions.TimeoutError`, and `GitHubAuthError` / `httpx.HTTPStatusError` where `status_code == 429`, `status_code in {500, 502, 503, 504}`, or `status_code == 403` **with** a rate-limit header (`Retry-After` or `X-RateLimit-Remaining == "0"`).
  - **Non-Retryable (`False` — fail immediately):** `PRSupersededError`, `GitHubAuthConfigError`, `ValidationError`, `ValueError`, and HTTP `400`, `401`, plain `403`, `404`, `422`.
- **Countdown Formula (`compute_retry_countdown`):**
  - `MAX_TASK_RETRIES: Final[int] = 3`.
  - When `retry_after_seconds` is present: `min(60.0, max(1.0, retry_after_seconds)) + uniform(0.0, 1.0)`.
  - Otherwise (Exponential Backoff + Full-Range Half Jitter): `cap = min(30.0, 2.0 * (2 ** retries))` (`2s, 4s, 8s`), returning `uniform(cap * 0.5, cap)` (`[1..2s]`, `[2..4s]`, `[4..8s]`).
- **Supersede-Aware Task Execution (`CodeSightTask`):**
  - Wraps task execution so any retryable exception checks:
    1. If `retries >= max_retries`: re-raises the original exception after structured `ERROR` logging.
    2. If the task carries a PR payload and `is_pr_event_superseded(payload)` is `True`: skips `self.retry(...)`, logs `"worker_pr_retry_aborted_superseded"`, and returns `{"status": "superseded", "trace_id": trace_id}`.
    3. Otherwise calls `self.retry(exc=exc, countdown=countdown)` when running inside an active Celery worker delivery (`not self.request.called_directly` and `self.request.id is not None`), or re-raises cleanly during direct Python function calls in unit tests unless invoked through Celery/task runner.

### 3.3 Worker PR Lifecycle Spine (`execute_pr_review_lifecycle`)
`execute_pr_review_lifecycle` wires the Day 3 primitives in deterministic order so `CA-8`, `CA-9`, and `CA-17` plug in via typed callbacks without restructuring `celery_app.py`:
1. **Checkpoint 1 (`stage="entry"`):** Check `AsyncSupersedeLock.ensure_active(...)`. On `PRSupersededError`, return `{"status": "superseded", "trace_id": trace_id}`.
2. **Workspace Scope (`NFR-3`):** Enter `async with async_ephemeral_workspace() as workspace_dir:`.
3. **Repository Config:** If `config_loader` is injected or `Settings` has GitHub credentials (`github_app_id` + `github_private_key`, or `github_token`), load `RepoConfig` from `default_branch` via `RepoConfigLoader`; otherwise use default `RepoConfig()` (preserving offline compatibility for `CA-6` unit tests).
4. **Optional `review_fn(ctx)` Hook:** Placeholder slot for `CA-8`/`CA-17` diff & multi-agent analysis.
5. **Checkpoint 2 (`stage="pre_publish"`, Mandatory):** Immediately before invoking `publish_fn(ctx)`, call `await lock.ensure_active(..., stage="pre_publish")`. If superseded while `review_fn` was running, abort publishing (`0` calls to `publish_fn`) and return `{"status": "superseded", "trace_id": trace_id}`.
6. **Optional `publish_fn(ctx)` Hook:** Placeholder slot for `CA-9` `GitHubReviewPublisher`.

---

## 4. Self-Destructing Ephemeral Workspace (`src/codesight/core/ephemeral.py`)

- **Interfaces:**
  - `@contextmanager def ephemeral_workspace(*, base_dir: Path | str | None = None, prefix: str = "codesight-ws-") -> Iterator[Path]`
  - `@asynccontextmanager def async_ephemeral_workspace(*, base_dir: Path | str | None = None, prefix: str = "codesight-ws-") -> AsyncIterator[Path]`
  - `def resolve_safe_subpath(workspace_root: Path, relative_path: str | Path) -> Path`
- **Security & Cleanup Guarantees (`NFR-3`):**
  1. Defaults `base_dir` to `tempfile.gettempdir()`, which resolves directly to the `128m` RAM-backed `tmpfs` mount at `/tmp` inside `codesight-api` and `codesight-worker` containers (`docker-compose.yml`).
  2. Restricts directory mode to `0o700` on POSIX (`os.name != "nt"`).
  3. `resolve_safe_subpath` rejects empty paths, null bytes, absolute paths, `..` traversal, and symlink targets escaping `workspace_root.resolve()`, raising `ValueError`.
  4. In `finally`, calls `shutil.rmtree(workspace_path, onexc=_on_rmtree_error)` (Python `3.12` API):
     - If a file or subdirectory is marked read-only (common on Windows), `_on_rmtree_error` applies `os.chmod(target_path, stat.S_IWRITE | stat.S_IREAD)` and retries `func(target_path)` once.
     - Any residual OS cleanup failure logs `"ephemeral_workspace_cleanup_failed"` (`ERROR`) without masking an exception raised inside the `with` block.

---

## 5. `.codesight.yaml` Configuration Reader (`src/codesight/config_reader/repo_config.py`)

### 5.1 `RepoConfig` Schema & `config_hash`
| Field | Type | Default | Constraints & Normalization | Downstream Consumer |
| :--- | :--- | :--- | :--- | :--- |
| `min_debt_score` | `int` | `60` | Strict `int` (reject `bool`), `0 <= value <= 100` | `CA-21` Smart Quality Gate |
| `review_action` | `Literal["COMMENT", "REQUEST_CHANGES"]` | `"COMMENT"` | Trimmed uppercase string matching allowed literals | `CA-21` / `CA-9` PR Review Event |
| `ignore_paths` | `tuple[str, ...]` | `()` | Sequence of `<= 100` strings (`1..256` chars each, `\` normalized to `/`, no `\x00`) | `CA-8` `FileFilter` |
| `custom_rules` | `str` | `""` | Trimmed string, `len <= 4000` characters | `CA-18` `ContextBuilder` & `CA-20` `Critic` |
| `config_hash` *(property)* | `str` | computed | First `16` lowercase hex chars of `SHA-256` over canonical JSON (`sort_keys=True`) | `CA-21` `sha_cache.py` (`file_review:...`) |

### 5.2 GitHub Contents API Fetch & Error Handling Matrix (`RepoConfigLoader`)
- **Ref Security Policy (Trust-on-Default-Branch):**
  - `await loader.load(repo_full_name, *, default_branch: str = "main", ref: str | None = None, installation_id: int | None = None) -> RepoConfig`
  - Uses `effective_ref = ref if ref is not None else default_branch`. Webhook PR jobs omit `ref` so untrusted PR branches cannot weaken `min_debt_score`, suppress `REQUEST_CHANGES`, add `ignore_paths: ["**"]`, or inject adversarial `custom_rules`. CLI debugging may pass an explicit `ref`.
- **Lookup Order & Bounds:**
  - Queries `GET /repos/{owner}/{repo}/contents/.codesight.yaml?ref={effective_ref}`; on `HTTP 404`, tries `.codesight.yml`; if both return `404`, logs `"repo_config_not_found"` (`INFO`) and returns `RepoConfig()`.
  - Enforces `MAX_REPO_CONFIG_BYTES = 65_536` (`64 KiB`) both on response metadata `size` and decoded UTF-8 bytes; parses strictly with `yaml.safe_load`.
- **Error Classification Matrix:**

| Failure Condition | Log Level & Event | Behavior |
| :--- | :--- | :--- |
| Both `.codesight.yaml` and `.codesight.yml` return `404` | `INFO` `repo_config_not_found` | Return `RepoConfig()` defaults |
| Empty / comment-only YAML (`safe_load -> None`) | `INFO` `repo_config_loaded` | Treat as `{}` -> `RepoConfig()` defaults |
| Unknown top-level YAML keys alongside valid fields | `WARNING` `repo_config_unknown_keys_ignored` | Log key names only; ignore unknown keys and validate known fields |
| Malformed YAML, invalid Base64/UTF-8, `type != "file"`, `> 64 KiB`, or non-dict root | `WARNING` `repo_config_invalid_fallback` | Log structured `reason` (**zero raw YAML content logged**, `NFR-3`); return `RepoConfig()` defaults |
| Pydantic `ValidationError` on specific known field(s) | `WARNING` `repo_config_invalid_fallback` | Log `reason="validation_error"` + `invalid_fields` (**zero raw YAML content logged**, `NFR-3`); drop only invalid fields and preserve valid sibling fields |
| GitHub `HTTP 429` or `HTTP 403` with rate-limit headers | `WARNING` `repo_config_rate_limited` | Raise `RateLimitTaskError(retry_after_seconds=...)` -> Celery backoff |
| GitHub `HTTP 500/502/503/504` or `httpx.TransportError` / `TimeoutException` | `WARNING` `repo_config_transient_error` | Raise `RetryableTaskError` -> Celery backoff |
| GitHub `HTTP 401` or non-rate-limit `HTTP 403` | `ERROR` `repo_config_auth_error` | Raise `GitHubAuthError(status_code=code)` -> fail fast (no retry) |

---

## 6. Acceptance Criteria Mapping (`CA-7`)

1. **`AC-1` (`Cancel-on-Supersede` Pre-Publish Guarantee):** `tests/unit/test_supersede_lock.py` proves that when `SHA_2` supersedes `SHA_1` (both before task start and mid-execution while `SHA_1` is running `review_fn` right before `publish_fn`), `SHA_1` detects `active_head_sha == SHA_2`, aborts `publish_fn` (`0` comments posted), cancels pending retries, and returns `status="superseded"`; also verifies atomic Lua CAS claim/rollback against live `codesight-redis` (`6381`).
2. **`AC-2` (`ephemeral_workspace()` Destruction on Exception):** Unit tests prove `ephemeral_workspace()` and `async_ephemeral_workspace()` wipe nested files (including read-only files) 100% on normal exit and when `RuntimeError` is raised inside the block, propagating the original `RuntimeError` intact, while `resolve_safe_subpath` blocks `..` and symlink escapes.
3. **`AC-3` (Resilient `.codesight.yaml` Parsing & Structured Logging):** `tests/unit/test_repo_config.py` proves malformed YAML syntax, oversize payloads (`> 64 KiB`), non-mapping roots, and invalid field types/ranges emit a single-line JSON `WARNING` log (`repo_config_invalid_fallback` without raw file contents) and return safe `RepoConfig()` defaults without crashing, while `429`/`5xx` raise `RateLimitTaskError`/`RetryableTaskError` for `CodeSightTask` backoff.
4. **`AC-4` (Quality & Port Coexistence Gates):** `uv run ruff check .`, `uv run mypy src`, `uv run pytest --cov=src --cov-fail-under=80`, and `python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket CA-7` all exit `0` with `flintflow-*` and `documind-*` untouched.
