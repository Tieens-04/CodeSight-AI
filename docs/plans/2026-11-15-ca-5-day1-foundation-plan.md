# CA-5 Day 1 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bootstrap the CodeSight-AI Python 3.12 (`uv`) repository, Public GitHub remote (`Tieens-04/CodeSight-AI`), dual-topology `Settings`, cross-boundary `trace_id` JSON logging, FastAPI `GET /health` gateway, Celery worker bootstrap, `codesight` CLI, and port-isolated 5-service Docker Compose stack (`CA-5`).

**Architecture:** Single-`.env` configuration defaults to host-mapped ports (`5433`, `6381`, `6333/6334`, `8082`) for Windows CLI/tests while `docker-compose.yml` overrides service hostnames on internal bridge network `codesight-net`. Pure ASGI `TraceIdMiddleware` and Celery publish/prerun signals propagate `trace_id` automatically through `structlog.contextvars` into single-line JSON logs on `stderr`, keeping CLI output isolated on `stdout`.

**Tech Stack:** Python `3.12` (`uv`), FastAPI, Uvicorn, Pydantic v2 & `pydantic-settings`, `structlog`, Typer, Rich, `asyncpg`, `redis` (`redis.asyncio`), `httpx`, Celery, Docker Compose (`postgres:16-alpine`, `redis:7-alpine`, `qdrant/qdrant:latest`), `ruff`, `mypy --strict`, `pytest` + `pytest-cov`.

**Spec:** `docs/specs/2026-11-15-ca-5-day1-foundation-design.md`

## Global Constraints

- Python runtime pinned to `3.12` (`.python-version = 3.12`, `requires-python = ">=3.12,<3.14"`), managed strictly via `uv` (`~/.local/bin/uv.exe` on Windows host; never run global `pip install`).
- Active implementation branch MUST be `feat/ca-5-day1-foundation` branched from `main` on Public repository `Tieens-04/CodeSight-AI`; zero `.env`, `*.pem`, or `*.key` files tracked or untracked in `git status --porcelain`.
- Never stop, restart, or bind ports reserved by `flintflow-*` (`3001`, `6379`, `8080`) or `documind-*` (`3002`, `5432`, `6380`, `8081`, `9000`, `9001`); always scope Docker Compose commands with `-p codesight`.
- Canonical host port defaults: `POSTGRES_PORT=5433`, `REDIS_PORT=6381`, `QDRANT_HTTP_PORT=6333`, `QDRANT_GRPC_PORT=6334`, `API_PORT=8082`; total container `mem_limit` cap `3.0 GiB` (`512m` + `256m` + `1g` + `512m` + `768m`) with `tmpfs: /tmp` on `codesight-api` and `codesight-worker`.
- Quality gates required before Jira transition `41` (`Done`): `uv run ruff check .` (`0` errors), `uv run mypy src` (`0` issues under `strict = true`), `uv run pytest --cov=src --cov-fail-under=80` (`>= 80%` coverage), and `python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket CA-5` exiting `0`.

## Review Focus

- Special characters (`@`, `:`, `/`, `#`, `%`) in `POSTGRES_PASSWORD` or `REDIS_PASSWORD` must be URL-encoded via `urllib.parse.quote_plus` in computed DSNs, and `repr(settings)` / `str(settings)` must never leak plaintext secrets (`Task 2`).
- Malformed or hostile `X-Trace-ID` / `X-GitHub-Delivery` headers containing `\r\n`, spaces, empty strings, or >128 characters must be rejected by `sanitize_trace_id()` and replaced with a clean 32-char hex UUID (`Task 3`).
- An unhandled exception raised inside a FastAPI route must still clear `structlog.contextvars` in `TraceIdMiddleware`'s `finally` block so subsequent requests never inherit a stale `trace_id` (`Task 4`).
- A hanging or failing health probe must time out at `health_timeout_seconds` (`1.5s`), flip `GET /health` to `503` (`status="degraded"`) without blocking sibling probes, and scrub credentials/DSNs from `detail` (`Task 4`).
- Running `codesight version` must emit output strictly to `stdout` with zero JSON log lines mixed in, while `codesight --verbose version` emits valid JSON logs strictly on `stderr` (`Task 5`).

