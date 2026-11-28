"""Unit tests for Celery trace_id signal propagation and CLI output isolation."""

import io
import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from codesight import __version__
from codesight.cli.main import app as cli_app
from codesight.core.logging import bind_trace_id, clear_trace_context, get_trace_id
from codesight.worker.celery_app import (
    MAX_TASK_RETRIES,
    celery_app,
    classify_retryable_exception,
    compute_retry_countdown,
    create_celery_app,
    inject_trace_id_header,
    on_task_postrun,
    on_task_prerun,
    on_worker_process_init,
    ping,
)


def test_celery_signals_propagate_and_clean_trace_id() -> None:
    clear_trace_context()
    bind_trace_id("http-to-celery-777")
    headers: dict[str, object] = {}
    inject_trace_id_header(headers=headers)
    assert headers["trace_id"] == "http-to-celery-777"

    preset_headers: dict[str, object] = {"trace_id": "already-present"}
    inject_trace_id_header(headers=preset_headers)
    assert preset_headers["trace_id"] == "already-present"
    clear_trace_context()

    inject_trace_id_header(headers=None)
    on_worker_process_init()

    dummy_task = SimpleNamespace(
        request=SimpleNamespace(headers={"trace_id": "http-to-celery-777"}),
    )

    on_task_prerun(task_id="task-fallback-1", task=dummy_task)
    result = ping()
    assert result == {"status": "pong", "trace_id": "http-to-celery-777"}
    on_task_postrun()
    assert get_trace_id() is None

    on_task_prerun(task_id="task-fallback-99", task=None)
    assert get_trace_id() == "task-fallback-99"
    on_task_postrun()
    assert get_trace_id() is None
    assert celery_app.main == "codesight"
    assert celery_app.conf.worker_hijack_root_logger is False
    assert celery_app.conf.worker_redirect_stdouts is False


