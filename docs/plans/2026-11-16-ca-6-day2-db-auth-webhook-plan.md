# CA-6 Day 2 PostgreSQL Schema, GitHub App Auth & Webhook Gateway Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver CodeSight-AI's PostgreSQL 16 relational schema & Alembic migrations (`repositories`, `webhook_events`, `pr_reviews`, `review_findings`), dual-mode GitHub App `RS256` / `GITHUB_TOKEN` authentication with 50-minute Redis caching, and `< 100ms` `POST /api/v1/webhooks/github` gateway (`CA-6`).

**Architecture:** `SQLAlchemy 2.0 Async` (`asyncpg` pool with `pool_pre_ping=False, pool_recycle=1800`) managed via FastAPI `lifespan` powers single-roundtrip `INSERT INTO webhook_events ... ON CONFLICT (delivery_id) DO NOTHING RETURNING delivery_id` idempotency and dual-key `repositories` upserts, committing only after `CeleryWebhookDispatcher` publishes stub tasks via `asyncio.to_thread` (preserving `trace_id` `contextvars`). `GitHubAuthProvider` normalizes multiline/escaped-`\n`/Base64 RSA keys, signs `RS256` JWTs, caches Installation Access Tokens in Redis for `3000s` (`50m`) with fail-open resilience, and falls back to `GITHUB_TOKEN` when App credentials are unset or a CLI target repo returns `404`.

**Tech Stack:** Python `3.12` (`uv`), FastAPI, Pydantic v2, `SQLAlchemy>=2.0.36` (`asyncio` + `asyncpg`), `Alembic>=1.14`, `PyJWT[crypto]>=2.10`, `redis.asyncio`, `httpx`, Celery, `structlog`, Docker Compose (`postgres:16-alpine` on host `5433`), `ruff`, `mypy --strict`, `pytest` + `pytest-cov`.

**Spec:** `docs/specs/2026-11-16-ca-6-day2-db-auth-webhook-design.md`

## Global Constraints

- Python runtime pinned to `3.12` (`.python-version = 3.12`), managed strictly via `uv` (`~/.local/bin/uv.exe` on Windows host; never run global `pip install`).
- Active implementation branch MUST be `feat/ca-6-day2-db-auth-webhook`; zero `.env`, `*.pem`, or `*.key` files tracked or untracked in `git status --porcelain`.
- Never stop, restart, or bind ports reserved by `flintflow-*` (`3001`, `6379`, `8080`) or `documind-*` (`3002`, `5432`, `6380`, `8081`, `9000`, `9001`); always scope Docker Compose commands with `-p codesight` (`POSTGRES_PORT=5433`, `REDIS_PORT=6381`, `QDRANT_HTTP_PORT=6333`, `QDRANT_GRPC_PORT=6334`, `API_PORT=8082`, exact 5-service stack).
- Zero-Code Persistence (`NFR-3`): never store customer raw source code or diffs in PostgreSQL, Redis, or logs; never log `GITHUB_PRIVATE_KEY`, JWTs, installation tokens, `GITHUB_TOKEN`, or `GITHUB_WEBHOOK_SECRET`.
- Quality gates required before Jira transition `41` (`Done`): `uv run ruff check .` (`0` errors, including `alembic/`), `uv run mypy src` (`0` issues under `strict = true`), `uv run pytest --cov=src --cov-fail-under=80` (`>= 80%` coverage), and `python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket CA-6` exiting `0`.

## Review Focus