---

## File Structure

- `.gitignore`, `.dockerignore`, `.python-version`, `pyproject.toml`, `uv.lock`, `.env.example`: Git hygiene, Python `3.12` lock, tool configs (`ruff`, `mypy`, `pytest`, `coverage`), and documented host defaults.
- `src/codesight/__init__.py` + empty package `__init__.py` files (`vcs`, `config_reader`, `diff`, `ast_engine`, `rag`, `agents`, `scoring`, `db`): Canonical 12-module namespace and `__version__ = "0.1.0"`.
- `src/codesight/core/config.py`: `Settings` (`BaseSettings`) with `SecretStr` fields, computed URL-encoded DSNs, and `@lru_cache` `get_settings()`.
- `src/codesight/core/logging.py`: `sanitize_trace_id()`, `bind_trace_id()`, `get_trace_id()`, `clear_trace_context()`, and `configure_logging()` (`structlog` + stdlib `ProcessorFormatter` JSON renderer).
- `src/codesight/api/health.py`: `ServiceCheckResult`, `HealthResponse`, `HealthProbe`, default async probes (`probe_postgres`, `probe_redis`, `probe_qdrant`), and `run_health_checks()`.
- `src/codesight/api/main.py`: Pure ASGI `TraceIdMiddleware`, `create_app()`, and module-level `app`.
- `src/codesight/worker/celery_app.py`: `create_celery_app()`, `celery_app`, `trace_id` publish/prerun/postrun signal handlers, and `codesight.ping` task.
- `src/codesight/cli/main.py`: Typer `app` with `--verbose` callback and `version` command.
- `Dockerfile`, `docker-compose.yml`: Non-root multi-stage Python `3.12` container build and 5-service isolated stack.
- `tests/unit/test_config_and_logging.py`, `tests/unit/test_health_api.py`, `tests/unit/test_worker_and_cli.py`: Offline unit test suite achieving `>= 85%` branch/line coverage.

---

### Task 1: Git Baseline, Public GitHub Remote & Python `3.12` (`uv`) Skeleton

**Files:**
- Create: `.gitignore`, `.python-version`, `pyproject.toml`
- Create: `src/codesight/__init__.py` and `__init__.py` in `core`, `cli`, `api`, `vcs`, `config_reader`, `diff`, `ast_engine`, `rag`, `agents`, `scoring`, `worker`, `db`
- Create: `tests/__init__.py`, `tests/unit/__init__.py`

**Interfaces:**
- Consumes: Pre-existing `AGENTS.md`, `product_overview_autonomous_pr_reviewer.md`, `.agents/`, `docs/`
- Produces: `codesight.__version__: str = "0.1.0"`, locked `.venv` + `uv.lock`, Git branch `feat/ca-5-day1-foundation` tracking Public remote `Tieens-04/CodeSight-AI`

- [ ] **Step 1: Initialize `main`, add `.gitignore` & `.python-version`, commit baseline docs, create Public GitHub repo, and branch `feat/ca-5-day1-foundation`**

Create `.gitignore` ignoring `.env`, `.env.*` (with `!.env.example`), `*.pem`, `*.key`, `.venv/`, `__pycache__/`, `.mypy_cache/`, `.ruff_cache/`, `.pytest_cache/`, `.coverage`, `htmlcov/`, `dist/`, `build/`. Write `3.12` to `.python-version`. Run:

```powershell
git init -b main
git add .gitignore .python-version AGENTS.md product_overview_autonomous_pr_reviewer.md .agents docs
git commit -m "chore: initialize repository baseline, skills, and CA-5 design docs"
gh repo create Tieens-04/CodeSight-AI --public --source=. --remote=origin --push
git checkout -b feat/ca-5-day1-foundation
```

- [ ] **Step 2: Author `pyproject.toml` and all 12 subpackage `__init__.py` files**

