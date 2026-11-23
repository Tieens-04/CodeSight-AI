# CA-7 Day 3 Celery Reliability, Cancel-on-Supersede Lock & `.codesight.yaml` Reader Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deliver CodeSight-AI's crash-resilient Celery + Redis worker (`acks_late=True`, `Retry-After` + Exponential Backoff + Jitter), unified `Cancel-on-Supersede` Lua CAS lock across Gateway and Worker, `NFR-3` self-destructing `ephemeral_workspace()`, and fault-tolerant `.codesight.yaml` repository configuration reader (`CA-7`).

**Architecture:** Domain retry exceptions (`RetryableTaskError`, `RateLimitTaskError`) and `ephemeral_workspace()` live in `codesight.core` so CLI and config consumers stay decoupled from Celery. `codesight.worker.supersede_lock` unifies `pr_active:{repo_id}:{pr_number}` state behind 1-RTT Lua Compare-And-Set scripts (`AsyncSupersedeLock` for `webhooks.py` + async worker checkpoints, `SyncSupersedeLock` for Celery entry/retry guards) with fail-open Redis degradation. `RepoConfigLoader` enforces Trust-on-Default-Branch ref resolution (`<= 64 KiB`, `yaml.safe_load`, `config_hash` SHA-256) and feeds `execute_pr_review_lifecycle()` inside `CodeSightTask` (`celery_app.py`), guaranteeing a mandatory pre-publish supersede check before any GitHub review can be posted.

**Tech Stack:** Python `3.12` (`uv`), Celery `5.4+`, `redis` (`redis.asyncio` + sync `redis` on port `6381`), Pydantic v2 (`strict` field validation), `PyYAML>=6.0.2` + `types-PyYAML`, `httpx`, `structlog`, Docker Compose (`tmpfs /tmp:size=128m`), `ruff`, `mypy --strict`, `pytest` + `pytest-cov`.

**Spec:** `docs/specs/2026-11-17-ca-7-day3-celery-supersede-config-design.md`

## Global Constraints

- Python runtime pinned to `3.12` (`.python-version = 3.12`), managed strictly via `uv` (`~/.local/bin/uv.exe` on Windows host; never run global `pip install`).
- Active implementation branch MUST be `feat/ca-7-day3-celery-supersede-config`; zero `.env`, `*.pem`, or `*.key` files tracked or untracked in `git status --porcelain`; zero production `assert` statements in `src/codesight/`.
- Never stop, restart, or bind ports reserved by `flintflow-*` (`3001`, `6379`, `8080`) or `documind-*` (`3002`, `5432`, `6380`, `8081`, `9000`, `9001`); always scope Docker Compose commands with `-p codesight` (`POSTGRES_PORT=5433`, `REDIS_PORT=6381`, `QDRANT_HTTP_PORT=6333`, `QDRANT_GRPC_PORT=6334`, `API_PORT=8082`).
- Zero-Code Persistence (`NFR-3`): `ephemeral_workspace()` must wipe 100% of temporary files in `finally`; `repo_config.py` must NEVER log raw `.codesight.yaml` file contents on parse/validation errors; never log GitHub tokens or secrets.
- Backward-compatibility & Quality Gates: preserve all 33 existing `CA-5`/`CA-6` tests (`redis_sync` monkeypatch hook in `celery_app.py`, `< 100ms` webhook gateway warm latency), and pass `uv run ruff check .` (`0` errors), `uv run mypy src` (`0` issues under `strict = true`), `uv run pytest --cov=src --cov-fail-under=80` (`>= 80%` branch coverage), and `python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket CA-7` before Jira transition `41` (`Done`).

## Review Focus

- **Concurrent `SHA_3` arriving mid-failure of `SHA_2` broker publish (`CAS Rollback Hazard`):** When `SHA_2` claims `pr_active`, fails to publish to Celery, and `SHA_3` claims `pr_active` before `SHA_2`'s rollback runs, `rollback_active_sha` MUST leave `pr_active == SHA_3` untouched (`Task 2`).
- **Mid-flight supersede between review analysis and GitHub publish (`Stale Comment Race`):** When `SHA_1` passes the entry check and executes `review_fn`, during which `SHA_2` updates `pr_active`, the mandatory `stage="pre_publish"` gate MUST abort execution with `status == "superseded"` and invoke `publish_fn` `0` times (`Task 4`).
- **Read-only files inside `ephemeral_workspace()` + inner `RuntimeError` (`Windows PermissionError & Exception Masking`):** A read-only (`stat.S_IREAD` / `0o444`) file inside nested workspace directories when the caller raises `RuntimeError("boom")` MUST be removed via `onexc` chmod recovery while propagating the exact original `RuntimeError("boom")` (`Task 1`).
- **Python `bool`-is-`int` footgun (`min_debt_score: true`), `> 64 KiB` files, and malformed YAML leakage (`NFR-3`):** `min_debt_score: true`, payloads exceeding `65_536` bytes, or broken YAML containing sensitive strings must emit `repo_config_invalid_fallback` (`WARNING`) containing `0` characters of raw YAML text and return atomic `RepoConfig()` defaults (`Task 3`).
- **GitHub `HTTP 403` secondary rate limit (`Retry-After` / `X-RateLimit-Remaining: 0`) vs permanent `403` permission error:** `403` carrying rate-limit headers must raise `RateLimitTaskError(retry_after_seconds=...)` for Celery retry, whereas plain `403` without rate-limit headers must raise non-retryable `GitHubAuthError(status_code=403)` (`Task 3` & `Task 4`).

