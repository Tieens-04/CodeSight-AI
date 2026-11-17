"""Unit and live-Redis (6381) CAS tests for Cancel-on-Supersede lock."""

from __future__ import annotations

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
    pr_active_sha_key,
    pr_active_task_key,
)


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
    finally:
        await async_client.delete(
            pr_active_sha_key(repo_id, pr_num),
            pr_active_task_key(repo_id, pr_num),
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
