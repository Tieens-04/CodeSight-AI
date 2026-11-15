# CA-5 Design Specification — Day 1 Python 3.12 (`uv`) Foundation, Port-Isolated Docker Stack & `trace_id` JSON Logging

- **Jira Ticket:** `CA-5` (*[Day 1] Khởi tạo Dự án Python 3.12 (uv), Docker Hạ tầng Cô lập Cổng & JSON Logging*)
- **Parent Epic / Sprint:** `CA-1` — `Sprint 1` (Board `74`)
- **Date:** 2026-11-15
- **Target Branch:** `feat/ca-5-day1-foundation` (branched from `main` on `Tieens-04/CodeSight-AI`)
- **Requirements Covered:** `NFR-1` (Gateway low-overhead middleware), `NFR-2` (Redis/Celery foundation), `NFR-3` (Secret redaction & `tmpfs` Zero-Code Persistence hygiene), `NFR-4` (Structured JSON logging with cross-boundary `trace_id`).

---

## 1. Purpose & Scope Boundaries

`CA-5` establishes the production-grade repository skeleton, local/container dual-topology configuration, structured JSON observability with end-to-end `trace_id` propagation, CLI entrypoint, and a 5-service Docker Compose stack that coexists safely alongside `flintflow-*` and `documind-*` on the same Windows/WSL2 host.

### In Scope (`CA-5`)
1. Initialize Git repository (`main` baseline + Public GitHub remote `Tieens-04/CodeSight-AI` + feature branch `feat/ca-5-day1-foundation`) and strict `.gitignore` preventing `.env`, `*.pem`, and `*.key` leaks.
2. Configure Python `3.12` via `uv` (`.python-version`, `pyproject.toml`, `uv.lock`) with strict linting (`ruff`), strict static typing (`mypy --strict` + `pydantic.mypy`), and unit testing (`pytest`, `pytest-asyncio`, `pytest-cov` enforcing `>= 80%` coverage on `src`).
3. Scaffold all 12 canonical subpackages under `src/codesight/`: `core`, `cli`, `api`, `vcs`, `config_reader`, `diff`, `ast_engine`, `rag`, `agents`, `scoring`, `worker`, `db`.
4. Implement `codesight.core.config.Settings` (`pydantic-settings`) with single-`.env` host defaults, container hostname overrides, URL-encoded computed DSNs, and `SecretStr` redaction.
5. Implement `codesight.core.logging` (`structlog` + `ProcessorFormatter`) emitting single-line JSON logs to `stderr`, bridging stdlib loggers (`uvicorn`, `celery`, `httpx`), and propagating `trace_id` across FastAPI ASGI requests and Celery tasks.
6. Implement FastAPI Gateway (`codesight.api.main`, `codesight.api.health`) exposing `GET /health` with concurrent bounded probes (`1.5s` timeout) against PostgreSQL 16, Redis 7, and Qdrant.
7. Implement minimal Celery worker bootstrap (`codesight.worker.celery_app`) with `codesight.ping` health task and signal-based `trace_id` propagation.
8. Implement `codesight` Typer + Rich CLI (`codesight --help`, `codesight version`, `--verbose`) keeping `stdout` strictly separate from `stderr` JSON logs.
9. Deliver multi-stage non-root `Dockerfile` and 5-service `docker-compose.yml` (`name: codesight`, `3.0 GiB` total memory ceiling, `tmpfs: /tmp`).

### Explicitly Out of Scope (Deferred to Subsequent Tickets)
- SQLAlchemy 2.0 Async ORM models, Alembic migrations, GitHub App RS256 JWT auth, and `POST /api/v1/webhooks/github` (`CA-6`).
- Celery exponential backoff policies, `Cancel-on-Supersede` Redis locks, `ephemeral_workspace()` context manager, and `.codesight.yaml` reader (`CA-7`).
- `SmartDiffParser`, `FileFilter`, and `DiffHunkMapper` (`CA-8`).
- `codesight review <pr_url> --dry-run` end-to-end execution (`CA-9`).

