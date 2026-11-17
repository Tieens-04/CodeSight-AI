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
        )
        await async_client.aclose()
