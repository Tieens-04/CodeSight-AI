# CA-6 Design Specification — Day 2 PostgreSQL Schema (Alembic), GitHub App Auth & `<100ms` Webhook Gateway

- **Jira Ticket:** `CA-6` (*[Day 2] Thiết kế PostgreSQL Schema (Alembic), GitHub App Auth & Webhook Gateway (<100ms)*)
- **Parent Epic / Sprint:** `CA-1` — `Sprint 1` (Board `74`)
- **Date:** 2026-11-16
- **Target Branch:** `feat/ca-6-day2-db-auth-webhook` (branched from `main` @ `8ab28be`)
- **Requirements Covered:** `FR-01` (GitHub App RS256 Auth & Webhook Ingestion), `NFR-1` (`< 100ms` Gateway SLA, well below `< 300ms` ceiling), `NFR-3` (Zero-Code Persistence & secret redaction), `NFR-4` (`trace_id` propagation from `X-GitHub-Delivery` to Celery tasks).

---

## 1. Purpose & Scope Boundaries

`CA-6` establishes CodeSight-AI's durable relational state layer on PostgreSQL 16 (`SQLAlchemy 2.0 Async` + `Alembic`), its dual-mode GitHub authentication provider (`RS256` GitHub App JWT -> 50-minute Redis-cached Installation Access Token with seamless `GITHUB_TOKEN` CLI fallback), and the high-throughput `POST /api/v1/webhooks/github` ingestion endpoint guaranteeing cryptographic `HMAC-SHA256` verification, atomic `X-GitHub-Delivery` idempotency, and `< 100ms` average response latency.

### In Scope (`CA-6`)
1. Add `sqlalchemy>=2.0.36`, `alembic>=1.14`, and `pyjwt[crypto]>=2.10` via `uv` (`pyproject.toml` + `uv.lock`).
2. Implement `src/codesight/db/models.py` (`SQLAlchemy 2.0` `DeclarativeBase`, deterministic constraint naming convention, `Mapped[...]` strict typing) and `alembic/versions/0001_initial_schema.py` creating all 4 core tables (`repositories`, `webhook_events`, `pr_reviews`, `review_findings`) plus forward-compatible nullable metadata columns for `CA-14`, `CA-21`, and `CA-25`.
3. Implement `src/codesight/db/session.py` managing `AsyncEngine` (`pool_size=5`, `max_overflow=10`, `pool_pre_ping=False`, `pool_recycle=1800`) and `async_sessionmaker[AsyncSession]` (`expire_on_commit=False`) wired into FastAPI's `lifespan` context manager.
4. Configure `alembic.ini` and async `alembic/env.py` reading `Settings.sqlalchemy_async_dsn` dynamically (zero plaintext credentials in `alembic.ini`), and bundle `alembic.ini` + `alembic/` into `Dockerfile` so `codesight-api` runs `alembic upgrade head` idempotently before `uvicorn` starts while keeping the 5-container Compose topology intact.
5. Implement `src/codesight/vcs/github_auth.py` supporting PEM normalization (multiline, escaped `\n`, or Base64), `RS256` JWT signing (`iat = now - 60s`, `exp = now + 540s`), Installation Access Token exchange cached in Redis (`gh_inst_token:{installation_id}`, TTL `3000s` = `50m`) with fail-open Redis degradation, and automatic fallback to `GITHUB_TOKEN` when GitHub App credentials are unset or target repository returns `404` (`App not installed`) during CLI runs.
6. Implement `src/codesight/api/routes/webhooks.py` (`POST /api/v1/webhooks/github`) with `25 MiB` streaming payload cap (`413`), `HMAC-SHA256` constant-time verification (`hmac.compare_digest` -> `401`), single-roundtrip PostgreSQL `INSERT ... ON CONFLICT (delivery_id) DO NOTHING RETURNING delivery_id` idempotency, event/branch filtering (`pull_request` `opened`/`synchronize`/`reopened`, non-deleted `push` on `default_branch`, `ping`), repository metadata upsert, and transactional dispatch via injectable `WebhookDispatcher` (`asyncio.to_thread` over Celery stub tasks in `celery_app.py`).
7. Deliver comprehensive unit tests (`tests/unit/test_webhook_hmac.py`, `tests/unit/test_github_auth.py`) and non-destructive PostgreSQL 16 integration tests (`tests/integration/test_db_migrations.py` using an isolated temporary database on port `5433`).

