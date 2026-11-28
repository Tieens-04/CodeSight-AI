"""Shared worker PR review lifecycle spine and exception retry classification."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import httpx
import redis.asyncio as redis_async
import redis.exceptions
import sqlalchemy.exc as sa_exc
import structlog
from celery.exceptions import SoftTimeLimitExceeded
from qdrant_client.http.exceptions import ResponseHandlingException, UnexpectedResponse

from codesight.config_reader.repo_config import RepoConfig, RepoConfigLoader
from codesight.core.config import Settings, get_settings
from codesight.core.ephemeral import async_ephemeral_workspace
from codesight.core.errors import (
    RetryableTaskError,
    is_rate_limited_403,
    parse_retry_after_header,
)
from codesight.core.logging import get_trace_id
from codesight.vcs.github_auth import (
    GitHubAuthConfigError,
    GitHubAuthError,
    GitHubAuthProvider,
)
from codesight.worker.supersede_lock import AsyncSupersedeLock, PRSupersededError

_RETRYABLE_GRPC_STATUS_NAMES: Final[frozenset[str]] = frozenset(
    {"UNAVAILABLE", "DEADLINE_EXCEEDED", "RESOURCE_EXHAUSTED", "ABORTED"}
)

logger = structlog.get_logger("codesight.worker.lifecycle")


@dataclass(frozen=True)
class PRReviewLifecycleContext:
    """Execution context passed through worker review and publish stages."""

    payload: Mapping[str, Any]
    trace_id: str
    # NFR-3 ephemeral workspace prepared for Sprint 2-3 AST/Blob consumers.
    workspace_dir: Path
    repo_config: RepoConfig
    supersede_lock: AsyncSupersedeLock | None = None


def has_github_credentials(settings: Settings) -> bool:
    """Return True when either GitHub App or PAT credentials are configured."""
    return settings.has_github_credentials


def extract_pr_coords(payload: Mapping[str, Any]) -> tuple[str, int, str] | None:
    """Extract (repo_id, pr_number, head_sha) from a dispatch payload when complete."""
    repo_id = payload.get("repo_id")
    pr_number = payload.get("pr_number")
    head_sha = payload.get("head_sha")
    if (
        isinstance(repo_id, str)
        and repo_id
        and isinstance(pr_number, int)
        and not isinstance(pr_number, bool)
        and isinstance(head_sha, str)
        and head_sha
    ):
        return repo_id, pr_number, head_sha
    return None


def _classify_http_status_response(resp: httpx.Response) -> tuple[bool, float | None]:
    code = resp.status_code
    if code == 429 or (code == 403 and is_rate_limited_403(resp.headers)):
        return True, parse_retry_after_header(resp.headers)
    if 500 <= code < 600:
        return True, None
    return False, None


def classify_retryable_exception(exc: BaseException) -> tuple[bool, float | None]:
    """Return (is_retryable, optional_retry_after_seconds) for worker exception triage."""
    if isinstance(exc, (PRSupersededError, GitHubAuthConfigError)):
        return False, None
    if isinstance(exc, SoftTimeLimitExceeded):
        return True, 5.0
    if isinstance(exc, RetryableTaskError):
        return True, exc.retry_after_seconds
    if isinstance(exc, GitHubAuthError):
        cause = exc.__cause__
        if isinstance(cause, httpx.HTTPStatusError):
            return _classify_http_status_response(cause.response)
        code = exc.status_code
        if code == 429 or (code is not None and 500 <= code < 600):
            return True, None
        if code is None and isinstance(cause, httpx.RequestError):
            return True, None
        return False, None
    if isinstance(exc, httpx.HTTPStatusError):
        return _classify_http_status_response(exc.response)
    if isinstance(exc, ResponseHandlingException):
        return True, None
    if isinstance(exc, UnexpectedResponse):
        status_code = exc.status_code
        if status_code == 429:
            delay = (
                parse_retry_after_header(exc.headers) if isinstance(exc.headers, Mapping) else None
            )
            return True, delay
        if status_code == 408 or (status_code is not None and 500 <= status_code < 600):
            return True, None
        return False, None
    code_fn = getattr(exc, "code", None)
    if callable(code_fn) and any(cls.__module__.startswith("grpc") for cls in type(exc).__mro__):
        rpc_code = code_fn()
        code_name = str(getattr(rpc_code, "name", rpc_code)).upper()
        return code_name in _RETRYABLE_GRPC_STATUS_NAMES, None
    if isinstance(exc, (sa_exc.OperationalError, sa_exc.InterfaceError, sa_exc.TimeoutError)):
        return True, None
    if isinstance(exc, sa_exc.DBAPIError) and bool(getattr(exc, "connection_invalidated", False)):
        return True, None
    if isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.TransportError,
            redis.exceptions.ConnectionError,
            redis.exceptions.TimeoutError,
            TimeoutError,
            ConnectionError,
        ),
    ):
        return True, None
    return False, None


async def execute_pr_review_lifecycle(
    payload: Mapping[str, Any],
    *,
    settings: Settings | None = None,
    supersede_lock: AsyncSupersedeLock | None = None,
    config_loader: RepoConfigLoader | None = None,
    review_fn: Callable[[PRReviewLifecycleContext], Awaitable[None]] | None = None,
    publish_fn: Callable[[PRReviewLifecycleContext], Awaitable[None]] | None = None,
    on_superseded_fn: Callable[[PRReviewLifecycleContext], Awaitable[None]] | None = None,
    check_entry_supersede: bool = True,
    redis_from_url: Callable[..., redis_async.Redis] | None = None,
) -> dict[str, str]:
    """Execute the NFR-3 workspace, .codesight.yaml load, and pre-publish supersede spine."""
    active_settings = settings if settings is not None else get_settings()
    trace_id = get_trace_id() or "-"
    coords = extract_pr_coords(payload)
    owned_redis: redis_async.Redis | None = None
    active_lock = supersede_lock

    if active_lock is None and coords is not None:
        redis_factory = redis_from_url if redis_from_url is not None else redis_async.from_url
        owned_redis = redis_factory(
            active_settings.redis_url,
            decode_responses=True,
            socket_timeout=0.5,
            socket_connect_timeout=0.5,
        )
        active_lock = AsyncSupersedeLock(owned_redis)

    try:
        if check_entry_supersede and active_lock is not None and coords is not None:
            try:
                await active_lock.ensure_active(coords[0], coords[1], coords[2], stage="entry")
            except PRSupersededError:
                logger.info(
                    "webhook_pr_superseded",
                    trace_id=trace_id,
                    repo=str(payload.get("repo_full_name", "")),
                    pr_number=coords[1],
                    head_sha=coords[2],
                )
                return {"status": "superseded", "trace_id": trace_id}

        async with async_ephemeral_workspace() as workspace_dir:
            repo_full_name = payload.get("repo_full_name")
            default_branch_raw = payload.get("default_branch")
            default_branch = (
                default_branch_raw
                if isinstance(default_branch_raw, str) and default_branch_raw.strip()
                else "main"
            )
            inst_raw = payload.get("installation_id")
            inst_id = (
                inst_raw if isinstance(inst_raw, int) and not isinstance(inst_raw, bool) else None
            )

            if config_loader is not None and isinstance(repo_full_name, str) and repo_full_name:
                repo_config = await config_loader.load(
                    repo_full_name,
                    default_branch=default_branch,
                    installation_id=inst_id,
                )
            elif (
                coords is not None
                and isinstance(repo_full_name, str)
                and repo_full_name
                and has_github_credentials(active_settings)
            ):
                # Defensive fallback for standalone callers outside Week1PRReviewRunner.
                auth = GitHubAuthProvider(active_settings, redis_client=owned_redis)
                repo_config = await RepoConfigLoader(auth).load(
                    repo_full_name,
                    default_branch=default_branch,
                    installation_id=inst_id,
                )
            else:
                repo_config = RepoConfig()

            ctx = PRReviewLifecycleContext(
                payload=payload,
                trace_id=trace_id,
                workspace_dir=workspace_dir,
                repo_config=repo_config,
                supersede_lock=active_lock,
            )

            try:
                if review_fn is not None:
                    await review_fn(ctx)

                # Mandatory pre-publish Cancel-on-Supersede gate right before posting to GitHub
                if active_lock is not None and coords is not None:
                    await active_lock.ensure_active(
                        coords[0], coords[1], coords[2], stage="pre_publish"
                    )

                if publish_fn is not None:
                    await publish_fn(ctx)
            except PRSupersededError as sup_exc:
                logger.info(
                    "worker_pr_publish_aborted_superseded",
                    trace_id=trace_id,
                    repo=str(payload.get("repo_full_name", "")),
                    pr_number=coords[1] if coords is not None else payload.get("pr_number"),
                    head_sha=coords[2] if coords is not None else str(payload.get("head_sha", "")),
                    stage=sup_exc.stage,
                )
                if on_superseded_fn is not None:
                    try:
                        await on_superseded_fn(ctx)
                    except Exception as cleanup_exc:
                        logger.warning(
                            "worker_pr_supersede_cleanup_failed",
                            trace_id=trace_id,
                            error_type=type(cleanup_exc).__name__,
                        )
                return {"status": "superseded", "trace_id": trace_id}

        if review_fn is None and publish_fn is None:
            logger.info(
                "webhook_pr_stub_received",
                trace_id=trace_id,
                repo=str(payload.get("repo_full_name", "")),
                pr_number=payload.get("pr_number"),
                action=payload.get("action"),
                config_hash=repo_config.config_hash,
            )
        return {"status": "accepted", "trace_id": trace_id}
    finally:
        if owned_redis is not None:
            try:
                await owned_redis.aclose()
            except (redis.exceptions.RedisError, OSError):
                pass
