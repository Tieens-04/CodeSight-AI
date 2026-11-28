"""Offline unit tests for Cancel-on-Supersede lock and fail-open behavior."""

from __future__ import annotations

import builtins
from types import SimpleNamespace
from typing import Any

import pytest
import redis.exceptions

from codesight.api.routes.webhooks import (
    CeleryWebhookDispatcher,
    PRWebhookDispatchPayload,
    _classify_webhook,
)
from codesight.core.config import Settings
from codesight.core.logging import bind_trace_id, clear_trace_context
from codesight.worker.celery_app import PRReviewLifecycleContext, execute_pr_review_lifecycle
from codesight.worker.supersede_lock import (
    AsyncSupersedeLock,
    SyncSupersedeLock,
    is_pr_event_superseded,
    pr_active_ancestors_key,
    pr_active_sha_key,
    pr_active_task_key,
)


@pytest.mark.asyncio
async def test_supersede_lock_fail_open_on_redis_errors_and_invalid_payloads() -> None:
    class _BrokenAsyncRedis:
        async def get(self, key: str) -> str | None:
            raise redis.exceptions.TimeoutError("redis timeout")

        async def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
            raise redis.exceptions.TimeoutError("redis timeout")

        async def delete(self, key: str) -> int:
            raise redis.exceptions.TimeoutError("redis timeout")

    class _BrokenSyncRedis:
        def get(self, key: str) -> str | None:
            raise redis.exceptions.ConnectionError("redis down")

    broken_lock = AsyncSupersedeLock(_BrokenAsyncRedis())  # type: ignore[arg-type]
    claim = await broken_lock.claim_active_sha("r1", 1, "sha-1")
    assert claim.claimed is False
    await broken_lock.rollback_active_sha("r1", 1, attempted_sha="sha-1", prev_sha="sha-0")
    await broken_lock.record_active_task("r1", 1, head_sha="sha-1", task_id="t1")
    assert await broken_lock.check_superseded("r1", 1, "sha-1") == (False, None)
    await broken_lock.ensure_active("r1", 1, "sha-1", stage="pre_publish")

    broken_sync = SyncSupersedeLock(_BrokenSyncRedis())  # type: ignore[arg-type]
    assert broken_sync.check_superseded("r1", 1, "sha-1") == (False, None)
    broken_sync.ensure_active("r1", 1, "sha-1", stage="entry")

    assert is_pr_event_superseded({"repo_id": "", "pr_number": True, "head_sha": "x"}) is False


