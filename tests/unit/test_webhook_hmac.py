"""Offline unit tests for GitHub Webhook Gateway HMAC, bounds, and dispatcher guards."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import redis.exceptions
from pydantic import SecretStr

from codesight.api.main import create_app
from codesight.api.routes.webhooks import (
    MAX_WEBHOOK_BODY_BYTES,
    CeleryWebhookDispatcher,
    PRWebhookDispatchPayload,
    PushWebhookDispatchPayload,
    verify_github_signature,
)
from codesight.core.config import Settings
from codesight.worker.celery_app import is_pr_event_superseded

TEST_WEBHOOK_SECRET = "super-secret-webhook-key-ca6"


def _sign_body(raw_body: bytes, secret: str = TEST_WEBHOOK_SECRET) -> str:
    digest = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class _SpyDispatcher:
    def __init__(self, *, fail_next: bool = False) -> None:
        self.pr_calls: list[PRWebhookDispatchPayload] = []
        self.push_calls: list[PushWebhookDispatchPayload] = []
        self.fail_next = fail_next

    async def dispatch_pull_request(self, payload: PRWebhookDispatchPayload) -> str:
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("redis broker temporarily down")
        self.pr_calls.append(payload)
        return f"task-pr-{len(self.pr_calls)}"

    async def dispatch_push(self, payload: PushWebhookDispatchPayload) -> str:
        if self.fail_next:
            self.fail_next = False
            raise ConnectionError("redis broker temporarily down")
        self.push_calls.append(payload)
        return f"task-push-{len(self.push_calls)}"


def _pr_payload(
    *,
    action: str = "opened",
    pr_number: int = 42,
    head_sha: str = "a1b2c3d4e5f6",
    repo_id: int | None = 9001,
    full_name: str = "Tieens-04/CodeSight-AI",
    default_branch: str = "main",
    installation_id: int | None = 555,
) -> dict[str, Any]:
    repo_obj: dict[str, Any] = {
        "full_name": full_name,
        "default_branch": default_branch,
    }
    if repo_id is not None:
        repo_obj["id"] = repo_id
    body: dict[str, Any] = {
        "action": action,
        "pull_request": {
            "number": pr_number,
            "head": {"sha": head_sha},
            "base": {"sha": "0000base"},
        },
        "repository": repo_obj,
    }
    if installation_id is not None:
        body["installation"] = {"id": installation_id}
    return body


def test_verify_github_signature_constant_time() -> None:
    body = b'{"action": "opened"}'
    sig = _sign_body(body)
    assert verify_github_signature(body, sig, TEST_WEBHOOK_SECRET) is True
    assert verify_github_signature(body + b" ", sig, TEST_WEBHOOK_SECRET) is False
    assert verify_github_signature(body, "sha1=deadbeef", TEST_WEBHOOK_SECRET) is False
    assert verify_github_signature(body, "sha256=\xff", TEST_WEBHOOK_SECRET) is False
    assert verify_github_signature(body, None, TEST_WEBHOOK_SECRET) is False
    assert verify_github_signature(body, sig, "") is False


@pytest.mark.asyncio
async def test_webhook_rejects_missing_secret_bad_hmac_bad_headers_and_oversize_stream() -> None:
    # 1. Missing secret -> 503
    unconfigured_app = create_app(
        settings=Settings(_env_file=None, github_webhook_secret=None),
        session_factory=MagicMock(),
        webhook_dispatcher=_SpyDispatcher(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=unconfigured_app),
        base_url="http://test",
    ) as client:
        resp_503 = await client.post("/api/v1/webhooks/github", content=b"{}")
    assert resp_503.status_code == 503

    # 2. Bad HMAC, tampered raw bytes, invalid headers, and >25 MiB payload
    app = create_app(
        settings=Settings(
            _env_file=None,
            github_webhook_secret=SecretStr(TEST_WEBHOOK_SECRET),
        ),
        session_factory=MagicMock(),
        webhook_dispatcher=_SpyDispatcher(),
    )
    raw = json.dumps(_pr_payload()).encode("utf-8")
    valid_sig = _sign_body(raw)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        r_missing_sig = await client.post(
            "/api/v1/webhooks/github",
            content=raw,
            headers={"X-GitHub-Event": "pull_request", "X-GitHub-Delivery": "d-1"},
        )
        r_wrong_sig = await client.post(
            "/api/v1/webhooks/github",
            content=raw,
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "d-2",
                "X-Hub-Signature-256": "sha256=" + ("0" * 64),
            },
        )
        r_tampered = await client.post(
            "/api/v1/webhooks/github",
            content=raw + b" ",
            headers={
                "X-GitHub-Event": "pull_request",
                "X-GitHub-Delivery": "d-3",
                "X-Hub-Signature-256": valid_sig,
            },
        )
        r_bad_headers = await client.post(
            "/api/v1/webhooks/github",
            content=raw,
            headers={"X-Hub-Signature-256": valid_sig},
        )
        r_bad_json = await client.post(
            "/api/v1/webhooks/github",
            content=b"[1, 2, 3]",
            headers={
                "X-GitHub-Event": "ping",
                "X-GitHub-Delivery": "d-4",
                "X-Hub-Signature-256": _sign_body(b"[1, 2, 3]"),
            },
        )
        r_content_length_413 = await client.post(
            "/api/v1/webhooks/github",
            content=b"{}",
            headers={"Content-Length": str(MAX_WEBHOOK_BODY_BYTES + 1)},
        )

        # Unauthenticated chunked stream without signature must be rejected (401) before buffering
        unauth_chunks_read = 0

        async def _unauth_oversize_chunks() -> AsyncIterator[bytes]:
            nonlocal unauth_chunks_read
            chunk = b"x" * (1024 * 1024)
            for _ in range(26):
                unauth_chunks_read += 1
                yield chunk

        r_unauth_stream_401 = await client.post(
            "/api/v1/webhooks/github",
            content=_unauth_oversize_chunks(),
        )

        # Streaming chunked body >25 MiB with syntactically valid signature header -> 413
        async def _oversize_chunks() -> AsyncIterator[bytes]:
            chunk = b"x" * (1024 * 1024)
            for _ in range(26):
                yield chunk

        r_stream_413 = await client.post(
            "/api/v1/webhooks/github",
            content=_oversize_chunks(),
            headers={"X-Hub-Signature-256": "sha256=" + ("0" * 64)},
        )

    assert r_missing_sig.status_code == 401
    assert r_wrong_sig.status_code == 401
    assert r_tampered.status_code == 401
    assert r_bad_headers.status_code == 400
    assert r_bad_json.status_code == 400
    assert r_content_length_413.status_code == 413
    assert r_unauth_stream_401.status_code == 401
    assert unauth_chunks_read == 0
    assert r_stream_413.status_code == 413


@pytest.mark.asyncio
async def test_dispatcher_retry_rollback_and_fail_open_guards() -> None:
    def _make_pr(head_sha: str) -> PRWebhookDispatchPayload:
        return PRWebhookDispatchPayload(
            delivery_id=f"d-{head_sha}",
            repo_id="repo-r1",
            github_repo_id=10,
            repo_full_name="org/retry",
            default_branch="main",
            installation_id=20,
            pr_number=7,
            head_sha=head_sha,
            action="synchronize",
        )

    class _MemoryBytesRedis:
        def __init__(self, initial: dict[str, str | bytes] | None = None) -> None:
            self.store: dict[str, str | bytes] = dict(initial or {})

        async def get(self, key: str) -> str | bytes | None:
            return self.store.get(key)

        async def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
            self.store[key] = value
            return True

        async def delete(self, key: str) -> int:
            return 1 if self.store.pop(key, None) is not None else 0

    class _FlakyCelery:
        def __init__(self, fail_count: int = 0, *, fail_revoke: bool = False) -> None:
            self.fail_count = fail_count
            self.fail_revoke = fail_revoke
            self.calls = 0
            self.revoked: list[str] = []
            self.control = self

        def send_task(self, name: str, **_: Any) -> SimpleNamespace:
            self.calls += 1
            if self.calls <= self.fail_count:
                raise ConnectionError("transient broker error")
            return SimpleNamespace(id=f"ok-task-{self.calls}")

        def revoke(self, task_id: str, *, terminate: bool = False) -> None:
            if self.fail_revoke:
                raise RuntimeError("pidbox unavailable")
            self.revoked.append(task_id)

    # 1. Transient failure on 1st attempt succeeds on retry and decodes bytes from Redis
    mem_redis = _MemoryBytesRedis(
        {"pr_active:repo-r1:7": b"sha-bytes", "pr_active_task:repo-r1:7": b"task-bytes"}
    )
    flaky_retry = _FlakyCelery(fail_count=1, fail_revoke=True)
    dispatcher = CeleryWebhookDispatcher(
        app=flaky_retry,
        redis_client=mem_redis,  # type: ignore[arg-type]
    )
    task_id = await dispatcher.dispatch_pull_request(_make_pr("sha-next"))
    assert task_id == "ok-task-2"
    assert flaky_retry.calls == 2
    assert mem_redis.store["pr_active:repo-r1:7"] == "sha-next"
    assert mem_redis.store["pr_active_task:repo-r1:7"] == "ok-task-2"

    # Transient failure on push dispatch also retries once
    push_flaky = _FlakyCelery(fail_count=1)
    push_disp = CeleryWebhookDispatcher(app=push_flaky)
    push_id = await push_disp.dispatch_push(
        PushWebhookDispatchPayload(
            delivery_id="d-push",
            repo_id="repo-r1",
            github_repo_id=10,
            repo_full_name="org/retry",
            default_branch="main",
            installation_id=20,
            ref="refs/heads/main",
            after_sha="abc123",
        )
    )
    assert push_id == "ok-task-2"

    # 2. Persistent failure deletes sha_key when no prior SHA existed
    empty_redis = _MemoryBytesRedis()
    down_celery = _FlakyCelery(fail_count=5)
    down_disp = CeleryWebhookDispatcher(
        app=down_celery,
        redis_client=empty_redis,  # type: ignore[arg-type]
    )
    with pytest.raises(ConnectionError):
        await down_disp.dispatch_pull_request(_make_pr("sha-orphan"))
    assert "pr_active:repo-r1:7" not in empty_redis.store
    assert "pr_active_task:repo-r1:7" not in empty_redis.store

    # 3. Persistent failure restores prev_sha and does NOT revoke prev_task_id
    seeded_redis = _MemoryBytesRedis(
        {"pr_active:repo-r1:7": "sha-prev", "pr_active_task:repo-r1:7": "task-prev"}
    )
    down_celery_2 = _FlakyCelery(fail_count=5)
    down_disp_2 = CeleryWebhookDispatcher(
        app=down_celery_2,
        redis_client=seeded_redis,  # type: ignore[arg-type]
    )
    with pytest.raises(ConnectionError):
        await down_disp_2.dispatch_pull_request(_make_pr("sha-failed"))
    assert seeded_redis.store["pr_active:repo-r1:7"] == "sha-prev"
    assert seeded_redis.store["pr_active_task:repo-r1:7"] == "task-prev"
    assert down_celery_2.revoked == []

    # 4. RedisError on get/set fails open and still publishes task
    class _FailingAsyncRedis:
        async def get(self, key: str) -> str | None:
            raise redis.exceptions.ConnectionError("redis offline")

        async def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
            raise redis.exceptions.ConnectionError("redis offline")

        async def delete(self, key: str) -> int:
            raise redis.exceptions.ConnectionError("redis offline")

    healthy_celery = _FlakyCelery(fail_count=0)
    fail_open_disp = CeleryWebhookDispatcher(
        app=healthy_celery,
        redis_client=_FailingAsyncRedis(),  # type: ignore[arg-type]
    )
    assert await fail_open_disp.dispatch_pull_request(_make_pr("sha-failopen")) == "ok-task-1"

    # 5. Worker is_pr_event_superseded handles bytes, missing key, and Redis ConnectionError
    class _SyncBytesRedis:
        def __init__(self, val: bytes | None = None, *, fail: bool = False) -> None:
            self._val = val
            self._fail = fail

        def get(self, key: str) -> bytes | None:
            if self._fail:
                raise redis.exceptions.ConnectionError("redis down")
            return self._val

    sample_payload = {"repo_id": "r1", "pr_number": 9, "head_sha": "sha-old"}
    assert (
        is_pr_event_superseded(
            sample_payload,
            redis_client=_SyncBytesRedis(b"sha-latest"),  # type: ignore[arg-type]
        )
        is True
    )
    assert (
        is_pr_event_superseded(
            {"repo_id": "r1", "pr_number": 9, "head_sha": "sha-latest"},
            redis_client=_SyncBytesRedis(b"sha-latest"),  # type: ignore[arg-type]
        )
        is False
    )
    assert (
        is_pr_event_superseded(
            sample_payload,
            redis_client=_SyncBytesRedis(None),  # type: ignore[arg-type]
        )
        is False
    )
    assert (
        is_pr_event_superseded(
            sample_payload,
            redis_client=_SyncBytesRedis(fail=True),  # type: ignore[arg-type]
        )
        is False
    )