---

## 2. Host Coexistence & Docker Infrastructure Architecture

### 2.1 Port Isolation & Memory Budget Matrix

Host WSL2 Docker capacity is `7.6 GiB` with `flintflow-*` (`3001`, `6379`, `8080`) and `documind-*` (`3002`, `5432`, `6380`, `8081`, `9000`, `9001`) actively running. Every CodeSight container runs under Compose project `codesight` on isolated bridge network `codesight-net`:

| Service / Container | Image / Build | Host -> Container Port | `mem_limit` | Healthcheck Mechanism |
| :--- | :--- | :--- | :--- | :--- |
| `codesight-postgres` | `postgres:16-alpine` | `${POSTGRES_PORT:-5433}:5432` | `512m` | `pg_isready -U $$POSTGRES_USER -d $$POSTGRES_DB` |
| `codesight-redis` | `redis:7-alpine` | `${REDIS_PORT:-6381}:6379` | `256m` | `redis-cli ping` (`--maxmemory 192mb --maxmemory-policy volatile-lru`) |
| `codesight-qdrant` | `qdrant/qdrant:latest` | `${QDRANT_HTTP_PORT:-6333}:6333`, `${QDRANT_GRPC_PORT:-6334}:6334` | `1g` | Bash `/dev/tcp/127.0.0.1/6333` HTTP `GET /readyz` (`200 OK`) |
| `codesight-api` | Local `Dockerfile` (`uvicorn`) | `${API_PORT:-8082}:8000` | `512m` | Python `urllib.request.urlopen("http://127.0.0.1:8000/health", timeout=3)` |
| `codesight-worker` | Local `Dockerfile` (`celery`) | None (internal only) | `768m` | `celery -A codesight.worker.celery_app inspect ping -d celery@$$HOSTNAME --timeout 5` |

### 2.2 Infrastructure Design Decisions
- **Redis Eviction Safety (`volatile-lru`):** Redis serves both transient caches (TTL-bound SHA/token keys) and persistent Celery broker lists (no TTL). `volatile-lru` evicts only TTL-bearing keys under memory pressure and never drops queued Celery tasks.
- **Curl-less Qdrant Healthcheck:** Official `qdrant/qdrant:latest` images omit `curl` and `wget`. Executing `bash -c 'exec 3<>/dev/tcp/127.0.0.1/6333 && printf "GET /readyz HTTP/1.0\r\nHost: localhost\r\n\r\n" >&3 && grep -q "200 OK" <&3'` verifies Qdrant readiness natively.
- **Zero-Code Persistence Hygiene (`NFR-3`):** Both `codesight-api` and `codesight-worker` run as non-root user `codesight` (`UID 10001`) and mount `tmpfs: ["/tmp:size=128m,mode=1777"]` so temporary workspace files reside strictly in RAM and vanish on container stop.
- **Dual-Topology Networking:** `.env` defaults target `localhost` with host-mapped ports (`5433`, `6381`, `6333`, `6334`, `8082`) so Windows host commands (`uv run pytest`, `uv run codesight`, `uv run alembic`) work directly. Inside `docker-compose.yml`, `codesight-api` and `codesight-worker` load `env_file: .env` and override only `POSTGRES_HOST=codesight-postgres`, `POSTGRES_PORT=5432`, `REDIS_HOST=codesight-redis`, `REDIS_PORT=6379`, and `QDRANT_HOST=codesight-qdrant`.

---

## 3. Component Design & Contracts

### 3.1 Repository Structure

