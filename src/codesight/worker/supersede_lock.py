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
local p_ts = tonumber(redis.call('GET', KEYS[4]) or '0') or 0
local head_sha = ARGV[1]
local ttl = tonumber(ARGV[2])
local before_sha = ARGV[3]
local event_ts = tonumber(ARGV[4]) or 0
local is_direct_successor = (p_sha and p_sha ~= '' and before_sha ~= '' and before_sha == p_sha)

if p_sha and p_sha ~= '' and p_sha ~= head_sha and not is_direct_successor then
    if redis.call('SISMEMBER', KEYS[3], head_sha) == 1 then
        return {p_sha, p_task or '', '0', tostring(p_ts), '', ''}
    end
    if event_ts > 0 and p_ts > 0 and event_ts < p_ts then
        return {p_sha, p_task or '', '0', tostring(p_ts), '', ''}
    end
end

redis.call('SET', KEYS[1], head_sha, 'EX', ttl)
local removed_anc = ''
if redis.call('SREM', KEYS[3], head_sha) == 1 then
    removed_anc = head_sha
end
local added_anc = ''
if before_sha ~= '' and before_sha ~= head_sha then
    if redis.call('SADD', KEYS[3], before_sha) == 1 then
        added_anc = before_sha
    end
    redis.call('EXPIRE', KEYS[3], ttl)
elseif redis.call('SCARD', KEYS[3]) > 0 then
    redis.call('EXPIRE', KEYS[3], ttl)
end
if event_ts > 0 then
    local next_ts = event_ts
    if not is_direct_successor and p_ts > next_ts then
        next_ts = p_ts
    end
    redis.call('SET', KEYS[4], tostring(next_ts), 'EX', ttl)
