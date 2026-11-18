"""Unit tests for GitHubReviewPublisher, Checks API lifecycle, and Zero HTTP 422 guard."""

from __future__ import annotations

import io
import json
import typing
from typing import Any

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from codesight.core.config import Settings
from codesight.core.errors import RateLimitTaskError, RetryableTaskError
from codesight.core.logging import bind_trace_id, clear_trace_context, configure_logging
from codesight.diff.models import PatchFileEntry
from codesight.diff.smart_diff_parser import SmartDiffParser
from codesight.vcs.github_auth import GitHubAuthError, GitHubAuthProvider
from codesight.vcs.github_publisher import (
    OUT_OF_HUNK_HEADING,
    GitHubPublishError,
    GitHubReviewPublisher,
    ReviewCommentDraft,
    partition_review_comments,
)
from codesight.worker.celery_app import classify_retryable_exception
from codesight.worker.supersede_lock import AsyncSupersedeLock, PRSupersededError
from tests.conftest import load_guard_validator


def _make_sample_diff() -> tuple[str, Any]:
    raw_diff = "\n".join(
        [
            "diff --git a/src/auth.py b/src/auth.py",
            "--- a/src/auth.py",
            "+++ b/src/auth.py",
            "@@ -10 +10 @@",
            "-old_flag = False",
            "\\ No newline at end of file",
            "+old_flag = True",
            "@@ -20,3 +20,4 @@ def login(user):",
            "     if user is None:",
            "-        return False",
            "+        raise ValueError('missing')",
            "+    audit(user)",
            "     return True",
            "diff --git a/src/removed.py b/src/removed.py",
            "deleted file mode 100644",
            "--- a/src/removed.py",
            "+++ /dev/null",
            "@@ -1,2 +0,0 @@",
            "-dead = 1",
            "-dead2 = 2",
            "diff --git a/package-lock.json b/package-lock.json",
            "--- a/package-lock.json",
            "+++ b/package-lock.json",
            "@@ -1 +1 @@",
            '-"a": 1',
            '+"a": 2',
        ]
    )
    parsed = SmartDiffParser().parse_diff(raw_diff)
    return raw_diff, parsed


def _make_auth(client: httpx.AsyncClient | None = None) -> GitHubAuthProvider:
    settings = Settings(github_token=SecretStr("ghp_test_secret_token_should_not_leak"))
    return GitHubAuthProvider(settings, http_client=client)


def test_partition_review_comments_guards_422_and_matches_oracle() -> None:
    raw_diff, parsed = _make_sample_diff()
    guard = load_guard_validator()
    oracle_map = guard.map_right_hunk_lines(raw_diff)

    drafts = [
        # 1. Valid single-line (start_line=None) -> omit start_line/start_side
        ReviewCommentDraft(
            path="./src/auth.py",
            line=10,
            body="Prefer explicit config check.",
            severity="INFO",
        ),
        # 2. Valid single-line where start_line == line -> normalize & omit start_line/start_side
        ReviewCommentDraft(
            path="src/auth.py",
            start_line=21,
            line=21,
            body="Avoid bare ValueError here.",
            severity="WARNING",
        ),
        # 3. Valid multi-line inside same hunk (hunk 2) -> keep start_line & start_side
        ReviewCommentDraft(
            path="src/auth.py",
            start_line=21,
            line=22,
            body="```suggestion\n        raise PermissionError('missing')\n    audit(user)\n```",
            severity="CRITICAL",
        ),
        # 4. Out-of-hunk line -> downgrade
        ReviewCommentDraft(
            path="src/auth.py",
            line=99,
            body="Out-of-hunk finding.",
            severity="WARNING",
        ),
        # 5. Cross-hunk range (10 in hunk 1, 21 in hunk 2) -> downgrade
        ReviewCommentDraft(
            path="src/auth.py",
            start_line=10,
            line=21,
            body=(
                "Cross-hunk observation.\n```Suggestion\nbad_patch()\n```\n"
                "~~~suggestion\nalt()\n~~~"
            ),
            severity="WARNING",
        ),
        # 6. Inverted range (start_line > line) -> downgrade
        ReviewCommentDraft(
            path="src/auth.py",
            start_line=22,
            line=21,
            body="Inverted range finding.",
        ),
        # 7. File-level finding (line=None) -> downgrade
        ReviewCommentDraft(
            path="src/auth.py",
            line=None,
            body="Add module-level docstring.",
        ),
        # 8. Deleted file -> downgrade
        ReviewCommentDraft(
            path="src/removed.py",
            line=1,
            body="Deleted file finding.",
        ),
        # 9. Skipped lockfile -> downgrade
        ReviewCommentDraft(
            path="package-lock.json",
            line=1,
            body="Skipped file finding.",
        ),
    ]

    prepared = partition_review_comments(
        parsed,
        drafts,
        summary_body="## Summary\nAutomated analysis complete.",
    )

    assert len(prepared.inline_comments) == 3
    assert prepared.inline_comments[0] == {
        "path": "src/auth.py",
        "line": 10,
        "side": "RIGHT",
        "body": "Prefer explicit config check.",
    }
    assert prepared.inline_comments[1] == {
        "path": "src/auth.py",
        "line": 21,
        "side": "RIGHT",
        "body": "Avoid bare ValueError here.",
    }
    assert prepared.inline_comments[2] == {
        "path": "src/auth.py",
        "start_line": 21,
        "start_side": "RIGHT",
        "line": 22,
        "side": "RIGHT",
        "body": "```suggestion\n        raise PermissionError('missing')\n    audit(user)\n```",
    }

    assert [d.reason for d in prepared.downgraded_comments] == [
        "line_outside_hunk",
        "cross_hunk_range",
        "invalid_range",
        "file_level",
        "deleted_file",
        "file_not_in_diff",
    ]
    assert OUT_OF_HUNK_HEADING in prepared.body
    assert "Out-of-hunk finding." in prepared.body
    assert "Cross-hunk observation." in prepared.body
    assert "```suggestion" not in prepared.body.lower()
    assert "~~~suggestion" not in prepared.body.lower()
    assert "bad_patch()" in prepared.body
    assert "alt()" in prepared.body
    assert guard.validate_review_comments(oracle_map, list(prepared.inline_comments)) == []