Configure `hatchling` build (`packages = ["src/codesight"]`), `[project.scripts] codesight = "codesight.cli.main:app"`, runtime dependencies (`fastapi>=0.115`, `uvicorn[standard]>=0.32`, `pydantic>=2.9`, `pydantic-settings>=2.6`, `structlog>=24.4`, `typer>=0.13`, `rich>=13.9`, `asyncpg>=0.30`, `redis>=5.2`, `httpx>=0.28`, `celery>=5.4`), and `[dependency-groups] dev` (`ruff>=0.8`, `mypy>=1.13`, `pytest>=8.3`, `pytest-asyncio>=0.24`, `pytest-cov>=6.0`). Configure:
- `[tool.ruff]`: `target-version = "py312"`, `line-length = 100`; `[tool.ruff.lint]` `select = ["E", "F", "I", "B", "UP", "ASYNC", "RUF"]`.
- `[tool.mypy]`: `python_version = "3.12"`, `strict = true`, `plugins = ["pydantic.mypy"]`, overrides with `ignore_missing_imports = true` for `["asyncpg.*", "celery.*"]`.
- `[tool.pytest.ini_options]`: `asyncio_mode = "auto"`, `testpaths = ["tests"]`.
- `[tool.coverage.run]`: `source = ["src/codesight"]`, `branch = true`.

Define `__version__ = "0.1.0"` in `src/codesight/__init__.py` and docstring-only `__init__.py` files across the 12 subpackages plus `tests/` and `tests/unit/`.

- [ ] **Step 3: Lock dependencies with `uv sync` and verify static tooling**

Run: `& "$HOME\.local\bin\uv.exe" sync && & "$HOME\.local\bin\uv.exe" run ruff check . && & "$HOME\.local\bin\uv.exe" run mypy src`
Expected: `uv.lock` created under Python `3.12.x`; `ruff` and `mypy` report `0` errors.

- [ ] **Step 4: Commit skeleton**

```powershell
git add pyproject.toml uv.lock src tests
git commit -m "build(ca-5): scaffold python 3.12 uv project and 12-module package tree"
```

---

### Task 2: Dual-Topology `Settings` & Secret Redaction (`config.py` + `.env.example`)

**Files:**
- Create: `src/codesight/core/config.py`, `.env.example`
- Create: `tests/unit/test_config_and_logging.py`

**Interfaces:**
- Consumes: `pydantic.SecretStr`, `pydantic_settings.BaseSettings`
- Produces:
  - `class Settings(BaseSettings)` with properties `postgres_dsn -> str`, `sqlalchemy_async_dsn -> str`, `redis_url -> str`, `qdrant_http_url -> str`
  - `get_settings() -> Settings` (`@lru_cache(maxsize=1)`)

- [ ] **Step 1: Write failing `Settings` unit tests in `tests/unit/test_config_and_logging.py`**

```python
from pydantic import SecretStr
import pytest
from codesight.core.config import Settings, get_settings


def test_settings_canonical_host_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("POSTGRES_PORT", raising=False)
    monkeypatch.delenv("REDIS_PORT", raising=False)
    s = Settings(_env_file=None)
    assert s.postgres_port == 5433
    assert s.redis_port == 6381
    assert s.qdrant_http_port == 6333
    assert s.qdrant_grpc_port == 6334
    assert s.api_port == 8082
    assert s.qdrant_http_url == "http://localhost:6333"
    assert s.github_webhook_secret is None
    assert s.gemini_api_key is None


def test_settings_docker_override_and_special_char_dsn_encoding() -> None:
    s = Settings(
        _env_file=None,
        postgres_host="codesight-postgres",
        postgres_port=5432,
        postgres_user="cs_user",
        postgres_password=SecretStr("p@ss:w/rd#1%"),
        postgres_db="codesight",
        redis_host="codesight-redis",
        redis_port=6379,
        redis_password=SecretStr("r#dis@pw"),
    )
    assert s.postgres_dsn == "postgresql://cs_user:p%40ss%3Aw%2Frd%231%25@codesight-postgres:5432/codesight"
    assert s.sqlalchemy_async_dsn.startswith("postgresql+asyncpg://cs_user:p%40ss%3Aw%2Frd%231%25@")
    assert s.redis_url == "redis://:r%23dis%40pw@codesight-redis:6379/0"
    assert "p@ss:w/rd#1%" not in repr(s)
    assert "p@ss:w/rd#1%" not in str(s)


def test_get_settings_cache_can_clear() -> None:
    get_settings.cache_clear()
    assert get_settings() is get_settings()
```