def test_cli_help_and_version_keep_stdout_free_of_json_logs(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    help_res = runner.invoke(cli_app, ["--help"])
    assert help_res.exit_code == 0
    assert "version" in help_res.stdout

    capsys.readouterr()
    ver_res = runner.invoke(cli_app, ["version"])
    captured_default = capsys.readouterr()
    assert ver_res.exit_code == 0
    assert __version__ in ver_res.stdout
    assert "5433" in ver_res.stdout and "8082" in ver_res.stdout
    assert "{" not in ver_res.stdout
    assert captured_default.err == ""
    assert getattr(ver_res, "stderr", "") == ""

    stderr_buf = io.StringIO()
    monkeypatch.setattr("sys.stderr", stderr_buf)
    verbose_res = runner.invoke(cli_app, ["--verbose", "version"])
    captured_verbose = capsys.readouterr()
    assert verbose_res.exit_code == 0
    assert "{" not in verbose_res.stdout
    stderr_out = getattr(verbose_res, "stderr", "") or stderr_buf.getvalue() or captured_verbose.err
    stderr_lines = [json.loads(line) for line in stderr_out.strip().splitlines()]
    assert any(
        entry["service"] == "codesight-cli" and entry["event"] == "cli_version_displayed"
        for entry in stderr_lines
    )


def test_celery_reliability_settings_retry_classification_and_jitter() -> None:
    import random

    import httpx
    import redis.exceptions

    from codesight.core.errors import RateLimitTaskError, RetryableTaskError
    from codesight.vcs.github_auth import GitHubAuthConfigError, GitHubAuthError
    from codesight.worker.supersede_lock import PRSupersededError

    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.worker_prefetch_multiplier == 1
    assert celery_app.conf.task_soft_time_limit == 60
    assert celery_app.conf.task_time_limit == 90
    assert MAX_TASK_RETRIES == 3

    req = httpx.Request("GET", "https://api.github.com/repos/o/r")
    assert classify_retryable_exception(RateLimitTaskError("429", retry_after_seconds=9.0)) == (
        True,
        9.0,
    )
    assert classify_retryable_exception(RetryableTaskError("503")) == (True, None)
    assert classify_retryable_exception(httpx.ReadTimeout("timeout", request=req)) == (True, None)
    assert classify_retryable_exception(redis.exceptions.ConnectionError("down")) == (True, None)
    assert classify_retryable_exception(GitHubAuthError("503", status_code=503)) == (True, None)

    resp_429 = httpx.Response(429, headers={"Retry-After": "11"}, request=req)
    assert classify_retryable_exception(
        httpx.HTTPStatusError("429", request=req, response=resp_429)
    ) == (True, 11.0)

    resp_403_rl = httpx.Response(403, headers={"Retry-After": "22"}, request=req)
    cause_403_rl = httpx.HTTPStatusError("403 rl", request=req, response=resp_403_rl)
    assert classify_retryable_exception(cause_403_rl) == (True, 22.0)

    gh_429_exc = GitHubAuthError("429 auth", status_code=429)
    gh_429_exc.__cause__ = httpx.HTTPStatusError("429", request=req, response=resp_429)
    assert classify_retryable_exception(gh_429_exc) == (True, 11.0)

    gh_403_rl_exc = GitHubAuthError("403 auth rl", status_code=403)
    gh_403_rl_exc.__cause__ = cause_403_rl
    assert classify_retryable_exception(gh_403_rl_exc) == (True, 22.0)

    resp_403_plain = httpx.Response(403, request=req)
    gh_403_plain = GitHubAuthError("403 forbidden", status_code=403)
    gh_403_plain.__cause__ = httpx.HTTPStatusError("403", request=req, response=resp_403_plain)
    assert classify_retryable_exception(gh_403_plain) == (False, None)

    resp_422 = httpx.Response(422, request=req)
    assert classify_retryable_exception(
        httpx.HTTPStatusError("422", request=req, response=resp_422)
    ) == (False, None)
    assert classify_retryable_exception(GitHubAuthConfigError("bad pem")) == (False, None)
    assert classify_retryable_exception(GitHubAuthError("401", status_code=401)) == (False, None)
    assert classify_retryable_exception(
        PRSupersededError(
            repo_id="r1",
            pr_number=1,
            head_sha="s1",
            active_head_sha="s2",
            stage="entry",
        )
    ) == (False, None)

    rng = random.Random(42)
    c0 = compute_retry_countdown(0, rng=rng)
    c1 = compute_retry_countdown(1, rng=rng)
    c2 = compute_retry_countdown(2, rng=rng)
    c_cap = compute_retry_countdown(10, rng=rng)
    c_ra = compute_retry_countdown(0, retry_after_seconds=12.0, rng=rng)
    assert 1.0 <= c0 <= 2.0
    assert 2.0 <= c1 <= 4.0
    assert 4.0 <= c2 <= 8.0
    assert 15.0 <= c_cap <= 30.0
    assert 12.0 <= c_ra <= 13.0


def test_codesight_task_retries_transient_errors_and_aborts_retry_when_superseded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import Any
    from unittest.mock import MagicMock

    from codesight.core.errors import RateLimitTaskError, RetryableTaskError

    local_app = create_celery_app()
    mode = "rate_limit"

    def _sample_pr_task_fn(payload: dict[str, Any]) -> dict[str, str]:
        if mode == "rate_limit":
            raise RateLimitTaskError("429 hit", retry_after_seconds=7.0)
        if mode == "transient":
            raise RetryableTaskError("502 gateway")
        raise ValueError("permanent invalid payload")

    sample_pr_task: Any = local_app.task(name="codesight.test.sample_pr_task")(_sample_pr_task_fn)

    # 1. Active worker delivery schedules retry with Retry-After + jitter
    monkeypatch.setattr(
        "codesight.worker.celery_app.is_pr_event_superseded", lambda *_, **__: False
    )
    retry_sentinel = RuntimeError("celery-retry-scheduled")
    retry_mock = MagicMock(side_effect=retry_sentinel)
    monkeypatch.setattr(sample_pr_task, "retry", retry_mock)

    sample_pr_task.push_request(id="worker-req-1", retries=0, called_directly=False)
    try:
        with pytest.raises(RuntimeError, match="celery-retry-scheduled"):
            sample_pr_task({"repo_id": "r1", "pr_number": 1, "head_sha": "s1"})
    finally:
        sample_pr_task.pop_request()
    _, kwargs = retry_mock.call_args
    assert 7.0 <= kwargs["countdown"] <= 8.0

    # 2. Exhausted retries (>= MAX_TASK_RETRIES) re-raises original RetryableTaskError
    mode = "transient"
    retry_mock.reset_mock()
    sample_pr_task.push_request(id="worker-req-2", retries=MAX_TASK_RETRIES, called_directly=False)
    try:
        with pytest.raises(RetryableTaskError, match="502 gateway"):
            sample_pr_task({"repo_id": "r1", "pr_number": 1, "head_sha": "s1"})
    finally:
        sample_pr_task.pop_request()
    retry_mock.assert_not_called()

    # 3. Permanent error fails immediately at retries=0 without scheduling retry
    mode = "permanent"
    sample_pr_task.push_request(id="worker-req-3", retries=0, called_directly=False)
    try:
        with pytest.raises(ValueError, match="permanent invalid payload"):
            sample_pr_task({"repo_id": "r1", "pr_number": 1, "head_sha": "s1"})
    finally:
        sample_pr_task.pop_request()
    retry_mock.assert_not_called()

    # 4. Superseded PR task aborts retry and returns status="superseded"
    mode = "rate_limit"
    monkeypatch.setattr("codesight.worker.celery_app.is_pr_event_superseded", lambda *_, **__: True)
    bind_trace_id("superseded-retry-trace")
    sample_pr_task.push_request(id="worker-req-4", retries=0, called_directly=False)
    try:
        res = sample_pr_task({"repo_id": "r1", "pr_number": 1, "head_sha": "s1"})
    finally:
        sample_pr_task.pop_request()
        clear_trace_context()
    assert res == {"status": "superseded", "trace_id": "superseded-retry-trace"}
    retry_mock.assert_not_called()

    # 5. Bound task preserves self.request (id, retries, called_directly=False) during run()
    def _bound_fn(self: Any, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": self.request.id,
            "retries": self.request.retries,
            "called_directly": self.request.called_directly,
            "ok": payload["ok"],
        }

    bound_task: Any = local_app.task(name="codesight.test.bound_task", bind=True)(_bound_fn)
    bound_task.push_request(id="live-worker-id-99", retries=2, called_directly=False)
    try:
        bound_res = bound_task({"ok": True})
    finally:
        bound_task.pop_request()
    assert bound_res == {
        "id": "live-worker-id-99",
        "retries": 2,
        "called_directly": False,
        "ok": True,
    }


async def test_soft_time_limit_exceeded_retryable_and_push_task_timeouts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uuid
    from typing import Any

    from billiard.exceptions import SoftTimeLimitExceeded
    from pydantic import SecretStr, ValidationError

    from codesight.core.config import Settings
    from codesight.worker.celery_app import handle_push_event, sync_default_branch
    from codesight.worker.tasks.indexing_tasks import PushSyncRunner

    # 1. SoftTimeLimitExceeded is classified as retryable with 5.0s delay
    assert classify_retryable_exception(SoftTimeLimitExceeded()) == (True, 5.0)

    # 2. Settings exposes index_task_soft_time_limit_seconds=300 and hard=360 with hard > soft
    defaults = Settings(_env_file=None)
    assert defaults.index_task_soft_time_limit_seconds == 300
    assert defaults.index_task_hard_time_limit_seconds == 360
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            index_task_soft_time_limit_seconds=300,
            index_task_hard_time_limit_seconds=300,
        )

    # 3. Dedicated push indexing tasks use 300s/360s limits while global defaults remain 60/90
    assert getattr(handle_push_event, "soft_time_limit", None) == 300
    assert getattr(handle_push_event, "time_limit", None) == 360
    assert getattr(sync_default_branch, "soft_time_limit", None) == 300
    assert getattr(sync_default_branch, "time_limit", None) == 360

    # 4. PushSyncRunner re-raises SoftTimeLimitExceeded on retry without marking failed prematurely
    recorded_statuses: list[str] = []
    runner = PushSyncRunner(
        settings=Settings(
            _env_file=None,
            github_token=SecretStr("ghp_test"),
            gemini_api_key=SecretStr("gem_test"),
        )
    )

    async def _fake_record(
        _delivery_id: str | None,
        *,
        webhook_status: str,
        trace_id: str,
    ) -> None:
        recorded_statuses.append(webhook_status)

    async def _raise_soft_limit(*_args: Any, **_kwargs: Any) -> Any:
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(runner, "_record_webhook_status", _fake_record)
    monkeypatch.setattr(
        "codesight.worker.tasks.indexing_tasks.IncrementalBranchSyncer.sync_push",
        _raise_soft_limit,
    )
    payload = {
        "delivery_id": "deliv-soft-timeout",
        "repo_id": str(uuid.uuid4()),
        "repo_full_name": "acme/timeout",
        "after_sha": "abc1234",
    }

    with pytest.raises(SoftTimeLimitExceeded):
        await runner.execute(payload, is_final_attempt=False)
    assert recorded_statuses == []

    with pytest.raises(SoftTimeLimitExceeded):
        await runner.execute(payload, is_final_attempt=True)
    assert recorded_statuses == ["failed"]