### Explicitly Out of Scope (Deferred to Subsequent Tickets)
- Celery `acks_late`, exponential backoff policies, `Cancel-on-Supersede` Redis locks (`5s` debounce on `synchronize`), `ephemeral_workspace()`, and `.codesight.yaml` parser (`CA-7`).
- `SmartDiffParser`, `FileFilter`, and `DiffHunkMapper` (`CA-8`).
- `GitHubReviewPublisher` (`Checks API` / `Pull Request Reviews API`) and `codesight review <pr_url> --dry-run` (`CA-9`).
- `IncrementalBranchSyncer` execution logic inside the `push` worker task (`CA-14`) and `pull_request.closed` (`merged == true`) acceptance tracking (`CA-25`).

---

## 2. Database Architecture & Schema Specification (`PostgreSQL 16`)

### 2.1 Engine, Session & Lifespan Management (`src/codesight/db/session.py`)
- **`create_db_engine(settings: Settings) -> AsyncEngine`:**
  - Uses `create_async_engine(settings.sqlalchemy_async_dsn, pool_size=5, max_overflow=10, pool_pre_ping=False, pool_recycle=1800, echo=False)`.
  - Omitting `pool_pre_ping` saves an extra `SELECT 1` network roundtrip on every webhook request, preserving `< 100ms` latency while `pool_recycle=1800` refreshes idle connections cleanly.
- **`create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]`:**
  - Configured with `expire_on_commit=False` and `autoflush=False`.
- **FastAPI Integration:**
  - `create_app()` accepts an optional `session_factory: async_sessionmaker[AsyncSession] | None = None` override for `< 1ms` unit tests. When `None`, FastAPI's `lifespan` creates the `AsyncEngine` + `async_sessionmaker` at startup, attaches them to `app.state`, and calls `await engine.dispose()` on shutdown.
  - `get_db_session(request: Request) -> AsyncIterator[AsyncSession]` yields a scoped session per request.

### 2.2 Relational Schema & Zero-Code Invariant (`src/codesight/db/models.py`)
All models inherit from `Base(DeclarativeBase)` with explicit PostgreSQL constraint naming conventions (`pk_%(table_name)s`, `uq_%(table_name)s_%(column_0_name)s`, `ck_%(table_name)s_%(constraint_name)s`, `fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s`, `ix_%(table_name)s_%(column_0_name)s`).

> **Zero-Code Persistence (`NFR-3`) Guarantee:** No table stores customer raw source files, diffs, or AST function bodies. `review_findings.message` holds AI-authored diagnostic prose, and `review_findings.suggestion_code` holds only AI-synthesized replacement lines required by `CA-25` to compute `accepted_on_merge` upon PR merge.

#### Table 1: `repositories`
| Column | SQL Type | Null | Default / Constraint | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| `id` | `UUID` | `NOT NULL` | `PK`, `gen_random_uuid()` | Internal surrogate key |
| `github_repo_id` | `BIGINT` | `NULL` | `UNIQUE` (`uq_repositories_github_repo_id`) | GitHub numeric repository ID (`NULL` allowed if registered via CLI without metadata lookup) |
| `installation_id` | `BIGINT` | `NULL` | Indexed (`ix_repositories_installation_id`) | Forward-compatible GitHub App installation ID for background worker token exchange |
| `full_name` | `VARCHAR(255)` | `NOT NULL` | `UNIQUE` (`uq_repositories_full_name`) | Canonical `"owner/repo"` identifier |
| `default_branch` | `VARCHAR(128)` | `NOT NULL` | `'main'` | Target branch used to filter `push` sync events (`FR-07`) |
| `last_indexed_sha` | `VARCHAR(64)` | `NULL` | — | Last commit SHA indexed into Qdrant (`CA-14`) |
| `installed_at` | `TIMESTAMPTZ` | `NOT NULL` | `now()` | Initial registration / installation timestamp |