- **Streaming body > `25 MiB` without `Content-Length` (`DoS` protection on `512m` container):** A chunked request omitting `Content-Length` whose stream exceeds `26_214_400` bytes must abort immediately at `25 MiB + 1` byte with `413 Content Too Large` before computing `HMAC-SHA256` (`Task 4`).
- **Celery Broker outage mid-webhook (`Lost-Delivery Hazard`):** If `dispatcher.dispatch_*` raises an exception after inserting `webhook_events`, the PostgreSQL session MUST roll back and return `503 Service Unavailable` so GitHub's retry with the same `X-GitHub-Delivery` succeeds (`202 Accepted`) instead of being dropped as `200 duplicate` (`Task 4`).
- **CLI-first repository (`github_repo_id = NULL`) later triggering a GitHub App webhook:** Upserting `repositories` must match `github_repo_id OR full_name` and backfill `github_repo_id` + `installation_id` without raising `UniqueViolationError` on `uq_repositories_full_name` (`Task 4`).
- **Escaped `\n` / Base64 PEM keys, dead Redis cache, and `404` uninstalled repo in CLI mode:** `GitHubAuthProvider` must parse single-line `\n` and Base64 PEM keys into valid `RS256` JWTs, fail open when Redis raises `RedisError` without leaking tokens in logs, and fall back to `GITHUB_TOKEN` when `GET /repos/{owner}/{repo}/installation` returns `404` (`Task 3`).
- **Branch-deletion `push` on `default_branch` (`deleted: true` or zero SHA `0000...`):** Must record `webhook_events.status = 'ignored'` and return `200 OK` (`0` jobs dispatched) rather than enqueueing an index sync for a deleted commit (`Task 4`).

---

## File Structure

- `pyproject.toml`, `uv.lock`: Add runtime dependencies `sqlalchemy>=2.0.36`, `alembic>=1.14`, and `pyjwt[crypto]>=2.10` (plus `[tool.mypy.overrides]` for `alembic.*` if needed).
- `src/codesight/db/models.py`, `src/codesight/db/__init__.py`: `Base` (`DeclarativeBase` with deterministic naming convention) and typed `Mapped` ORM models `Repository`, `WebhookEvent`, `PRReview`, `ReviewFinding`.
- `src/codesight/db/session.py`: `create_db_engine()`, `create_session_factory()`, and FastAPI dependency `get_db_session()`.
- `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, `alembic/versions/0001_initial_schema.py`: Async Alembic runner wired to `Settings.sqlalchemy_async_dsn` and reversible `0001` migration creating all 4 tables, check constraints, FKs, and indexes.
- `src/codesight/vcs/github_auth.py`, `src/codesight/vcs/__init__.py`: `GitHubAuthError`, `GitHubAuthConfigError`, `normalize_private_key()`, `create_app_jwt()`, and `GitHubAuthProvider`.
- `src/codesight/worker/celery_app.py`: Add typed Celery stub tasks `codesight.webhook.handle_pr_event` and `codesight.webhook.handle_push_event`.
- `src/codesight/api/routes/__init__.py`, `src/codesight/api/routes/webhooks.py`, `src/codesight/api/main.py`: `POST /api/v1/webhooks/github` router, `verify_github_signature()`, `read_bounded_body()`, `WebhookDispatcher` / `CeleryWebhookDispatcher`, dual-key repository upsert, and FastAPI `lifespan` wiring.
- `Dockerfile`: Copy `alembic.ini` + `alembic/` into `/app` (`codesight:codesight`) and run `alembic upgrade head` before `uvicorn` in default `CMD`.
- `tests/conftest.py`, `tests/unit/test_db_models_and_session.py`, `tests/unit/test_github_auth.py`, `tests/unit/test_webhook_hmac.py`, `tests/integration/__init__.py`, `tests/integration/test_db_migrations.py`: Unit and PostgreSQL 16 integration test suites (`>= 85%` coverage).

---

### Task 1: Dependencies, SQLAlchemy 2.0 Async Models & Session Factory

**Files:**
- Modify: `pyproject.toml`, `uv.lock`, `src/codesight/db/__init__.py`
- Create: `src/codesight/db/models.py`, `src/codesight/db/session.py`
- Test: `tests/unit/test_db_models_and_session.py`

**Interfaces:**
- Consumes: `codesight.core.config.Settings` (`sqlalchemy_async_dsn`)
- Produces:
  - `Base` (`DeclarativeBase`), `NAMING_CONVENTION: dict[str, str]`
  - `Repository`, `WebhookEvent`, `PRReview`, `ReviewFinding` ORM classes matching Section 2.2 of the spec
  - `create_db_engine(settings: Settings) -> AsyncEngine`
  - `create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]`
  - `get_db_session(request: Request) -> AsyncIterator[AsyncSession]`

- [ ] **Step 1: Add `sqlalchemy`, `alembic`, and `pyjwt[crypto]` via `uv`**

Run: `& "$HOME\.local\bin\uv.exe" add "sqlalchemy>=2.0.36" "alembic>=1.14" "pyjwt[crypto]>=2.10"`
Expected: `pyproject.toml` and `uv.lock` updated cleanly under Python `3.12`.

- [ ] **Step 2: Write failing unit tests for ORM metadata & session factory in `tests/unit/test_db_models_and_session.py`**

```python
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from codesight.core.config import Settings
from codesight.db.models import Base, PRReview, Repository, ReviewFinding, WebhookEvent
from codesight.db.session import create_db_engine, create_session_factory, get_db_session