---

## File Structure

- `pyproject.toml`, `uv.lock`: Add `pyyaml>=6.0.2` to `[project].dependencies` and `types-pyyaml>=6.0.12` to `[dependency-groups].dev`.
- `src/codesight/core/errors.py`: `RetryableTaskError`, `RateLimitTaskError`, and `parse_retry_after_header()` (`Retry-After` delta-seconds / RFC 7231 HTTP-date + `X-RateLimit-Reset` clamped to `[1.0, 60.0]`).
- `src/codesight/core/ephemeral.py`, `src/codesight/core/__init__.py`: `ephemeral_workspace()`, `async_ephemeral_workspace()`, and `resolve_safe_subpath()` (`0o700` POSIX permissions, `shutil.rmtree(..., onexc=...)` read-only bit recovery, traversal/symlink rejection).
- `src/codesight/worker/supersede_lock.py`, `src/codesight/worker/__init__.py`: `PR_SUPERSEDE_TTL_SECONDS`, `PR_SYNCHRONIZE_DEBOUNCE_SECONDS`, `pr_active_sha_key()`, `pr_active_task_key()`, `PRSupersededError`, `SupersedeClaim`, `AsyncSupersedeLock` (1-RTT Lua CAS + fallback for non-`eval` test doubles), `SyncSupersedeLock`, and `is_pr_event_superseded()`.
- `src/codesight/api/routes/webhooks.py`: Delegate `CeleryWebhookDispatcher.dispatch_pull_request` claim, CAS rollback, and CAS task recording to `AsyncSupersedeLock`.
- `src/codesight/config_reader/repo_config.py`, `src/codesight/config_reader/__init__.py`, `.codesight.example.yaml`: `RepoConfig` (`min_debt_score`, `review_action`, `ignore_paths`, `custom_rules`, `config_hash`), `parse_repo_config_yaml()`, `RepoConfigLoader` (Trust-on-Default-Branch, `.codesight.yaml`/`.codesight.yml`, `64 KiB` cap, error classification matrix), and documented `.codesight.example.yaml`.
- `src/codesight/worker/celery_app.py`: `acks_late=True` + worker reliability config, `classify_retryable_exception()`, `compute_retry_countdown()`, `CodeSightTask(celery.Task)` (supersede-before-retry + `Retry-After`/jitter backoff), `PRReviewLifecycleContext`, `execute_pr_review_lifecycle()`, and updated `handle_pr_event()`.
- `tests/unit/test_ephemeral_workspace.py`, `tests/unit/test_supersede_lock.py`, `tests/unit/test_repo_config.py`, `tests/unit/test_worker_and_cli.py`: Unit and live-Redis (`6381`) test suites covering all ACs and edge cases.

---

### Task 1: Domain Retry Errors & Self-Destructing `ephemeral_workspace()` (`NFR-3`)

**Files:**
- Create: `src/codesight/core/errors.py`, `src/codesight/core/ephemeral.py`
- Modify: `src/codesight/core/__init__.py`
- Test: `tests/unit/test_ephemeral_workspace.py`

**Interfaces:**
- Consumes: Python `3.12` stdlib (`tempfile`, `shutil`, `stat`, `os`, `pathlib`, `contextlib`, `email.utils`)
- Produces:
  - `MAX_RETRY_AFTER_SECONDS: Final[float] = 60.0`, `MIN_RETRY_AFTER_SECONDS: Final[float] = 1.0`
  - `class RetryableTaskError(RuntimeError)`
  - `class RateLimitTaskError(RetryableTaskError)` with `retry_after_seconds: float | None = None`
  - `parse_retry_after_header(headers: Mapping[str, str], *, now: float | None = None) -> float | None`
  - `@contextmanager def ephemeral_workspace(*, base_dir: Path | str | None = None, prefix: str = "codesight-ws-") -> Iterator[Path]`
  - `@asynccontextmanager def async_ephemeral_workspace(*, base_dir: Path | str | None = None, prefix: str = "codesight-ws-") -> AsyncIterator[Path]`
  - `resolve_safe_subpath(workspace_root: Path, relative_path: str | Path) -> Path`

- [ ] **Step 1: Write failing unit tests in `tests/unit/test_ephemeral_workspace.py`**