- [ ] **Step 2: Run `pytest` to confirm failure**

Run: `& "$HOME\.local\bin\uv.exe" run pytest tests/unit/test_config_and_logging.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codesight.core.config'`

- [ ] **Step 3: Implement `src/codesight/core/config.py` and `.env.example`**

Implement `Settings` matching Section 3.2 of the spec (`quote_plus` on user/password in `postgres_dsn`, `sqlalchemy_async_dsn`, and `redis_url`; `redis_url` omits `:password@` when `redis_password is None`). Populate `.env.example` with commented sections for Core App, Host-Isolated Ports (`5433`, `6381`, `6333`, `6334`, `8082`), and Optional Phase 2+ Secrets (`GITHUB_*`, `GEMINI_API_KEY`, `LANGFUSE_*`).

- [ ] **Step 4: Run tests and static checks**

Run: `& "$HOME\.local\bin\uv.exe" run pytest tests/unit/test_config_and_logging.py -v && & "$HOME\.local\bin\uv.exe" run mypy src && & "$HOME\.local\bin\uv.exe" run ruff check .`
Expected: PASS (`3 passed`, `0` mypy/ruff issues).

- [ ] **Step 5: Commit `Settings` and `.env.example`**

```powershell
git add src/codesight/core/config.py .env.example tests/unit/test_config_and_logging.py
git commit -m "feat(ca-5): add dual-topology Settings with SecretStr redaction"
```

---

### Task 3: Structured JSON Logging & `trace_id` Context (`logging.py`)

**Files:**
- Create: `src/codesight/core/logging.py`
- Modify: `tests/unit/test_config_and_logging.py`

**Interfaces:**
- Consumes: `structlog`, `structlog.contextvars`, stdlib `logging`
- Produces:
  - `sanitize_trace_id(raw: str | None) -> str`
  - `bind_trace_id(trace_id: str | None = None) -> str`
  - `get_trace_id() -> str | None`
  - `clear_trace_context() -> None`
  - `configure_logging(level: str = "INFO", service_name: str = "codesight-api", stream: TextIO | None = None) -> None`

- [ ] **Step 1: Append failing JSON logging & `trace_id` sanitization tests to `tests/unit/test_config_and_logging.py`**

```python
import io
import json
import logging
import structlog
from codesight.core.logging import (
    bind_trace_id,
    clear_trace_context,
    configure_logging,
    get_trace_id,
    sanitize_trace_id,
)


def test_sanitize_trace_id_accepts_valid_and_rejects_hostile_inputs() -> None:
    assert sanitize_trace_id("gh-delivery_123.abc") == "gh-delivery_123.abc"
    for bad in [None, "", "   ", "evil\r\ninjected: true", "x" * 129, "bad space"]:
        cleaned = sanitize_trace_id(bad)
        assert len(cleaned) == 32 and cleaned.isalnum()


def test_configure_logging_emits_json_with_trace_id_for_structlog_and_stdlib() -> None:
    buf = io.StringIO()
    clear_trace_context()
    configure_logging(level="INFO", service_name="codesight-test", stream=buf)
    tid = bind_trace_id("trace-abc-001")
    assert tid == "trace-abc-001"
    assert get_trace_id() == "trace-abc-001"

    structlog.get_logger("codesight.unit").info("custom_event", answer=42)
    logging.getLogger("uvicorn.error").warning("stdlib_warning")

    clear_trace_context()
    assert get_trace_id() is None

    lines = [json.loads(line) for line in buf.getvalue().strip().splitlines()]
    assert len(lines) == 2
    assert lines[0]["event"] == "custom_event"
    assert lines[0]["answer"] == 42
    assert lines[0]["trace_id"] == "trace-abc-001"
    assert lines[0]["service"] == "codesight-test"
    assert "timestamp" in lines[0]
    assert lines[1]["event"] == "stdlib_warning"
    assert lines[1]["trace_id"] == "trace-abc-001"
```

- [ ] **Step 2: Run `pytest` to confirm failure**

Run: `& "$HOME\.local\bin\uv.exe" run pytest tests/unit/test_config_and_logging.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codesight.core.logging'`

- [ ] **Step 3: Implement `src/codesight/core/logging.py`**