def test_orm_metadata_defines_all_four_tables_and_forward_columns() -> None:
    assert set(Base.metadata.tables.keys()) == {
        "repositories",
        "webhook_events",
        "pr_reviews",
        "review_findings",
    }
    repo_cols = set(Repository.__table__.columns.keys())
    assert {
        "id", "github_repo_id", "installation_id", "full_name",
        "default_branch", "last_indexed_sha", "installed_at",
    } <= repo_cols
    assert Repository.__table__.columns["github_repo_id"].nullable is True

    pr_cols = set(PRReview.__table__.columns.keys())
    assert {
        "id", "repo_id", "pr_number", "head_sha", "debt_score",
        "critical_count", "warning_count", "info_count",
        "tokens_saved_pct", "latency_ms", "status", "trace_id", "created_at",
    } <= pr_cols

    finding_cols = set(ReviewFinding.__table__.columns.keys())
    assert {
        "id", "pr_review_id", "file_path", "start_line", "end_line",
        "severity", "category", "confidence", "message",
        "suggestion_code", "accepted_on_merge",
    } <= finding_cols

    wh_cols = set(WebhookEvent.__table__.columns.keys())
    assert wh_cols == {"delivery_id", "event_type", "action", "status", "received_at"}


@pytest.mark.asyncio
async def test_engine_pool_kwargs_and_get_db_session_lifecycle() -> None:
    settings = Settings(_env_file=None)
    with patch("codesight.db.session.create_async_engine") as mock_create:
        sentinel_engine = MagicMock()
        mock_create.return_value = sentinel_engine
        eng = create_db_engine(settings)
        assert eng is sentinel_engine
        mock_create.assert_called_once_with(
            settings.sqlalchemy_async_dsn,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=False,
            pool_recycle=1800,
            echo=False,
        )

    fake_session = AsyncMock()
    cm = AsyncMock()
    cm.__aenter__.return_value = fake_session
    cm.__aexit__.return_value = False
    factory = MagicMock(return_value=cm)
    fake_request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(session_factory=factory)))

    yielded = [s async for s in get_db_session(fake_request)]  # type: ignore[arg-type]
    assert yielded == [fake_session]
    assert create_session_factory(sentinel_engine) is not None
```

- [ ] **Step 3: Run `pytest` to confirm failure**

Run: `& "$HOME\.local\bin\uv.exe" run pytest tests/unit/test_db_models_and_session.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codesight.db.models'`

- [ ] **Step 4: Implement `src/codesight/db/models.py`, `src/codesight/db/session.py`, and `src/codesight/db/__init__.py`**

- In `models.py`: define `NAMING_CONVENTION`, `Base(DeclarativeBase)` (`metadata = MetaData(naming_convention=NAMING_CONVENTION)`), and all 4 models (`Repository`, `WebhookEvent`, `PRReview`, `ReviewFinding`) with exact `Mapped[...]` types, `UUID(as_uuid=True)` (`server_default=sa.text("gen_random_uuid()")`), `DateTime(timezone=True)` (`server_default=sa.func.now()`), `ForeignKey(..., ondelete="CASCADE")`, `CheckConstraint`s, `Index`es, and `relationship()` definitions from Section 2.2 of the spec.
- In `session.py`: implement `create_db_engine(settings)`, `create_session_factory(engine)` (`async_sessionmaker(engine, expire_on_commit=False, autoflush=False)`), and `async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]` yielding from `request.app.state.session_factory()`. Re-export public symbols in `src/codesight/db/__init__.py`.

