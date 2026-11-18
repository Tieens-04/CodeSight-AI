"""Unit tests for GitHubPullRequestClient, Week1PRReviewRunner, and Celery E2E wiring."""

from __future__ import annotations

import ast
import asyncio
import inspect
import io
import json
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from codesight.config_reader.repo_config import RepoConfig
from codesight.core.config import Settings
from codesight.core.errors import RateLimitTaskError, RetryableTaskError
from codesight.core.logging import (
    bind_trace_id,
    clear_trace_context,
    configure_logging,
    get_trace_id,
)
from codesight.diff.models import PatchFileEntry
from codesight.diff.smart_diff_parser import SmartDiffParser
from codesight.vcs.github_auth import (
    GitHubAuthConfigError,
    GitHubAuthError,
    GitHubAuthProvider,
)
from codesight.vcs.github_pr_client import FetchedPullRequestDiff, GitHubPullRequestClient
from codesight.vcs.github_publisher import GitHubPublishError
from codesight.worker.celery_app import (
    PRReviewLifecycleContext,
    execute_pr_review_lifecycle,
    run_pr_webhook_task,
)
from codesight.worker.pr_review_runner import (
    Week1PRReviewRunner,
    build_week1_smoke_drafts,
    build_week1_summary_markdown,
)
from codesight.worker.supersede_lock import (
    AsyncSupersedeLock,
    PRSupersededError,
    pr_active_sha_key,
)


def _make_auth(client: httpx.AsyncClient | None = None) -> GitHubAuthProvider:
    settings = Settings(_env_file=None, github_token=SecretStr("ghp_unit_test_secret_token"))
    return GitHubAuthProvider(settings, http_client=client)


def test_settings_week1_smoke_inline_comment_flag() -> None:
    default_settings = Settings(_env_file=None)
    assert default_settings.week1_smoke_inline_comment is True

    disabled_settings = Settings(_env_file=None, week1_smoke_inline_comment=False)
    assert disabled_settings.week1_smoke_inline_comment is False


async def test_github_pr_client_fetches_raw_diff_filters_noise_and_respects_config() -> None:
    raw_diff = "\n".join(
        [
            "diff --git a/package-lock.json b/package-lock.json",
            "--- a/package-lock.json",
            "+++ b/package-lock.json",
            "@@ -1 +1,2 @@",
            ' "lockfileVersion": 3,',
            '+ "extra": true',
            "diff --git a/docs/generated/schema.ts b/docs/generated/schema.ts",
            "--- a/docs/generated/schema.ts",
            "+++ b/docs/generated/schema.ts",
            "@@ -1 +1 @@",
            "-export const X = 1;",
            "+export const X = 2;",
            "diff --git a/src/app.py b/src/app.py",
            "--- a/src/app.py",
            "+++ b/src/app.py",
            "@@ -5,2 +5,3 @@ def process():",
            "     x = 1",
            "+    y = x + 2",
            "     return x",
        ]
    )

    seen_accepts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_accepts.append(request.headers.get("accept", ""))
        if request.url.path == "/repos/acme/api/pulls/42":
            return httpx.Response(200, text=raw_diff)
        return httpx.Response(404)

    repo_cfg = RepoConfig(ignore_paths=("docs/generated/**",))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        pr_client = GitHubPullRequestClient(_make_auth(http_client), http_client=http_client)
        fetched = await pr_client.fetch_pull_request_diff(
            "acme/api",
            42,
            repo_config=repo_cfg,
            installation_id=101,
        )

    assert isinstance(fetched, FetchedPullRequestDiff)
    assert fetched.diff_source == "diff"
    assert seen_accepts == ["application/vnd.github.diff"]
    assert fetched.parsed_diff.metrics.total_files == 3
    assert fetched.parsed_diff.metrics.kept_files == 1
    assert fetched.parsed_diff.metrics.skipped_files == 2
    app_file = fetched.parsed_diff.get_file("src/app.py")
    assert app_file is not None
    assert app_file.valid_commentable_lines == {5, 6, 7}


@pytest.mark.parametrize("fallback_reason", ["406", "422", "content_length", "stream_limit"])
async def test_github_pr_client_falls_back_to_paginated_files_api(fallback_reason: str) -> None:
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested_paths.append(f"{request.url.path}?{request.url.query.decode()}")
        if request.url.path == "/repos/acme/api/pulls/7":
            if fallback_reason == "406":
                return httpx.Response(406, json={"message": "Diff too large"})
            if fallback_reason == "422":
                return httpx.Response(422, json={"message": "Too many lines"})
            if fallback_reason == "content_length":
                return httpx.Response(200, headers={"Content-Length": "9999"}, text="unused")

            class _OversizeStream(httpx.AsyncByteStream):
                async def __aiter__(self) -> AsyncIterator[bytes]:
                    yield b"a" * 300
                    yield b"b" * 300

            return httpx.Response(200, stream=_OversizeStream())

        if request.url.path == "/repos/acme/api/pulls/7/files":
            page = request.url.params.get("page", "1")
            if page == "1":
                items = [
                    {
                        "filename": f"src/file_{idx}.py",
                        "status": "modified",
                        "patch": "@@ -1 +1 @@\n-old\n+new\n",
                    }
                    for idx in range(100)
                ]
                return httpx.Response(200, json=items)
            if page == "2":
                return httpx.Response(
                    200,
                    json=[
                        {
                            "filename": "package-lock.json",
                            "status": "modified",
                            "patch": '@@ -1 +1 @@\n-"a":1\n+"a":2\n',
                        },
                        {
                            "filename": "src/final.py",
                            "status": "added",
                            "patch": "@@ -0,0 +1,2 @@\n+def ok():\n+    return 1\n",
                        },
                    ],
                )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        pr_client = GitHubPullRequestClient(
            _make_auth(http_client),
            http_client=http_client,
            max_diff_bytes=512,
        )
        fetched = await pr_client.fetch_pull_request_diff("acme/api", 7)

    assert fetched.diff_source == "files_api"
    assert fetched.parsed_diff.metrics.total_files == 102
    assert fetched.parsed_diff.metrics.kept_files == 101
    assert fetched.parsed_diff.metrics.skipped_files == 1
    assert any("page=1" in path for path in requested_paths)
    assert any("page=2" in path for path in requested_paths)