Implement `sanitize_trace_id` with regex `^[A-Za-z0-9._-]{1,128}$`. Store/retrieve `trace_id` via `structlog.contextvars.bind_contextvars` / `get_contextvars` / `clear_contextvars`. In `configure_logging`, build shared pre-chain processors (`merge_contextvars`, `add_log_level`, `add_logger_name`, `TimeStamper(fmt="iso", utc=True)`, custom processor injecting `service=service_name` and default `trace_id="-"` when absent) fed into `structlog.stdlib.ProcessorFormatter(processor=JSONRenderer())` on a `logging.StreamHandler(stream or sys.stderr)`. Clear existing root handlers before attaching, and disable `uvicorn.access` propagation.

- [ ] **Step 4: Run tests and static checks**

Run: `& "$HOME\.local\bin\uv.exe" run pytest tests/unit/test_config_and_logging.py -v && & "$HOME\.local\bin\uv.exe" run mypy src && & "$HOME\.local\bin\uv.exe" run ruff check .`
Expected: PASS (`5 passed`, `0` mypy/ruff issues).

- [ ] **Step 5: Commit `logging.py`**

```powershell
git add src/codesight/core/logging.py tests/unit/test_config_and_logging.py
git commit -m "feat(ca-5): implement structured JSON logging and trace_id contextvars"
```

---

### Task 4: FastAPI Gateway, Pure ASGI `TraceIdMiddleware` & Concurrent `GET /health`

**Files:**
- Create: `src/codesight/api/health.py`, `src/codesight/api/main.py`
- Create: `tests/unit/test_health_api.py`

**Interfaces:**
- Consumes: `Settings`, `get_settings()`, `configure_logging()`, `bind_trace_id()`, `get_trace_id()`, `clear_trace_context()`
- Produces:
  - `ServiceCheckResult(BaseModel)`: `status: Literal["up", "down"]`, `latency_ms: float`, `detail: str | None = None`
  - `HealthResponse(BaseModel)`: `status: Literal["ok", "degraded"]`, `version: str`, `trace_id: str`, `checks: dict[str, ServiceCheckResult]`
  - `HealthProbe = Callable[[Settings], Awaitable[None]]` (raises on failure; `run_health_checks` measures latency, enforces timeout, and scrubs error text)
  - `probe_postgres(settings: Settings) -> None`, `probe_redis(settings: Settings) -> None`, `probe_qdrant(settings: Settings) -> None`
  - `create_app(settings: Settings | None = None, probes: Mapping[str, HealthProbe] | None = None) -> FastAPI` and `app: FastAPI`

- [ ] **Step 1: Write failing API & middleware unit tests in `tests/unit/test_health_api.py`**

```python
import asyncio
import httpx
from pydantic import SecretStr
import pytest
from codesight.api.main import create_app
from codesight.core.config import Settings
from codesight.core.logging import get_trace_id


async def _ok_probe(_: Settings) -> None:
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_health_ok_and_github_delivery_trace_header_precedence() -> None:
    settings = Settings(_env_file=None)
    app = create_app(
        settings=settings,
        probes={"postgres": _ok_probe, "redis": _ok_probe, "qdrant": _ok_probe},
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health", headers={"X-GitHub-Delivery": "delivery-guid-99"})
    assert resp.status_code == 200
    assert resp.headers["x-trace-id"] == "delivery-guid-99"
    body = resp.json()
    assert body["status"] == "ok"
    assert body["trace_id"] == "delivery-guid-99"
    assert set(body["checks"].keys()) == {"postgres", "redis", "qdrant"}
    assert get_trace_id() is None


@pytest.mark.asyncio
async def test_health_degraded_503_on_timeout_and_scrubs_secrets() -> None:
    secret_pw = "super_secret_pg_pw"
    settings = Settings(
        _env_file=None,
        postgres_password=SecretStr(secret_pw),
        health_timeout_seconds=0.05,
    )

    async def _slow_probe(_: Settings) -> None:
        await asyncio.sleep(0.5)

    async def _leaky_probe(s: Settings) -> None:
        raise ConnectionError(f"failed connecting to {s.postgres_dsn}")

    app = create_app(
        settings=settings,
        probes={"postgres": _leaky_probe, "redis": _slow_probe, "qdrant": _ok_probe},
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health", headers={"X-Trace-ID": "bad\r\nheader"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["postgres"]["status"] == "down"
    assert secret_pw not in (body["checks"]["postgres"]["detail"] or "")
    assert body["checks"]["redis"]["detail"] == "TimeoutError"
    assert body["checks"]["qdrant"]["status"] == "up"
    assert len(resp.headers["x-trace-id"]) == 32


@pytest.mark.asyncio
async def test_middleware_clears_trace_context_when_route_raises() -> None:
    app = create_app(
        settings=Settings(_env_file=None),
        probes={"postgres": _ok_probe, "redis": _ok_probe, "qdrant": _ok_probe},
    )

    @app.get("/boom")
    async def _boom() -> None:
        assert get_trace_id() == "req-boom-1"
        raise RuntimeError("kaboom")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/boom", headers={"X-Request-ID": "req-boom-1"})
    assert resp.status_code == 500
    assert get_trace_id() is None
```

