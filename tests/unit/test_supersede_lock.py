"""Unit and live-Redis (6381) CAS tests for Cancel-on-Supersede lock."""

from __future__ import annotations

import builtins
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
    pr_active_ancestors_key,
    pr_active_sha_key,
    pr_active_task_key,
    pr_active_ts_key,
)


@pytest.mark.integration
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
        assert c2.prev_sha == "sha-1" and c2.prev_task_id == "task-1" and c2.claimed is True

        # SHA_1 worker sees superseded via both sync & async guards
        assert sync_lock.check_superseded(repo_id, pr_num, "sha-1") == (True, "sha-2")
        with pytest.raises(PRSupersededError) as sync_exc:
            sync_lock.ensure_active(repo_id, pr_num, "sha-1", stage="entry")
        assert sync_exc.value.active_head_sha == "sha-2"
        assert sync_exc.value.stage == "entry"

        with pytest.raises(PRSupersededError) as exc_info:
            await lock.ensure_active(repo_id, pr_num, "sha-1", stage="pre_publish")
        assert exc_info.value.active_head_sha == "sha-2"
        assert exc_info.value.stage == "pre_publish"

        # Active SHA_2 passes ensure_active cleanly
        await lock.ensure_active(repo_id, pr_num, "sha-2", stage="pre_publish")
        sync_lock.ensure_active(repo_id, pr_num, "sha-2", stage="entry")

        # 2. CAS Rollback Hazard: SHA_3 arrives BEFORE SHA_2's failed-publish rollback runs
        await lock.claim_active_sha(repo_id, pr_num, "sha-3")
        await lock.rollback_active_sha(
            repo_id, pr_num, attempted_sha="sha-2", prev_sha="sha-1"
        )
        await lock.record_active_task(repo_id, pr_num, head_sha="sha-2", task_id="stale-task-2")
        assert await async_client.get(pr_active_sha_key(repo_id, pr_num)) == "sha-3"
        assert await async_client.get(pr_active_task_key(repo_id, pr_num)) == "task-1"

        # 3. Legitimate rollback when SHA_3 itself fails broker publish restores SHA_1 (or deletes)
        await lock.rollback_active_sha(
            repo_id, pr_num, attempted_sha="sha-3", prev_sha="sha-1"
        )
        assert await async_client.get(pr_active_sha_key(repo_id, pr_num)) == "sha-1"
        await lock.rollback_active_sha(
            repo_id, pr_num, attempted_sha="sha-1", prev_sha=None
        )
        assert await async_client.get(pr_active_sha_key(repo_id, pr_num)) is None

        # 4. Out-of-order SHA_2 before SHA_1 rejects SHA_1, while direct revert succeeds
        oo_pr = pr_num + 1
        c_oo2 = await lock.claim_active_sha(
            repo_id, oo_pr, "sha-2", before_sha="sha-1", event_ts_ms=2_000_000
        )
        assert c_oo2.claimed is True and c_oo2.stale is False

        c_oo1_late = await lock.claim_active_sha(
            repo_id, oo_pr, "sha-1", before_sha="sha-0", event_ts_ms=1_000_000
        )
        assert c_oo1_late.claimed is False and c_oo1_late.stale is True
        assert c_oo1_late.prev_sha == "sha-2"
        assert await async_client.get(pr_active_sha_key(repo_id, oo_pr)) == "sha-2"

        c_revert = await lock.claim_active_sha(
            repo_id, oo_pr, "sha-1", before_sha="sha-2", event_ts_ms=3_000_000
        )
        assert c_revert.claimed is True and c_revert.stale is False
        assert c_revert.added_ancestor == "sha-2"
        assert c_revert.removed_ancestor == "sha-1"
        assert await async_client.get(pr_active_sha_key(repo_id, oo_pr)) == "sha-1"

        await lock.rollback_active_sha(
            repo_id,
            oo_pr,
            attempted_sha="sha-1",
            prev_sha=c_revert.prev_sha,
            added_ancestor=c_revert.added_ancestor,
            removed_ancestor=c_revert.removed_ancestor,
            prev_ts_ms=c_revert.prev_ts_ms,
        )
        assert await async_client.get(pr_active_sha_key(repo_id, oo_pr)) == "sha-2"
        assert await async_client.get(pr_active_ts_key(repo_id, oo_pr)) == "2000000"
        assert bool(await async_client.sismember(pr_active_ancestors_key(repo_id, oo_pr), "sha-1"))
        assert not bool(
            await async_client.sismember(pr_active_ancestors_key(repo_id, oo_pr), "sha-2")
        )
        # Reverted-then-rolled-back sha-1 remains rejected as stale when arriving out of order
        c_oo1_after_rb = await lock.claim_active_sha(
            repo_id, oo_pr, "sha-1", before_sha="sha-0", event_ts_ms=1_500_000
        )
        assert c_oo1_after_rb.claimed is False and c_oo1_after_rb.stale is True

        # Pre-existing ancestor in pr_active_ancestors is preserved on rollback
        await async_client.sadd(pr_active_ancestors_key(repo_id, oo_pr), "sha-0")
        c_dup_anc = await lock.claim_active_sha(
            repo_id, oo_pr, "sha-2b", before_sha="sha-0", event_ts_ms=2_500_000
        )
        assert c_dup_anc.claimed is True and c_dup_anc.added_ancestor is None
        await lock.rollback_active_sha(
            repo_id,
            oo_pr,
            attempted_sha="sha-2b",
            prev_sha=c_dup_anc.prev_sha,
            added_ancestor=c_dup_anc.added_ancestor,
            removed_ancestor=c_dup_anc.removed_ancestor,
            prev_ts_ms=c_dup_anc.prev_ts_ms,
        )
        assert bool(await async_client.sismember(pr_active_ancestors_key(repo_id, oo_pr), "sha-0"))

        # 5. Lua + non-Lua Set fallback interoperability on the same live Redis keys (no WRONGTYPE)
        class _NoEvalProxy:
            def __init__(self, inner: redis_async.Redis) -> None:
                self._inner = inner

            def __getattr__(self, name: str) -> object:
                if name == "eval":
                    raise AttributeError("eval disabled")
                return getattr(self._inner, name)

        fallback_lock = AsyncSupersedeLock(_NoEvalProxy(async_client))  # type: ignore[arg-type]
        c_lua_3 = await lock.claim_active_sha(
            repo_id, oo_pr, "sha-3", before_sha="sha-2", event_ts_ms=3_000_000
        )
        assert c_lua_3.claimed is True and c_lua_3.stale is False
        assert await async_client.type(pr_active_ancestors_key(repo_id, oo_pr)) == "set"

        # Non-Lua fallback detects sha-2 in Redis Set written by Lua without WRONGTYPE
        c_fb_stale = await fallback_lock.claim_active_sha(
            repo_id, oo_pr, "sha-2", before_sha="sha-1", event_ts_ms=4_000_000
        )
        assert c_fb_stale.claimed is False and c_fb_stale.stale is True

        # Non-Lua fallback advances to sha-4 and Lua path sees sha-3 in the shared Set
        c_fb_4 = await fallback_lock.claim_active_sha(
            repo_id, oo_pr, "sha-4", before_sha="sha-3", event_ts_ms=5_000_000
        )
        assert c_fb_4.claimed is True and c_fb_4.stale is False
        assert await async_client.type(pr_active_ancestors_key(repo_id, oo_pr)) == "set"

        c_lua_stale = await lock.claim_active_sha(
            repo_id, oo_pr, "sha-3", before_sha="sha-2", event_ts_ms=6_000_000
        )
        assert c_lua_stale.claimed is False and c_lua_stale.stale is True

        await fallback_lock.rollback_active_sha(
            repo_id,
            oo_pr,
            attempted_sha="sha-4",
            prev_sha=c_fb_4.prev_sha,
            added_ancestor=c_fb_4.added_ancestor,
            removed_ancestor=c_fb_4.removed_ancestor,
            prev_ts_ms=c_fb_4.prev_ts_ms,
        )
        assert await async_client.get(pr_active_sha_key(repo_id, oo_pr)) == "sha-3"
    finally:
        await async_client.delete(
            pr_active_sha_key(repo_id, pr_num),
            pr_active_task_key(repo_id, pr_num),
            pr_active_ancestors_key(repo_id, pr_num),
            pr_active_ts_key(repo_id, pr_num),
            pr_active_sha_key(repo_id, pr_num + 1),
            pr_active_task_key(repo_id, pr_num + 1),
            pr_active_ancestors_key(repo_id, pr_num + 1),
            pr_active_ts_key(repo_id, pr_num + 1),
        )
        await async_client.aclose()
        sync_client.close()


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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_execute_pr_review_lifecycle_blocks_stale_publish_and_cleans_workspace() -> None:
    from pathlib import Path
    from unittest.mock import AsyncMock

    from codesight.config_reader.repo_config import RepoConfig
    from codesight.core.logging import bind_trace_id, clear_trace_context
    from codesight.worker.celery_app import PRReviewLifecycleContext, execute_pr_review_lifecycle

    settings = Settings(_env_file=None)
    async_client = redis_async.Redis.from_url(Settings().redis_url, decode_responses=True)
    repo_id = f"lifecycle-repo-{uuid.uuid4().hex[:8]}"
    pr_num = 99
    lock = AsyncSupersedeLock(async_client)
    fake_loader = AsyncMock()
    fake_loader.load.return_value = RepoConfig(min_debt_score=80, review_action="REQUEST_CHANGES")

    observed_workspaces: list[Path] = []
    publish_calls: list[str] = []

    try:
        await lock.claim_active_sha(repo_id, pr_num, "sha-1")
        bind_trace_id("trace-lifecycle-1")

        async def _review_then_supersede(ctx: PRReviewLifecycleContext) -> None:
            assert ctx.workspace_dir.exists()
            assert ctx.repo_config.min_debt_score == 80
            (ctx.workspace_dir / "scratch.diff").write_text("diff", encoding="utf-8")
            observed_workspaces.append(ctx.workspace_dir)
            # Developer pushes SHA_2 while SHA_1 is midway through review!
            await lock.claim_active_sha(repo_id, pr_num, "sha-2")

        async def _publish(ctx: PRReviewLifecycleContext) -> None:
            publish_calls.append(str(ctx.payload["head_sha"]))

        res_stale = await execute_pr_review_lifecycle(
            {
                "repo_id": repo_id,
                "repo_full_name": "org/demo",
                "default_branch": "main",
                "pr_number": pr_num,
                "head_sha": "sha-1",
            },
            settings=settings,
            supersede_lock=lock,
            config_loader=fake_loader,
            review_fn=_review_then_supersede,
            publish_fn=_publish,
        )
        assert res_stale == {"status": "superseded", "trace_id": "trace-lifecycle-1"}
        assert publish_calls == []
        assert len(observed_workspaces) == 1 and not observed_workspaces[0].exists()

        # Active SHA_2 completes review + publish and still wipes workspace
        async def _review_active(ctx: PRReviewLifecycleContext) -> None:
            observed_workspaces.append(ctx.workspace_dir)

        res_ok = await execute_pr_review_lifecycle(
            {
                "repo_id": repo_id,
                "repo_full_name": "org/demo",
                "default_branch": "main",
                "pr_number": pr_num,
                "head_sha": "sha-2",
            },
            settings=settings,
            supersede_lock=lock,
            config_loader=fake_loader,
            review_fn=_review_active,
            publish_fn=_publish,
        )
        assert res_ok == {"status": "accepted", "trace_id": "trace-lifecycle-1"}
        assert publish_calls == ["sha-2"]
        assert not observed_workspaces[1].exists()

        # Entry supersede check (check_entry_supersede=True, owned Redis auto-created)
        res_entry_stale = await execute_pr_review_lifecycle(
            {
                "repo_id": repo_id,
                "repo_full_name": "org/demo",
                "default_branch": "main",
                "pr_number": pr_num,
                "head_sha": "sha-1",
            },
            settings=Settings(_env_file=None),
            check_entry_supersede=True,
        )
        assert res_entry_stale["status"] == "superseded"

        # Incomplete stub payload does not call live GitHub even when GITHUB_TOKEN is configured
        from pydantic import SecretStr

        from codesight.worker.celery_app import handle_pr_event

        stub_settings = Settings(_env_file=None, github_token=SecretStr("ghp_offline_unit_stub"))
        res_stub = await execute_pr_review_lifecycle(
            {"repo_full_name": "org/unconfigured-stub", "pr_number": 5},
            settings=stub_settings,
            check_entry_supersede=False,
        )
        assert res_stub["status"] == "accepted"
        assert handle_pr_event({"repo_full_name": "org/unconfigured-stub", "pr_number": 5})[
            "status"
        ] == "accepted"
    finally:
        clear_trace_context()
        await async_client.delete(
            pr_active_sha_key(repo_id, pr_num),
            pr_active_task_key(repo_id, pr_num),
            pr_active_ancestors_key(repo_id, pr_num),
            pr_active_ts_key(repo_id, pr_num),
        )
        await async_client.aclose()


@pytest.mark.asyncio
async def test_offline_out_of_order_guard_and_worker_pre_publish_lock_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace
    from typing import Any

    from codesight.api.routes.webhooks import (
        CeleryWebhookDispatcher,
        PRWebhookDispatchPayload,
        _classify_webhook,
    )
    from codesight.core.logging import bind_trace_id, clear_trace_context
    from codesight.worker.celery_app import PRReviewLifecycleContext, execute_pr_review_lifecycle

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
    assert t_stale == "superseded:sha-2"
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