```text
CodeSight-AI/
├── .agents/
├── .dockerignore
├── .env.example
├── .gitignore
├── .python-version                  # pinned to 3.12
├── AGENTS.md
├── Dockerfile
├── docker-compose.yml
├── docs/
│   ├── plans/
│   └── specs/
├── pyproject.toml
├── uv.lock
├── src/
│   └── codesight/
│       ├── __init__.py              # __version__ = "0.1.0"
│       ├── core/
│       │   ├── __init__.py
│       │   ├── config.py            # Settings, get_settings()
│       │   └── logging.py           # configure_logging(), trace_id helpers
│       ├── api/
│       │   ├── __init__.py
│       │   ├── health.py            # ServiceCheckResult, HealthResponse, probes
│       │   └── main.py              # TraceIdMiddleware, create_app(), app
│       ├── worker/
│       │   ├── __init__.py
│       │   └── celery_app.py        # create_celery_app(), celery_app, ping task & signals
│       ├── cli/
│       │   ├── __init__.py
│       │   └── main.py              # Typer app: --help, version, --verbose
│       ├── vcs/__init__.py
│       ├── config_reader/__init__.py
│       ├── diff/__init__.py
│       ├── ast_engine/__init__.py
│       ├── rag/__init__.py
│       ├── agents/__init__.py
│       ├── scoring/__init__.py
│       └── db/__init__.py
└── tests/
    └── unit/
        ├── __init__.py
        ├── test_config_and_logging.py
        ├── test_health_api.py
        └── test_worker_and_cli.py
```

### 3.2 `codesight.core.config` (`Settings`)
- Inherits `pydantic_settings.BaseSettings` with `SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore")`.
- **Core Fields:**
  - `app_name: str = "codesight-ai"`
  - `app_env: Literal["development", "test", "production"] = "development"`
  - `log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"`
  - `api_host: str = "0.0.0.0"`, `api_port: int = 8082`
  - `postgres_host: str = "localhost"`, `postgres_port: int = 5433`, `postgres_user: str = "codesight"`, `postgres_password: SecretStr = SecretStr("codesight_dev_password")`, `postgres_db: str = "codesight"`
  - `redis_host: str = "localhost"`, `redis_port: int = 6381`, `redis_db: int = 0`, `redis_password: SecretStr | None = None`
  - `qdrant_host: str = "localhost"`, `qdrant_http_port: int = 6333`, `qdrant_grpc_port: int = 6334`
  - `health_timeout_seconds: float = 1.5`
- **Optional Future Secrets (`None` on Day 1):**
  - `github_app_id: str | None = None`
  - `github_private_key: SecretStr | None = None`
  - `github_webhook_secret: SecretStr | None = None`
  - `github_token: SecretStr | None = None`
  - `gemini_api_key: SecretStr | None = None`
  - `langfuse_public_key: str | None = None`
  - `langfuse_secret_key: SecretStr | None = None`
  - `langfuse_host: str = "https://cloud.langfuse.com"`
- **Computed DSNs (`@property`):**
  - `postgres_dsn -> str`: `postgresql://{quote_plus(user)}:{quote_plus(pw)}@{host}:{port}/{db}` (used by `asyncpg`).
  - `sqlalchemy_async_dsn -> str`: `postgresql+asyncpg://{quote_plus(user)}:{quote_plus(pw)}@{host}:{port}/{db}` (ready for `CA-6`).
  - `redis_url -> str`: `redis://[:{quote_plus(pw)}@]{host}:{port}/{db}`.
  - `qdrant_http_url -> str`: `http://{host}:{http_port}`.

### 3.3 `codesight.core.logging` (Structured JSON & `trace_id`)
- **`sanitize_trace_id(raw: str | None) -> str`:** Strips surrounding whitespace; if `raw` matches `^[A-Za-z0-9._-]{1,128}$`, returns it verbatim; otherwise generates `uuid.uuid4().hex`. Prevents log-forging (`\r\n`), empty whitespace IDs, and oversized header payloads.
- **Context Helpers:** `bind_trace_id(trace_id: str | None = None) -> str`, `get_trace_id() -> str | None`, `clear_trace_context() -> None` wrapping `structlog.contextvars`.
- **`configure_logging(level: str = "INFO", service_name: str = "codesight-api", stream: TextIO | None = None) -> None`:**
  - Configures `structlog` and stdlib `logging` root handler via `ProcessorFormatter` writing single-line JSON to `stream or sys.stderr`.
  - Every JSON record includes `timestamp` (ISO-8601 UTC), `level`, `service` (`service_name`), `logger`, `event`, and `trace_id` (merged from `structlog.contextvars`, defaulting to `"-" ` only for startup events outside any request/task context).
  - Sets `logging.getLogger("uvicorn.access").handlers.clear()` and `propagate = False` to avoid duplicate non-`trace_id` access lines alongside `TraceIdMiddleware`.