- [ ] **Step 5: Run tests, `mypy`, and `ruff`**

Run: `& "$HOME\.local\bin\uv.exe" run pytest tests/unit/test_db_models_and_session.py -v && & "$HOME\.local\bin\uv.exe" run mypy src && & "$HOME\.local\bin\uv.exe" run ruff check .`
Expected: PASS (`2 passed`, `0` mypy/ruff issues).

- [ ] **Step 6: Commit ORM models & session layer**

```powershell
git add pyproject.toml uv.lock src/codesight/db tests/unit/test_db_models_and_session.py
git commit -m "feat(ca-6): add SQLAlchemy 2.0 async models and session factory"
```

---

### Task 2: Alembic Async Setup, `0001_initial_schema.py` & Temporary-DB Integration Tests

**Files:**
- Create: `alembic.ini`, `alembic/env.py`, `alembic/script.py.mako`, `alembic/versions/0001_initial_schema.py`
- Create: `tests/conftest.py`, `tests/integration/__init__.py`, `tests/integration/test_db_migrations.py`

**Interfaces:**
- Consumes: `codesight.db.models.Base`, `codesight.core.config.get_settings()`
- Produces:
  - Reversible Alembic migration `0001_initial_schema` (`revision = "0001_initial_schema"`, `down_revision = None`)
  - Shared pytest fixture `isolated_postgres_dsn` / `isolated_session_factory` in `tests/conftest.py` creating and dropping `codesight_test_<uuid8>` on `codesight-postgres` (`5433`)

- [ ] **Step 1: Ensure `codesight-postgres`, `codesight-redis`, and `codesight-qdrant` backing containers are running**

Run: `docker compose -p codesight up -d --wait codesight-postgres codesight-redis codesight-qdrant`
Expected: All 3 infrastructure containers reach `Healthy` on ports `5433`, `6381`, `6333/6334` without touching `flintflow-*` or `documind-*`.

- [ ] **Step 2: Write failing migration & constraint integration tests in `tests/conftest.py` and `tests/integration/test_db_migrations.py`**

In `tests/conftest.py`, provide an async fixture `isolated_postgres_dsn` that connects to `settings.postgres_dsn` via `asyncpg`, executes `CREATE DATABASE codesight_test_<hex>`, yields `postgresql+asyncpg://.../codesight_test_<hex>`, and terminates backend sessions + `DROP DATABASE IF EXISTS codesight_test_<hex>` in `finally`.

In `tests/integration/test_db_migrations.py`, assert:
1. Running Alembic `command.upgrade(cfg, "head")` against `isolated_postgres_dsn` creates `repositories`, `webhook_events`, `pr_reviews`, `review_findings`, and `alembic_version` (`0001_initial_schema`).
2. `sqlalchemy.inspect` reflects expected indexes (`ix_repositories_installation_id`, `ix_webhook_events_received_at`, `ix_pr_reviews_repo_pr_created`, `ix_pr_reviews_repo_head_sha`, `ix_pr_reviews_created_at`, `ix_review_findings_pr_review_id`, `ix_review_findings_file_path`, `ix_review_findings_accepted_on_merge`) and unique constraints (`uq_repositories_github_repo_id`, `uq_repositories_full_name`).
3. Inserting `debt_score = 105` into `pr_reviews` or `confidence = 1.5` / `end_line < start_line` into `review_findings` raises `IntegrityError` (proving check constraints work in PostgreSQL 16), and deleting a `Repository` cascades to `pr_reviews` and `review_findings`.
4. Running `command.downgrade(cfg, "base")` drops all 4 application tables cleanly, and a subsequent `command.upgrade(cfg, "head")` recreates them cleanly.

- [ ] **Step 3: Run `pytest` to confirm failure**

Run: `& "$HOME\.local\bin\uv.exe" run pytest tests/integration/test_db_migrations.py -v`
Expected: FAIL (`alembic.ini` / `alembic` directory missing).

- [ ] **Step 4: Author `alembic.ini`, async `alembic/env.py`, `alembic/script.py.mako`, and `alembic/versions/0001_initial_schema.py`**