- [ ] **Step 2: Run `pytest` to confirm failure**

Run: `& "$HOME\.local\bin\uv.exe" run pytest tests/unit/test_health_api.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codesight.api.main'`

- [ ] **Step 3: Implement `src/codesight/api/health.py` and `src/codesight/api/main.py`**

In `health.py`:
- `probe_postgres`: `conn = await asyncpg.connect(dsn=settings.postgres_dsn)` -> `try: await conn.fetchval("SELECT 1") finally: await conn.close()`.
- `probe_redis`: `client = redis.asyncio.from_url(settings.redis_url)` -> `try: await client.ping() finally: await client.aclose()`.
- `probe_qdrant`: `async with httpx.AsyncClient() as client: r = await client.get(f"{settings.qdrant_http_url}/readyz"); r.raise_for_status()`.
- `_execute_probe(name, probe, settings)` wraps `await asyncio.wait_for(probe(settings), timeout=settings.health_timeout_seconds)`; on any `Exception as exc`, sets `status="down"` and `detail=type(exc).__name__` (never formatting raw exception message containing DSNs!).
- Also add unit tests mocking `asyncpg.connect`, `redis.asyncio.from_url`, and `httpx.AsyncClient.get` in `test_health_api.py` so default probe functions are covered.

In `main.py`:
- Implement pure ASGI `TraceIdMiddleware` checking lowercase header keys `b"x-trace-id"`, `b"x-request-id"`, `b"x-github-delivery"` in order, binding via `bind_trace_id`, injecting `(b"x-trace-id", trace_id.encode("ascii"))` into `http.response.start`, logging `http_request_completed`, and calling `clear_trace_context()` in `finally`.
- Expose `create_app(...)` and `app = create_app()`.

- [ ] **Step 4: Run tests and static checks**

Run: `& "$HOME\.local\bin\uv.exe" run pytest tests/unit/test_health_api.py -v && & "$HOME\.local\bin\uv.exe" run mypy src && & "$HOME\.local\bin\uv.exe" run ruff check .`
Expected: PASS (`0` mypy/ruff issues).

- [ ] **Step 5: Commit FastAPI Gateway & `/health`**

```powershell
git add src/codesight/api/health.py src/codesight/api/main.py tests/unit/test_health_api.py
git commit -m "feat(ca-5): add FastAPI gateway, ASGI TraceIdMiddleware, and concurrent /health"
```

---

### Task 5: Celery Worker Bootstrap (`trace_id` Signals) & `codesight` CLI

**Files:**
- Create: `src/codesight/worker/celery_app.py`, `src/codesight/cli/main.py`
- Create: `tests/unit/test_worker_and_cli.py`

**Interfaces:**
- Consumes: `Settings`, `get_settings()`, `configure_logging()`, `bind_trace_id()`, `get_trace_id()`, `clear_trace_context()`, `codesight.__version__`
- Produces:
  - `create_celery_app(settings: Settings | None = None) -> Celery`, `celery_app: Celery`, task `codesight.ping() -> dict[str, str]`
  - `codesight.cli.main:app` (`Typer`) supporting `codesight --help`, `codesight version`, and `--verbose / -v`

