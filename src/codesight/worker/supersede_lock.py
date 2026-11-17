"""Unified Cancel-on-Supersede Redis lock with atomic Lua CAS scripts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, cast

import redis as redis_sync
import redis.asyncio as redis_async
import redis.exceptions
import structlog

from codesight.core.config import get_settings

PR_SUPERSEDE_TTL_SECONDS: Final[int] = 86_400
PR_SYNCHRONIZE_DEBOUNCE_SECONDS: Final[int] = 5

_CLAIM_LUA: Final[str] = """
local p_sha = redis.call('GET', KEYS[1])
local p_task = redis.call('GET', KEYS[2])
redis.call('SET', KEYS[1], ARGV[1], 'EX', tonumber(ARGV[2]))
return {p_sha or '', p_task or ''}
"""

_ROLLBACK_LUA: Final[str] = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    if ARGV[2] ~= '' then
        redis.call('SET', KEYS[1], ARGV[2], 'EX', tonumber(ARGV[3]))
    else
        redis.call('DEL', KEYS[1])
    end
    return 1
end
return 0
"""

_RECORD_TASK_LUA: Final[str] = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    redis.call('SET', KEYS[2], ARGV[2], 'EX', tonumber(ARGV[3]))
    return 1
end
return 0
"""

logger = structlog.get_logger("codesight.worker.supersede")


def pr_active_sha_key(repo_id: str, pr_number: int) -> str:
    """Return canonical Redis key storing the active head_sha for a PR."""
    return f"pr_active:{repo_id}:{pr_number}"


def pr_active_task_key(repo_id: str, pr_number: int) -> str:
    """Return canonical Redis key storing the active Celery task_id for a PR."""
    return f"pr_active_task:{repo_id}:{pr_number}"


def _decode_redis_str(raw: object) -> str | None:
    if isinstance(raw, (bytes, bytearray)):
        decoded = raw.decode("utf-8")
        return decoded or None
    if isinstance(raw, str):
        return raw or None
    return str(raw) if raw else None


class PRSupersededError(Exception):
    """Raised when a newer commit head_sha has superseded the current PR review job."""

    def __init__(
        self,
        *,
        repo_id: str,
        pr_number: int,
        head_sha: str,
        active_head_sha: str,
        stage: str,
    ) -> None:
        super().__init__(
            f"PR {repo_id}#{pr_number} @ {head_sha} superseded by {active_head_sha} at {stage}."
        )
        self.repo_id = repo_id
        self.pr_number = pr_number
        self.head_sha = head_sha
        self.active_head_sha = active_head_sha
        self.stage = stage


@dataclass(frozen=True)
class SupersedeClaim:
    """Snapshot returned when claiming active_head_sha for a PR delivery."""

    prev_sha: str | None
    prev_task_id: str | None
    claimed: bool


class AsyncSupersedeLock:
    """Async Cancel-on-Supersede lock for FastAPI Gateway and async worker checkpoints."""

    def __init__(self, redis_client: redis_async.Redis) -> None:
        self._client = redis_client

    def _eval_callable(self) -> Callable[..., Awaitable[Any]] | None:
        raw_eval = getattr(self._client, "eval", None)
        if callable(raw_eval):
            return cast(Callable[..., Awaitable[Any]], raw_eval)
        return None

    async def claim_active_sha(
        self,
        repo_id: str,
        pr_number: int,
        head_sha: str,
        *,
        ttl_seconds: int = PR_SUPERSEDE_TTL_SECONDS,
    ) -> SupersedeClaim:
        """Atomically read previous SHA/task and set active_head_sha in Redis."""
        sha_key = pr_active_sha_key(repo_id, pr_number)
        task_key = pr_active_task_key(repo_id, pr_number)
        eval_fn = self._eval_callable()
        try:
            if eval_fn is not None:
                raw_res = await eval_fn(_CLAIM_LUA, 2, sha_key, task_key, head_sha, ttl_seconds)
                items = cast(Sequence[object], raw_res)
                prev_sha = _decode_redis_str(items[0]) if len(items) > 0 else None
                prev_task = _decode_redis_str(items[1]) if len(items) > 1 else None
                return SupersedeClaim(prev_sha=prev_sha, prev_task_id=prev_task, claimed=True)

            prev_sha = _decode_redis_str(await self._client.get(sha_key))
            prev_task = _decode_redis_str(await self._client.get(task_key))
            await self._client.set(sha_key, head_sha, ex=ttl_seconds)
            return SupersedeClaim(prev_sha=prev_sha, prev_task_id=prev_task, claimed=True)
        except (redis.exceptions.RedisError, OSError) as exc:
            logger.warning(
                "webhook_supersede_cache_unavailable",
                error_type=type(exc).__name__,
            )
            return SupersedeClaim(prev_sha=None, prev_task_id=None, claimed=False)

    async def rollback_active_sha(
        self,
        repo_id: str,
        pr_number: int,
        *,
        attempted_sha: str,
        prev_sha: str | None,
        ttl_seconds: int = PR_SUPERSEDE_TTL_SECONDS,
    ) -> None:
        """Atomically restore prev_sha (or delete key) iff active_head_sha == attempted_sha."""
        sha_key = pr_active_sha_key(repo_id, pr_number)
        eval_fn = self._eval_callable()
        try:
            if eval_fn is not None:
                await eval_fn(
                    _ROLLBACK_LUA,
                    1,
                    sha_key,
                    attempted_sha,
                    prev_sha or "",
                    ttl_seconds,
                )
                return

            current = _decode_redis_str(await self._client.get(sha_key))
            if current == attempted_sha:
                if prev_sha is not None:
                    await self._client.set(sha_key, prev_sha, ex=ttl_seconds)
                else:
                    await self._client.delete(sha_key)
        except (redis.exceptions.RedisError, OSError) as exc:
            logger.warning(
                "webhook_supersede_rollback_failed",
                error_type=type(exc).__name__,
            )

    async def record_active_task(
        self,
        repo_id: str,
        pr_number: int,
        *,
        head_sha: str,
        task_id: str,
        ttl_seconds: int = PR_SUPERSEDE_TTL_SECONDS,
    ) -> None:
        """Atomically record active Celery task_id iff active_head_sha == head_sha."""
        sha_key = pr_active_sha_key(repo_id, pr_number)
        task_key = pr_active_task_key(repo_id, pr_number)
        eval_fn = self._eval_callable()
        try:
            if eval_fn is not None:
                await eval_fn(
                    _RECORD_TASK_LUA,
                    2,
                    sha_key,
                    task_key,
                    head_sha,
                    task_id,
                    ttl_seconds,
                )
                return

            current = _decode_redis_str(await self._client.get(sha_key))
            if current is None or current == head_sha:
                await self._client.set(task_key, task_id, ex=ttl_seconds)
        except (redis.exceptions.RedisError, OSError) as exc:
            logger.warning(
                "webhook_supersede_cache_unavailable",
                error_type=type(exc).__name__,
            )

    async def check_superseded(
        self,
        repo_id: str,
        pr_number: int,
        head_sha: str,
    ) -> tuple[bool, str | None]:
        """Return (True, active_sha) if superseded; fail-open (False, None) on Redis errors."""
        sha_key = pr_active_sha_key(repo_id, pr_number)
        try:
            active_sha = _decode_redis_str(await self._client.get(sha_key))
            if active_sha and active_sha != head_sha:
                return True, active_sha
            return False, active_sha
        except (redis.exceptions.RedisError, OSError) as exc:
            logger.warning(
                "supersede_check_unavailable",
                error_type=type(exc).__name__,
            )
            return False, None

    async def ensure_active(
        self,
        repo_id: str,
        pr_number: int,
        head_sha: str,
        *,
        stage: str = "pre_publish",
    ) -> None:
        """Raise PRSupersededError if a newer head_sha has replaced this PR review job."""
        superseded, active_sha = await self.check_superseded(repo_id, pr_number, head_sha)
        if superseded and active_sha is not None:
            raise PRSupersededError(
                repo_id=repo_id,
                pr_number=pr_number,
                head_sha=head_sha,
                active_head_sha=active_sha,
                stage=stage,
            )


class SyncSupersedeLock:
    """Synchronous Cancel-on-Supersede guard for Celery worker entrypoints and retry checks."""

    def __init__(self, redis_client: redis_sync.Redis) -> None:
        self._client = redis_client

    def check_superseded(
        self,
        repo_id: str,
        pr_number: int,
        head_sha: str,
    ) -> tuple[bool, str | None]:
        """Return (True, active_sha) if superseded; fail-open (False, None) on Redis errors."""
        sha_key = pr_active_sha_key(repo_id, pr_number)
        try:
            active_sha = _decode_redis_str(self._client.get(sha_key))
            if active_sha and active_sha != head_sha:
                return True, active_sha
            return False, active_sha
        except (redis.exceptions.RedisError, OSError) as exc:
            logger.warning(
                "worker_supersede_check_unavailable",
                error_type=type(exc).__name__,
            )
            return False, None

    def ensure_active(
        self,
        repo_id: str,
        pr_number: int,
        head_sha: str,
        *,
        stage: str = "entry",
    ) -> None:
        """Raise PRSupersededError if superseded."""
        superseded, active_sha = self.check_superseded(repo_id, pr_number, head_sha)
        if superseded and active_sha is not None:
            raise PRSupersededError(
                repo_id=repo_id,
                pr_number=pr_number,
                head_sha=head_sha,
                active_head_sha=active_sha,
                stage=stage,
            )


def is_pr_event_superseded(
    payload: Mapping[str, Any],
    *,
    redis_client: redis_sync.Redis | None = None,
    redis_from_url: Callable[..., redis_sync.Redis] | None = None,
) -> bool:
    """Return True if a newer head_sha has superseded this pull_request review task."""
    repo_id = payload.get("repo_id")
    pr_number = payload.get("pr_number")
    head_sha = payload.get("head_sha")
    if (
        not isinstance(repo_id, str)
        or not repo_id
        or not isinstance(pr_number, int)
        or isinstance(pr_number, bool)
        or not isinstance(head_sha, str)
        or not head_sha
    ):
        return False

    owns_client = redis_client is None
    factory = redis_from_url if redis_from_url is not None else redis_sync.Redis.from_url
    client = (
        redis_client
        if redis_client is not None
        else factory(
            get_settings().redis_url,
            decode_responses=True,
            socket_timeout=0.5,
        )
    )
    try:
        superseded, _ = SyncSupersedeLock(client).check_superseded(repo_id, pr_number, head_sha)
        return superseded
    finally:
        if owns_client:
            try:
                client.close()
            except (redis.exceptions.RedisError, OSError):
                pass
