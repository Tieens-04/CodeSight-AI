"""Celery application bootstrap, retry/backoff policy, and PR review lifecycle spine."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import random
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, ParamSpec, TypeVar, cast

import httpx
import redis as redis_sync
import redis.asyncio as redis_async
import redis.exceptions
import structlog
from celery import Celery, Task
from celery.signals import (
    before_task_publish,
    setup_logging,
    task_postrun,
    task_prerun,
    worker_process_init,
)

from codesight.config_reader.repo_config import RepoConfig, RepoConfigLoader
from codesight.core.config import Settings, get_settings
from codesight.core.ephemeral import async_ephemeral_workspace
from codesight.core.errors import (
    RateLimitTaskError,
    RetryableTaskError,
    parse_retry_after_header,
)
from codesight.core.logging import (
    bind_trace_id,
    clear_trace_context,
    configure_logging,
    get_trace_id,
)
from codesight.vcs.github_auth import (
    GitHubAuthConfigError,
    GitHubAuthError,
    GitHubAuthProvider,
)
from codesight.worker.supersede_lock import (
    AsyncSupersedeLock,
    PRSupersededError,
)
from codesight.worker.supersede_lock import (
    is_pr_event_superseded as _is_pr_superseded_impl,
)

MAX_TASK_RETRIES: Final[int] = 3
RETRY_BASE_SECONDS: Final[float] = 2.0
RETRY_CAP_SECONDS: Final[float] = 30.0
TASK_SOFT_TIME_LIMIT_SECONDS: Final[int] = 60
TASK_HARD_TIME_LIMIT_SECONDS: Final[int] = 90

_P = ParamSpec("_P")
_R = TypeVar("_R")
_SYSTEM_RNG: Final[random.Random] = random.SystemRandom()

logger = structlog.get_logger("codesight.worker")


def inject_trace_id_header(
    headers: dict[str, Any] | None = None,
    **_: Any,
) -> None:
    """Propagate active trace_id into outbound Celery message headers."""
    if headers is None:
        return
    if "trace_id" not in headers:
        active_trace_id = get_trace_id()
        if active_trace_id is not None:
            headers["trace_id"] = active_trace_id


def on_worker_process_init(**_: Any) -> None:
    """Configure structured JSON logging when a Celery worker process boots."""
    settings = get_settings()
    configure_logging(level=settings.log_level, service_name="codesight-worker")


def on_task_prerun(
    task_id: str | None = None,
    task: Any = None,
    **_: Any,
) -> None:
    """Bind trace_id from Celery task headers (or task_id fallback) before execution."""
    request = getattr(task, "request", None)
    req_headers = getattr(request, "headers", None)
    raw_trace_id: str | None = None
    if isinstance(req_headers, Mapping) and req_headers.get("trace_id") is not None:
        raw_trace_id = str(req_headers["trace_id"])
    else:
        raw_trace_id = task_id
    bind_trace_id(raw_trace_id)


def on_task_postrun(**_: Any) -> None:
    """Clear trace context after task completion to prevent cross-task leakage."""
    clear_trace_context()


def is_pr_event_superseded(
    payload: Mapping[str, Any],
    *,
    redis_client: redis_sync.Redis | None = None,
) -> bool:
    """Return True if a newer head_sha has superseded this pull_request review task."""
    return _is_pr_superseded_impl(
        payload,
        redis_client=redis_client,
        redis_from_url=redis_sync.Redis.from_url,
    )


def _classify_http_status_response(resp: httpx.Response) -> tuple[bool, float | None]:
    code = resp.status_code
    is_rl_403 = code == 403 and bool(
        resp.headers.get("retry-after") or resp.headers.get("x-ratelimit-remaining") == "0"
    )
    if code == 429 or is_rl_403:
        return True, parse_retry_after_header(resp.headers)
    if 500 <= code < 600:
        return True, None
    return False, None


def classify_retryable_exception(exc: BaseException) -> tuple[bool, float | None]:
    """Return (is_retryable, optional_retry_after_seconds) for worker exception triage."""
    if isinstance(exc, (PRSupersededError, GitHubAuthConfigError)):
        return False, None
    if isinstance(exc, RateLimitTaskError):
        return True, exc.retry_after_seconds
    if isinstance(exc, RetryableTaskError):
        return True, None
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
    if isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.TransportError,
            redis.exceptions.ConnectionError,
            redis.exceptions.TimeoutError,
        ),
    ):
        return True, None
    return False, None


def compute_retry_countdown(
    retries: int,
    *,
    retry_after_seconds: float | None = None,
    rng: random.Random | None = None,
) -> float:
    """Compute jittered retry delay honoring Retry-After (<= 60s) or 2s->4s->8s backoff."""
    active_rng = rng if rng is not None else _SYSTEM_RNG
    if retry_after_seconds is not None:
        clamped = min(60.0, max(1.0, retry_after_seconds))
        return clamped + active_rng.uniform(0.0, 1.0)
    exp_cap = min(RETRY_CAP_SECONDS, RETRY_BASE_SECONDS * (2.0 ** max(0, retries)))
    return active_rng.uniform(exp_cap * 0.5, exp_cap)


class CodeSightTask(Task):  # type: ignore[misc]
    """Custom Celery Task base enforcing Retry-After backoff and supersede-before-retry."""

    abstract = True

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        req = getattr(self, "request", None)
        is_worker_delivery = not bool(getattr(req, "called_directly", True)) and bool(
            getattr(req, "id", None)
        )
        try:
            if is_worker_delivery:
                return self.run(*args, **kwargs)
            return super().__call__(*args, **kwargs)
        except Exception as exc:
            if not is_worker_delivery:
                raise

            should_retry, retry_after = classify_retryable_exception(exc)
            current_retries = int(getattr(req, "retries", 0) or 0)
            if not should_retry:
                raise
            if current_retries >= MAX_TASK_RETRIES:
                logger.error(
                    "worker_task_retries_exhausted",
                    task_name=self.name,
                    retries=current_retries,
                    error_type=type(exc).__name__,
                )
                raise

            payload_arg = args[0] if args else kwargs.get("payload")
            if isinstance(payload_arg, Mapping) and is_pr_event_superseded(payload_arg):
                trace_id = get_trace_id() or "-"
                logger.info(
                    "worker_pr_retry_aborted_superseded",
                    task_name=self.name,
                    trace_id=trace_id,
                )
                return {"status": "superseded", "trace_id": trace_id}

            countdown = compute_retry_countdown(
                current_retries,
                retry_after_seconds=retry_after,
            )
            logger.warning(
                "worker_task_retry_scheduled",
                task_name=self.name,
                attempt=current_retries + 1,
                countdown_seconds=round(countdown, 2),
                error_type=type(exc).__name__,
            )
            raise self.retry(
                exc=exc,
                countdown=countdown,
                max_retries=MAX_TASK_RETRIES,
            ) from exc


def create_celery_app(settings: Settings | None = None) -> Celery:
    """Create and configure the CodeSight-AI Celery application."""
    active_settings = settings if settings is not None else get_settings()
    app = Celery(
        "codesight",
        broker=active_settings.redis_url,
        backend=active_settings.redis_url,
        task_cls=CodeSightTask,
    )
    app.conf.update(
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        broker_connection_retry_on_startup=True,
        worker_hijack_root_logger=False,
        worker_redirect_stdouts=False,
        task_acks_late=True,
        task_reject_on_worker_lost=True,
        worker_prefetch_multiplier=1,
        task_soft_time_limit=TASK_SOFT_TIME_LIMIT_SECONDS,
        task_time_limit=TASK_HARD_TIME_LIMIT_SECONDS,
        task_default_retry_delay=int(RETRY_BASE_SECONDS),
    )
    return app


def _typed_task(
    app_instance: Celery,
    *,
    name: str,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    def _decorator(fn: Callable[_P, _R]) -> Callable[_P, _R]:
        registered = app_instance.task(name=name, base=CodeSightTask)(fn)
        return cast(Callable[_P, _R], registered)

    return _decorator


before_task_publish.connect(inject_trace_id_header, weak=False)
setup_logging.connect(on_worker_process_init, weak=False)
worker_process_init.connect(on_worker_process_init, weak=False)
task_prerun.connect(on_task_prerun, weak=False)
task_postrun.connect(on_task_postrun, weak=False)

celery_app: Celery = create_celery_app()


@_typed_task(celery_app, name="codesight.ping")
def ping() -> dict[str, str]:
    """Minimal worker health task verifying broker round-trip and trace_id propagation."""
    trace_id = get_trace_id() or "-"
    logger.info("worker_ping_executed", trace_id=trace_id)
    return {"status": "pong", "trace_id": trace_id}


@dataclass(frozen=True)
class PRReviewLifecycleContext:
    """Execution context passed through worker review and publish stages."""

    payload: Mapping[str, Any]
    trace_id: str
    workspace_dir: Path
    repo_config: RepoConfig


def _has_github_credentials(settings: Settings) -> bool:
    has_app = bool(
        settings.github_app_id
        and settings.github_app_id.strip()
        and settings.github_private_key is not None
        and settings.github_private_key.get_secret_value().strip()
    )
    has_pat = bool(
        settings.github_token is not None and settings.github_token.get_secret_value().strip()
    )
    return has_app or has_pat


def _extract_pr_coords(payload: Mapping[str, Any]) -> tuple[str, int, str] | None:
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


async def execute_pr_review_lifecycle(
    payload: Mapping[str, Any],
    *,
    settings: Settings | None = None,
    supersede_lock: AsyncSupersedeLock | None = None,
    config_loader: RepoConfigLoader | None = None,
    review_fn: Callable[[PRReviewLifecycleContext], Awaitable[None]] | None = None,
    publish_fn: Callable[[PRReviewLifecycleContext], Awaitable[None]] | None = None,
    check_entry_supersede: bool = True,
) -> dict[str, str]:
    """Execute the NFR-3 workspace, .codesight.yaml load, and pre-publish supersede spine."""
    active_settings = settings if settings is not None else get_settings()
    trace_id = get_trace_id() or "-"
    coords = _extract_pr_coords(payload)
    owned_redis: redis_async.Redis | None = None
    active_lock = supersede_lock

    if (
        active_lock is None
        and coords is not None
        and (check_entry_supersede or publish_fn is not None)
    ):
        owned_redis = redis_async.from_url(
            active_settings.redis_url,
            decode_responses=True,
            socket_timeout=0.5,
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
                inst_raw
                if isinstance(inst_raw, int) and not isinstance(inst_raw, bool)
                else None
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
                and _has_github_credentials(active_settings)
            ):
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
            )

            if review_fn is not None:
                await review_fn(ctx)

            # Mandatory pre-publish Cancel-on-Supersede gate right before posting to GitHub
            if active_lock is not None and coords is not None:
                try:
                    await active_lock.ensure_active(
                        coords[0], coords[1], coords[2], stage="pre_publish"
                    )
                except PRSupersededError:
                    logger.info(
                        "worker_pr_publish_aborted_superseded",
                        trace_id=trace_id,
                        repo=str(payload.get("repo_full_name", "")),
                        pr_number=coords[1],
                        head_sha=coords[2],
                    )
                    return {"status": "superseded", "trace_id": trace_id}

            if publish_fn is not None:
                await publish_fn(ctx)

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


def _run_coro_sync[T](coro: Coroutine[Any, Any, T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    ctx = contextvars.copy_context()
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(ctx.run, asyncio.run, coro)
        return future.result()


@_typed_task(celery_app, name="codesight.webhook.handle_pr_event")
def handle_pr_event(payload: dict[str, Any]) -> dict[str, str]:
    """Handle pull_request webhook events through the Day 3 Supersede + Workspace spine."""
    trace_id = get_trace_id() or "-"
    if is_pr_event_superseded(payload):
        logger.info(
            "webhook_pr_superseded",
            trace_id=trace_id,
            repo=str(payload.get("repo_full_name", "")),
            pr_number=payload.get("pr_number"),
            head_sha=str(payload.get("head_sha", "")),
        )
        return {"status": "superseded", "trace_id": trace_id}
    return _run_coro_sync(
        execute_pr_review_lifecycle(payload, check_entry_supersede=False)
    )


@_typed_task(celery_app, name="codesight.webhook.handle_push_event")
def handle_push_event(payload: dict[str, Any]) -> dict[str, str]:
    """Handle default-branch push webhook events (stub wired to syncer in CA-14)."""
    trace_id = get_trace_id() or "-"
    logger.info(
        "webhook_push_stub_received",
        trace_id=trace_id,
        repo=str(payload.get("repo_full_name", "")),
        ref=str(payload.get("ref", "")),
    )
    return {"status": "accepted", "trace_id": trace_id}