- `alembic.ini`: set `script_location = alembic` and `sqlalchemy.url =` blank (resolved dynamically so zero credentials live in `alembic.ini`).
- `alembic/env.py`: set `target_metadata = Base.metadata`. Resolve URL from `config.get_main_option("sqlalchemy.url") or get_settings().sqlalchemy_async_dsn`. Support async `create_async_engine(..., poolclass=pool.NullPool)` via `connection.run_sync(do_run_migrations)`; if already inside a running event loop (e.g. called from an async pytest test), run `asyncio.run(...)` inside a worker thread (`concurrent.futures.ThreadPoolExecutor(max_workers=1)`).
- `alembic/versions/0001_initial_schema.py`: explicitly create and drop all 4 tables, check constraints, unique constraints, FKs, and indexes matching `Base.metadata` (`ruff`-clean).

- [ ] **Step 5: Run migration integration tests, host `alembic upgrade head`, and `ruff`**

Run: `& "$HOME\.local\bin\uv.exe" run pytest tests/integration/test_db_migrations.py -v && & "$HOME\.local\bin\uv.exe" run alembic upgrade head && & "$HOME\.local\bin\uv.exe" run ruff check .`
Expected: PASS (`test_db_migrations.py` green, dev DB `codesight` on `5433` migrated to `0001_initial_schema`, `0` ruff errors).

- [ ] **Step 6: Commit Alembic setup & migration integration tests**

```powershell
git add alembic.ini alembic tests/conftest.py tests/integration
git commit -m "feat(ca-6): add async Alembic configuration and 0001 initial PostgreSQL schema"
```

---

### Task 3: GitHub App Authentication (`RS256` JWT, `50m` Redis Cache & CLI Fallback)

**Files:**
- Create: `src/codesight/vcs/github_auth.py`
- Modify: `src/codesight/vcs/__init__.py`
- Test: `tests/unit/test_github_auth.py`

**Interfaces:**
- Consumes: `Settings`, `PyJWT` (`jwt`), `cryptography`, `httpx.AsyncClient`, `redis.asyncio.Redis`
- Produces:
  - `INSTALLATION_TOKEN_TTL_SECONDS: Final[int] = 3000` (`50` minutes)
  - `class GitHubAuthError(RuntimeError)` and `class GitHubAuthConfigError(GitHubAuthError)`
  - `normalize_private_key(raw_key: str) -> str`
  - `create_app_jwt(app_id: str, private_key_pem: str, *, now: int | None = None) -> str`
  - `class GitHubAuthProvider`:
    - `async def get_token(self, *, installation_id: int | None = None, repo_full_name: str | None = None) -> SecretStr`
    - `async def get_auth_headers(self, *, installation_id: int | None = None, repo_full_name: str | None = None) -> dict[str, str]`

- [ ] **Step 1: Write failing unit tests in `tests/unit/test_github_auth.py`**

Generate an ephemeral 2048-bit RSA keypair in-memory via `cryptography.hazmat.primitives.asymmetric.rsa.generate_private_key` (never writing `.pem` files to disk) and test:
1. `normalize_private_key` accepts multiline PEM, single-line escaped `\n` PEM, and Base64-encoded PEM, and raises `GitHubAuthConfigError` (without echoing raw input) on garbage strings.
2. `create_app_jwt(app_id="98765", private_key_pem=pem, now=1_700_000_000)` decodes with the matching RSA public key (`algorithms=["RS256"]`, `options={"verify_exp": False}`) to `{"iss": "98765", "iat": 1_699_999_940, "exp": 1_700_000_540}`.
3. `GitHubAuthProvider.get_token(installation_id=42)` returns cached token on Redis hit (`0` HTTP calls); on Redis miss, exchanges JWT at `POST /app/installations/42/access_tokens`, caches `gh_inst_token:42` with `ex=3000`, and returns `SecretStr` whose secret never appears in `repr()` or logs.
4. When Redis raises `redis.exceptions.ConnectionError` on `get`/`set`, `get_token(installation_id=42)` logs `"github_token_cache_unavailable"` without leaking the token and still returns the fresh GitHub installation token (fail-open).
5. `get_token(repo_full_name="ext/repo")` calls `GET /repos/ext/repo/installation`: on `200`, exchanges & caches installation token; on `404` when `settings.github_token` is set, falls back cleanly to `settings.github_token`; on `404` without `github_token`, raises `GitHubAuthError`.
6. Pure CLI mode (`github_app_id=None`, `github_token=SecretStr("ghp_cli_pat")`) returns `ghp_cli_pat` directly via `get_auth_headers()`; when neither App nor PAT is configured, raises `GitHubAuthError`.