```python
import os
import stat
from pathlib import Path
import pytest
from codesight.core.ephemeral import (
    async_ephemeral_workspace,
    ephemeral_workspace,
    resolve_safe_subpath,
)
from codesight.core.errors import (
    RateLimitTaskError,
    RetryableTaskError,
    parse_retry_after_header,
)


def test_ephemeral_workspace_wipes_readonly_files_even_on_runtime_error(tmp_path: Path) -> None:
    captured_dir: Path | None = None
    with pytest.raises(RuntimeError, match="simulated pipeline crash"):
        with ephemeral_workspace(base_dir=tmp_path) as ws:
            captured_dir = ws
            nested = resolve_safe_subpath(ws, "pkg/module.py")
            nested.parent.mkdir(parents=True, exist_ok=True)
            nested.write_text("secret = 1\n", encoding="utf-8")
            os.chmod(nested, stat.S_IREAD)
            raise RuntimeError("simulated pipeline crash")

    assert captured_dir is not None
    assert not captured_dir.exists()


@pytest.mark.asyncio
async def test_async_ephemeral_workspace_and_path_traversal_guards(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("host-file", encoding="utf-8")

    async with async_ephemeral_workspace(base_dir=tmp_path) as ws:
        assert ws.exists() and ws.parent == tmp_path
        valid = resolve_safe_subpath(ws, "a/b/c.ts")
        assert valid.is_relative_to(ws.resolve())
        for bad in ("", "../escape.py", "/etc/passwd", "a/../../escape.py", "bad\x00name.py"):
            with pytest.raises(ValueError):
                resolve_safe_subpath(ws, bad)
        link_path = ws / "escape_link"
        try:
            link_path.symlink_to(outside)
        except OSError:
            pass
        else:
            with pytest.raises(ValueError):
                resolve_safe_subpath(ws, "escape_link")

    assert not ws.exists()


def test_parse_retry_after_clamps_delta_http_date_and_ratelimit_reset() -> None:
    assert parse_retry_after_header({"Retry-After": "12"}) == 12.0
    assert parse_retry_after_header({"retry-after": "999"}) == 60.0
    assert parse_retry_after_header({"Retry-After": "0"}) == 1.0
    assert parse_retry_after_header({"X-RateLimit-Reset": "1700000025"}, now=1700000000.0) == 25.0
    assert (
        parse_retry_after_header(
            {"Retry-After": "Tue, 14 Nov 2023 22:13:35 GMT"},
            now=1700000000.0,
        )
        == 15.0
    )
    assert parse_retry_after_header({}) is None
    err = RateLimitTaskError("429 hit", retry_after_seconds=8.5)
    assert isinstance(err, RetryableTaskError) and err.retry_after_seconds == 8.5
```

- [ ] **Step 2: Run `pytest` to verify failure**

Run: `& "$HOME\.local\bin\uv.exe" run pytest tests/unit/test_ephemeral_workspace.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codesight.core.ephemeral'`

- [ ] **Step 3: Implement `src/codesight/core/errors.py`, `src/codesight/core/ephemeral.py`, and update `src/codesight/core/__init__.py`**

- In `errors.py`: normalize header lookup case-insensitively; parse `Retry-After` (float/int delta-seconds first, then `email.utils.parsedate_to_datetime` vs `now`), falling back to `X-RateLimit-Reset` epoch delta when `X-RateLimit-Remaining == "0"` or `X-RateLimit-Reset` is present; clamp positive/zero delays to `[1.0, 60.0]`.
- In `ephemeral.py`: create temp dir via `tempfile.mkdtemp(prefix=prefix, dir=str(base_dir) if base_dir else None)`, set `os.chmod(ws_path, 0o700)` when `os.name != "nt"`, and in `finally` run `shutil.rmtree(ws_path, onexc=_on_rmtree_error)` where `_on_rmtree_error(func, target_path, exc)` sets `os.chmod(target_path, stat.S_IWRITE | stat.S_IREAD)` and retries `func(target_path)`, logging `"ephemeral_workspace_cleanup_failed"` (`ERROR`) if retry raises `OSError` without suppressing inner caller exceptions. `async_ephemeral_workspace` offloads `shutil.rmtree` via `asyncio.to_thread` in `finally`.

- [ ] **Step 4: Run tests, `mypy`, and `ruff`**

Run: `& "$HOME\.local\bin\uv.exe" run pytest tests/unit/test_ephemeral_workspace.py -v; & "$HOME\.local\bin\uv.exe" run mypy src; & "$HOME\.local\bin\uv.exe" run ruff check .`
Expected: PASS (`3 passed`, `0` mypy/ruff issues).

- [ ] **Step 5: Commit `errors.py` & `ephemeral.py`**

```powershell
git add src/codesight/core tests/unit/test_ephemeral_workspace.py
git commit -m "feat(ca-7): add ephemeral_workspace NFR-3 guard and retryable error primitives"
```

---

### Task 2: Unified `Cancel-on-Supersede` Lua CAS Lock (`supersede_lock.py` + `webhooks.py`)

**Files:**
- Create: `src/codesight/worker/supersede_lock.py`
- Modify: `src/codesight/api/routes/webhooks.py`, `src/codesight/worker/__init__.py`
- Test: `tests/unit/test_supersede_lock.py`