### 3.4 `codesight.api.health` & `codesight.api.main`
- **Pure ASGI `TraceIdMiddleware`:**
  - Passes non-`http` scopes directly through without modification.
  - Resolves incoming `trace_id` by checking lowercase ASGI headers in strict order: `x-trace-id` -> `x-request-id` -> `x-github-delivery` -> generated UUID via `sanitize_trace_id`.
  - Binds `trace_id` in `structlog.contextvars`, appends `(b"x-trace-id", trace_id.encode("ascii"))` on `http.response.start`, logs `http_request_completed` (`method`, `path`, `status_code`, `duration_ms`), and guarantees `clear_trace_context()` inside `finally`.
- **`GET /health` Endpoint:**
  - Runs 3 async probes (`postgres`, `redis`, `qdrant`) concurrently via `asyncio.gather`, wrapping each probe in `asyncio.wait_for(..., timeout=settings.health_timeout_seconds)` and measuring monotonic `latency_ms`.
  - Probe failure details report only error type / concise safe reason (`TimeoutError`, `ConnectionRefusedError`, `HTTP 503`) and strip/never interpolate DSNs or passwords.
  - Returns `200 OK` (`status="ok"`) when all 3 probes report `"up"`, or `503 Service Unavailable` (`status="degraded"`) if one or more probes report `"down"`.

### 3.5 `codesight.worker.celery_app` & `codesight.cli.main`
- **Celery Worker (`celery_app.py`):**
  - `create_celery_app(settings: Settings | None = None) -> Celery` configures `broker_url=settings.redis_url`, `result_backend=settings.redis_url`, JSON task serialization, UTC timezone.
  - Registers Celery signals:
    - `before_task_publish`: copies active `get_trace_id()` into `headers["trace_id"]` if present and not already set.
    - `worker_process_init`: initializes JSON logging with `service_name="codesight-worker"`.
    - `task_prerun` / `task_postrun`: binds `headers["trace_id"]` (falling back to `task_id`) before execution and clears contextvars afterward.
  - Defines `@celery_app.task(name="codesight.ping")` returning `{"status": "pong", "trace_id": bound_trace_id}`.
- **CLI (`cli/main.py`):**
  - Typer root callback accepts `--verbose / -v` (`bool = False`), configuring JSON logging to `sys.stderr` at `DEBUG` when verbose or `WARNING` otherwise.
  - `codesight version` writes a clean human-readable version and port summary strictly to `sys.stdout`.

---

## 4. Verification & Acceptance Criteria Mapping

1. **Docker Coexistence & Health (`AC-1`):** `docker compose -p codesight up -d --build --wait` brings all 5 `codesight-*` containers to `healthy` status while `flintflow-*` and `documind-*` remain `Up (healthy)` with zero port collisions.
2. **Real `GET /health` & `trace_id` JSON Log (`AC-2`):** `GET http://localhost:8082/health` returns `200 OK` with `"status": "ok"` for `postgres`, `redis`, and `qdrant`, echoes `X-Trace-ID`, and emits a JSON log entry on `codesight-api` containing the matching `trace_id`.
3. **Static Analysis, Type Safety & Coverage (`AC-3`):**
   - `uv run ruff check .` -> `0` errors.
   - `uv run mypy src` -> `0` issues (`strict = true`).
   - `uv run pytest --cov=src --cov-fail-under=80` -> `100%` pass, coverage `>= 80%`.
   - `python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket CA-5` -> exits `0`.