- [ ] **Step 2: Run `pytest` to confirm failure**

Run: `& "$HOME\.local\bin\uv.exe" run pytest tests/unit/test_github_auth.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codesight.vcs.github_auth'`

- [ ] **Step 3: Implement `src/codesight/vcs/github_auth.py` and re-export in `src/codesight/vcs/__init__.py`**

Implement `normalize_private_key`, `create_app_jwt`, and `GitHubAuthProvider` following Section 3.1–3.2 of the spec. Validate `repo_full_name` matches `^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$` before interpolating into `/repos/{owner}/{repo}/installation` to prevent path-traversal URL injection. Ensure any owned transient `httpx.AsyncClient` / `redis.asyncio.Redis` created when none was injected is closed cleanly (`async with` / `aclose()`).

- [ ] **Step 4: Run tests, `mypy`, and `ruff`**

Run: `& "$HOME\.local\bin\uv.exe" run pytest tests/unit/test_github_auth.py -v && & "$HOME\.local\bin\uv.exe" run mypy src && & "$HOME\.local\bin\uv.exe" run ruff check .`
Expected: PASS (`100%` green, `0` mypy/ruff issues).

- [ ] **Step 5: Commit `github_auth.py`**

```powershell
git add src/codesight/vcs tests/unit/test_github_auth.py
git commit -m "feat(ca-6): add GitHub App RS256 auth, 50m Redis cache, and CLI PAT fallback"
```

---

### Task 4: FastAPI Webhook Gateway (`< 100ms`), `WebhookDispatcher` & Celery Stubs

**Files:**
- Modify: `src/codesight/worker/celery_app.py`, `src/codesight/api/main.py`
- Create: `src/codesight/api/routes/__init__.py`, `src/codesight/api/routes/webhooks.py`
- Test: `tests/unit/test_webhook_hmac.py`

**Interfaces:**
- Consumes: `Settings`, `Repository`, `WebhookEvent`, `get_db_session`, `create_db_engine`, `create_session_factory`, `celery_app`
- Produces:
  - Celery stub tasks in `celery_app.py`:
    - `codesight.webhook.handle_pr_event(payload: dict[str, Any]) -> dict[str, Any]`
    - `codesight.webhook.handle_push_event(payload: dict[str, Any]) -> dict[str, Any]`
  - In `src/codesight/api/routes/webhooks.py`:
    - `MAX_WEBHOOK_BODY_BYTES: Final[int] = 25 * 1024 * 1024`
    - `verify_github_signature(raw_body: bytes, signature_header: str | None, secret: str) -> bool`
    - `PRWebhookDispatchPayload(BaseModel)` & `PushWebhookDispatchPayload(BaseModel)`
    - `WebhookDispatcher(Protocol)` & `CeleryWebhookDispatcher`
    - `WebhookAckResponse(BaseModel)`: `status: Literal["queued", "duplicate", "ignored", "pong"]`, `delivery_id: str`, `event: str`, `task_id: str | None = None`
    - `router: APIRouter` exposing `POST /api/v1/webhooks/github`
  - Updated `create_app(settings=None, probes=None, session_factory=None, webhook_dispatcher=None) -> FastAPI` with `lifespan` engine management

- [ ] **Step 1: Write failing unit & PostgreSQL-backed `< 100ms` tests in `tests/unit/test_webhook_hmac.py`**