**Interfaces:**
- Consumes: `redis.asyncio.Redis`, `redis.Redis`, `codesight.core.config.get_settings`
- Produces:
  - `PR_SUPERSEDE_TTL_SECONDS: Final[int] = 86_400`, `PR_SYNCHRONIZE_DEBOUNCE_SECONDS: Final[int] = 5`
  - `pr_active_sha_key(repo_id: str, pr_number: int) -> str`, `pr_active_task_key(repo_id: str, pr_number: int) -> str`
  - `class PRSupersededError(Exception)` (`repo_id`, `pr_number`, `head_sha`, `active_head_sha`, `stage`)
  - `@dataclass(frozen=True) class SupersedeClaim`: `prev_sha: str | None`, `prev_task_id: str | None`, `claimed: bool`
  - `class AsyncSupersedeLock`:
    - `async def claim_active_sha(self, repo_id: str, pr_number: int, head_sha: str, *, ttl_seconds: int = PR_SUPERSEDE_TTL_SECONDS) -> SupersedeClaim`
    - `async def rollback_active_sha(self, repo_id: str, pr_number: int, *, attempted_sha: str, prev_sha: str | None, ttl_seconds: int = PR_SUPERSEDE_TTL_SECONDS) -> None`
    - `async def record_active_task(self, repo_id: str, pr_number: int, *, head_sha: str, task_id: str, ttl_seconds: int = PR_SUPERSEDE_TTL_SECONDS) -> None`
    - `async def check_superseded(self, repo_id: str, pr_number: int, head_sha: str) -> tuple[bool, str | None]`
    - `async def ensure_active(self, repo_id: str, pr_number: int, head_sha: str, *, stage: str = "pre_publish") -> None`
  - `class SyncSupersedeLock`:
    - `def check_superseded(self, repo_id: str, pr_number: int, head_sha: str) -> tuple[bool, str | None]`
    - `def ensure_active(self, repo_id: str, pr_number: int, head_sha: str, *, stage: str = "entry") -> None`
  - `is_pr_event_superseded(payload: Mapping[str, Any], *, redis_client: redis_sync.Redis | None = None, redis_from_url: Callable[..., redis_sync.Redis] | None = None) -> bool`

- [ ] **Step 1: Write failing unit & live-Redis (`6381`) CAS tests in `tests/unit/test_supersede_lock.py`**

```python
import uuid
import pytest
import redis as redis_sync
import redis.asyncio as redis_async
import redis.exceptions
from codesight.core.config import Settings
from codesight.worker.supersede_lock import (
    AsyncSupersedeLock,
    PRSupersededError,
    SyncSupersedeLock,
    is_pr_event_superseded,
    pr_active_sha_key,
    pr_active_task_key,
)


@pytest.mark.asyncio
async def test_live_redis_lua_cas_claim_rollback_and_superseded_detection() -> None:
    settings = Settings()
    async_client = redis_async.Redis.from_url(settings.redis_url, decode_responses=True)
    sync_client = redis_sync.Redis.from_url(settings.redis_url, decode_responses=True)
    repo_id = f"test-repo-{uuid.uuid4().hex[:8]}"
    pr_num = 17
    lock = AsyncSupersedeLock(async_client)
    sync_lock = SyncSupersedeLock(sync_client)

    try:
        # 1. SHA_1 claims clean slot -> SHA_2 supersedes SHA_1
        c1 = await lock.claim_active_sha(repo_id, pr_num, "sha-1")
        await lock.record_active_task(repo_id, pr_num, head_sha="sha-1", task_id="task-1")
        assert c1.prev_sha is None and c1.prev_task_id is None and c1.claimed is True

        c2 = await lock.claim_active_sha(repo_id, pr_num, "sha-2")
        assert c2.prev_sha == "sha-1" and c2.prev_task_id == "task-1"

        # SHA_1 worker sees superseded via both sync & async guards
        assert sync_lock.check_superseded(repo_id, pr_num, "sha-1") == (True, "sha-2")
        with pytest.raises(PRSupersededError) as exc_info:
            await lock.ensure_active(repo_id, pr_num, "sha-1", stage="pre_publish")
        assert exc_info.value.active_head_sha == "sha-2"
        assert exc_info.value.stage == "pre_publish"

        # 2. CAS Rollback Hazard: SHA_3 arrives BEFORE SHA_2's failed-publish rollback runs
        await lock.claim_active_sha(repo_id, pr_num, "sha-3")
        await lock.rollback_active_sha(repo_id, pr_num, attempted_sha="sha-2", prev_sha="sha-1")
        await lock.record_active_task(repo_id, pr_num, head_sha="sha-2", task_id="stale-task-2")
        # SHA_3 MUST remain active and task_key MUST NOT be overwritten by stale SHA_2
        assert await async_client.get(pr_active_sha_key(repo_id, pr_num)) == "sha-3"
        assert await async_client.get(pr_active_task_key(repo_id, pr_num)) == "task-1"
    finally:
        await async_client.delete(
            pr_active_sha_key(repo_id, pr_num),
            pr_active_task_key(repo_id, pr_num),
        )
        await async_client.aclose()
        sync_client.close()


@pytest.mark.asyncio
async def test_supersede_lock_fail_open_on_redis_errors_and_invalid_payloads() -> None:
    class _BrokenAsyncRedis:
        async def get(self, key: str) -> str | None:
            raise redis.exceptions.TimeoutError("redis timeout")

    broken_lock = AsyncSupersedeLock(_BrokenAsyncRedis())  # type: ignore[arg-type]
    assert await broken_lock.check_superseded("r1", 1, "sha-1") == (False, None)
    await broken_lock.ensure_active("r1", 1, "sha-1", stage="pre_publish")
    assert is_pr_event_superseded({"repo_id": "", "pr_number": True, "head_sha": "x"}) is False
```