def test_worker_runtime_soft_time_limit_cancels_coro_without_premature_failed_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading
    import uuid
    from concurrent.futures import Future
    from typing import Any
    from unittest.mock import patch

    from billiard.exceptions import SoftTimeLimitExceeded
    from pydantic import SecretStr

    from codesight.core.config import Settings
    from codesight.core.logging import configure_logging
    from codesight.worker.celery_app import _WorkerAsyncRuntime
    from codesight.worker.tasks.indexing_tasks import PushSyncRunner

    configure_logging()
    runtime = _WorkerAsyncRuntime()
    started_evt = threading.Event()
    cancelled_evt = threading.Event()
    recorded_statuses: list[str] = []

    async def _blocking_sync(*_args: Any, **_kwargs: Any) -> Any:
        started_evt.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled_evt.set()
            raise

    async def _fake_record(
        _self: Any,
        _delivery_id: str | None,
        *,
        webhook_status: str,
        trace_id: str,
    ) -> None:
        recorded_statuses.append(webhook_status)

    monkeypatch.setattr(
        "codesight.worker.tasks.indexing_tasks.IncrementalBranchSyncer.sync_push",
        _blocking_sync,
    )
    monkeypatch.setattr(PushSyncRunner, "_record_webhook_status", _fake_record)
    monkeypatch.setattr(
        runtime,
        "_ensure_pools",
        lambda _s: asyncio.sleep(0, result=(object(), object(), object())),
    )
    monkeypatch.setattr(
        runtime,
        "_ensure_rag_pools",
        lambda _s: asyncio.sleep(0, result=(object(), None)),
    )

    test_settings = Settings(
        _env_file=None,
        github_token=SecretStr("ghp_test"),
        gemini_api_key=SecretStr("gem_test"),
    )
    payload = {
        "delivery_id": "deliv-runtime-soft",
        "repo_id": str(uuid.uuid4()),
        "repo_full_name": "acme/runtime-soft",
        "after_sha": "abc1234",
    }

    orig_result = Future.result

    def _raise_soft_limit_on_main(self: Any, timeout: float | None = None) -> Any:
        if started_evt.wait(timeout=2.0):
            raise SoftTimeLimitExceeded()
        return orig_result(self, timeout=timeout)

    try:
        with patch.object(Future, "result", _raise_soft_limit_on_main):
            with pytest.raises(SoftTimeLimitExceeded):
                runtime.run_push_webhook_task_sync(
                    payload,
                    settings=test_settings,
                    is_final_attempt=False,
                )
        assert cancelled_evt.wait(timeout=2.0) is True
        assert recorded_statuses == []

        started_evt.clear()
        cancelled_evt.clear()
        with patch.object(Future, "result", _raise_soft_limit_on_main):
            with pytest.raises(SoftTimeLimitExceeded):
                runtime.run_push_webhook_task_sync(
                    payload,
                    settings=test_settings,
                    is_final_attempt=True,
                )
        assert cancelled_evt.wait(timeout=2.0) is True
        assert recorded_statuses == ["failed"]
    finally:
        runtime.shutdown()