Cover all contract & Review Focus cases:
1. Missing `github_webhook_secret` -> `503 Service Unavailable`.
2. Missing signature, wrong prefix (`sha1=...`), tampered raw body bytes (e.g. extra whitespace changing JSON bytes), or wrong secret -> `401 Unauthorized` via `hmac.compare_digest`.
3. Streaming body exceeding `MAX_WEBHOOK_BODY_BYTES` both with `Content-Length` header and via chunked generator without `Content-Length` -> `413 Content Too Large` before full buffering.
4. Missing `X-GitHub-Delivery` / `X-GitHub-Event` or malformed non-object JSON -> `400 Bad Request`.
5. Against `isolated_postgres_dsn` (`async_sessionmaker` injected into `create_app`) + spy `WebhookDispatcher`:
   - `X-GitHub-Event: ping` -> `200 OK` (`status="pong"`), `0` dispatches, `webhook_events.status == "ignored"`.
   - `pull_request` (`opened`, `synchronize`, `reopened`) -> `202 Accepted` (`status="queued"`, `X-Trace-ID == delivery_id`), upserts `repositories`, dispatches `1` PR job; replaying the exact same `X-GitHub-Delivery` returns `200 OK` (`status="duplicate"`) with **still `1` total dispatch**.
   - Pre-existing CLI `Repository(github_repo_id=None, full_name="Tieens-04/CodeSight-AI")` followed by webhook with `repository.id = 777` updates the existing row (`github_repo_id == 777`, `installation_id == 99`) without `UniqueViolationError`.
   - `pull_request` (`closed`, `edited`) -> `200 OK` (`status="ignored"`), `0` dispatches.
   - `push` to `refs/heads/main` (`deleted=False`) -> `202 Accepted` (`1` push dispatch); `push` to `refs/heads/feat/x` or `push` to `refs/heads/main` with `deleted=True` / `after == "0" * 40` -> `200 OK` (`status="ignored"`, `0` dispatches).
   - Dispatcher raising `ConnectionError` -> rolls back DB transaction and returns `503`; retrying the exact same `X-GitHub-Delivery` once dispatcher recovers succeeds with `202 Accepted`!
   - `CeleryWebhookDispatcher` wraps `send_task` in `asyncio.to_thread` and preserves active `trace_id` inside outbound Celery headers.
   - Warm 50-request benchmark (`pull_request.synchronize` with unique `X-GitHub-Delivery` against live `codesight-postgres:5433` + `asyncio.to_thread` dispatcher) asserts average response latency `< 100.0` ms.

- [ ] **Step 2: Run `pytest` to confirm failure**

Run: `& "$HOME\.local\bin\uv.exe" run pytest tests/unit/test_webhook_hmac.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codesight.api.routes'`

- [ ] **Step 3: Implement Celery stub tasks, `webhooks.py`, and `main.py` lifespan/router wiring**

- In `src/codesight/worker/celery_app.py`: register `@_typed_task(celery_app, name="codesight.webhook.handle_pr_event")` and `@_typed_task(celery_app, name="codesight.webhook.handle_push_event")` logging structured JSON (`"webhook_pr_stub_received"` / `"webhook_push_stub_received"`) with `trace_id` and returning `{"status": "accepted", "trace_id": trace_id}` (ready for `CA-7` and `CA-14` to replace with full pipeline logic).
- In `src/codesight/api/routes/webhooks.py`: implement `verify_github_signature` (`hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()` compared with `hmac.compare_digest`), chunk-bounded `read_bounded_body`, in-memory event classifier, `pg_insert(WebhookEvent).values(...).on_conflict_do_nothing(index_elements=[WebhookEvent.delivery_id]).returning(WebhookEvent.delivery_id)`, dual-key `Repository` lookup/upsert (`or_(Repository.github_repo_id == gh_id, Repository.full_name == full_name)`), and transactional `CeleryWebhookDispatcher` rollback on broker error.
- In `src/codesight/api/main.py`: add `@asynccontextmanager async def lifespan(app: FastAPI)` creating & disposing `AsyncEngine` when `session_factory` was not pre-injected, attach `app.state.settings`, `app.state.session_factory`, and `app.state.webhook_dispatcher`, and call `application.include_router(webhooks_router)`.

- [ ] **Step 4: Run full `pytest` suite with coverage gate, `mypy`, and `ruff`**