def test_draft_rejects_bool_lines_and_empty_review_gets_fallback_body() -> None:
    with pytest.raises(ValidationError):
        ReviewCommentDraft(path="src/auth.py", line=True, body="bad")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        ReviewCommentDraft(path="src/auth.py", start_line=False, line=10, body="bad")  # type: ignore[arg-type]

    parsed = SmartDiffParser().parse_patch_entries(
        [PatchFileEntry(filename="src/a.py", status="modified", patch="@@ -1 +1 @@\n-a\n+b\n")]
    )
    prepared = partition_review_comments(parsed, (), summary_body="   ")
    assert prepared.inline_comments == ()
    assert prepared.body.strip() != ""


async def test_publisher_check_run_lifecycle_and_single_batch_review_post() -> None:
    requests_log: list[tuple[str, str, dict[str, Any], dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8")) if request.content else {}
        requests_log.append((request.method, str(request.url), payload, dict(request.headers)))
        if request.method == "POST" and request.url.path == "/repos/acme/app/check-runs":
            return httpx.Response(
                201,
                json={
                    "id": 9001,
                    "name": payload["name"],
                    "status": "in_progress",
                    "conclusion": None,
                    "html_url": "https://github.com/acme/app/runs/9001",
                },
            )
        if request.method == "POST" and request.url.path == "/repos/acme/app/pulls/7/reviews":
            return httpx.Response(
                200,
                json={
                    "id": 5501,
                    "html_url": "https://github.com/acme/app/pull/7#pullrequestreview-5501",
                    "state": "COMMENTED",
                },
            )
        if request.method == "PATCH" and request.url.path == "/repos/acme/app/check-runs/9001":
            return httpx.Response(
                200,
                json={
                    "id": 9001,
                    "name": "CodeSight-AI Review",
                    "status": "completed",
                    "conclusion": payload["conclusion"],
                    "html_url": "https://github.com/acme/app/runs/9001",
                },
            )
        return httpx.Response(404, json={"message": "Not Found"})

    _, parsed = _make_sample_diff()
    stream = io.StringIO()
    configure_logging(level="DEBUG", service_name="test-publisher", stream=stream)
    bind_trace_id("trace-ca9-batch")
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            publisher = GitHubReviewPublisher(_make_auth(client), http_client=client)
            created = await publisher.create_check_run("acme/app", head_sha="deadbeef")
            assert created.check_run_id == 9001
            assert created.status == "in_progress"

            review = await publisher.publish_review(
                "acme/app",
                7,
                head_sha="deadbeef",
                parsed_diff=parsed,
                drafts=[
                    ReviewCommentDraft(path="src/auth.py", line=10, body="SECRET_RAW_CODE_CHECK"),
                    ReviewCommentDraft(path="src/auth.py", line=777, body="Outside hunk body"),
                ],
                summary_body="Overall LGTM with one note.",
                event="COMMENT",
            )
            assert review.review_id == 5501
            assert review.inline_count == 1
            assert review.downgraded_count == 1
            assert review.used_422_fallback is False

            for conclusion in ("success", "neutral", "failure"):
                completed = await publisher.complete_check_run(
                    "acme/app",
                    created.check_run_id,
                    conclusion=conclusion,
                    title="CodeSight Summary",
                    summary="x" * 70_000,
                    text="y" * 70_000,
                )
                assert completed.status == "completed"
                assert completed.conclusion == conclusion
    finally:
        clear_trace_context()

    # Exactly 1 batch POST to /pulls/7/reviews
    review_posts = [r for r in requests_log if r[1].endswith("/pulls/7/reviews")]
    assert len(review_posts) == 1
    _, _, body_sent, headers_sent = review_posts[0]
    assert body_sent["commit_id"] == "deadbeef"
    assert body_sent["event"] == "COMMENT"
    assert body_sent["comments"] == [
        {
            "path": "src/auth.py",
            "line": 10,
            "side": "RIGHT",
            "body": "SECRET_RAW_CODE_CHECK",
        }
    ]
    assert OUT_OF_HUNK_HEADING in body_sent["body"]
    assert "Outside hunk body" in body_sent["body"]
    assert headers_sent["authorization"] == "Bearer ghp_test_secret_token_should_not_leak"

    # Check output truncation protects 65,535-char GitHub Checks limit
    patch_req = next(r for r in requests_log if r[0] == "PATCH")
    assert len(patch_req[2]["output"]["summary"]) <= 65_535
    assert len(patch_req[2]["output"]["text"]) <= 65_535

    # NFR-3: logs never contain comment bodies or tokens
    raw_logs = stream.getvalue()
    assert "SECRET_RAW_CODE_CHECK" not in raw_logs
    assert "ghp_test_secret_token_should_not_leak" not in raw_logs
    assert "trace-ca9-batch" in raw_logs


async def test_publisher_reactive_422_fallback_and_non_recoverable_422() -> None:
    attempt_payloads: list[dict[str, Any]] = []
    mode = "recover"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        attempt_payloads.append(payload)
        if mode == "recover" and payload.get("comments"):
            return httpx.Response(422, json={"message": "Unprocessable Entity"})
        if mode == "always_422":
            return httpx.Response(422, json={"message": "Invalid commit_id"})
        return httpx.Response(
            200,
            json={"id": 888, "html_url": "https://github.com/o/r/pull/1#r", "state": "COMMENTED"},
        )

    _, parsed = _make_sample_diff()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = GitHubReviewPublisher(_make_auth(client), http_client=client)
        res = await publisher.publish_review(
            "o/r",
            1,
            head_sha="abc123",
            parsed_diff=parsed,
            drafts=[
                ReviewCommentDraft(
                    path="src/auth.py",
                    line=10,
                    body="Inline candidate.",
                    severity="CRITICAL",
                )
            ],
            summary_body="Review summary.",
        )
        assert res.used_422_fallback is True
        assert res.inline_count == 0
        assert res.downgraded_count == 1
        assert len(attempt_payloads) == 2
        assert attempt_payloads[1]["comments"] == []
        assert OUT_OF_HUNK_HEADING in attempt_payloads[1]["body"]
        assert "**[CRITICAL]**" in attempt_payloads[1]["body"]
        assert "Inline candidate." in attempt_payloads[1]["body"]

        mode = "always_422"
        with pytest.raises(GitHubPublishError) as exc_info:
            await publisher.publish_review(
                "o/r",
                1,
                head_sha="abc123",
                parsed_diff=parsed,
                drafts=[],
                summary_body="Empty inline",
            )
        assert exc_info.value.status_code == 422


async def test_publisher_pre_publish_supersede_aborts_before_http_and_error_mapping() -> None:
    called_urls: list[str] = []
    response_mode = "ok"

    def handler(request: httpx.Request) -> httpx.Response:
        called_urls.append(str(request.url))
        if response_mode == "rate_limit":
            return httpx.Response(429, headers={"Retry-After": "14"}, json={"message": "slow"})
        if response_mode == "secondary_403":
            return httpx.Response(403, headers={"Retry-After": "9"}, json={"message": "abuse"})
        if response_mode == "auth_401":
            return httpx.Response(401, json={"message": "Bad credentials"})
        if response_mode == "server_503":
            return httpx.Response(503, json={"message": "Unavailable"})
        if response_mode == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(404, json={"message": "Missing"})

    class _FakeRedis:
        async def get(self, _key: str) -> str:
            return "newer_sha_999"

    _, parsed = _make_sample_diff()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = GitHubReviewPublisher(_make_auth(client), http_client=client)
        lock = AsyncSupersedeLock(_FakeRedis())  # type: ignore[arg-type]

        with pytest.raises(PRSupersededError) as sup_exc:
            await publisher.publish_review(
                "acme/app",
                42,
                head_sha="stale_sha_111",
                parsed_diff=parsed,
                drafts=[ReviewCommentDraft(path="src/auth.py", line=10, body="Note")],
                supersede_lock=lock,
                repo_id="repo-uuid-1",
            )
        assert sup_exc.value.stage == "pre_publish"
        assert called_urls == []

        response_mode = "rate_limit"
        with pytest.raises(RateLimitTaskError) as rl_exc:
            await publisher.create_check_run("acme/app", head_sha="abc")
        assert rl_exc.value.retry_after_seconds == 14.0

        response_mode = "secondary_403"
        with pytest.raises(RateLimitTaskError) as rl403_exc:
            await publisher.create_check_run("acme/app", head_sha="abc")
        assert rl403_exc.value.retry_after_seconds == 9.0

        response_mode = "auth_401"
        with pytest.raises(GitHubAuthError) as auth_exc:
            await publisher.create_check_run("acme/app", head_sha="abc")
        assert auth_exc.value.status_code == 401

        response_mode = "server_503"
        with pytest.raises(RetryableTaskError):
            await publisher.create_check_run("acme/app", head_sha="abc")

        response_mode = "timeout"
        with pytest.raises(RetryableTaskError):
            await publisher.create_check_run("acme/app", head_sha="abc")

        response_mode = "not_found"
        with pytest.raises(GitHubPublishError) as nf_exc:
            await publisher.create_check_run("acme/app", head_sha="abc")
        assert nf_exc.value.status_code == 404
        assert classify_retryable_exception(nf_exc.value) == (False, None)

        with pytest.raises(ValueError, match="conclusion"):
            await publisher.complete_check_run(
                "acme/app",
                1,
                conclusion="cancelled",  # type: ignore[arg-type]
                title="t",
                summary="s",
            )


async def test_publisher_owns_http2_client_pool_and_respects_injected_client() -> None:
    auth = _make_auth()
    async with GitHubReviewPublisher(auth) as owned_pub:
        client1 = owned_pub._ensure_client()
        client2 = owned_pub._ensure_client()
        assert client1 is client2
        assert client1.is_closed is False
    assert client1.is_closed is True

    external = httpx.AsyncClient()
    async with GitHubReviewPublisher(auth, http_client=external) as ext_pub:
        assert ext_pub._ensure_client() is external
    assert external.is_closed is False
    await external.aclose()
    with pytest.raises(RuntimeError, match="closed externally"):
        ext_pub._ensure_client()


async def test_publish_review_type_hints_and_supersede_lock_protocol_guard() -> None:
    from codesight.vcs.github_publisher import AsyncSupersedeLockProtocol

    hints_cls = typing.get_type_hints(GitHubReviewPublisher.publish_review)
    assert "supersede_lock" in hints_cls

    class _ActiveRedis:
        async def get(self, _key: str) -> str:
            return "active_sha_123"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"id": 42, "html_url": "https://github.com/acme/app/pull/7#r42", "state": "OK"},
        )

    lock = AsyncSupersedeLock(_ActiveRedis())  # type: ignore[arg-type]
    assert isinstance(lock, AsyncSupersedeLockProtocol)

    _, parsed = _make_sample_diff()
    stream = io.StringIO()
    configure_logging(level="WARNING", service_name="test-publisher", stream=stream)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = GitHubReviewPublisher(_make_auth(client), http_client=client)
        hints_inst = typing.get_type_hints(publisher.publish_review)
        assert "supersede_lock" in hints_inst

        await publisher.publish_review(
            "acme/app",
            7,
            head_sha="active_sha_123",
            parsed_diff=parsed,
            drafts=[],
            summary_body="No lock passed.",
        )
        assert "github_review_publish_without_supersede_lock" in stream.getvalue()

        stream.truncate(0)
        stream.seek(0)
        await publisher.publish_review(
            "acme/app",
            7,
            head_sha="active_sha_123",
            parsed_diff=parsed,
            drafts=[],
            summary_body="Lock passed.",
            supersede_lock=lock,
            repo_id="repo-1",
        )
        assert "github_review_publish_without_supersede_lock" not in stream.getvalue()

        with pytest.raises(TypeError, match="AsyncSupersedeLockProtocol"):
            await publisher.publish_review(
                "acme/app",
                7,
                head_sha="active_sha_123",
                parsed_diff=parsed,
                drafts=[],
                supersede_lock=object(),  # type: ignore[arg-type]
            )