#### Table 2: `webhook_events`
| Column | SQL Type | Null | Default / Constraint | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| `delivery_id` | `VARCHAR(128)` | `NOT NULL` | `PK` (`pk_webhook_events`) | Exact `X-GitHub-Delivery` GUID enforcing atomic idempotency |
| `event_type` | `VARCHAR(64)` | `NOT NULL` | — | `X-GitHub-Event` header (`pull_request`, `push`, `ping`, etc.) |
| `action` | `VARCHAR(64)` | `NULL` | — | Payload `action` (`opened`, `synchronize`, `reopened`, etc.) |
| `status` | `VARCHAR(32)` | `NOT NULL` | `CHECK` in `('received','queued','ignored','processed','failed')` | Lifecycle state of webhook delivery |
| `received_at` | `TIMESTAMPTZ` | `NOT NULL` | `now()`, Indexed (`ix_webhook_events_received_at`) | Ingestion timestamp |

#### Table 3: `pr_reviews`
| Column | SQL Type | Null | Default / Constraint | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| `id` | `UUID` | `NOT NULL` | `PK`, `gen_random_uuid()` | Review execution ID |
| `repo_id` | `UUID` | `NOT NULL` | `FK -> repositories.id ON DELETE CASCADE` | Owning repository |
| `pr_number` | `INTEGER` | `NOT NULL` | `CHECK (pr_number > 0)` | Pull Request number |
| `head_sha` | `VARCHAR(64)` | `NOT NULL` | — | Evaluated commit SHA |
| `debt_score` | `INTEGER` | `NULL` | `CHECK (debt_score IS NULL OR (debt_score >= 0 AND debt_score <= 100))` | Technical Debt Score (`0–100` once completed; `NULL` while pending/running) |
| `critical_count` | `INTEGER` | `NOT NULL` | `0`, `CHECK (critical_count >= 0)` | Count of `CRITICAL` findings |
| `warning_count` | `INTEGER` | `NOT NULL` | `0`, `CHECK (warning_count >= 0)` | Count of `WARNING` findings |
| `info_count` | `INTEGER` | `NOT NULL` | `0`, `CHECK (info_count >= 0)` | Count of `INFO` findings |
| `tokens_saved_pct` | `DOUBLE PRECISION` | `NULL` | — | Context compression ratio vs full-file baseline (`CA-15`) |
| `latency_ms` | `INTEGER` | `NULL` | `CHECK (latency_ms IS NULL OR latency_ms >= 0)` | End-to-end review duration in ms |
| `status` | `VARCHAR(32)` | `NOT NULL` | `'pending'`, `CHECK` in `('pending','running','completed','superseded','failed')` | Execution state |
| `trace_id` | `VARCHAR(128)` | `NULL` | — | Correlated `trace_id` / Langfuse trace key for `/radar` (`CA-25`) |
| `created_at` | `TIMESTAMPTZ` | `NOT NULL` | `now()` | Record creation timestamp |

- **Indexes on `pr_reviews`:**
  - `ix_pr_reviews_repo_pr_created` on `(repo_id, pr_number, created_at)`
  - `ix_pr_reviews_repo_head_sha` on `(repo_id, head_sha)`
  - `ix_pr_reviews_created_at` on `(created_at)` (accelerates `/radar` time-series queries `< 150ms`)