- [ ] **Step 1: Write failing Worker signal & CLI stream isolation tests in `tests/unit/test_worker_and_cli.py`**

```python
import json
import pytest
from typer.testing import CliRunner
from codesight import __version__
from codesight.cli.main import app as cli_app
from codesight.core.logging import bind_trace_id, clear_trace_context, get_trace_id
from codesight.worker.celery_app import (
    celery_app,
    inject_trace_id_header,
    on_task_postrun,
    on_task_prerun,
    ping,
)


def test_celery_signals_propagate_and_clean_trace_id() -> None:
    clear_trace_context()
    bind_trace_id("http-to-celery-777")
    headers: dict[str, object] = {}
    inject_trace_id_header(headers=headers)
    assert headers["trace_id"] == "http-to-celery-777"
    clear_trace_context()

    class _DummyTask:
        class request:
            headers = {"trace_id": "http-to-celery-777"}

    on_task_prerun(task_id="task-fallback-1", task=_DummyTask())
    result = ping()
    assert result == {"status": "pong", "trace_id": "http-to-celery-777"}
    on_task_postrun()
    assert get_trace_id() is None
    assert celery_app.main == "codesight"


def test_cli_help_and_version_keep_stdout_free_of_json_logs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    runner = CliRunner()
    help_res = runner.invoke(cli_app, ["--help"])
    assert help_res.exit_code == 0

    capsys.readouterr()
    ver_res = runner.invoke(cli_app, ["version"])
    captured_default = capsys.readouterr()
    assert ver_res.exit_code == 0
    assert __version__ in ver_res.stdout
    assert "5433" in ver_res.stdout and "8082" in ver_res.stdout
    assert "{" not in ver_res.stdout
    assert captured_default.err == ""

    verbose_res = runner.invoke(cli_app, ["--verbose", "version"])
    captured_verbose = capsys.readouterr()
    assert verbose_res.exit_code == 0
    assert "{" not in verbose_res.stdout
    stderr_out = getattr(verbose_res, "stderr", "") or captured_verbose.err
    stderr_lines = [json.loads(line) for line in stderr_out.strip().splitlines()]
    assert any(entry["service"] == "codesight-cli" for entry in stderr_lines)
```

- [ ] **Step 2: Run `pytest` to confirm failure**