async def test_github_pr_client_redirect_and_input_validation_guards() -> None:
    def same_origin_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/api/pulls/5":
            return httpx.Response(302, headers={"Location": "/repos/acme/api/pulls/5-canonical"})
        if request.url.path == "/repos/acme/api/pulls/5-canonical":
            return httpx.Response(
                200,
                text=(
                    "diff --git a/src/x.py b/src/x.py\n"
                    "--- a/src/x.py\n"
                    "+++ b/src/x.py\n"
                    "@@ -1 +1 @@\n-a\n+b\n"
                ),
            )
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(same_origin_handler)) as client:
        pr_client = GitHubPullRequestClient(_make_auth(client), http_client=client)
        ok = await pr_client.fetch_pull_request_diff("acme/api", 5)
        assert ok.parsed_diff.get_file("src/x.py") is not None

        with pytest.raises(GitHubAuthConfigError):
            await pr_client.fetch_pull_request_diff("../evil", 5)
        with pytest.raises(ValueError, match="positive integer"):
            await pr_client.fetch_pull_request_diff("acme/api", True)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="positive integer"):
            await pr_client.fetch_pull_request_diff("acme/api", 0)

    def cross_origin_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "https://evil.example/steal-token"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(cross_origin_handler)) as client:
        pr_client = GitHubPullRequestClient(_make_auth(client), http_client=client)
        with pytest.raises(GitHubPublishError, match="cross-origin"):
            await pr_client.fetch_pull_request_diff("acme/api", 5)

    def missing_location_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    async with httpx.AsyncClient(transport=httpx.MockTransport(missing_location_handler)) as client:
        pr_client = GitHubPullRequestClient(_make_auth(client), http_client=client)
        with pytest.raises(GitHubPublishError, match="Location"):
            await pr_client.fetch_pull_request_diff("acme/api", 5)

    def loop_handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"Location": "/repos/acme/api/pulls/5"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(loop_handler)) as client:
        pr_client = GitHubPullRequestClient(_make_auth(client), http_client=client)
        with pytest.raises(GitHubPublishError, match="redirects"):
            await pr_client.fetch_pull_request_diff("acme/api", 5)


async def test_github_pr_client_error_classification_and_http_pool_ownership() -> None:
    mode = "rate_429"

    def handler(request: httpx.Request) -> httpx.Response:
        if mode == "rate_429":
            return httpx.Response(429, headers={"Retry-After": "12"})
        if mode == "rate_403":
            return httpx.Response(403, headers={"X-RateLimit-Remaining": "0", "Retry-After": "6"})
        if mode == "server_503":
            return httpx.Response(503)
        if mode == "timeout":
            raise httpx.ReadTimeout("slow github", request=request)
        if mode == "connect":
            raise httpx.ConnectError("dns fail", request=request)
        if mode == "auth_401":
            return httpx.Response(401)
        if mode == "auth_403":
            return httpx.Response(403)
        if mode == "not_found":
            return httpx.Response(404)
        if mode == "files_bad_json":
            if request.url.path.endswith("/files"):
                return httpx.Response(200, content=b"{bad json")
            return httpx.Response(406)
        if mode == "files_non_list":
            if request.url.path.endswith("/files"):
                return httpx.Response(200, content=json.dumps({"not": "a list"}).encode("utf-8"))
            return httpx.Response(406)
        return httpx.Response(200, text="")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        pr_client = GitHubPullRequestClient(_make_auth(client), http_client=client)

        with pytest.raises(RateLimitTaskError) as rl_exc:
            await pr_client.fetch_pull_request_diff("acme/api", 1)
        assert rl_exc.value.retry_after_seconds == 12.0

        mode = "rate_403"
        with pytest.raises(RateLimitTaskError) as rl403_exc:
            await pr_client.fetch_pull_request_diff("acme/api", 1)
        assert rl403_exc.value.retry_after_seconds == 6.0

        for retryable_mode in ("server_503", "timeout", "connect"):
            mode = retryable_mode
            with pytest.raises(RetryableTaskError):
                await pr_client.fetch_pull_request_diff("acme/api", 1)

        mode = "auth_401"
        with pytest.raises(GitHubAuthError) as auth_401:
            await pr_client.fetch_pull_request_diff("acme/api", 1)
        assert auth_401.value.status_code == 401

        mode = "auth_403"
        with pytest.raises(GitHubAuthError) as auth_403:
            await pr_client.fetch_pull_request_diff("acme/api", 1)
        assert auth_403.value.status_code == 403

        for perm_mode in ("not_found", "files_bad_json", "files_non_list"):
            mode = perm_mode
            with pytest.raises(GitHubPublishError):
                await pr_client.fetch_pull_request_diff("acme/api", 1)

    # HTTP/2 pool ownership lifecycle
    async with GitHubPullRequestClient(_make_auth()) as owned_pr_client:
        c1 = owned_pr_client._ensure_client()
        c2 = owned_pr_client._ensure_client()
        assert c1 is c2
        assert c1.is_closed is False
    assert c1.is_closed is True

    external_client = httpx.AsyncClient()
    async with GitHubPullRequestClient(_make_auth(), http_client=external_client) as ext_pr_client:
        assert ext_pr_client._ensure_client() is external_client
    assert external_client.is_closed is False
    await external_client.aclose()
    with pytest.raises(RuntimeError, match="closed externally"):
        ext_pr_client._ensure_client()