async def test_publish_review_supersede_in_422_fallback_completes_check_run() -> None:
    class _FlippingRedis:
        def __init__(self) -> None:
            self.active_sha = "stale_sha_1"

        async def get(self, _key: str) -> str:
            return self.active_sha

    redis_state = _FlippingRedis()
    review_post_count = 0
    patch_payloads: list[dict[str, Any]] = []
    patch_mode = "ok"

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal review_post_count
        payload = json.loads(request.content.decode("utf-8")) if request.content else {}
        if request.method == "POST" and request.url.path == "/repos/acme/app/check-runs":
            return httpx.Response(
                201,
                json={"id": 9001, "name": "CodeSight-AI Review", "status": "in_progress"},
            )
        if request.method == "POST" and request.url.path == "/repos/acme/app/pulls/7/reviews":
            review_post_count += 1
            redis_state.active_sha = "newer_sha_after_422"
            return httpx.Response(422, json={"message": "Unprocessable Entity"})
        if request.method == "PATCH" and request.url.path == "/repos/acme/app/check-runs/9001":
            patch_payloads.append(payload)
            if patch_mode == "error_503":
                return httpx.Response(503, json={"message": "Service Unavailable"})
            return httpx.Response(
                200,
                json={
                    "id": 9001,
                    "name": "CodeSight-AI Review",
                    "status": "completed",
                    "conclusion": payload["conclusion"],
                },
            )
        return httpx.Response(404, json={"message": "Not Found"})

    _, parsed = _make_sample_diff()
    lock = AsyncSupersedeLock(redis_state)  # type: ignore[arg-type]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        publisher = GitHubReviewPublisher(_make_auth(client), http_client=client)
        created = await publisher.create_check_run("acme/app", head_sha="stale_sha_1")

        # Scenario A: supersede during 422 fallback finalizes check run as neutral & aborts 2nd POST
        with pytest.raises(PRSupersededError) as exc_info:
            await publisher.publish_review(
                "acme/app",
                7,
                head_sha="stale_sha_1",
                parsed_diff=parsed,
                drafts=[ReviewCommentDraft(path="src/auth.py", line=10, body="Note")],
                supersede_lock=lock,
                repo_id="repo-1",
                check_run_id=created.check_run_id,
            )
        assert exc_info.value.stage == "pre_422_fallback"
        assert exc_info.value.active_head_sha == "newer_sha_after_422"
        assert review_post_count == 1
        assert len(patch_payloads) == 1
        assert patch_payloads[0]["status"] == "completed"
        assert patch_payloads[0]["conclusion"] == "neutral"

        # Scenario B: transient 503 during Check Run cleanup does NOT mask PRSupersededError
        redis_state.active_sha = "stale_sha_1"
        review_post_count = 0
        patch_mode = "error_503"
        with pytest.raises(PRSupersededError) as exc_b:
            await publisher.publish_review(
                "acme/app",
                7,
                head_sha="stale_sha_1",
                parsed_diff=parsed,
                drafts=[ReviewCommentDraft(path="src/auth.py", line=10, body="Note")],
                supersede_lock=lock,
                repo_id="repo-1",
                check_run_id=created.check_run_id,
            )
        assert exc_b.value.stage == "pre_422_fallback"
        assert review_post_count == 1