#### Table 4: `review_findings`
| Column | SQL Type | Null | Default / Constraint | Purpose |
| :--- | :--- | :--- | :--- | :--- |
| `id` | `UUID` | `NOT NULL` | `PK`, `gen_random_uuid()` | Finding ID |
| `pr_review_id` | `UUID` | `NOT NULL` | `FK -> pr_reviews.id ON DELETE CASCADE`, Indexed (`ix_review_findings_pr_review_id`) | Parent PR review |
| `file_path` | `VARCHAR(1024)` | `NOT NULL` | Indexed (`ix_review_findings_file_path`) | Repo-relative POSIX path (powers `/radar` Top 5 hotspots) |
| `start_line` | `INTEGER` | `NOT NULL` | `CHECK (start_line >= 1 AND end_line >= start_line)` | 1-based start line on `RIGHT` |
| `end_line` | `INTEGER` | `NOT NULL` | *(covered by `ck_review_findings_line_order`)* | 1-based inclusive end line on `RIGHT` |
| `severity` | `VARCHAR(16)` | `NOT NULL` | `CHECK` in `('CRITICAL','WARNING','INFO')` | Critic-assigned severity |
| `category` | `VARCHAR(64)` | `NOT NULL` | — | Domain category (`security`, `performance`, `architecture`, etc.) |
| `confidence` | `DOUBLE PRECISION` | `NOT NULL` | `CHECK (confidence >= 0.0 AND confidence <= 1.0)` | Critic confidence (`0.0–1.0`) |
| `message` | `TEXT` | `NULL` | — | AI diagnostic explanation (no customer raw code) |
| `suggestion_code` | `TEXT` | `NULL` | — | Validated AI suggestion block for `CA-25` merge matching |
| `accepted_on_merge` | `BOOLEAN` | `NULL` | Indexed (`ix_review_findings_accepted_on_merge`) | `NULL` pre-merge; `TRUE`/`FALSE` after `CA-25` merge check |

---

## 3. GitHub Authentication Provider (`src/codesight/vcs/github_auth.py`)

### 3.1 Key Normalization & RS256 JWT Signing
- **`normalize_private_key(raw_key: str) -> str`:**
  1. Strips outer whitespace and optional surrounding quotes.
  2. Replaces literal escaped `\\n` sequences with real newlines `\n`.
  3. If `"-----BEGIN "` is still absent, attempts strict Base64 decoding (`base64.b64decode(..., validate=True)`) into UTF-8 PEM text; raises `GitHubAuthConfigError` (without echoing key contents!) if invalid.
- **`create_app_jwt(app_id: str, private_key_pem: str, *, now: int | None = None) -> str`:**
  - Signs payload `{"iat": ts - 60, "exp": ts + 540, "iss": app_id}` with `jwt.encode(..., algorithm="RS256")` (`PyJWT[crypto]`).
  - `ts - 60` guards against host/GitHub clock drift; `ts + 540` (`9m`) stays safely inside GitHub's `10m` hard ceiling.