class _InMemoryAsyncRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
        self.store[key] = value
        return True

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if self.store.pop(key, None) is not None:
                removed += 1
        return removed

    async def sismember(self, key: str, member: str) -> int:
        return 1 if member in self.sets.get(key, set()) else 0

    async def sadd(self, key: str, *members: str) -> int:
        bucket = self.sets.setdefault(key, set())
        added = 0
        for m in members:
            if m not in bucket:
                bucket.add(m)
                added += 1
        return added

    async def srem(self, key: str, *members: str) -> int:
        bucket = self.sets.get(key, set())
        removed = 0
        for m in members:
            if m in bucket:
                bucket.remove(m)
                removed += 1
        return removed

    async def scard(self, key: str) -> int:
        return len(self.sets.get(key, set()))

    async def expire(self, key: str, seconds: int) -> bool:
        return key in self.store or key in self.sets


def test_build_week1_smoke_drafts_and_summary_markdown() -> None:
    parser = SmartDiffParser()
    diff_with_add = parser.parse_patch_entries(
        [
            PatchFileEntry(
                filename="package-lock.json",
                status="modified",
                patch='@@ -1 +1 @@\n-"a": 1\n+"a": 2\n',
            ),
            PatchFileEntry(
                filename="src/service.py",
                status="modified",
                patch=(
                    "@@ -10,3 +10,3 @@ def run():\n"
                    "     x = 1\n"
                    "+    secret_line = 'TOP_SECRET_RAW_CODE'\n"
                    "     return x\n"
                ),
            ),
        ]
    )

    assert build_week1_smoke_drafts(diff_with_add, enabled=False) == ()
    drafts = build_week1_smoke_drafts(diff_with_add, enabled=True)
    assert len(drafts) == 1
    assert drafts[0].path == "src/service.py"
    assert drafts[0].line == 11
    assert drafts[0].start_line is None
    assert drafts[0].severity == "INFO"
    assert "TOP_SECRET_RAW_CODE" not in drafts[0].body

    # Context-only hunk falls back to min(valid_commentable_lines)
    context_only = parser.parse_patch_entries(
        [
            PatchFileEntry(
                filename="src/ctx.py",
                status="modified",
                patch="@@ -4,3 +4,2 @@\n     keep_a()\n-    removed()\n     keep_b()\n",
            )
        ]
    )
    ctx_drafts = build_week1_smoke_drafts(context_only, enabled=True)
    assert len(ctx_drafts) == 1
    assert ctx_drafts[0].line == 4

    # Noise-only or deleted-only PR yields zero drafts
    noise_or_deleted = parser.parse_patch_entries(
        [
            PatchFileEntry(
                filename="src/old.py",
                status="removed",
                patch="@@ -1,2 +0,0 @@\n-a\n-b\n",
            ),
            PatchFileEntry(
                filename="package-lock.json",
                status="modified",
                patch='@@ -1 +1 @@\n-"x": 1\n+"x": 2\n',
            ),
        ]
    )
    assert build_week1_smoke_drafts(noise_or_deleted, enabled=True) == ()

    summary = build_week1_summary_markdown(
        diff_with_add,
        head_sha="abcdef1234567890",
        config_hash="0123456789abcdef",
        trace_id="trace-summary-1",
    )
    assert "abcdef123456" in summary
    assert "0123456789abcdef" in summary
    assert "trace-summary-1" in summary
    assert "package-lock.json" in summary
    assert "TOP_SECRET_RAW_CODE" not in summary