def test_worker_runtime_pr_soft_time_limit_cancels_coro_without_premature_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    import threading
    import uuid
    from concurrent.futures import Future
    from typing import Any
    from unittest.mock import patch

    import httpx
    from billiard.exceptions import SoftTimeLimitExceeded
    from pydantic import SecretStr

    from codesight.core.config import Settings
    from codesight.core.logging import configure_logging
    from codesight.db.models import PRReview
    from codesight.worker.celery_app import _WorkerAsyncRuntime
    from codesight.worker.supersede_lock import pr_active_sha_key

    configure_logging()
    runtime = _WorkerAsyncRuntime()
    started_evt = threading.Event()
    cancelled_evt = threading.Event()
    repo_uuid = str(uuid.uuid4())
    head_sha = "sha-pr-soft-limit"
    patch_bodies: list[dict[str, Any]] = []
    db_updates: list[dict[str, Any]] = []
    added_rows: list[Any] = []

    class _FakeRedis:
        def __init__(self) -> None:
            self._store: dict[str, str] = {pr_active_sha_key(repo_uuid, 44): head_sha}

        async def get(self, key: str) -> str | None:
            return self._store.get(key)

    class _FakeSession:
        async def __aenter__(self) -> "_FakeSession":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, stmt: Any) -> None:
            db_updates.append(dict(stmt.compile().params))

        async def scalar(self, stmt: Any) -> Any:
            if "webhook_events" in str(stmt):
                return "queued"
            return uuid.UUID(repo_uuid)

        def add(self, obj: Any) -> None:
            added_rows.append(obj)

        async def commit(self) -> None:
            return None

    def _gh_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        if request.url.path.endswith((".codesight.yaml", ".codesight.yml")):
            return httpx.Response(404)
        if request.url.path.endswith("/check-runs") and request.method == "POST":
            return httpx.Response(201, json={"id": 8101, "status": "in_progress"})
        if request.url.path.endswith("/check-runs/8101") and request.method == "PATCH":
            patch_bodies.append(body)
            return httpx.Response(200, json={"id": 8101, "status": "completed"})
        return httpx.Response(404)

    async def _blocking_fetch(*_args: Any, **_kwargs: Any) -> Any:
        started_evt.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled_evt.set()
            raise

    monkeypatch.setattr(
        "codesight.worker.pr_review_runner.GitHubPullRequestClient.fetch_pull_request_diff",
        _blocking_fetch,
    )

    test_settings = Settings(_env_file=None, github_token=SecretStr("ghp_test"))
    payload = {
        "delivery_id": "deliv-pr-runtime-soft",
        "repo_id": repo_uuid,
        "repo_full_name": "acme/pr-soft",
        "default_branch": "main",
        "pr_number": 44,
        "head_sha": head_sha,
    }
    fake_redis = _FakeRedis()
    orig_result = Future.result
    main_thread = threading.current_thread()

    def _raise_soft_limit_on_main(self: Any, timeout: float | None = None) -> Any:
        if threading.current_thread() is not main_thread:
            return orig_result(self, timeout=timeout)
        if started_evt.wait(timeout=5.0):
            raise SoftTimeLimitExceeded()
        raise TimeoutError("Timed out waiting for _blocking_fetch to start")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_gh_handler))

    async def _fake_ensure_pools(_s: Settings) -> tuple[Any, Any, Any]:
        return http_client, fake_redis, lambda: _FakeSession()

    monkeypatch.setattr(runtime, "_ensure_pools", _fake_ensure_pools)

    try:
        with patch.object(Future, "result", _raise_soft_limit_on_main):
            with pytest.raises(SoftTimeLimitExceeded):
                runtime.run_pr_webhook_task_sync(
                    payload,
                    settings=test_settings,
                    is_final_attempt=False,
                )
        assert cancelled_evt.wait(timeout=2.0) is True
        assert len(patch_bodies) == 1
        assert patch_bodies[0]["conclusion"] == "neutral"
        assert "Retry Scheduled" in patch_bodies[0]["output"]["title"]
        assert db_updates == []
        assert added_rows == []

        started_evt.clear()
        cancelled_evt.clear()
        patch_bodies.clear()
        with patch.object(Future, "result", _raise_soft_limit_on_main):
            with pytest.raises(SoftTimeLimitExceeded):
                runtime.run_pr_webhook_task_sync(
                    payload,
                    settings=test_settings,
                    is_final_attempt=True,
                )
        assert cancelled_evt.wait(timeout=2.0) is True
        assert len(patch_bodies) == 1
        assert patch_bodies[0]["conclusion"] == "failure"
        assert patch_bodies[0]["output"]["title"] == "CodeSight-AI Review Timed Out"
        assert len(db_updates) == 1 and db_updates[0].get("status") == "failed"
        assert len(added_rows) == 1 and isinstance(added_rows[0], PRReview)
        assert added_rows[0].status == "failed"
    finally:
        runtime.run_coro(http_client.aclose())
        runtime.shutdown()