- [ ] **Step 2: Run `pytest` to verify failure**

Run: `& "$HOME\.local\bin\uv.exe" run pytest tests/unit/test_supersede_lock.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codesight.worker.supersede_lock'`

- [ ] **Step 3: Implement `src/codesight/worker/supersede_lock.py` and refactor `src/codesight/api/routes/webhooks.py`**

- Implement the 3 Lua scripts in `supersede_lock.py`:
  - `_CLAIM_LUA`: `local p_sha = redis.call('GET', KEYS[1]); local p_task = redis.call('GET', KEYS[2]); redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2])); return {p_sha or '', p_task or ''}`
  - `_ROLLBACK_LUA`: `if redis.call('GET', KEYS[1]) == ARGV[1] then if ARGV[2] ~= '' then redis.call('SET', KEYS[1], ARGV[2], 'EX', tonumber(ARGV[3])) else redis.call('DEL', KEYS[1]) end return 1 end return 0`
  - `_RECORD_TASK_LUA`: `if redis.call('GET', KEYS[1]) == ARGV[1] then redis.call('SET', KEYS[2], ARGV[2], 'EX', tonumber(ARGV[3])) return 1 end return 0`
  - When `getattr(client, "eval", None)` is `None` (lightweight unit-test doubles in `test_webhook_hmac.py`), fall back cleanly to `get`/`set`/`delete`.
- Update `CeleryWebhookDispatcher.dispatch_pull_request` in `src/codesight/api/routes/webhooks.py` to use `AsyncSupersedeLock(self._redis_client)` and import `PR_SUPERSEDE_TTL_SECONDS` / `PR_SYNCHRONIZE_DEBOUNCE_SECONDS` from `codesight.worker.supersede_lock`.

- [ ] **Step 4: Run `test_supersede_lock.py`, `test_webhook_hmac.py` (including `<100ms` latency benchmark), `mypy`, and `ruff`**

Run: `& "$HOME\.local\bin\uv.exe" run pytest tests/unit/test_supersede_lock.py tests/unit/test_webhook_hmac.py -v; & "$HOME\.local\bin\uv.exe" run mypy src; & "$HOME\.local\bin\uv.exe" run ruff check .`
Expected: PASS (`100%` green including `< 100ms` warm webhook SLA, `0` mypy/ruff issues).

- [ ] **Step 5: Commit `supersede_lock.py` & `webhooks.py` refactor**

```powershell
git add src/codesight/worker/supersede_lock.py src/codesight/worker/__init__.py src/codesight/api/routes/webhooks.py tests/unit/test_supersede_lock.py
git commit -m "feat(ca-7): add Lua CAS Cancel-on-Supersede lock and unify webhook dispatcher"
```

---

### Task 3: `.codesight.yaml` Reader (`RepoConfig`, `config_hash`, Default-Branch Loader & `.codesight.example.yaml`)

**Files:**
- Modify: `pyproject.toml`, `uv.lock`, `src/codesight/config_reader/__init__.py`
- Create: `src/codesight/config_reader/repo_config.py`, `.codesight.example.yaml`
- Test: `tests/unit/test_repo_config.py`

**Interfaces:**
- Consumes: `GitHubAuthProvider`, `validate_repo_full_name`, `RetryableTaskError`, `RateLimitTaskError`, `parse_retry_after_header`, `PyYAML` (`yaml.safe_load`), `httpx.AsyncClient`
- Produces:
  - `MAX_REPO_CONFIG_BYTES: Final[int] = 65_536`, `MAX_IGNORE_PATHS: Final[int] = 100`, `MAX_CUSTOM_RULES_CHARS: Final[int] = 4_000`
  - `CONFIG_FILENAMES: Final[tuple[str, str]] = (".codesight.yaml", ".codesight.yml")`
  - `ReviewAction = Literal["COMMENT", "REQUEST_CHANGES"]`
  - `class RepoConfig(BaseModel)` (`frozen=True`, `extra="ignore"`):
    - `min_debt_score: int = 60` (strict `int`, `0..100`, rejects `bool`)
    - `review_action: ReviewAction = "COMMENT"`
    - `ignore_paths: tuple[str, ...] = ()`
    - `custom_rules: str = ""`
    - `@property def config_hash(self) -> str` (16-char lowercase SHA-256 hex digest)
  - `parse_repo_config_yaml(raw_yaml: str | bytes, *, source: str = ".codesight.yaml") -> RepoConfig`
  - `class RepoConfigLoader`:
    - `async def load(self, repo_full_name: str, *, default_branch: str = "main", ref: str | None = None, installation_id: int | None = None) -> RepoConfig`

- [ ] **Step 1: Add `pyyaml` and `types-pyyaml` via `uv`**

Run: `& "$HOME\.local\bin\uv.exe" add "pyyaml>=6.0.2"; & "$HOME\.local\bin\uv.exe" add --dev "types-pyyaml>=6.0.12"`
Expected: `pyproject.toml` and `uv.lock` updated cleanly.

- [ ] **Step 2: Write failing unit tests in `tests/unit/test_repo_config.py`**