async def test_execute_pr_review_lifecycle_on_superseded_hook_covers_all_stages() -> None:
    redis_mem = _InMemoryAsyncRedis()
    lock = AsyncSupersedeLock(redis_mem)  # type: ignore[arg-type]
    cleanup_stages: list[str] = []

    async def _on_superseded(ctx: PRReviewLifecycleContext) -> None:
        cleanup_stages.append(str(ctx.payload["head_sha"]))

    # 1. Superseded inside review_fn
    async def _raise_in_review(_ctx: PRReviewLifecycleContext) -> None:
        raise PRSupersededError(
            repo_id="r1",
            pr_number=1,
            head_sha="sha-rev",
            active_head_sha="sha-new",
            stage="pre_check_run",
        )

    bind_trace_id("trace-hook-1")
    try:
        res_rev = await execute_pr_review_lifecycle(
            {"repo_id": "r1", "repo_full_name": "acme/api", "pr_number": 1, "head_sha": "sha-rev"},
            settings=Settings(_env_file=None),
            supersede_lock=lock,
            review_fn=_raise_in_review,
            on_superseded_fn=_on_superseded,
            check_entry_supersede=False,
        )
        assert res_rev == {"status": "superseded", "trace_id": "trace-hook-1"}
        assert cleanup_stages == ["sha-rev"]

        # 2. Superseded inside publish_fn, plus cleanup failure does not mask superseded return
        async def _raise_in_publish(_ctx: PRReviewLifecycleContext) -> None:
            raise PRSupersededError(
                repo_id="r1",
                pr_number=1,
                head_sha="sha-pub",
                active_head_sha="sha-new",
                stage="pre_422_fallback",
            )

        async def _broken_cleanup(_ctx: PRReviewLifecycleContext) -> None:
            raise RuntimeError("cleanup exploded")

        res_pub = await execute_pr_review_lifecycle(
            {"repo_id": "r1", "repo_full_name": "acme/api", "pr_number": 1, "head_sha": "sha-pub"},
            settings=Settings(_env_file=None),
            supersede_lock=lock,
            publish_fn=_raise_in_publish,
            on_superseded_fn=_broken_cleanup,
            check_entry_supersede=False,
        )
        assert res_pub == {"status": "superseded", "trace_id": "trace-hook-1"}

        # 3. Normal completion preserves 'accepted' contract
        res_ok = await execute_pr_review_lifecycle(
            {"repo_id": "r1", "repo_full_name": "acme/api", "pr_number": 1, "head_sha": "sha-ok"},
            settings=Settings(_env_file=None),
            supersede_lock=lock,
            on_superseded_fn=_on_superseded,
            check_entry_supersede=False,
        )
        assert res_ok == {"status": "accepted", "trace_id": "trace-hook-1"}
    finally:
        clear_trace_context()