@pytest.mark.asyncio
async def test_offline_out_of_order_guard_and_worker_pre_publish_lock_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _OfflineAsyncRedis:
        def __init__(self) -> None:
            self.store: dict[str, str] = {}
            self.sets: dict[str, set[str]] = {}
            self.closed = False

        @staticmethod
        def _wrongtype() -> redis.exceptions.ResponseError:
            return redis.exceptions.ResponseError(
                "WRONGTYPE Operation against a key holding the wrong kind of value"
            )

        async def get(self, key: str) -> str | None:
            if key in self.sets:
                raise self._wrongtype()
            return self.store.get(key)

        async def set(self, key: str, value: str, *, ex: int | None = None) -> bool:
            if key in self.sets:
                raise self._wrongtype()
            self.store[key] = value
            return True

        async def sismember(self, key: str, member: str) -> int:
            if key in self.store:
                raise self._wrongtype()
            return 1 if member in self.sets.get(key, set()) else 0

        async def sadd(self, key: str, *members: str) -> int:
            if key in self.store:
                raise self._wrongtype()
            bucket = self.sets.setdefault(key, set())
            added = 0
            for member in members:
                if member not in bucket:
                    bucket.add(member)
                    added += 1
            return added

        async def srem(self, key: str, *members: str) -> int:
            if key in self.store:
                raise self._wrongtype()
            bucket = self.sets.get(key)
            if not bucket:
                return 0
            removed = 0
            for member in members:
                if member in bucket:
                    bucket.remove(member)
                    removed += 1
            if not bucket:
                self.sets.pop(key, None)
            return removed

        async def scard(self, key: str) -> int:
            if key in self.store:
                raise self._wrongtype()
            return len(self.sets.get(key, set()))

        async def smembers(self, key: str) -> builtins.set[str]:
            if key in self.store:
                raise self._wrongtype()
            return set(self.sets.get(key, set()))

        async def expire(self, key: str, seconds: int) -> bool:
            return key in self.store or key in self.sets

        async def delete(self, *keys: str) -> int:
            removed = 0
            for key in keys:
                if self.store.pop(key, None) is not None:
                    removed += 1
                if self.sets.pop(key, None) is not None:
                    removed += 1
            return removed

        async def aclose(self) -> None:
            self.closed = True

    sent_tasks: list[tuple[str, str]] = []
    revoked_tasks: list[str] = []

    class _FakeControl:
        def revoke(self, task_id: str, *, terminate: bool = False) -> None:
            revoked_tasks.append(task_id)

    class _FakeCelery:
        control = _FakeControl()

        def send_task(self, name: str, *, args: tuple[Any, ...] = (), **_: Any) -> SimpleNamespace:
            tid = f"task-{len(sent_tasks) + 1}"
            sent_tasks.append((tid, str(args[0]["head_sha"])))
            return SimpleNamespace(id=tid)

    # 1. Verify webhook classifier extracts before_sha and updated_at_ms
    classified = _classify_webhook(
        "pull_request",
        {
            "action": "synchronize",
            "before": "sha-1",
            "repository": {"id": 10, "full_name": "org/demo", "default_branch": "main"},
            "pull_request": {
                "number": 8,
                "updated_at": "2026-11-17T06:00:02Z",
                "head": {"sha": "sha-2"},
                "base": {"sha": "base-0"},
            },
        },
    )
    assert getattr(classified, "before_sha", None) == "sha-1"
    assert getattr(classified, "updated_at_ms", None) is not None

    # 2. Out-of-order SHA_1 arriving after SHA_2 does NOT publish or revoke SHA_2's task
    mem_redis = _OfflineAsyncRedis()
    dispatcher = CeleryWebhookDispatcher(
        app=_FakeCelery(),
        redis_client=mem_redis,  # type: ignore[arg-type]
    )
    t_sha2 = await dispatcher.dispatch_pull_request(
        PRWebhookDispatchPayload(
            delivery_id="d2",
            repo_id="repo-oo",
            github_repo_id=10,
            repo_full_name="org/demo",
            default_branch="main",
            installation_id=None,
            pr_number=8,
            head_sha="sha-2",
            before_sha="sha-1",
            updated_at_ms=2_000_000,
            action="synchronize",
        )
    )
    assert t_sha2 == "task-1"

    t_stale = await dispatcher.dispatch_pull_request(
        PRWebhookDispatchPayload(
            delivery_id="d1",
            repo_id="repo-oo",
            github_repo_id=10,
            repo_full_name="org/demo",
            default_branch="main",
            installation_id=None,
            pr_number=8,
            head_sha="sha-1",
            before_sha="sha-0",
            updated_at_ms=1_000_000,
            action="synchronize",
        )
    )
    assert t_stale is None
    assert sent_tasks == [("task-1", "sha-2")]
    assert revoked_tasks == []
    assert mem_redis.store[pr_active_sha_key("repo-oo", 8)] == "sha-2"

    # Direct force-push revert (before_sha == active sha-2) succeeds
    t_revert = await dispatcher.dispatch_pull_request(
        PRWebhookDispatchPayload(
            delivery_id="d3",
            repo_id="repo-oo",
            github_repo_id=10,
            repo_full_name="org/demo",
            default_branch="main",
            installation_id=None,
            pr_number=8,
            head_sha="sha-1",
            before_sha="sha-2",
            updated_at_ms=3_000_000,
            action="synchronize",
        )
    )
    assert t_revert == "task-2"
    assert revoked_tasks == ["task-1"]

    # Offline fallback rollback restores removed revert ancestor and preserves pre-existing ones
    fb_lock = AsyncSupersedeLock(mem_redis)  # type: ignore[arg-type]
    c_fb_dup = await fb_lock.claim_active_sha(
        "repo-oo", 8, "sha-2c", before_sha="sha-2", event_ts_ms=4_000_000
    )
    assert c_fb_dup.claimed is True and c_fb_dup.added_ancestor is None
    await fb_lock.rollback_active_sha(
        "repo-oo",
        8,
        attempted_sha="sha-2c",
        prev_sha=c_fb_dup.prev_sha,
        added_ancestor=c_fb_dup.added_ancestor,
        removed_ancestor=c_fb_dup.removed_ancestor,
        prev_ts_ms=c_fb_dup.prev_ts_ms,
    )
    assert "sha-2" in mem_redis.sets[pr_active_ancestors_key("repo-oo", 8)]

    c_fb_rev = await fb_lock.claim_active_sha(
        "repo-oo", 8, "sha-2", before_sha="sha-1", event_ts_ms=5_000_000
    )
    assert c_fb_rev.added_ancestor == "sha-1" and c_fb_rev.removed_ancestor == "sha-2"
    await fb_lock.rollback_active_sha(
        "repo-oo",
        8,
        attempted_sha="sha-2",
        prev_sha=c_fb_rev.prev_sha,
        added_ancestor=c_fb_rev.added_ancestor,
        removed_ancestor=c_fb_rev.removed_ancestor,
        prev_ts_ms=c_fb_rev.prev_ts_ms,
    )
    assert "sha-2" in mem_redis.sets[pr_active_ancestors_key("repo-oo", 8)]
    assert "sha-1" not in mem_redis.sets[pr_active_ancestors_key("repo-oo", 8)]

    # Fallback rejects known ancestor even with before_sha=None and event_ts_ms=None (Lua parity)
    c_fb_no_meta = await fb_lock.claim_active_sha(
        "repo-oo", 8, "sha-2", before_sha=None, event_ts_ms=None
    )
    assert c_fb_no_meta.claimed is False and c_fb_no_meta.stale is True
    assert mem_redis.store[pr_active_sha_key("repo-oo", 8)] == "sha-1"

    # Fallback record_active_task refuses orphaned task writes when sha_key is absent
    await fb_lock.record_active_task("repo-empty", 99, head_sha="sha-x", task_id="orphan-task")
    assert pr_active_task_key("repo-empty", 99) not in mem_redis.store

    # 3. Worker spine creates AsyncSupersedeLock even without publish_fn / entry check
    worker_redis = _OfflineAsyncRedis()
    worker_redis.store[pr_active_sha_key("repo-w", 4)] = "sha-a"
    monkeypatch.setattr(
        "codesight.worker.celery_app.redis_async.from_url",
        lambda *_, **__: worker_redis,
    )
    bind_trace_id("trace-offline-prepub")
    seen_locks: list[AsyncSupersedeLock | None] = []
    try:

        async def _review_with_mid_supersede(ctx: PRReviewLifecycleContext) -> None:
            seen_locks.append(ctx.supersede_lock)
            assert ctx.supersede_lock is not None
            await ctx.supersede_lock.claim_active_sha("repo-w", 4, "sha-b")

        res = await execute_pr_review_lifecycle(
            {
                "repo_id": "repo-w",
                "repo_full_name": "org/demo",
                "default_branch": "main",
                "pr_number": 4,
                "head_sha": "sha-a",
            },
            settings=Settings(_env_file=None),
            review_fn=_review_with_mid_supersede,
            check_entry_supersede=False,
        )
        assert res == {"status": "superseded", "trace_id": "trace-offline-prepub"}
        assert len(seen_locks) == 1 and seen_locks[0] is not None
        assert worker_redis.closed is True
    finally:
        clear_trace_context()