Run: `& "$HOME\.local\bin\uv.exe" run pytest --cov=src --cov-fail-under=80 -v && & "$HOME\.local\bin\uv.exe" run mypy src && & "$HOME\.local\bin\uv.exe" run ruff check .`
Expected: PASS (`100%` green, branch coverage `>= 85%`, mean webhook latency `< 100ms`, `0` mypy/ruff issues).

- [ ] **Step 5: Commit Webhook Gateway & Celery stubs**

```powershell
git add src/codesight/worker/celery_app.py src/codesight/api tests/unit/test_webhook_hmac.py
git commit -m "feat(ca-6): add <100ms GitHub webhook gateway with HMAC, idempotency, and Celery dispatch"
```

---

### Task 5: Docker Alembic Startup, Live End-to-End Verification & Jira `CA-6` Closure

**Files:**
- Modify: `Dockerfile`

**Interfaces:**
- Consumes: `alembic.ini`, `alembic/`, `codesight.api.main:app`, `docker-compose.yml`
- Produces: Running `healthy` 5-container `codesight` stack auto-migrating PostgreSQL on startup, live verified `< 100ms` `POST http://localhost:8082/api/v1/webhooks/github` -> `codesight-worker` round-trip, PR on `Tieens-04/CodeSight-AI`, and closed Jira ticket `CA-6` (`41` `Done`)

- [ ] **Step 1: Update `Dockerfile` to bundle `alembic.ini` + `alembic/` and run `alembic upgrade head` on API boot**

In `Dockerfile` `runtime` stage (before `USER codesight`):
```dockerfile
COPY --chown=codesight:codesight alembic.ini ./alembic.ini
COPY --chown=codesight:codesight alembic ./alembic
```
And update default `CMD`:
```dockerfile
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn codesight.api.main:app --host 0.0.0.0 --port 8000"]
```
(`codesight-worker` overrides `command:` in `docker-compose.yml`, keeping the exact 5-service stack with zero migration race).

- [ ] **Step 2: Rebuild & boot the 5-service `codesight` Docker stack and verify live HMAC + idempotency + `< 100ms` SLA + `trace_id` propagation**

Configure a dev `GITHUB_WEBHOOK_SECRET` in gitignored `.env` (never committed), run `docker compose -p codesight up -d --build --wait`, verify `flintflow-*` / `documind-*` coexistence via `docker ps`, and execute a short Python verification probe against `http://localhost:8082/api/v1/webhooks/github`:
- Invalid HMAC -> `401`
- Valid signed `pull_request.opened` (`X-GitHub-Delivery: ca6-live-001`) -> `202 Accepted` in `< 100ms`
- Replay `ca6-live-001` -> `200 OK` (`"status": "duplicate"`) in `< 100ms`
- Confirm `docker logs codesight-worker --tail 20` shows `"webhook_pr_stub_received"` with `"trace_id": "ca6-live-001"`

- [ ] **Step 3: Run mechanical pre-close gate, commit `Dockerfile`, push branch & open PR**

Run: `python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket CA-6`
Expected: `[PASS] All CodeSight-AI pre-close gates passed` (`exit 0`).

```powershell
git add Dockerfile
git commit -m "build(ca-6): bundle Alembic into runtime image and auto-migrate on API startup"
git push -u origin feat/ca-6-day2-db-auth-webhook
gh pr create --base main --head feat/ca-6-day2-db-auth-webhook --title "feat(ca-6): Day 2 PostgreSQL schema (Alembic), GitHub App auth & <100ms webhook gateway" --body "Implements Jira CA-6."
```

- [ ] **Step 4: Transition `CA-6` to `31` (`Verify / Test`), post Jira Verification Evidence comment, and transition to `41` (`Done`)**

1. Call `jira_transition_issue` (`issue_key="CA-6"`, `transition="31"`).
2. Re-confirm `preclose_audit.py --ticket CA-6` output (`ruff`, `mypy`, `pytest --cov=src --cov-fail-under=80` >= 80%).
3. Call `jira_add_comment` on `CA-6` following the mandatory 5-section Verification Evidence template from `.agents/skills/codesight-jira-workflow/SKILL.md`.
4. Call `jira_transition_issue` (`issue_key="CA-6"`, `transition="41"`).