end
return {p_sha or '', p_task or '', '1', tostring(p_ts), added_anc, removed_anc}
"""

_ROLLBACK_LUA: Final[str] = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    local ttl = tonumber(ARGV[3])
    if ARGV[2] ~= '' then
        redis.call('SET', KEYS[1], ARGV[2], 'EX', ttl)
    else
        redis.call('DEL', KEYS[1])
    end
    if ARGV[4] ~= '' then
        redis.call('SREM', KEYS[2], ARGV[4])
    end
    if ARGV[5] ~= '' then
        redis.call('SADD', KEYS[2], ARGV[5])
    end
    if redis.call('SCARD', KEYS[2]) > 0 then
        redis.call('EXPIRE', KEYS[2], ttl)
    end
    if ARGV[6] ~= '' then
        redis.call('SET', KEYS[3], ARGV[6], 'EX', ttl)
    else
        redis.call('DEL', KEYS[3])
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


def pr_active_ancestors_key(repo_id: str, pr_number: int) -> str:
    """Return canonical Redis key storing superseded ancestor head_shas for a PR."""
    return f"pr_active_ancestors:{repo_id}:{pr_number}"


def pr_active_ts_key(repo_id: str, pr_number: int) -> str:
    """Return canonical Redis key storing the latest PR event timestamp (epoch ms)."""
    return f"pr_active_ts:{repo_id}:{pr_number}"


def _decode_redis_str(raw: object) -> str | None:
    if isinstance(raw, (bytes, bytearray)):
        decoded = raw.decode("utf-8")
        return decoded or None
    if isinstance(raw, str):
        return raw or None
    return str(raw) if raw else None


def _parse_int_str(raw: object) -> int | None:
    decoded = _decode_redis_str(raw)
    if decoded is None:
        return None
    try:
        val = int(decoded)
    except ValueError:
        return None
    return val if val > 0 else None


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
    stale: bool = False
    prev_ts_ms: int | None = None
    added_ancestor: str | None = None
    removed_ancestor: str | None = None


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
        before_sha: str | None = None,
        event_ts_ms: int | None = None,
    ) -> SupersedeClaim:
        """Atomically read previous SHA/task and set active_head_sha in Redis.

        Guards against out-of-order GitHub webhook delivery by checking the causal
        ``before_sha`` ancestor set and monotonic ``event_ts_ms`` while still
        allowing direct force-push reverts (where ``before_sha == active_sha``).
        Residual known limitation: 3+ rapid non-adjacent force-pushes reordered
        within the same 1-second GitHub ``updated_at`` resolution remain ambiguous.
        """
        sha_key = pr_active_sha_key(repo_id, pr_number)
        task_key = pr_active_task_key(repo_id, pr_number)
        ancestors_key = pr_active_ancestors_key(repo_id, pr_number)
        ts_key = pr_active_ts_key(repo_id, pr_number)
        clean_before = before_sha.strip() if before_sha else ""
        norm_ts = event_ts_ms if event_ts_ms is not None and event_ts_ms > 0 else 0
        eval_fn = self._eval_callable()
        try:
            if eval_fn is not None:
                raw_res = await eval_fn(
                    _CLAIM_LUA,
                    4,
                    sha_key,
                    task_key,
                    ancestors_key,
                    ts_key,
                    head_sha,
                    ttl_seconds,
                    clean_before,
                    norm_ts,
                )
                items = cast(Sequence[object], raw_res)
                prev_sha = _decode_redis_str(items[0]) if len(items) > 0 else None
                prev_task = _decode_redis_str(items[1]) if len(items) > 1 else None
                claimed_flag = _decode_redis_str(items[2]) if len(items) > 2 else "1"
                prev_ts_ms = _parse_int_str(items[3]) if len(items) > 3 else None
                added_ancestor = _decode_redis_str(items[4]) if len(items) > 4 else None
                removed_ancestor = _decode_redis_str(items[5]) if len(items) > 5 else None
                is_stale = claimed_flag == "0"
                return SupersedeClaim(
                    prev_sha=prev_sha,
                    prev_task_id=prev_task,
                    claimed=not is_stale,
                    stale=is_stale,
                    prev_ts_ms=prev_ts_ms,
                    added_ancestor=added_ancestor,
                    removed_ancestor=removed_ancestor,
                )

            prev_sha = _decode_redis_str(await self._client.get(sha_key))
            prev_task = _decode_redis_str(await self._client.get(task_key))
            track_causal = bool(clean_before) or norm_ts > 0
            prev_ts_ms = None
            added_ancestor = None
            removed_ancestor = None
            if track_causal:
                prev_ts_ms = _parse_int_str(await self._client.get(ts_key))
                is_direct_successor = bool(
                    prev_sha and clean_before and clean_before == prev_sha
                )
                if prev_sha and prev_sha != head_sha and not is_direct_successor:
                    is_known_ancestor = bool(
                        await self._client.sismember(ancestors_key, head_sha)
                    )
                    if is_known_ancestor or (
                        norm_ts > 0 and prev_ts_ms is not None and norm_ts < prev_ts_ms
                    ):
                        return SupersedeClaim(
                            prev_sha=prev_sha,
                            prev_task_id=prev_task,
                            claimed=False,
                            stale=True,
                            prev_ts_ms=prev_ts_ms,
                        )
                await self._client.set(sha_key, head_sha, ex=ttl_seconds)
                if int(await self._client.srem(ancestors_key, head_sha)) > 0:
                    removed_ancestor = head_sha
                if clean_before and clean_before != head_sha:
                    if int(await self._client.sadd(ancestors_key, clean_before)) > 0:
                        added_ancestor = clean_before
                    await self._client.expire(ancestors_key, ttl_seconds)
                elif int(await self._client.scard(ancestors_key)) > 0:
                    await self._client.expire(ancestors_key, ttl_seconds)
                if norm_ts > 0:
                    next_ts = norm_ts
                    if (
                        not is_direct_successor
                        and prev_ts_ms is not None
                        and prev_ts_ms > next_ts
                    ):
                        next_ts = prev_ts_ms
                    await self._client.set(ts_key, str(next_ts), ex=ttl_seconds)
            else:
                await self._client.set(sha_key, head_sha, ex=ttl_seconds)
            return SupersedeClaim(
                prev_sha=prev_sha,
                prev_task_id=prev_task,
                claimed=True,
                stale=False,
                prev_ts_ms=prev_ts_ms,
                added_ancestor=added_ancestor,
                removed_ancestor=removed_ancestor,
            )
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
        added_ancestor: str | None = None,
        removed_ancestor: str | None = None,
        prev_ts_ms: int | None = None,
    ) -> None:
        """Restore prev_sha and exact ancestor delta iff active_head_sha == attempted_sha."""
        sha_key = pr_active_sha_key(repo_id, pr_number)
        ancestors_key = pr_active_ancestors_key(repo_id, pr_number)
        ts_key = pr_active_ts_key(repo_id, pr_number)
        clean_added = added_ancestor.strip() if added_ancestor else ""
        clean_removed = removed_ancestor.strip() if removed_ancestor else ""
        ts_arg = str(prev_ts_ms) if prev_ts_ms is not None and prev_ts_ms > 0 else ""
        eval_fn = self._eval_callable()
        try:
            if eval_fn is not None:
                await eval_fn(
                    _ROLLBACK_LUA,
                    3,
                    sha_key,
                    ancestors_key,
                    ts_key,
                    attempted_sha,
                    prev_sha or "",
                    ttl_seconds,
                    clean_added,
                    clean_removed,
                    ts_arg,
                )
                return

            current = _decode_redis_str(await self._client.get(sha_key))
            if current == attempted_sha:
                if prev_sha is not None:
                    await self._client.set(sha_key, prev_sha, ex=ttl_seconds)
                else:
                    await self._client.delete(sha_key)
                if clean_added:
                    await self._client.srem(ancestors_key, clean_added)
                if clean_removed:
                    await self._client.sadd(ancestors_key, clean_removed)
                if (clean_added or clean_removed) and int(
                    await self._client.scard(ancestors_key)
                ) > 0:
                    await self._client.expire(ancestors_key, ttl_seconds)
                if ts_arg:
                    await self._client.set(ts_key, ts_arg, ex=ttl_seconds)
                else:
                    await self._client.delete(ts_key)
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