async def test_week1_pr_review_runner_happy_path_and_nfr3_log_safety() -> None:
    raw_diff = "\n".join(
        [
            "diff --git a/package-lock.json b/package-lock.json",
            "--- a/package-lock.json",
            "+++ b/package-lock.json",
            "@@ -1 +1,2 @@",
            ' "name": "demo",',
            '+ "version": "1.0.1"',
            "diff --git a/src/service.py b/src/service.py",
            "--- a/src/service.py",
            "+++ b/src/service.py",
            "@@ -10,2 +10,3 @@ def charge():",
            "     amount = 10",
            "+    secret_code = 'NFR3_MUST_NOT_LEAK_IN_LOGS'",
            "     return amount",
        ]
    )
    gh_calls: list[tuple[str, str, dict[str, Any]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else {}
        gh_calls.append((request.method, request.url.path, body))
        if request.url.path == "/repos/acme/api/contents/.codesight.yaml":
            return httpx.Response(404)
        if request.url.path == "/repos/acme/api/contents/.codesight.yml":
            return httpx.Response(404)
        if request.url.path == "/repos/acme/api/check-runs" and request.method == "POST":
            return httpx.Response(
                201,
                json={"id": 3001, "name": body["name"], "status": "in_progress"},
            )
        if request.url.path == "/repos/acme/api/pulls/9" and request.method == "GET":
            return httpx.Response(200, text=raw_diff)
        if request.url.path == "/repos/acme/api/pulls/9/reviews" and request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "id": 8801,
                    "state": "COMMENTED",
                    "html_url": "https://github.com/acme/api/pull/9#r",
                },
            )
        if request.url.path == "/repos/acme/api/check-runs/3001" and request.method == "PATCH":
            return httpx.Response(
                200,
                json={"id": 3001, "status": "completed", "conclusion": body["conclusion"]},
            )
        return httpx.Response(404)

    redis_mem = _InMemoryAsyncRedis()
    redis_mem.store[pr_active_sha_key("repo-1", 9)] = "headsha1111"
    log_stream = io.StringIO()
    configure_logging(level="INFO", service_name="test-runner", stream=log_stream)
    bind_trace_id("trace-runner-ok")
    try:
        settings = Settings(
            _env_file=None,
            github_token=SecretStr("ghp_runner_secret_token"),
            week1_smoke_inline_comment=True,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runner = Week1PRReviewRunner(
                settings=settings,
                http_client=client,
                redis_client=redis_mem,  # type: ignore[arg-type]
            )
            res = await runner.execute(
                {
                    "delivery_id": "trace-runner-ok",
                    "repo_id": "repo-1",
                    "repo_full_name": "acme/api",
                    "default_branch": "main",
                    "pr_number": 9,
                    "head_sha": "headsha1111",
                    "action": "opened",
                }
            )
    finally:
        clear_trace_context()

    assert res == {"status": "completed", "trace_id": "trace-runner-ok"}
    review_posts = [c for c in gh_calls if c[1] == "/repos/acme/api/pulls/9/reviews"]
    assert len(review_posts) == 1
    assert review_posts[0][2]["event"] == "COMMENT"
    assert review_posts[0][2]["comments"] == [
        {
            "path": "src/service.py",
            "line": 11,
            "side": "RIGHT",
            "body": review_posts[0][2]["comments"][0]["body"],
        }
    ]
    patch_calls = [c for c in gh_calls if c[0] == "PATCH"]
    assert len(patch_calls) == 1
    assert patch_calls[0][2]["conclusion"] == "success"

    raw_logs = log_stream.getvalue()
    assert "trace-runner-ok" in raw_logs
    assert "ghp_runner_secret_token" not in raw_logs
    assert "NFR3_MUST_NOT_LEAK_IN_LOGS" not in raw_logs


async def test_week1_pr_review_runner_supersede_cleanup_and_error_finalization() -> None:
    raw_diff = (
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1 +1 @@\n"
        "-x = 1\n"
        "+x = 2\n"
    )
    redis_mem = _InMemoryAsyncRedis()

    async def _run_scenario(
        mode: str,
    ) -> tuple[dict[str, str] | BaseException, list[tuple[str, str, dict[str, Any]]]]:
        calls: list[tuple[str, str, dict[str, Any]]] = []
        redis_mem.store[pr_active_sha_key("repo-2", 3)] = "sha-old"

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            calls.append((request.method, request.url.path, body))
            if request.url.path.endswith("/contents/.codesight.yaml") or request.url.path.endswith(
                "/contents/.codesight.yml"
            ):
                return httpx.Response(404)
            if request.url.path == "/repos/acme/api/check-runs" and request.method == "POST":
                return httpx.Response(201, json={"id": 4401, "status": "in_progress"})
            if request.url.path == "/repos/acme/api/pulls/3" and request.method == "GET":
                if mode == "supersede_pre_publish":
                    redis_mem.store[pr_active_sha_key("repo-2", 3)] = "sha-newer"
                    return httpx.Response(200, text=raw_diff)
                if mode == "perm_404":
                    return httpx.Response(404)
                if mode == "rate_429":
                    return httpx.Response(429, headers={"Retry-After": "5"})
                return httpx.Response(200, text=raw_diff)
            if request.url.path == "/repos/acme/api/pulls/3/reviews" and request.method == "POST":
                if mode == "supersede_422_fallback":
                    redis_mem.store[pr_active_sha_key("repo-2", 3)] = "sha-newer-422"
                    return httpx.Response(422, json={"message": "Unprocessable Entity"})
                return httpx.Response(200, json={"id": 1, "state": "COMMENTED"})
            if request.url.path == "/repos/acme/api/check-runs/4401" and request.method == "PATCH":
                return httpx.Response(200, json={"id": 4401, "status": "completed"})
            return httpx.Response(404)

        settings = Settings(_env_file=None, github_token=SecretStr("ghp_runner_test"))
        payload = {
            "delivery_id": f"deliv-{mode}",
            "repo_id": "repo-2",
            "repo_full_name": "acme/api",
            "default_branch": "main",
            "pr_number": 3,
            "head_sha": "sha-old",
            "action": "opened",
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runner = Week1PRReviewRunner(
                settings=settings,
                http_client=client,
                redis_client=redis_mem,  # type: ignore[arg-type]
            )
            try:
                res = await runner.execute(payload)
                return res, calls
            except BaseException as exc:
                return exc, calls

    # 1. Superseded between review_fn and publish_fn -> 0 review POSTs, 1 PATCH neutral
    res1, calls1 = await _run_scenario("supersede_pre_publish")
    assert isinstance(res1, dict) and res1["status"] == "superseded"
    assert not any(c[1].endswith("/reviews") for c in calls1)
    patches1 = [c for c in calls1 if c[0] == "PATCH"]
    assert len(patches1) == 1 and patches1[0][2]["conclusion"] == "neutral"

    # 2. Superseded inside publisher 422 fallback -> already PATCHed once (no duplicate PATCH)
    res2, calls2 = await _run_scenario("supersede_422_fallback")
    assert isinstance(res2, dict) and res2["status"] == "superseded"
    patches2 = [c for c in calls2 if c[0] == "PATCH"]
    assert len(patches2) == 1 and patches2[0][2]["conclusion"] == "neutral"

    # 3. Non-retryable 404 after Check Run creation -> PATCH failure + re-raises GitHubPublishError
    exc3, calls3 = await _run_scenario("perm_404")
    assert isinstance(exc3, GitHubPublishError)
    patches3 = [c for c in calls3 if c[0] == "PATCH"]
    assert len(patches3) == 1 and patches3[0][2]["conclusion"] == "failure"

    # 4. Retryable 429 after Check Run creation -> PATCH neutral + re-raises RateLimitTaskError
    exc4, calls4 = await _run_scenario("rate_429")
    assert isinstance(exc4, RateLimitTaskError)
    patches4 = [c for c in calls4 if c[0] == "PATCH"]
    assert len(patches4) == 1 and patches4[0][2]["conclusion"] == "neutral"
    assert "Retry Scheduled" in patches4[0][2]["output"]["title"]


async def test_run_pr_webhook_task_stub_fallback() -> None:
    bind_trace_id("trace-stub-fallback")
    try:
        res = await run_pr_webhook_task(
            {"repo_full_name": "org/stub", "pr_number": 5},
            settings=Settings(_env_file=None),
        )
        assert res == {"status": "accepted", "trace_id": "trace-stub-fallback"}
    finally:
        clear_trace_context()


async def test_pr_review_runner_contains_no_bare_assert_and_raises_runtime_error_if_diff_unset(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    runner_path = Path("src/codesight/worker/pr_review_runner.py")
    runner_source = await asyncio.to_thread(runner_path.read_text, encoding="utf-8")
    tree = ast.parse(runner_source)
    assert [n for n in ast.walk(tree) if isinstance(n, ast.Assert)] == []

    async def _skip_review_lifecycle(
        payload: dict[str, Any],
        *,
        publish_fn: Any = None,
        **_: Any,
    ) -> dict[str, str]:
        ctx = PRReviewLifecycleContext(
            payload=payload,
            trace_id="trace-unset-diff",
            workspace_dir=tmp_path,
            repo_config=RepoConfig(),
            supersede_lock=None,
        )
        assert publish_fn is not None
        await publish_fn(ctx)
        return {"status": "completed", "trace_id": "trace-unset-diff"}

    monkeypatch.setattr(
        "codesight.worker.pr_review_runner.execute_pr_review_lifecycle",
        _skip_review_lifecycle,
    )
    redis_mem = _InMemoryAsyncRedis()
    settings = Settings(_env_file=None, github_token=SecretStr("ghp_runner_test"))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _r: httpx.Response(200))
    ) as client:
        runner = Week1PRReviewRunner(
            settings=settings,
            http_client=client,
            redis_client=redis_mem,  # type: ignore[arg-type]
        )
        with pytest.raises(RuntimeError, match="fetched_diff"):
            await runner.execute(
                {
                    "delivery_id": "trace-unset-diff",
                    "repo_id": "repo-1",
                    "repo_full_name": "acme/api",
                    "default_branch": "main",
                    "pr_number": 4,
                    "head_sha": "sha-unset",
                }
            )


async def test_runner_422_already_completed_check_run_on_supersede_no_failure_patch() -> None:
    raw_diff = (
        "diff --git a/src/a.py b/src/a.py\n"
        "--- a/src/a.py\n"
        "+++ b/src/a.py\n"
        "@@ -1 +1 @@\n"
        "-x = 1\n"
        "+x = 2\n"
    )

    async def _run_case(patch_statuses: list[int]) -> tuple[dict[str, str], list[dict[str, Any]]]:
        redis_mem = _InMemoryAsyncRedis()
        redis_mem.store[pr_active_sha_key("repo-422", 8)] = "sha-old"
        patch_bodies: list[dict[str, Any]] = []
        status_iter = iter(patch_statuses)

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content.decode("utf-8")) if request.content else {}
            if request.url.path.endswith("/contents/.codesight.yaml") or request.url.path.endswith(
                "/contents/.codesight.yml"
            ):
                return httpx.Response(404)
            if request.url.path == "/repos/acme/api/check-runs" and request.method == "POST":
                return httpx.Response(201, json={"id": 4401, "status": "in_progress"})
            if request.url.path == "/repos/acme/api/pulls/8" and request.method == "GET":
                return httpx.Response(200, text=raw_diff)
            if request.url.path == "/repos/acme/api/pulls/8/reviews" and request.method == "POST":
                redis_mem.store[pr_active_sha_key("repo-422", 8)] = "sha-newer"
                return httpx.Response(422, json={"message": "Unprocessable Entity"})
            if request.url.path == "/repos/acme/api/check-runs/4401" and request.method == "PATCH":
                patch_bodies.append(body)
                code = next(status_iter, 200)
                if code == 422:
                    return httpx.Response(422, json={"message": "Already completed"})
                if code == 503:
                    return httpx.Response(503, json={"message": "Service Unavailable"})
                return httpx.Response(200, json={"id": 4401, "status": "completed"})
            return httpx.Response(404)

        settings = Settings(_env_file=None, github_token=SecretStr("ghp_runner_test"))
        payload = {
            "delivery_id": "deliv-422-checkrun",
            "repo_id": "repo-422",
            "repo_full_name": "acme/api",
            "default_branch": "main",
            "pr_number": 8,
            "head_sha": "sha-old",
            "action": "opened",
        }
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            runner = Week1PRReviewRunner(
                settings=settings,
                http_client=client,
                redis_client=redis_mem,  # type: ignore[arg-type]
            )
            res = await runner.execute(payload)
        return res, patch_bodies

    res_422, patches_422 = await _run_case([422])
    assert res_422["status"] == "superseded"
    assert len(patches_422) == 1
    assert patches_422[0]["conclusion"] == "neutral"

    res_503, patches_503 = await _run_case([503, 200])
    assert res_503["status"] == "superseded"
    assert len(patches_503) == 2
    assert all(p["conclusion"] == "neutral" for p in patches_503)