Run: `& "$HOME\.local\bin\uv.exe" run pytest tests/unit/test_worker_and_cli.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Implement `src/codesight/worker/celery_app.py` and `src/codesight/cli/main.py`**

In `celery_app.py`: connect `before_task_publish.connect(inject_trace_id_header)`, `worker_process_init.connect(...)`, `task_prerun.connect(on_task_prerun)`, `task_postrun.connect(on_task_postrun)`, and register `@celery_app.task(name="codesight.ping") def ping() -> dict[str, str]`.
In `cli/main.py`: configure `Typer(name="codesight", no_args_is_help=True, add_completion=False)`. Emit Rich `Console(file=sys.stdout)` output in `version`, pass `stream=sys.stderr` lazily (`configure_logging(..., stream=None)` resolving `sys.stderr` at call time so `capsys`/CLI capture sees `stderr`), and log `"cli_version_displayed"` at `DEBUG` when `--verbose` is set.

- [ ] **Step 4: Run full unit test suite with `>= 80%` coverage gate, `mypy`, and `ruff`**

Run: `& "$HOME\.local\bin\uv.exe" run pytest --cov=src --cov-fail-under=80 -v && & "$HOME\.local\bin\uv.exe" run mypy src && & "$HOME\.local\bin\uv.exe" run ruff check .`
Expected: PASS (`100%` tests green, coverage `>= 85%`, `0` mypy/ruff issues). Also verify `& "$HOME\.local\bin\uv.exe" run codesight version`.

- [ ] **Step 5: Commit Celery Worker & CLI**

```powershell
git add src/codesight/worker/celery_app.py src/codesight/cli/main.py tests/unit/test_worker_and_cli.py
git commit -m "feat(ca-5): add Celery trace_id signal bootstrap and codesight CLI"
```

---

### Task 6: `Dockerfile`, `docker-compose.yml`, Live Stack Verification & Jira `CA-5` Closure

**Files:**
- Create: `.dockerignore`, `Dockerfile`, `docker-compose.yml`

**Interfaces:**
- Consumes: `pyproject.toml`, `uv.lock`, `codesight.api.main:app`, `codesight.worker.celery_app:celery_app`, `.env.example`
- Produces: Running `healthy` 5-container stack (`codesight-postgres`, `codesight-redis`, `codesight-qdrant`, `codesight-api`, `codesight-worker`), verified PR on `Tieens-04/CodeSight-AI`, and closed Jira ticket `CA-5` (`41` `Done`)

- [ ] **Step 1: Create `.dockerignore`, multi-stage non-root `Dockerfile`, and 5-service `docker-compose.yml`**

- `.dockerignore`: exclude `.git`, `.venv`, `__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, `.env`, `tests`, `docs`, `.agents`.
- `Dockerfile`: stage 1 (`python:3.12-slim` + `COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv`) runs `uv sync --frozen --no-dev --no-install-project`, copies `src/` and `pyproject.toml` + `README`/metadata, runs `uv sync --frozen --no-dev`; stage 2 (`python:3.12-slim`) creates user `codesight` (`UID 10001`), copies `/app/.venv` and `/app/src`, sets `PATH="/app/.venv/bin:$PATH"`, `USER codesight`, default `CMD ["uvicorn", "codesight.api.main:app", "--host", "0.0.0.0", "--port", "8000"]`.
- `docker-compose.yml`: implement exact service names, ports (`5433:5432`, `6381:6379`, `6333:6333`, `6334:6334`, `8082:8000`), `mem_limit` values (`512m`, `256m`, `1g`, `512m`, `768m`), Redis `volatile-lru` command, Qdrant `/dev/tcp/127.0.0.1/6333` `/readyz` healthcheck, `tmpfs: ["/tmp:size=128m,mode=1777"]` on `codesight-api` & `codesight-worker`, and internal host overrides (`POSTGRES_HOST=codesight-postgres`, `POSTGRES_PORT=5432`, `REDIS_HOST=codesight-redis`, `REDIS_PORT=6379`, `QDRANT_HOST=codesight-qdrant`) from Section 2.1–2.2 of the spec.

- [ ] **Step 2: Copy `.env.example` to gitignored `.env`, boot Docker stack (`-p codesight`), and verify live health + coexistence**

Run:
```powershell
Copy-Item .env.example .env -Force
docker compose -p codesight up -d --build --wait
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
Invoke-RestMethod -Uri "http://localhost:8082/health" -Headers @{"X-Trace-ID"="ca5-live-verify-001"} | ConvertTo-Json -Depth 5
docker logs codesight-api --tail 5
```
Expected: All 5 `codesight-*` containers show `(healthy)`; all `flintflow-*` and `documind-*` containers remain `Up (healthy)`; `GET /health` returns `"status": "ok"` with `"trace_id": "ca5-live-verify-001"` and all 3 backing services `"up"`; `docker logs codesight-api` shows single-line JSON containing `"trace_id": "ca5-live-verify-001"`.

- [ ] **Step 3: Commit Docker artifacts, push `feat/ca-5-day1-foundation`, and open PR**

```powershell
git add .dockerignore Dockerfile docker-compose.yml
git commit -m "feat(ca-5): add non-root multi-stage Dockerfile and port-isolated Compose stack"
git push -u origin feat/ca-5-day1-foundation
gh pr create --base main --head feat/ca-5-day1-foundation --title "feat(ca-5): Day 1 Python 3.12 (uv) foundation, Docker stack & trace_id JSON logging" --body "Implements Jira CA-5."
```

- [ ] **Step 4: Transition `CA-5` to `31` (`Verify / Test`), run mechanical `preclose_audit.py`, post Jira Verification Evidence, and transition to `41` (`Done`)**

1. Call `jira_transition_issue` (`issue_key="CA-5"`, `transition="31"`).
2. Run: `python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket CA-5` -> confirm `[PASS]` and exit code `0`.
3. Call `jira_add_comment` on `CA-5` using the exact 5-section Verification Evidence template from `.agents/skills/codesight-jira-workflow/SKILL.md` with real command outputs.
4. Call `jira_transition_issue` (`issue_key="CA-5"`, `transition="41"`).
