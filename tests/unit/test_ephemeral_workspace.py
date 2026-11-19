"""Unit tests for NFR-3 ephemeral_workspace() and retryable error primitives."""

from __future__ import annotations

import io
import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

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
from codesight.core.logging import configure_logging


def test_ephemeral_workspace_wipes_readonly_files_even_on_runtime_error(tmp_path: Path) -> None:
    normal_dir: Path | None = None
    with ephemeral_workspace(base_dir=tmp_path) as ws_ok:
        normal_dir = ws_ok
        (ws_ok / "temp.txt").write_text("ephemeral", encoding="utf-8")
        assert (ws_ok / "temp.txt").exists()
    assert normal_dir is not None and not normal_dir.exists()

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


def test_ephemeral_workspace_logs_unrecoverable_cleanup_error_without_masking_caller_exc(
    tmp_path: Path,
) -> None:
    stream = io.StringIO()
    configure_logging(level="ERROR", service_name="test-ephemeral", stream=stream)

    with patch("codesight.core.ephemeral.shutil.rmtree", side_effect=OSError("disk locked")):
        with pytest.raises(ValueError, match="original caller error"):
            with ephemeral_workspace(base_dir=tmp_path):
                raise ValueError("original caller error")

    logs = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    assert any(entry["event"] == "ephemeral_workspace_cleanup_failed" for entry in logs)


@pytest.mark.asyncio
async def test_async_ephemeral_workspace_and_path_traversal_guards(tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("host-file", encoding="utf-8")

    async with async_ephemeral_workspace(base_dir=tmp_path) as ws:
        assert ws.exists() and ws.parent == tmp_path
        valid = resolve_safe_subpath(ws, "a\\b/c.ts")
        assert valid.is_relative_to(ws.resolve())
        invalid_paths = (
            "",
            "   ",
            "../escape.py",
            "/etc/passwd",
            "a/../../escape.py",
            "bad\x00name.py",
        )
        for bad in invalid_paths:
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


def test_rmtree_onexc_grants_dir_exec_bit_and_skips_chmod_on_symlinks(tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    from codesight.core.ephemeral import _on_rmtree_error

    ro_dir = tmp_path / "ro_subdir"
    ro_dir.mkdir()
    chmod_calls: list[tuple[str, int]] = []
    retry_fn = MagicMock()

    with patch(
        "codesight.core.ephemeral.os.chmod",
        side_effect=lambda p, m: chmod_calls.append((str(p), m)),
    ):
        _on_rmtree_error(retry_fn, str(ro_dir), PermissionError("denied"))
        assert chmod_calls == [(str(ro_dir), stat.S_IRWXU)]
        retry_fn.assert_called_once_with(str(ro_dir))

        chmod_calls.clear()
        retry_fn.reset_mock()
        with patch("codesight.core.ephemeral.os.path.islink", return_value=True):
            _on_rmtree_error(retry_fn, str(tmp_path / "sym"), PermissionError("denied"))
        assert chmod_calls == []
        retry_fn.assert_called_once_with(str(tmp_path / "sym"))

        # Handler bound to ws_root also restores read-only parent directory inside ws_root
        from codesight.core.ephemeral import _make_rmtree_error_handler

        ws_root = tmp_path / "ws_bound"
        inner_dir = ws_root / "locked_parent"
        inner_file = inner_dir / "child.txt"
        inner_dir.mkdir(parents=True)
        inner_file.write_text("x", encoding="utf-8")

        chmod_calls.clear()
        retry_fn.reset_mock()
        bound_handler = _make_rmtree_error_handler(ws_root)
        fake_stat = os.stat_result((stat.S_IFDIR | 0o500, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        with patch("codesight.core.ephemeral.os.lstat", return_value=fake_stat):
            bound_handler(retry_fn, str(inner_file), PermissionError("denied"))
        assert chmod_calls[0] == (str(inner_dir), stat.S_IRWXU)
        assert chmod_calls[1][0] == str(inner_file)
        retry_fn.assert_called_once_with(str(inner_file))


def test_parse_retry_after_clamps_delta_http_date_and_ratelimit_reset() -> None:
    assert parse_retry_after_header({"Retry-After": "12"}) == 12.0
    assert parse_retry_after_header({"retry-after": "999"}) == 60.0
    assert parse_retry_after_header({"Retry-After": "0"}) == 1.0
    assert parse_retry_after_header({"Retry-After": "-5"}) == 1.0
    assert parse_retry_after_header({"X-RateLimit-Reset": "1700000025"}, now=1700000000.0) == 25.0
    assert parse_retry_after_header({"X-RateLimit-Reset": "not-an-epoch"}, now=1700000000.0) is None
    assert (
        parse_retry_after_header(
            {"Retry-After": "Tue, 14 Nov 2023 22:13:35 GMT"},
            now=1700000000.0,
        )
        == 15.0
    )
    assert (
        parse_retry_after_header(
            {"Retry-After": "Tue, 14 Nov 2023 22:13:00 GMT"},
            now=1700000000.0,
        )
        == 1.0
    )
    assert parse_retry_after_header({"Retry-After": "invalid-date-format"}) is None
    assert parse_retry_after_header({}) is None

    err = RateLimitTaskError("429 hit", retry_after_seconds=8.5)
    assert isinstance(err, RetryableTaskError)
    assert err.retry_after_seconds == 8.5