def test_worker_modules_have_acyclic_top_level_imports() -> None:
    module_paths = (
        Path("src/codesight/worker/lifecycle.py"),
        Path("src/codesight/worker/pr_review_runner.py"),
        Path("src/codesight/worker/celery_app.py"),
    )
    for path in module_paths:
        assert path.exists(), f"Missing expected worker module {path}"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        nested_imports: list[int] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for child in ast.walk(node):
                    if isinstance(child, (ast.Import, ast.ImportFrom)):
                        nested_imports.append(child.lineno)
        assert nested_imports == [], f"Found function-level imports in {path}: {nested_imports}"

    forward = subprocess.run(
        [
            sys.executable,
            "-c",
            "import codesight.worker.lifecycle, codesight.worker.pr_review_runner, "
            "codesight.worker.celery_app",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert forward.returncode == 0, forward.stderr

    reverse = subprocess.run(
        [
            sys.executable,
            "-c",
            "import codesight.worker.pr_review_runner, codesight.worker.celery_app, "
            "codesight.worker.lifecycle",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert reverse.returncode == 0, reverse.stderr


async def test_github_pr_client_and_smoke_drafts_handle_no_newline_at_eof_in_both_modes() -> None:
    raw_diff = "\n".join(
        [
            "diff --git a/src/no_nl.py b/src/no_nl.py",
            "--- a/src/no_nl.py",
            "+++ b/src/no_nl.py",
            "@@ -5,2 +5,2 @@",
            " keep_a()",
            "-old_eof()",
            "\\ No newline at end of file",
            "+new_eof()",
            "\\ No newline at end of file",
            "@@ -12 +12,2 @@",
            " tail_ctx()",
            "+tail_added()",
            "\\ No newline at end of file",
        ]
    )
    patch_body = "\n".join(
        [
            "@@ -5,2 +5,2 @@",
            " keep_a()",
            "-old_eof()",
            "\\ No newline at end of file",
            "+new_eof()",
            "\\ No newline at end of file",
            "@@ -12 +12,2 @@",
            " tail_ctx()",
            "+tail_added()",
            "\\ No newline at end of file",
        ]
    )

    for mode in ("diff", "files_api"):

        def handler(request: httpx.Request, *, active_mode: str = mode) -> httpx.Response:
            if request.url.path == "/repos/acme/app/pulls/19" and request.method == "GET":
                if active_mode == "diff":
                    return httpx.Response(200, text=raw_diff)
                return httpx.Response(406)
            if request.url.path == "/repos/acme/app/pulls/19/files" and request.method == "GET":
                return httpx.Response(
                    200,
                    json=[{"filename": "src/no_nl.py", "status": "modified", "patch": patch_body}],
                )
            return httpx.Response(404)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            pr_client = GitHubPullRequestClient(_make_auth(client), http_client=client)
            fetched = await pr_client.fetch_pull_request_diff("acme/app", 19)

        assert fetched.diff_source == mode
        parsed_file = fetched.parsed_diff.get_file("src/no_nl.py")
        assert parsed_file is not None
        assert parsed_file.valid_commentable_lines == {5, 6, 12, 13}
        assert 7 not in parsed_file.valid_commentable_lines
        assert 14 not in parsed_file.valid_commentable_lines
        no_nl_lines = [
            ln for hunk in parsed_file.hunks for ln in hunk.lines if ln.kind == "no_newline"
        ]
        assert len(no_nl_lines) == 3
        assert all(ln.left_line is None and ln.right_line is None for ln in no_nl_lines)

        drafts = build_week1_smoke_drafts(fetched.parsed_diff, enabled=True)
        assert len(drafts) == 1
        assert drafts[0].path == "src/no_nl.py"
        assert drafts[0].line == 6
        assert parsed_file.is_line_commentable(drafts[0].line) is True


def test_worker_async_runtime_reuses_pools_preserves_trace_id_and_shuts_down_cleanly() -> None:
    from codesight.worker.celery_app import _WorkerAsyncRuntime

    runtime = _WorkerAsyncRuntime()
    try:

        async def _probe_loop_and_trace() -> tuple[int, str | None]:
            return id(asyncio.get_running_loop()), get_trace_id()

        bind_trace_id("trace-pool-1")
        try:
            loop_id_1, seen_trace_1 = runtime.run_coro(_probe_loop_and_trace())
        finally:
            clear_trace_context()

        bind_trace_id("trace-pool-2")
        try:
            loop_id_2, seen_trace_2 = runtime.run_coro(_probe_loop_and_trace())
        finally:
            clear_trace_context()

        assert loop_id_1 == loop_id_2
        assert seen_trace_1 == "trace-pool-1"
        assert seen_trace_2 == "trace-pool-2"
        assert runtime._thread is not None and runtime._thread.is_alive()

        settings = Settings(_env_file=None, github_token=SecretStr("ghp_pooled_secret"))

        async def _check_pools() -> tuple[Any, Any, Any, Any, Any, Any]:
            http1, redis1, sf1 = await runtime._ensure_pools(settings)
            http2, redis2, sf2 = await runtime._ensure_pools(settings)
            return http1, redis1, sf1, http2, redis2, sf2

        http1, redis1, sf1, http2, redis2, sf2 = runtime.run_coro(_check_pools())
        assert http1 is http2 and not http1.is_closed
        assert redis1 is redis2
        assert sf1 is sf2

        thread_ref = runtime._thread
        runtime.shutdown()
        assert http1.is_closed is True
        assert runtime._loop is None
        assert runtime._thread is None
        assert thread_ref is not None and not thread_ref.is_alive()

        # Even with an injected/pooled http_client, unconfigured credentials keep stub fallback
        res_stub = runtime.run_coro(
            run_pr_webhook_task(
                {
                    "repo_id": "repo-stub",
                    "repo_full_name": "org/stub",
                    "pr_number": 5,
                    "head_sha": "sha-stub",
                },
                settings=Settings(_env_file=None),
                http_client=http1,
            )
        )
        assert res_stub["status"] == "accepted"
    finally:
        runtime.shutdown()


async def test_runner_owned_http_aclose_error_does_not_mask_primary_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_send(
        self: httpx.AsyncClient, request: httpx.Request, **_: Any
    ) -> httpx.Response:
        return httpx.Response(404, request=request, json={"message": "Not Found"})

    async def _broken_aclose(self: httpx.AsyncClient) -> None:
        raise OSError("socket close failed")

    monkeypatch.setattr(httpx.AsyncClient, "send", _fake_send)
    monkeypatch.setattr(httpx.AsyncClient, "aclose", _broken_aclose)

    redis_mem = _InMemoryAsyncRedis()
    redis_mem.store[pr_active_sha_key("repo-aclose", 11)] = "sha-aclose"
    settings = Settings(_env_file=None, github_token=SecretStr("ghp_runner_test"))
    runner = Week1PRReviewRunner(
        settings=settings,
        http_client=None,
        redis_client=redis_mem,  # type: ignore[arg-type]
    )
    with pytest.raises(GitHubPublishError) as exc_info:
        await runner.execute(
            {
                "delivery_id": "deliv-aclose",
                "repo_id": "repo-aclose",
                "repo_full_name": "acme/api",
                "default_branch": "main",
                "pr_number": 11,
                "head_sha": "sha-aclose",
            }
        )
    assert exc_info.value.status_code == 404


async def test_runner_records_audit_via_injected_session_factory_and_tolerates_db_errors() -> None:
    assert "persist_audit" not in inspect.signature(Week1PRReviewRunner.__init__).parameters
    assert "persist_audit" not in inspect.signature(run_pr_webhook_task).parameters

    import uuid

    from codesight.db.models import PRReview

    repo_uuid = str(uuid.uuid4())

    class _FakeSession:
        def __init__(self, *, fail_commit: bool = False) -> None:
            self.executed: list[Any] = []
            self.added: list[Any] = []
            self.committed = False
            self.fail_commit = fail_commit

        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, stmt: Any) -> Any:
            self.executed.append(stmt)
            return None

        async def scalar(self, _stmt: Any) -> uuid.UUID:
            return uuid.UUID(repo_uuid)

        def add(self, obj: Any) -> None:
            self.added.append(obj)

        async def commit(self) -> None:
            if self.fail_commit:
                raise RuntimeError("db down")
            self.committed = True

    sessions_created: list[_FakeSession] = []
    fail_flag = False

    def _fake_session_factory() -> _FakeSession:
        sess = _FakeSession(fail_commit=fail_flag)
        sessions_created.append(sess)
        return sess

    raw_diff = (
        "diff --git a/src/app.py b/src/app.py\n"
        "--- a/src/app.py\n"
        "+++ b/src/app.py\n"
        "@@ -1 +1 @@\n"
        "-a = 1\n"
        "+a = 2\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/contents/.codesight.yaml") or request.url.path.endswith(
            "/contents/.codesight.yml"
        ):
            return httpx.Response(404)
        if request.url.path == "/repos/acme/api/check-runs" and request.method == "POST":
            return httpx.Response(201, json={"id": 5501, "status": "in_progress"})
        if request.url.path == "/repos/acme/api/pulls/15" and request.method == "GET":
            return httpx.Response(200, text=raw_diff)
        if request.url.path == "/repos/acme/api/pulls/15/reviews" and request.method == "POST":
            return httpx.Response(200, json={"id": 5502, "state": "COMMENTED"})
        if request.url.path == "/repos/acme/api/check-runs/5501" and request.method == "PATCH":
            return httpx.Response(200, json={"id": 5501, "status": "completed"})
        return httpx.Response(404)

    redis_mem = _InMemoryAsyncRedis()
    redis_mem.store[pr_active_sha_key(repo_uuid, 15)] = "sha-audit-1"
    settings = Settings(_env_file=None, github_token=SecretStr("ghp_runner_test"))
    payload = {
        "delivery_id": "deliv-audit-ok",
        "repo_id": repo_uuid,
        "repo_full_name": "acme/api",
        "default_branch": "main",
        "pr_number": 15,
        "head_sha": "sha-audit-1",
    }

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        runner = Week1PRReviewRunner(
            settings=settings,
            http_client=client,
            session_factory=_fake_session_factory,  # type: ignore[arg-type]
            redis_client=redis_mem,  # type: ignore[arg-type]
        )
        res_ok = await runner.execute(payload)
        assert res_ok["status"] == "completed"
        assert len(sessions_created) == 1
        sess_ok = sessions_created[0]
        assert sess_ok.committed is True
        assert len(sess_ok.executed) == 1
        assert len(sess_ok.added) == 1
        assert isinstance(sess_ok.added[0], PRReview)
        assert sess_ok.added[0].status == "completed"
        assert sess_ok.added[0].info_count == 1

        # DB failure during audit is logged cleanly without failing the review
        fail_flag = True
        log_stream = io.StringIO()
        configure_logging(level="WARNING", service_name="test-audit-err", stream=log_stream)
        res_db_err = await runner.execute({**payload, "delivery_id": "deliv-audit-db-err"})
        assert res_db_err["status"] == "completed"
        assert "worker_audit_persist_failed" in log_stream.getvalue()