def test_codesight_task_schedules_retry_on_soft_time_limit_for_pr_and_push_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import Any
    from unittest.mock import MagicMock

    from billiard.exceptions import SoftTimeLimitExceeded
    from pydantic import SecretStr

    from codesight.core.config import Settings
    from codesight.worker.celery_app import _WORKER_RUNTIME, handle_pr_event, handle_push_event

    test_settings = Settings(
        _env_file=None,
        github_token=SecretStr("ghp_test"),
        gemini_api_key=SecretStr("gem_test"),
    )
    monkeypatch.setattr("codesight.worker.celery_app.get_settings", lambda: test_settings)
    monkeypatch.setattr(
        "codesight.worker.celery_app.is_pr_event_superseded",
        lambda *_args, **_kwargs: False,
    )

    pr_final_flags: list[bool] = []
    push_final_flags: list[bool] = []

    def _raise_pr_soft_limit(
        _payload: Any,
        *,
        settings: Settings | None = None,
        is_final_attempt: bool = False,
    ) -> dict[str, str]:
        pr_final_flags.append(is_final_attempt)
        raise SoftTimeLimitExceeded()

    def _raise_push_soft_limit(
        _payload: Any,
        *,
        settings: Settings | None = None,
        is_final_attempt: bool = False,
    ) -> dict[str, str]:
        push_final_flags.append(is_final_attempt)
        raise SoftTimeLimitExceeded()

    monkeypatch.setattr(_WORKER_RUNTIME, "run_pr_webhook_task_sync", _raise_pr_soft_limit)
    monkeypatch.setattr(_WORKER_RUNTIME, "run_push_webhook_task_sync", _raise_push_soft_limit)

    retry_sentinel = RuntimeError("celery-retry-scheduled")
    pr_retry_mock = MagicMock(side_effect=retry_sentinel)
    push_retry_mock = MagicMock(side_effect=retry_sentinel)
    monkeypatch.setattr(handle_pr_event, "retry", pr_retry_mock)
    monkeypatch.setattr(handle_push_event, "retry", push_retry_mock)

    pr_payload = {
        "delivery_id": "deliv-pr-task-soft",
        "repo_id": "repo-pr-1",
        "repo_full_name": "acme/pr-app",
        "pr_number": 7,
        "head_sha": "sha-pr-7",
    }
    push_payload = {
        "delivery_id": "deliv-push-task-soft",
        "repo_id": "repo-push-1",
        "repo_full_name": "acme/push-app",
        "ref": "refs/heads/main",
        "default_branch": "main",
        "after_sha": "sha-push-1",
    }

    # 1. Non-final attempt on handle_pr_event schedules Celery retry with SoftTimeLimitExceeded
    handle_pr_event.push_request(id="pr-task-1", retries=0, called_directly=False)
    try:
        with pytest.raises(RuntimeError, match="celery-retry-scheduled"):
            handle_pr_event(pr_payload)
    finally:
        handle_pr_event.pop_request()
    assert pr_final_flags == [False]
    pr_retry_mock.assert_called_once()
    _, pr_kwargs = pr_retry_mock.call_args
    assert isinstance(pr_kwargs["exc"], SoftTimeLimitExceeded)
    assert 5.0 <= pr_kwargs["countdown"] <= 6.0
    assert pr_kwargs["max_retries"] == MAX_TASK_RETRIES

    # 2. Final attempt on handle_pr_event sets is_final_attempt=True and does not schedule retry
    pr_retry_mock.reset_mock()
    handle_pr_event.push_request(
        id="pr-task-final", retries=MAX_TASK_RETRIES, called_directly=False
    )
    try:
        with pytest.raises(SoftTimeLimitExceeded):
            handle_pr_event(pr_payload)
    finally:
        handle_pr_event.pop_request()
    assert pr_final_flags == [False, True]
    pr_retry_mock.assert_not_called()

    # 3. Non-final attempt on handle_push_event schedules Celery retry with SoftTimeLimitExceeded
    handle_push_event.push_request(id="push-task-1", retries=0, called_directly=False)
    try:
        with pytest.raises(RuntimeError, match="celery-retry-scheduled"):
            handle_push_event(push_payload)
    finally:
        handle_push_event.pop_request()
    assert push_final_flags == [False]
    push_retry_mock.assert_called_once()
    _, push_kwargs = push_retry_mock.call_args
    assert isinstance(push_kwargs["exc"], SoftTimeLimitExceeded)
    assert 5.0 <= push_kwargs["countdown"] <= 6.0
    assert push_kwargs["max_retries"] == MAX_TASK_RETRIES

    # 4. Final attempt on handle_push_event sets is_final_attempt=True and does not schedule retry
    push_retry_mock.reset_mock()
    handle_push_event.push_request(
        id="push-task-final", retries=MAX_TASK_RETRIES, called_directly=False
    )
    try:
        with pytest.raises(SoftTimeLimitExceeded):
            handle_push_event(push_payload)
    finally:
        handle_push_event.pop_request()
    assert push_final_flags == [False, True]
    push_retry_mock.assert_not_called()


