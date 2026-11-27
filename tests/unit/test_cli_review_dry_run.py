"""Unit tests for `codesight review <pr_url> --dry-run` CLI command (CA-9)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from codesight.cli.commands.review import (
    CLIReviewError,
    fetch_pull_request_dry_run,
    format_line_ranges,
    parse_github_pr_url,
)
from codesight.cli.main import app as cli_app
from codesight.core.config import Settings


def test_parse_github_pr_url_validation_and_ssrf_guard() -> None:
    parsed = parse_github_pr_url("https://github.com/Tieens-04/CodeSight-AI/pull/5")
    assert parsed.owner == "Tieens-04"
    assert parsed.repo == "CodeSight-AI"
    assert parsed.pull_number == 5
    assert parsed.repo_full_name == "Tieens-04/CodeSight-AI"

    with_subpath = parse_github_pr_url(
        "https://www.github.com/acme/web-api/pull/128/files?w=1#diff-123"
    )
    assert with_subpath.repo_full_name == "acme/web-api"
    assert with_subpath.pull_number == 128

    for bad_url in [
        "http://github.com/acme/api/pull/1",
        "https://evil.example.com/acme/api/pull/1",
        "https://169.254.169.254/acme/api/pull/1",
        "https://user:pass@github.com/acme/api/pull/1",
        "https://github.com:8443/acme/api/pull/1",
        "https://github.com/acme/api/issues/1",
        "https://github.com/acme/api/pull/0",
        "https://github.com/acme/api/pull/abc",
        "https://github.com/../etc/pull/1",
        "not-a-url",
    ]:
        with pytest.raises(ValueError):
            parse_github_pr_url(bad_url)


def test_format_line_ranges_compacts_contiguous_spans() -> None:
    assert format_line_ranges(set()) == "—"
    assert format_line_ranges({10}) == "L10"
    assert format_line_ranges({22, 10, 20, 21, 25, 26}) == "L10, L20-L22, L25-L26"


async def test_fetch_pull_request_dry_run_supports_public_and_authenticated_prs() -> None:
    import base64

    captured_auth_headers: list[str | None] = []
    sample_yaml = base64.b64encode(
        b"ignore_paths:\n  - docs/generated/**\nmin_debt_score: 75\n"
    ).decode("ascii")
    lock_patch = "\n".join(f'+  "pkg-{i}": "1.0.{i}",' for i in range(40))
    raw_diff = "\n".join(
        [
            "diff --git a/package-lock.json b/package-lock.json",
            "--- a/package-lock.json",
            "+++ b/package-lock.json",
            "@@ -1,1 +1,41 @@",
            " {",
            lock_patch,
            "diff --git a/docs/generated/types.ts b/docs/generated/types.ts",
            "--- a/docs/generated/types.ts",
            "+++ b/docs/generated/types.ts",
            "@@ -1 +1 @@",
            "-export const A = 1;",
            "+export const A = 2;",
            "diff --git a/src/service.py b/src/service.py",
            "--- a/src/service.py",
            "+++ b/src/service.py",
            "@@ -10,2 +10,3 @@ def run():",
            "     init()",
            "+    validate()",
            "     return True",
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured_auth_headers.append(request.headers.get("authorization"))
        if request.url.path == "/repos/acme/app/pulls/12":
            accept = request.headers.get("accept", "")
            if "vnd.github.diff" in accept:
                return httpx.Response(200, text=raw_diff)
            return httpx.Response(
                200,
                json={
                    "title": "Fix [critical] auth bug",
                    "state": "open",
                    "base": {"ref": "main"},
                    "head": {"ref": "feat/auth", "sha": "abc123def4567890"},
                },
            )
        if request.url.path == "/repos/acme/app/contents/.codesight.yaml":
            return httpx.Response(
                200,
                json={
                    "type": "file",
                    "size": 64,
                    "encoding": "base64",
                    "content": sample_yaml,
                },
            )
        return httpx.Response(404, json={"message": "Not Found"})

    pr_ref = parse_github_pr_url("https://github.com/acme/app/pull/12")

    # 1. Public unauthenticated run (no GITHUB_TOKEN / App credentials)
    no_auth_settings = Settings(github_app_id=None, github_private_key=None, github_token=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bundle = await fetch_pull_request_dry_run(
            pr_ref,
            settings=no_auth_settings,
            http_client=client,
        )
    assert all(h is None for h in captured_auth_headers)
    assert bundle.diff_source == "diff"
    assert bundle.repo_config.min_debt_score == 75
    assert bundle.parsed_diff.metrics.kept_files == 1
    assert bundle.parsed_diff.metrics.skipped_files == 2
    assert bundle.parsed_diff.metrics.tokens_saved_pct > 50.0
    kept_file = bundle.parsed_diff.get_file("src/service.py")
    assert kept_file is not None
    assert kept_file.valid_commentable_lines == {10, 11, 12}

    # 2. Authenticated run sends Bearer header
    captured_auth_headers.clear()
    pat_settings = Settings(github_token=SecretStr("ghp_cli_pat_token"))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await fetch_pull_request_dry_run(
            pr_ref,
            settings=pat_settings,
            http_client=client,
        )
    assert captured_auth_headers and all(
        h == "Bearer ghp_cli_pat_token" for h in captured_auth_headers
    )


async def test_fetch_pull_request_dry_run_falls_back_to_files_api_on_406() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/big/pulls/99":
            if "vnd.github.diff" in request.headers.get("accept", ""):
                return httpx.Response(406, json={"message": "Diff exceeded maximum lines"})
            return httpx.Response(
                200,
                json={
                    "title": "Large PR",
                    "state": "open",
                    "base": {"ref": "main"},
                    "head": {"ref": "feat/big", "sha": "111222333444"},
                },
            )
        if request.url.path == "/repos/acme/big/pulls/99/files":
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "src/core.py",
                        "status": "modified",
                        "patch": "@@ -1,2 +1,3 @@\n def f():\n+    step()\n     return 1\n",
                    },
                    {
                        "filename": "yarn.lock",
                        "status": "modified",
                        "patch": "@@ -1 +1 @@\n-old\n+new\n",
                    },
                ],
            )
        return httpx.Response(404, json={"message": "Not Found"})

    pr_ref = parse_github_pr_url("https://github.com/acme/big/pull/99")
    settings = Settings(github_app_id=None, github_private_key=None, github_token=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bundle = await fetch_pull_request_dry_run(pr_ref, settings=settings, http_client=client)

    assert bundle.diff_source == "files_api"
    assert bundle.parsed_diff.metrics.kept_files == 1
    assert bundle.parsed_diff.metrics.skipped_files == 1
    assert bundle.parsed_diff.get_file("src/core.py") is not None


async def test_fetch_pull_request_dry_run_maps_http_404_and_429_to_cli_error() -> None:
    status_to_return = 404

    def handler(_request: httpx.Request) -> httpx.Response:
        headers = {"Retry-After": "30"} if status_to_return == 429 else {}
        return httpx.Response(status_to_return, headers=headers, json={"message": "err"})

    pr_ref = parse_github_pr_url("https://github.com/acme/missing/pull/5")
    settings = Settings(github_app_id=None, github_private_key=None, github_token=None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(CLIReviewError, match="404"):
            await fetch_pull_request_dry_run(pr_ref, settings=settings, http_client=client)

        status_to_return = 429
        with pytest.raises(CLIReviewError, match="rate limit"):
            await fetch_pull_request_dry_run(pr_ref, settings=settings, http_client=client)


async def test_fetch_pull_request_dry_run_redirect_ssrf_and_invalid_auth_config() -> None:
    def redirect_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/evil/pulls/1":
            return httpx.Response(
                302, headers={"Location": "http://169.254.169.254/latest/meta-data"}
            )
        if request.url.path == "/repos/acme/renamed/pulls/12":
            return httpx.Response(
                301, headers={"Location": "https://api.github.com/repositories/123/pulls/12"}
            )
        if request.url.path == "/repositories/123/pulls/12":
            if "vnd.github.diff" in request.headers.get("accept", ""):
                return httpx.Response(
                    200,
                    text=(
                        "diff --git a/a.py b/a.py\n"
                        "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-a=1\n+a=2\n"
                    ),
                )
            return httpx.Response(
                200,
                json={
                    "title": "Moved repo PR",
                    "state": "open",
                    "base": {"ref": "main"},
                    "head": {"ref": "feat/move", "sha": "999888777"},
                },
            )
        return httpx.Response(404, json={"message": "Not Found"})

    no_auth = Settings(github_app_id=None, github_private_key=None, github_token=None)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(redirect_handler),
        follow_redirects=False,
    ) as client:
        with pytest.raises(CLIReviewError, match="cross-origin redirect"):
            await fetch_pull_request_dry_run(
                parse_github_pr_url("https://github.com/acme/evil/pull/1"),
                settings=no_auth,
                http_client=client,
            )

        bundle = await fetch_pull_request_dry_run(
            parse_github_pr_url("https://github.com/acme/renamed/pull/12"),
            settings=no_auth,
            http_client=client,
        )
        assert bundle.title == "Moved repo PR"
        assert bundle.parsed_diff.metrics.kept_files == 1

    bad_pem_settings = Settings(
        github_app_id="12345",
        github_private_key=SecretStr("not-a-valid-pem-key"),
        github_token=None,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(redirect_handler)) as client:
        with pytest.raises(CLIReviewError, match="Invalid GitHub authentication configuration"):
            await fetch_pull_request_dry_run(
                parse_github_pr_url("https://github.com/acme/renamed/pull/12"),
                settings=bad_pem_settings,
                http_client=client,
            )


def test_cli_review_dry_run_outputs_clean_rich_report_and_isolates_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from codesight.cli.commands.review import PRDryRunBundle
    from codesight.config_reader.repo_config import RepoConfig
    from codesight.diff.smart_diff_parser import SmartDiffParser

    raw_diff = "\n".join(
        [
            "diff --git a/package-lock.json b/package-lock.json",
            "--- a/package-lock.json",
            "+++ b/package-lock.json",
            "@@ -1 +1,10 @@",
            '-"a": 1',
            '+"a": 2',
            '+"b": 3',
            '+"c": 4',
            '+"d": 5',
            '+"e": 6',
            '+"f": 7',
            '+"g": 8',
            '+"h": 9',
            '+"i": 10',
            '+"j": 11',
            "diff --git a/src/[bracket]/app.py b/src/[bracket]/app.py",
            "--- a/src/[bracket]/app.py",
            "+++ b/src/[bracket]/app.py",
            "@@ -5,2 +5,3 @@ def run():",
            "     a = 1",
            "+    b = 2",
            "     return a",
        ]
    )
    parsed_diff = SmartDiffParser().parse_diff(raw_diff)
    fake_bundle = PRDryRunBundle(
        pr_ref=parse_github_pr_url("https://github.com/acme/app/pull/42"),
        title="[bold red]Unsafe[/bold red] PR Title",
        state="open",
        base_ref="main",
        head_ref="feat/ca9",
        head_sha="deadbeef12345678",
        repo_config=RepoConfig(),
        parsed_diff=parsed_diff,
        diff_source="diff",
        elapsed_ms=142.5,
    )

    async def _fake_fetch(*_args: Any, **_kwargs: Any) -> PRDryRunBundle:
        return fake_bundle

    monkeypatch.setattr("codesight.cli.commands.review.fetch_pull_request_dry_run", _fake_fetch)

    runner = CliRunner()

    # 1. Default run keeps stdout pure Rich report with zero JSON logs
    res = runner.invoke(
        cli_app,
        ["review", "https://github.com/acme/app/pull/42", "--dry-run"],
    )
    assert res.exit_code == 0
    assert "src/[bracket]/app.py" in res.stdout
    assert "[bold red]Unsafe[/bold red]" in res.stdout
    assert "package-lock.json" in res.stdout
    assert "L5-L7" in res.stdout
    assert "cli_review_dry_run_completed" not in res.stdout
    assert res.stderr == ""

    # 2. --verbose writes structured JSON log ONLY to stderr, never stdout
    verbose_res = runner.invoke(
        cli_app,
        ["--verbose", "review", "https://github.com/acme/app/pull/42", "--dry-run"],
    )
    assert verbose_res.exit_code == 0
    assert "cli_review_dry_run_completed" not in verbose_res.stdout
    assert verbose_res.stderr.strip() != ""
    log_records = [
        json.loads(line) for line in verbose_res.stderr.strip().splitlines() if line.strip()
    ]
    assert any(
        rec.get("event") == "cli_review_dry_run_completed"
        and rec.get("repo") == "acme/app"
        and rec.get("pr_number") == 42
        for rec in log_records
    )

    # 3. Missing --dry-run exits cleanly with code 2 when GEMINI_API_KEY is absent
    monkeypatch.setattr(
        "codesight.cli.commands.review.get_settings",
        lambda: Settings(_env_file=None, gemini_api_key=None),
    )
    no_dry = runner.invoke(cli_app, ["review", "https://github.com/acme/app/pull/42"])
    assert no_dry.exit_code == 2

    # 4. Invalid SSRF URL exits cleanly with code 2
    bad_host = runner.invoke(
        cli_app,
        ["review", "https://evil.example.com/acme/app/pull/42", "--dry-run"],
    )
    assert bad_host.exit_code == 2


async def test_fetch_pull_request_dry_run_handles_cancelled_and_sibling_timeout_cleanly() -> None:
    pr_ref = parse_github_pr_url("https://github.com/acme/flaky/pull/3")
    settings = Settings(github_app_id=None, github_private_key=None, github_token=None)

    # 1. Sibling ReadTimeout cancels pending diff task cleanly and raises CLIReviewError
    diff_entered = asyncio.Event()
    diff_cancelled = False

    async def timeout_handler(request: httpx.Request) -> httpx.Response:
        if "vnd.github.diff" in request.headers.get("accept", ""):
            diff_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                nonlocal diff_cancelled
                diff_cancelled = True
                raise
        await diff_entered.wait()
        raise httpx.ReadTimeout("meta timed out", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(timeout_handler)) as client:
        with pytest.raises(CLIReviewError, match="ReadTimeout"):
            await fetch_pull_request_dry_run(pr_ref, settings=settings, http_client=client)
    assert diff_cancelled is True

    # 2. Child CancelledError while outer task is NOT cancelled maps to CLIReviewError
    async def child_cancel_handler(request: httpx.Request) -> httpx.Response:
        if "vnd.github.diff" in request.headers.get("accept", ""):
            await asyncio.Event().wait()
        raise asyncio.CancelledError("simulated child stream abort")

    async with httpx.AsyncClient(transport=httpx.MockTransport(child_cancel_handler)) as client:
        with pytest.raises(CLIReviewError, match="CancelledError"):
            await fetch_pull_request_dry_run(pr_ref, settings=settings, http_client=client)

    # 3. Legitimate outer task cancellation propagates asyncio.CancelledError
    both_waiting = asyncio.Event()
    waiting_count = 0

    async def hang_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal waiting_count
        waiting_count += 1
        if waiting_count >= 2:
            both_waiting.set()
        await asyncio.Event().wait()
        return httpx.Response(200)  # pragma: no cover

    async with httpx.AsyncClient(transport=httpx.MockTransport(hang_handler)) as client:
        task = asyncio.create_task(
            fetch_pull_request_dry_run(pr_ref, settings=settings, http_client=client)
        )
        await both_waiting.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_fetch_pull_request_dry_run_falls_back_to_files_api_when_diff_exceeds_max_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("codesight.cli.commands.review.MAX_DIFF_BYTES", 256)
    mode = "content_length"

    class _ChunkedOversizedStream(httpx.AsyncByteStream):
        async def __aiter__(self) -> AsyncIterator[bytes]:
            yield b"+" * 200
            yield b"+" * 200

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/huge/pulls/8":
            if "vnd.github.diff" in request.headers.get("accept", ""):
                if mode == "content_length":
                    return httpx.Response(
                        200,
                        headers={"Content-Length": "1024"},
                        content=b"diff --git a/x.py b/x.py\n",
                    )
                return httpx.Response(200, stream=_ChunkedOversizedStream())
            return httpx.Response(
                200,
                json={
                    "title": "Oversized PR",
                    "state": "open",
                    "base": {"ref": "main"},
                    "head": {"ref": "feat/huge", "sha": "abcdef123456"},
                },
            )
        if request.url.path == "/repos/acme/huge/pulls/8/files":
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "src/core.py",
                        "status": "modified",
                        "patch": "@@ -1 +1,2 @@\n def ok():\n+    return 1\n",
                    }
                ],
            )
        return httpx.Response(404, json={"message": "Not Found"})

    pr_ref = parse_github_pr_url("https://github.com/acme/huge/pull/8")
    settings = Settings(github_app_id=None, github_private_key=None, github_token=None)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bundle_hdr = await fetch_pull_request_dry_run(pr_ref, settings=settings, http_client=client)
        assert bundle_hdr.diff_source == "files_api"
        assert bundle_hdr.parsed_diff.get_file("src/core.py") is not None

        mode = "streaming_no_content_length"
        bundle_stream = await fetch_pull_request_dry_run(
            pr_ref, settings=settings, http_client=client
        )
        assert bundle_stream.diff_source == "files_api"
        assert bundle_stream.parsed_diff.get_file("src/core.py") is not None


async def test_fetch_pull_request_dry_run_handles_gzip_responses_and_default_branch_config() -> (
    None
):
    import base64
    import gzip

    raw_diff = (
        "diff --git a/src/service.py b/src/service.py\n"
        "--- a/src/service.py\n"
        "+++ b/src/service.py\n"
        "@@ -1 +1,2 @@\n"
        " def ok():\n"
        "+    return 42\n"
    )
    gzipped_diff = gzip.compress(raw_diff.encode("utf-8"))
    gzipped_406 = gzip.compress(b'{"message": "Diff exceeded maximum lines"}')
    cfg_b64 = base64.b64encode(b"min_debt_score: 88\n").decode("ascii")
    seen_config_refs: list[str] = []
    mode = "gzip_200"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/gz/pulls/14":
            if "vnd.github.diff" in request.headers.get("accept", ""):
                if mode == "gzip_200":
                    return httpx.Response(
                        200,
                        headers={"Content-Encoding": "gzip", "Content-Type": "text/plain"},
                        content=gzipped_diff,
                    )
                return httpx.Response(
                    406,
                    headers={"Content-Encoding": "gzip", "Content-Type": "application/json"},
                    content=gzipped_406,
                )
            return httpx.Response(
                200,
                json={
                    "title": "PR targeting non-default feature branch",
                    "state": "open",
                    "base": {
                        "ref": "feat/untrusted-base",
                        "repo": {"default_branch": "production"},
                    },
                    "head": {"ref": "feat/child", "sha": "fedcba987654"},
                },
            )
        if request.url.path == "/repos/acme/gz/contents/.codesight.yaml":
            seen_config_refs.append(request.url.params.get("ref", ""))
            return httpx.Response(
                200,
                json={"type": "file", "size": 32, "encoding": "base64", "content": cfg_b64},
            )
        if request.url.path == "/repos/acme/gz/pulls/14/files":
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "src/service.py",
                        "status": "modified",
                        "sha": "1111222233334444555566667777888899990000",
                        "changes": 1,
                        "patch": "@@ -1 +1,2 @@\n def ok():\n+    return 42\n",
                    }
                ],
            )
        return httpx.Response(404)

    pr_ref = parse_github_pr_url("https://github.com/acme/gz/pull/14")
    settings = Settings(github_app_id=None, github_private_key=None, github_token=None)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        bundle_gz = await fetch_pull_request_dry_run(pr_ref, settings=settings, http_client=client)
        assert bundle_gz.diff_source == "diff"
        assert bundle_gz.repo_config.min_debt_score == 88
        assert seen_config_refs == ["production"]
        kept = bundle_gz.parsed_diff.get_file("src/service.py")
        assert kept is not None and kept.valid_commentable_lines == {1, 2}

        mode = "gzip_406"
        bundle_406 = await fetch_pull_request_dry_run(pr_ref, settings=settings, http_client=client)
        assert bundle_406.diff_source == "files_api"
        kept_fb = bundle_406.parsed_diff.get_file("src/service.py")
        assert kept_fb is not None
        assert kept_fb.blob_sha == "1111222233334444555566667777888899990000"