```python
import base64
import io
import json
from pathlib import Path
import httpx
import pytest
from pydantic import SecretStr
from codesight.config_reader.repo_config import (
    MAX_REPO_CONFIG_BYTES,
    RepoConfig,
    RepoConfigLoader,
    parse_repo_config_yaml,
)
from codesight.core.config import Settings
from codesight.core.errors import RateLimitTaskError, RetryableTaskError
from codesight.core.logging import configure_logging
from codesight.vcs.github_auth import GitHubAuthError, GitHubAuthProvider


def test_parse_valid_empty_unknown_keys_and_deterministic_config_hash(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", service_name="test", stream=stream)

    default_cfg = parse_repo_config_yaml("# empty config\n")
    assert default_cfg == RepoConfig()
    assert len(default_cfg.config_hash) == 16

    cfg = parse_repo_config_yaml(
        """
        min_debt_score: 75
        review_action: request_changes
        ignore_paths:
          - 'generated\\**'
          - 'docs/*.md'
        custom_rules: 'Enforce repository pattern.'
        unknown_future_key: 123
        """
    )
    assert cfg.min_debt_score == 75
    assert cfg.review_action == "REQUEST_CHANGES"
    assert cfg.ignore_paths == ("generated/**", "docs/*.md")
    assert cfg.custom_rules == "Enforce repository pattern."
    assert cfg.config_hash != default_cfg.config_hash
    logs = [json.loads(line) for line in stream.getvalue().splitlines()]
    assert any(entry["event"] == "repo_config_unknown_keys_ignored" for entry in logs)


@pytest.mark.parametrize(
    "bad_yaml",
    [
        "min_debt_score: [unclosed",
        "- list_root_not_dict",
        "min_debt_score: true",
        "min_debt_score: 105",
        "review_action: APPROVE",
        f"custom_rules: '{'x' * 4001}'",
        b"x" * (MAX_REPO_CONFIG_BYTES + 1),
    ],
)
def test_invalid_yaml_or_schema_falls_back_atomically_without_logging_raw_content(
    bad_yaml: str | bytes,
) -> None:
    stream = io.StringIO()
    configure_logging(level="WARNING", service_name="test", stream=stream)
    sentinel = "SUPER_SECRET_INLINE_SNIPPET_DO_NOT_LOG"
    payload = (
        f"min_debt_score: [broken # {sentinel}"
        if isinstance(bad_yaml, str) and "[unclosed" in bad_yaml
        else bad_yaml
    )
    cfg = parse_repo_config_yaml(payload)
    assert cfg == RepoConfig()
    raw_logs = stream.getvalue()
    assert "repo_config_invalid_fallback" in raw_logs
    assert sentinel not in raw_logs


@pytest.mark.asyncio
async def test_loader_trusts_default_branch_falls_back_yml_and_propagates_429_5xx() -> None:
    requested_urls: list[str] = []
    yml_b64 = base64.b64encode(b"min_debt_score: 82\nreview_action: REQUEST_CHANGES\n").decode()

    def _handler(request: httpx.Request) -> httpx.Response:
        requested_urls.append(str(request.url))
        path = request.url.path
        if path.endswith("/org/yml-repo/contents/.codesight.yaml"):
            return httpx.Response(404, json={"message": "Not Found"})
        if path.endswith("/org/yml-repo/contents/.codesight.yml"):
            return httpx.Response(
                200,
                json={"type": "file", "size": 45, "encoding": "base64", "content": yml_b64},
            )
        if path.endswith("/org/missing/contents/.codesight.yaml") or path.endswith(
            "/org/missing/contents/.codesight.yml"
        ):
            return httpx.Response(404, json={"message": "Not Found"})
        if path.endswith("/org/ratelimited/contents/.codesight.yaml"):
            return httpx.Response(
                403, headers={"Retry-After": "14"}, json={"message": "rate limit"}
            )
        if path.endswith("/org/forbidden/contents/.codesight.yaml"):
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        return httpx.Response(502, json={"message": "Bad Gateway"})

    settings = Settings(_env_file=None, github_token=SecretStr("ghp_test_token"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as http:
        auth = GitHubAuthProvider(settings, http_client=http)
        loader = RepoConfigLoader(auth, http_client=http)

        # Default branch ("develop") used when ref is omitted; .codesight.yml fallback works
        loaded = await loader.load("org/yml-repo", default_branch="develop")
        assert loaded.min_debt_score == 82 and loaded.review_action == "REQUEST_CHANGES"
        assert all("ref=develop" in u for u in requested_urls[:2])

        # Missing both .yaml and .yml -> RepoConfig() defaults
        assert await loader.load("org/missing", default_branch="main") == RepoConfig()

        # Secondary rate limit 403 + Retry-After -> RateLimitTaskError
        with pytest.raises(RateLimitTaskError) as rl_exc:
            await loader.load("org/ratelimited")
        assert rl_exc.value.retry_after_seconds == 14.0

        # Non-rate-limit 403 -> permanent GitHubAuthError
        with pytest.raises(GitHubAuthError) as auth_exc:
            await loader.load("org/forbidden")
        assert auth_exc.value.status_code == 403

        # 502 -> RetryableTaskError
        with pytest.raises(RetryableTaskError):
            await loader.load("org/server-down")


def test_example_codesight_yaml_parses_cleanly() -> None:
    example_bytes = Path(".codesight.example.yaml").read_bytes()
    parsed = parse_repo_config_yaml(example_bytes, source=".codesight.example.yaml")
    assert 0 <= parsed.min_debt_score <= 100
```