### 3.2 Installation Token Exchange, Redis Cache (`50m`) & CLI Fallback
- **`GitHubAuthProvider`:**
  - Dependencies: `settings: Settings`, optional `redis_client: redis.asyncio.Redis | None`, optional `http_client: httpx.AsyncClient | None`, base URL `"https://api.github.com"`.
  - **Redis Cache Contract:** Key `gh_inst_token:{installation_id}`, TTL `3000` seconds (`50` minutes vs GitHub's `60` minute expiry, leaving a `10`-minute safety buffer).
  - **Fail-Open Cache Resilience:** Any `redis.exceptions.RedisError` or `OSError` on `GET`/`SET` logs `"github_token_cache_unavailable"` (`error_type=type(exc).__name__`, zero token values) and proceeds directly to GitHub REST API.
  - **Resolution Order (`get_token(*, installation_id: int | None = None, repo_full_name: str | None = None) -> SecretStr`):**
    1. **GitHub App Path** (when both `settings.github_app_id` and `settings.github_private_key` are non-empty):
       - If `installation_id` is provided: check Redis `gh_inst_token:{installation_id}` -> on miss, mint RS256 JWT and `POST /app/installations/{installation_id}/access_tokens` -> cache for `3000s` -> return `SecretStr(token)`.
       - Else if `repo_full_name` (`"owner/repo"`) is provided: mint RS256 JWT and `GET /repos/{owner}/{repo}/installation`.
         - If `200 OK`: extract `id` as `installation_id`, check/populate `gh_inst_token:{installation_id}`, return `SecretStr(token)`.
         - If `404 Not Found` (App not installed on target repo, common in CLI `codesight review`): log `"github_app_not_installed_falling_back"` and fall through to step 2.
    2. **CLI Personal Token Fallback Path:**
       - If `settings.github_token` is configured and non-empty, return `settings.github_token`.
    3. **Failure:** Raise `GitHubAuthError` with an actionable message naming `GITHUB_APP_ID`/`GITHUB_PRIVATE_KEY` or `GITHUB_TOKEN`.
  - **`get_auth_headers(...) -> dict[str, str]`:** Builds standard headers `{"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "Authorization": f"Bearer {token.get_secret_value()}"}`.

---

## 4. FastAPI Webhook Gateway (`POST /api/v1/webhooks/github`)

### 4.1 Security, Validation & `< 100ms` Request Pipeline
```text
Incoming POST /api/v1/webhooks/github
  │  (TraceIdMiddleware already bound trace_id = sanitized X-GitHub-Delivery)
  ├─ 1. Check settings.github_webhook_secret configured ──(missing)──► 503 Service Unavailable
  ├─ 2. Stream raw body with 25 MiB cap (Content-Length + chunk counter) ──(>25 MiB)──► 413 Content Too Large
  ├─ 3. Verify X-Hub-Signature-256 ("sha256=<hex>") via hmac.compare_digest ──(bad/missing)──► 401 Unauthorized
  ├─ 4. Validate X-GitHub-Delivery & X-GitHub-Event headers + parse JSON object ──(invalid)──► 400 Bad Request
  ├─ 5. Classify event in-memory (`ping` | actionable `pull_request` | actionable `push` | `ignored`) -> target_status (`'queued'` or `'ignored'`)
  ├─ 6. Atomic Postgres INSERT INTO webhook_events (..., status=target_status) ON CONFLICT (delivery_id) DO NOTHING RETURNING delivery_id
  │     └──(conflict: None returned)──► 200 OK {"status": "duplicate", "delivery_id": ...} (0 jobs dispatched)
  ├─ 7. Non-actionable path (`target_status == 'ignored'`):
  │     ├── event == "ping" ──► commit, return 200 OK {"status": "pong", "delivery_id": ...}
  │     └── other events / actions / non-default or deleted refs ──► commit, return 200 OK {"status": "ignored", "delivery_id": ...}
  └─ 8. Actionable path (`target_status == 'queued'`, inside same DB transaction):
        ├── Upsert `repositories` matching `github_repo_id` OR pre-existing CLI `full_name`
        ├── Dispatch typed payload via `await dispatcher.dispatch_*(...)` (`asyncio.to_thread` -> Celery)
        │     └──(if Broker raises)──► ROLLBACK transaction -> 503 Service Unavailable (safe for GitHub retry!)
        └── Commit transaction ──► 202 Accepted {"status": "queued", "delivery_id": ..., "task_id": ...}
```

### 4.2 Repository Upsert & Dispatcher Contract
- **Repository Upsert Strategy:**
  - Looks up an existing `Repository` row matching `Repository.github_repo_id == gh_repo_id` or `Repository.full_name == full_name` within the active transaction:
    - Updates `github_repo_id`, `full_name`, `default_branch`, and `installation_id` if already present (handling both CLI-first rows where `github_repo_id` was `NULL` and GitHub repository renames without `UniqueViolationError`), or inserts a new `Repository` row when neither matches.
- **`WebhookDispatcher` Protocol & Celery Stubs:**
  - Defined in `src/codesight/api/routes/webhooks.py` (with DTOs `PRWebhookDispatchPayload` and `PushWebhookDispatchPayload`):
    - `async def dispatch_pull_request(self, payload: PRWebhookDispatchPayload) -> str` (returns `task_id`)
    - `async def dispatch_push(self, payload: PushWebhookDispatchPayload) -> str` (returns `task_id`)
  - `CeleryWebhookDispatcher` calls `await asyncio.to_thread(send_task_fn, ...)` targeting stub tasks registered in `src/codesight/worker/celery_app.py`:
    - `codesight.webhook.handle_pr_event(payload: dict[str, Any]) -> dict[str, Any]`
    - `codesight.webhook.handle_push_event(payload: dict[str, Any]) -> dict[str, Any]`
  - Because `asyncio.to_thread` in Python `3.12` copies the active `contextvars.Context`, `TraceIdMiddleware`'s bound `trace_id` (`X-GitHub-Delivery`) is automatically injected into Celery message headers by `inject_trace_id_header` (`CA-5`).

---

## 5. Docker Packaging & Testing Strategy

- **`Dockerfile` & `codesight-api` Startup:**
  - Copy `alembic.ini` and `alembic/` into `/app/` in the `runtime` stage (`--chown=codesight:codesight`).
  - Update default `CMD` to `["sh", "-c", "alembic upgrade head && exec uvicorn codesight.api.main:app --host 0.0.0.0 --port 8000"]`.
  - `codesight-worker` overrides `command:` in `docker-compose.yml`, so only `codesight-api` executes migrations on container boot, preserving the exact 5-service stack and `3.0 GiB` memory budget.
- **Unit & Integration Tests:**
  - `tests/unit/test_github_auth.py`: Offline tests (`httpx.MockTransport` + fake async Redis) covering PEM formats (`\n`, multiline, Base64, malformed), RS256 JWT claims/signature verification using the corresponding RSA public key, 50-minute Redis cache hit/miss, Redis connection error fail-open, `404` installation fallback to `GITHUB_TOKEN`, and missing-credentials error handling.
  - `tests/unit/test_webhook_hmac.py`: Tests valid/invalid/missing `X-Hub-Signature-256` (`401`), tampered raw bytes, >25 MiB payload (`413`), duplicate `X-GitHub-Delivery` (`200` with `0` duplicate dispatches), `ping` (`200`), `pull_request` (`opened`/`synchronize`/`reopened` -> `202`, `closed`/`edited` -> `200 ignored`), `push` (`refs/heads/main` -> `202`, feature branch / deleted branch -> `200 ignored`), broker failure rollback (`503` -> retry succeeds `202`), and 50-request warm benchmark asserting mean latency `< 100ms`.
  - `tests/integration/test_db_migrations.py`: Connects to `codesight-postgres` (`localhost:5433`), creates temporary DB `codesight_mig_<uuid8>`, runs programmatic/CLI `alembic upgrade head` -> inspects all 4 tables, columns, FKs, check constraints, unique constraints, and indexes -> exercises atomic `ON CONFLICT DO NOTHING` + `repositories`/`pr_reviews`/`review_findings` cascade -> runs `alembic downgrade base` -> `alembic upgrade head` -> drops temporary DB in `finally`.

---

## 6. Acceptance Criteria Mapping

1. **`AC-1` (Alembic Schema & Indexes on `5433`):** `uv run alembic upgrade head` against `codesight-postgres` (`5433`) creates `repositories`, `webhook_events`, `pr_reviews`, and `review_findings` with all PKs, FKs, check constraints, and indexes cleanly; `test_db_migrations.py` verifies full upgrade/downgrade round-trip.
2. **`AC-2` (HMAC Security, Idempotency & `< 100ms` SLA):** Invalid/missing `X-Hub-Signature-256` returns `401 Unauthorized`; repeated `X-GitHub-Delivery` returns `200 OK` (`status="duplicate"`) without dispatching a second Celery task; valid actionable deliveries return `202 Accepted` in `< 100ms` mean latency (well under `< 300ms` SLA).
3. **`AC-3` (Quality & Coexistence Gates):** `uv run ruff check .`, `uv run mypy src`, `uv run pytest --cov=src --cov-fail-under=80`, and `python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket CA-6` all exit `0` with `flintflow-*` and `documind-*` untouched.
