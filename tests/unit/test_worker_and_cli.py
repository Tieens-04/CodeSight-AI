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