- [ ] **Step 3: Run `pytest` to verify failure**

Run: `& "$HOME\.local\bin\uv.exe" run pytest tests/unit/test_repo_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'codesight.config_reader.repo_config'`

- [ ] **Step 4: Implement `src/codesight/config_reader/repo_config.py`, `src/codesight/config_reader/__init__.py`, and `.codesight.example.yaml`**

- Implement `RepoConfig` with `@field_validator("min_debt_score", mode="before")` rejecting `isinstance(v, bool)`, `@field_validator("review_action", mode="before")` trimming & uppercasing strings, `@field_validator("ignore_paths", mode="before")` normalizing `\` -> `/`, rejecting non-strings, empty strings, `len > 256`, `\x00`, or `len > 100` items, and `@field_validator("custom_rules", mode="before")` stripping strings and enforcing `<= 4000` chars.
- Implement `RepoConfigLoader.load(...)` following Section 5.2 of the spec (URL-quoting `effective_ref`, checking `size <= 65_536` before Base64 decode, never logging raw file bytes).
- Create `.codesight.example.yaml` documenting `min_debt_score: 60`, `review_action: COMMENT`, sample `ignore_paths`, and sample `custom_rules`.

- [ ] **Step 5: Run tests, `mypy`, and `ruff`**

Run: `& "$HOME\.local\bin\uv.exe" run pytest tests/unit/test_repo_config.py -v; & "$HOME\.local\bin\uv.exe" run mypy src; & "$HOME\.local\bin\uv.exe" run ruff check .`
Expected: PASS (`100%` green, `0` mypy/ruff issues).

- [ ] **Step 6: Commit `repo_config.py` & `.codesight.example.yaml`**

```powershell
git add pyproject.toml uv.lock .codesight.example.yaml src/codesight/config_reader tests/unit/test_repo_config.py
git commit -m "feat(ca-7): add resilient .codesight.yaml reader, config_hash, and example config"
```

---

### Task 4: Celery `acks_late`, `CodeSightTask` Backoff + Jitter & Lifecycle Spine (`celery_app.py`)

**Files:**
- Modify: `src/codesight/worker/celery_app.py`, `src/codesight/worker/__init__.py`
- Modify: `tests/unit/test_worker_and_cli.py`, `tests/unit/test_supersede_lock.py`

**Interfaces:**
- Consumes: `RetryableTaskError`, `RateLimitTaskError`, `parse_retry_after_header`, `ephemeral_workspace`, `async_ephemeral_workspace`, `AsyncSupersedeLock`, `PRSupersededError`, `is_pr_event_superseded`, `RepoConfig`, `RepoConfigLoader`, `GitHubAuthProvider`, `GitHubAuthError`, `GitHubAuthConfigError`
- Produces:
  - `MAX_TASK_RETRIES: Final[int] = 3`, `RETRY_BASE_SECONDS: Final[float] = 2.0`, `RETRY_CAP_SECONDS: Final[float] = 30.0`
  - `classify_retryable_exception(exc: BaseException) -> tuple[bool, float | None]`
  - `compute_retry_countdown(retries: int, *, retry_after_seconds: float | None = None, rng: random.Random | None = None) -> float`
  - `class CodeSightTask(celery.Task)` (registered as default task base on `celery_app`)
  - `@dataclass(frozen=True) class PRReviewLifecycleContext`: `payload: Mapping[str, Any]`, `trace_id: str`, `workspace_dir: Path`, `repo_config: RepoConfig`
  - `async def execute_pr_review_lifecycle(payload: Mapping[str, Any], *, settings: Settings | None = None, supersede_lock: AsyncSupersedeLock | None = None, config_loader: RepoConfigLoader | None = None, review_fn: Callable[[PRReviewLifecycleContext], Awaitable[None]] | None = None, publish_fn: Callable[[PRReviewLifecycleContext], Awaitable[None]] | None = None) -> dict[str, str]`

- [ ] **Step 1: Add failing tests for `acks_late`, `CodeSightTask` retry/jitter/supersede-abort, and pre-publish lifecycle gate in `tests/unit/test_worker_and_cli.py` and `tests/unit/test_supersede_lock.py`**

In `tests/unit/test_worker_and_cli.py`, assert:
1. `celery_app.conf.task_acks_late is True`, `celery_app.conf.task_reject_on_worker_lost is True`, `celery_app.conf.worker_prefetch_multiplier == 1`, `celery_app.conf.task_soft_time_limit == 60`, `celery_app.conf.task_time_limit == 90`.
2. `classify_retryable_exception` marks `RateLimitTaskError(retry_after_seconds=9.0)`, `RetryableTaskError`, `httpx.ReadTimeout`, `redis.exceptions.ConnectionError`, and `GitHubAuthError(..., status_code=503)` as retryable, while rejecting `PRSupersededError`, `GitHubAuthConfigError`, `GitHubAuthError(..., status_code=401)`, `ValueError`, and HTTP `422`.
3. `compute_retry_countdown` produces jittered delays within `[1.0, 2.0]`, `[2.0, 4.0]`, `[4.0, 8.0]` for `retries = 0, 1, 2`, and `[12.0, 13.0]` when `retry_after_seconds=12.0`.
4. Executing a `CodeSightTask` inside a simulated Celery worker delivery (`request.called_directly = False`, `request.id = "task-123"`) calls `self.retry(exc=..., countdown=...)` on `RateLimitTaskError`, stops retrying when `request.retries >= 3`, and **skips retry returning `{"status": "superseded", ...}`** if `is_pr_event_superseded(payload)` returns `True`.

In `tests/unit/test_supersede_lock.py`, assert:
5. `execute_pr_review_lifecycle` runs `review_fn` inside an existing `workspace_dir` (`NFR-3`), and if `review_fn` simulates a concurrent `SHA_2` push into `AsyncSupersedeLock`, the mandatory `stage="pre_publish"` gate aborts before `publish_fn` (`publish_called == 0`), deletes `workspace_dir`, and returns `{"status": "superseded", ...}`.

- [ ] **Step 2: Run `pytest` to verify failure**

Run: `& "$HOME\.local\bin\uv.exe" run pytest tests/unit/test_worker_and_cli.py tests/unit/test_supersede_lock.py -v`
Expected: FAIL (`task_acks_late` not configured / `CodeSightTask` & `execute_pr_review_lifecycle` undefined).

- [ ] **Step 3: Implement `CodeSightTask`, retry helpers, and `execute_pr_review_lifecycle` in `src/codesight/worker/celery_app.py`**

- Update `create_celery_app` with `task_cls=CodeSightTask` and all Section 3.1 `conf.update(...)` keys.
- Preserve `import redis as redis_sync` in `celery_app.py` and keep `is_pr_event_superseded(payload, *, redis_client=None)` delegating `redis_from_url=redis_sync.Redis.from_url` into `supersede_lock.is_pr_event_superseded` so `test_webhook_hmac.py` monkeypatches continue working identically.
- Implement `CodeSightTask.__call__` (wrapping `super().__call__`) to intercept exceptions during active worker deliveries (`not self.request.called_directly` and `bool(self.request.id)`), checking `classify_retryable_exception`, `retries < MAX_TASK_RETRIES`, and `is_pr_event_superseded` before invoking `self.retry(exc=exc, countdown=countdown)`.
- Implement `execute_pr_review_lifecycle` and wire `handle_pr_event` to run the synchronous entry supersede check followed by `asyncio.run(execute_pr_review_lifecycle(payload))` (or thread executor if an event loop is already active in an async test).

- [ ] **Step 4: Run full test suite, `mypy`, and `ruff`**

Run: `& "$HOME\.local\bin\uv.exe" run pytest --cov=src --cov-fail-under=80 -v; & "$HOME\.local\bin\uv.exe" run mypy src tests; & "$HOME\.local\bin\uv.exe" run ruff check .`
Expected: PASS (`>= 85%` coverage, all unit + integration tests green, `0` mypy/ruff issues).

- [ ] **Step 5: Commit `celery_app.py` reliability & lifecycle upgrades**

```powershell
git add src/codesight/worker tests/unit/test_worker_and_cli.py tests/unit/test_supersede_lock.py
git commit -m "feat(ca-7): add Celery acks_late, jittered Retry-After backoff, and PR lifecycle spine"
```

---

### Task 5: Docker Worker Verification, Pre-Close Audit & Jira Evidence Gate (`CA-7`)

**Files:**
- Verify: `docker-compose.yml` (`codesight-api` & `codesight-worker` rebuild with `pyyaml` and `acks_late=True`)
- Audit: `.agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket CA-7`

**Interfaces:**
- Consumes: All `Task 1`–`Task 4` deliverables, Docker Compose project `codesight`
- Produces: Healthy 5-container stack (`codesight-migrate` exited `0`, `codesight-postgres`/`redis`/`qdrant`/`api`/`worker` healthy), passing `preclose_audit.py --ticket CA-7` output, and Jira `CA-7` transition `31` -> Evidence comment -> `41` (`Done`).

- [ ] **Step 1: Transition Jira `CA-7` to `Verify / Test` (`"31"`) and rebuild `codesight` containers cleanly**

Run: `docker compose -p codesight up -d --build --wait`
Expected: `codesight-migrate` exits `0`; `codesight-api` (`8082`) and `codesight-worker` reach `Healthy`; `celery inspect conf` or worker health ping succeeds; `flintflow-*` and `documind-*` containers remain untouched.

- [ ] **Step 2: Execute mechanical pre-close gate**

Run: `python .agents/skills/codesight-jira-workflow/scripts/preclose_audit.py --ticket CA-7`
Expected: `[GATE] ruff: PASS`, `[GATE] mypy: PASS`, `[GATE] pytest: PASS`, exiting with code `0`.

- [ ] **Step 3: Post structured Verification Evidence to Jira `CA-7` and transition to `Done` (`"41"`)**

Follow `codesight-jira-workflow`: post real command outputs covering `AC-1`, `AC-2`, `AC-3`, quality gates, and port/`NFR-3` coexistence via `jira_add_comment`, then transition `CA-7` to `"41"` (`Done`), push `feat/ca-7-day3-celery-supersede-config`, and open PR to `main`.