async def test_pr_task_timeouts_and_truncated_push_sync_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1-06a & P2-06a: PR task limits are configurable and truncated tree marks failed."""
    from types import SimpleNamespace
    from typing import Any
    from unittest.mock import MagicMock

    import httpx
    from pydantic import SecretStr, ValidationError

    from codesight.core.config import Settings
    from codesight.worker.celery_app import handle_pr_event
    from codesight.worker.tasks.indexing_tasks import PushSyncRunner

    defaults = Settings(_env_file=None)
    assert defaults.pr_task_soft_time_limit_seconds == 120
    assert defaults.pr_task_hard_time_limit_seconds == 150
    assert getattr(handle_pr_event, "soft_time_limit", None) == 120
    assert getattr(handle_pr_event, "time_limit", None) == 150
    with pytest.raises(ValidationError, match="PR_TASK_HARD_TIME_LIMIT_SECONDS"):
        Settings(
            _env_file=None,
            pr_task_soft_time_limit_seconds=120,
            pr_task_hard_time_limit_seconds=120,
        )

    recorded: list[str] = []

    class _TruncatedSyncer:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def sync_push(self, **_kwargs: Any) -> Any:
            return SimpleNamespace(
                mode="full_bootstrap",
                changed_files=100,
                upserted_points=50,
                elapsed_ms=25.0,
                truncated_tree=True,
                persisted_cursor=False,
            )

    runner = PushSyncRunner(
        settings=Settings(
            _env_file=None,
            github_token=SecretStr("ghp_test"),
            gemini_api_key=SecretStr("gem_test"),
        ),
        http_client=httpx.AsyncClient(),
        redis_client=MagicMock(),
        store=MagicMock(),
        embedder=MagicMock(),
    )

    async def _rec(delivery_id: str | None, *, webhook_status: str, trace_id: str) -> None:
        _ = delivery_id, trace_id
        recorded.append(webhook_status)

    monkeypatch.setattr(runner, "_record_webhook_status", _rec)
    monkeypatch.setattr(
        "codesight.worker.tasks.indexing_tasks.IncrementalBranchSyncer",
        _TruncatedSyncer,
    )
    out = await runner.execute(
        {
            "delivery_id": "deliv-trunc",
            "repo_id": "repo-1",
            "repo_full_name": "acme/big",
            "after_sha": "a" * 40,
            "default_branch": "main",
            "pushed_ref": "refs/heads/main",
        }
    )
    assert out["status"] == "incomplete"
    assert recorded == ["failed"]
    await runner._http_client.aclose()  # type: ignore[union-attr]
