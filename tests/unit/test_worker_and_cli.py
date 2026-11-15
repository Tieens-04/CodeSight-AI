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
    celery_app,
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
