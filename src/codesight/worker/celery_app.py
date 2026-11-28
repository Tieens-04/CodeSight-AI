"""Celery application bootstrap, retry/backoff policy, and PR review lifecycle spine."""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import contextvars
import hashlib
import os
import random
import threading
from collections.abc import Awaitable, Callable, Coroutine, Mapping
from typing import Any, Final, ParamSpec, TypeVar, cast

import httpx
import redis as redis_sync
import redis.asyncio as redis_async
import redis.exceptions
import structlog
from celery import Celery, Task
from celery.exceptions import SoftTimeLimitExceeded
from celery.signals import (
    before_task_publish,
    setup_logging,
    task_postrun,
    task_prerun,
    worker_process_init,
    worker_process_shutdown,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from codesight.agents.llm_factory import LLMFactory
from codesight.config_reader.repo_config import RepoConfigLoader
from codesight.core.config import Settings, get_settings
from codesight.core.logging import (
    bind_trace_id,
    clear_trace_context,
    configure_logging,
    get_trace_id,
)
from codesight.db.session import create_db_engine, create_session_factory
from codesight.rag.embedder import GeminiEmbedder
from codesight.rag.qdrant_store import QdrantZeroCodeStore
from codesight.vcs.github_auth import GitHubAuthConfigError
from codesight.worker.lifecycle import (
    PRReviewLifecycleContext,
    classify_retryable_exception,
    extract_pr_coords,
    has_github_credentials,
)
from codesight.worker.lifecycle import (
    execute_pr_review_lifecycle as _execute_pr_review_lifecycle_impl,
)
from codesight.worker.pr_review_runner import Sprint3PRReviewRunner, Week1PRReviewRunner
from codesight.worker.supersede_lock import AsyncSupersedeLock
from codesight.worker.supersede_lock import (
    is_pr_event_superseded as _is_pr_superseded_impl,
)
from codesight.worker.tasks.indexing_tasks import (
    can_run_live_push_sync,
    extract_push_coords,
    has_indexing_credentials,
    log_push_stub_fallback,
    run_push_webhook_task,
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
    """Configure structured JSON logging and reset inherited runtime state on worker boot."""
    _WORKER_RUNTIME.shutdown()
    settings = get_settings()
    configure_logging(level=settings.log_level, service_name="codesight-worker")


def on_worker_process_shutdown(**_: Any) -> None:
    """Drain and close worker-level async pools and loop thread on shutdown."""
    _WORKER_RUNTIME.shutdown()


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
                _WORKER_RUNTIME.record_superseded_audit_sync(payload_arg, trace_id=trace_id)
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
        broker_connection_timeout=2.0,
        broker_transport_options={
            "socket_connect_timeout": 1.0,
            "socket_timeout": 1.0,
            "retry_on_timeout": False,
        },
        task_publish_retry=False,
        result_expires=300,
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
    soft_time_limit: int | None = None,
    time_limit: int | None = None,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    task_opts: dict[str, Any] = {"name": name, "base": CodeSightTask}
    if soft_time_limit is not None:
        task_opts["soft_time_limit"] = soft_time_limit
    if time_limit is not None:
        task_opts["time_limit"] = time_limit

    def _decorator(fn: Callable[_P, _R]) -> Callable[_P, _R]:
        registered = app_instance.task(**task_opts)(fn)
        return cast(Callable[_P, _R], registered)

    return _decorator


before_task_publish.connect(inject_trace_id_header, weak=False)
setup_logging.connect(on_worker_process_init, weak=False)
worker_process_init.connect(on_worker_process_init, weak=False)
worker_process_shutdown.connect(on_worker_process_shutdown, weak=False)
task_prerun.connect(on_task_prerun, weak=False)
task_postrun.connect(on_task_postrun, weak=False)

celery_app: Celery = create_celery_app()


@_typed_task(celery_app, name="codesight.ping")
def ping() -> dict[str, str]:
    """Minimal worker health task verifying broker round-trip and trace_id propagation."""
    trace_id = get_trace_id() or "-"
    logger.info("worker_ping_executed", trace_id=trace_id)
    return {"status": "pong", "trace_id": trace_id}


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
) -> dict[str, str]:
    """Delegate lifecycle execution while honoring monkeypatches on celery_app.redis_async."""
    return await _execute_pr_review_lifecycle_impl(
        payload,
        settings=settings,
        supersede_lock=supersede_lock,
        config_loader=config_loader,
        review_fn=review_fn,
        publish_fn=publish_fn,
        on_superseded_fn=on_superseded_fn,
        check_entry_supersede=check_entry_supersede,
        redis_from_url=redis_async.from_url,
    )


class _WorkerAsyncRuntime:
    """Fork-safe persistent event loop thread and pooled HTTP/Redis/DB resources per worker."""

    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        self._pid: int | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pool_lock: asyncio.Lock | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._redis_client: redis_async.Redis | None = None
        self._redis_url: str | None = None
        self._db_engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker[AsyncSession] | None = None
        self._db_dsn: str | None = None
        self._qdrant_store: QdrantZeroCodeStore | None = None
        self._qdrant_key: tuple[str, int, int, str] | None = None
        self._embedder: GeminiEmbedder | None = None
        self._embedder_key_hash: str | None = None

    def _reset_fields(self) -> None:
        self._pid = None
        self._loop = None
        self._thread = None
        self._pool_lock = None
        self._http_client = None
        self._redis_client = None
        self._redis_url = None
        self._db_engine = None
        self._session_factory = None
        self._db_dsn = None
        self._qdrant_store = None
        self._qdrant_key = None
        self._embedder = None
        self._embedder_key_hash = None

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._state_lock:
            current_pid = os.getpid()
            if self._pid is not None and self._pid != current_pid:
                self._reset_fields()

            if (
                self._loop is not None
                and self._thread is not None
                and self._thread.is_alive()
                and not self._loop.is_closed()
            ):
                return self._loop

            ready = threading.Event()
            loop_holder: list[asyncio.AbstractEventLoop] = []

            def _loop_thread() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop_holder.append(loop)
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    loop.close()

            thread = threading.Thread(
                target=_loop_thread,
                name=f"codesight-worker-loop-{current_pid}",
                daemon=True,
            )
            thread.start()
            ready.wait()
            self._pid = current_pid
            self._loop = loop_holder[0]
            self._thread = thread
            self._pool_lock = None
            return self._loop

    def run_coro[T](self, coro: Coroutine[Any, Any, T]) -> T:
        loop = self._ensure_loop()
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            coro.close()
            raise RuntimeError("Cannot call run_coro synchronously from inside worker event loop.")

        ctx = contextvars.copy_context()
        result_future: concurrent.futures.Future[T] = concurrent.futures.Future()
        task_holder: list[asyncio.Task[T]] = []

        def _schedule() -> None:
            if not result_future.set_running_or_notify_cancel():
                coro.close()
                return
            task = loop.create_task(coro, context=ctx)
            task_holder.append(task)

            def _on_done(done_task: asyncio.Task[T]) -> None:
                if done_task.cancelled():
                    result_future.cancel()
                    return
                exc = done_task.exception()
                if exc is not None:
                    result_future.set_exception(exc)
                else:
                    result_future.set_result(done_task.result())

            task.add_done_callback(_on_done)

        try:
            loop.call_soon_threadsafe(_schedule)
        except RuntimeError:
            coro.close()
            raise

        try:
            return result_future.result()
        except BaseException as exc:
            cancel_msg = (
                f"Cancelled due to {type(exc).__name__}"
                if isinstance(exc, SoftTimeLimitExceeded)
                else None
            )

            def _cancel_running() -> None:
                if task_holder and not task_holder[0].done():
                    task_holder[0].cancel(cancel_msg)
                else:
                    result_future.cancel()

            if not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(_cancel_running)
                    concurrent.futures.wait((result_future,), timeout=3.0)
                except Exception:
                    pass
            raise

    async def _ensure_pools(
        self, settings: Settings
    ) -> tuple[httpx.AsyncClient, redis_async.Redis, async_sessionmaker[AsyncSession]]:
        if self._pool_lock is None:
            self._pool_lock = asyncio.Lock()

        async with self._pool_lock:
            if self._http_client is None or self._http_client.is_closed:
                self._http_client = httpx.AsyncClient(
                    http2=True,
                    timeout=15.0,
                    follow_redirects=False,
                    limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
                )

            if self._redis_client is None or self._redis_url != settings.redis_url:
                if self._redis_client is not None:
                    try:
                        await self._redis_client.aclose()
                    except (redis.exceptions.RedisError, OSError):
                        pass
                self._redis_client = redis_async.from_url(
                    settings.redis_url,
                    decode_responses=True,
                    socket_timeout=0.5,
                    max_connections=10,
                )
                self._redis_url = settings.redis_url

            dsn = settings.sqlalchemy_async_dsn
            if self._session_factory is None or self._db_dsn != dsn:
                if self._db_engine is not None:
                    try:
                        await self._db_engine.dispose()
                    except Exception:
                        pass
                self._db_engine = create_db_engine(settings)
                self._session_factory = create_session_factory(self._db_engine)
                self._db_dsn = dsn

            return self._http_client, self._redis_client, self._session_factory

    async def _ensure_rag_pools(
        self, settings: Settings
    ) -> tuple[QdrantZeroCodeStore, GeminiEmbedder]:
        if self._pool_lock is None:
            self._pool_lock = asyncio.Lock()

        async with self._pool_lock:
            q_key = (
                settings.qdrant_host,
                settings.qdrant_http_port,
                settings.qdrant_grpc_port,
                settings.qdrant_collection_name,
            )
            if self._qdrant_store is None or self._qdrant_key != q_key:
                if self._qdrant_store is not None:
                    try:
                        await self._qdrant_store.aclose()
                    except Exception:
                        pass
                self._qdrant_store = QdrantZeroCodeStore(settings=settings)
                self._qdrant_key = q_key

            raw_gemini = (
                settings.gemini_api_key.get_secret_value().strip()
                if settings.gemini_api_key is not None
                else ""
            )
            key_hash = hashlib.sha256(raw_gemini.encode("utf-8")).hexdigest()
            if self._embedder is None or self._embedder_key_hash != key_hash:
                if self._embedder is not None:
                    try:
                        await self._embedder.aclose()
                    except Exception:
                        pass
                self._embedder = GeminiEmbedder(settings=settings)
                self._embedder_key_hash = key_hash

            return self._qdrant_store, self._embedder

    async def _aclose_pools(self) -> None:
        if self._embedder is not None:
            try:
                await self._embedder.aclose()
            except Exception:
                pass
            self._embedder = None
            self._embedder_key_hash = None
        if self._qdrant_store is not None:
            try:
                await self._qdrant_store.aclose()
            except Exception:
                pass
            self._qdrant_store = None
            self._qdrant_key = None
        if self._redis_client is not None:
            try:
                await self._redis_client.aclose()
            except (redis.exceptions.RedisError, OSError):
                pass
            self._redis_client = None
            self._redis_url = None
        if self._http_client is not None:
            try:
                if not self._http_client.is_closed:
                    await self._http_client.aclose()
            except (httpx.HTTPError, OSError):
                pass
            self._http_client = None
        if self._db_engine is not None:
            try:
                await self._db_engine.dispose()
            except Exception:
                pass
            self._db_engine = None
            self._session_factory = None
            self._db_dsn = None

    def shutdown(self) -> None:
        with self._state_lock:
            loop = self._loop
            thread = self._thread
            pid = self._pid
            if loop is None or thread is None:
                self._reset_fields()
                return
            if pid != os.getpid():
                self._reset_fields()
                return

        if not loop.is_closed() and thread.is_alive():
            close_done: concurrent.futures.Future[None] = concurrent.futures.Future()

            def _schedule_close() -> None:
                if not close_done.set_running_or_notify_cancel():
                    return
                task = loop.create_task(self._aclose_pools())

                def _after_close(done_task: asyncio.Task[None]) -> None:
                    exc = done_task.exception() if not done_task.cancelled() else None
                    if exc is not None:
                        close_done.set_exception(exc)
                    else:
                        close_done.set_result(None)
                    loop.stop()

                task.add_done_callback(_after_close)

            try:
                loop.call_soon_threadsafe(_schedule_close)
                close_done.result(timeout=5.0)
            except Exception:
                if not loop.is_closed():
                    loop.call_soon_threadsafe(loop.stop)
            thread.join(timeout=5.0)

        with self._state_lock:
            self._reset_fields()

    def record_superseded_audit_sync(
        self,
        payload: Mapping[str, Any],
        *,
        trace_id: str,
        settings: Settings | None = None,
    ) -> None:
        """Best-effort DB audit persistence when retry is aborted due to supersede."""
        active_settings = settings if settings is not None else get_settings()
        if not has_github_credentials(active_settings) or extract_pr_coords(payload) is None:
            return

        async def _persist() -> None:
            _, _, session_factory = await self._ensure_pools(active_settings)
            runner = Week1PRReviewRunner(
                settings=active_settings,
                session_factory=session_factory,
            )
            await runner.record_superseded_audit(payload, trace_id=trace_id)

        try:
            self.run_coro(_persist())
        except Exception as exc:
            logger.warning(
                "worker_superseded_audit_dispatch_failed",
                trace_id=trace_id,
                error_type=type(exc).__name__,
            )

    def run_pr_webhook_task_sync(
        self,
        payload: Mapping[str, Any],
        *,
        settings: Settings | None = None,
        is_final_attempt: bool = False,
    ) -> dict[str, str]:
        async def _dispatch() -> dict[str, str]:
            active_settings = settings if settings is not None else get_settings()
            coords = extract_pr_coords(payload)
            repo_full_name = payload.get("repo_full_name")
            has_repo_slug = isinstance(repo_full_name, str) and bool(repo_full_name.strip())
            if coords is not None and has_repo_slug and has_github_credentials(active_settings):
                http_client, redis_client, session_factory = await self._ensure_pools(
                    active_settings
                )
                store: QdrantZeroCodeStore | None = None
                embedder: GeminiEmbedder | None = None
                if active_settings.has_gemini_api_key:
                    try:
                        store, embedder = await self._ensure_rag_pools(active_settings)
                    except Exception as rag_exc:
                        logger.warning(
                            "worker_pr_rag_pools_degraded",
                            trace_id=get_trace_id() or "-",
                            error_type=type(rag_exc).__name__,
                        )
                return await run_pr_webhook_task(
                    payload,
                    settings=active_settings,
                    http_client=http_client,
                    session_factory=session_factory,
                    redis_client=redis_client,
                    qdrant_store=store,
                    embedder=embedder,
                    is_final_attempt=is_final_attempt,
                )
            return await run_pr_webhook_task(
                payload,
                settings=active_settings,
                is_final_attempt=is_final_attempt,
            )

        return self.run_coro(_dispatch())

    def run_push_webhook_task_sync(
        self,
        payload: Mapping[str, Any],
        *,
        settings: Settings | None = None,
        is_final_attempt: bool = False,
    ) -> dict[str, str]:
        async def _dispatch() -> dict[str, str]:
            active_settings = settings if settings is not None else get_settings()
            coords = extract_push_coords(payload)
            has_creds = has_indexing_credentials(active_settings)
            if coords is not None and (
                can_run_live_push_sync(active_settings) or active_settings.app_env == "production"
            ):
                http_client, redis_client, session_factory = await self._ensure_pools(
                    active_settings
                )
                store: QdrantZeroCodeStore | None = None
                embedder: GeminiEmbedder | None = None
                if has_creds:
                    store, embedder = await self._ensure_rag_pools(active_settings)
                return await run_push_webhook_task(
                    payload,
                    settings=active_settings,
                    http_client=http_client,
                    session_factory=session_factory,
                    redis_client=redis_client,
                    store=store,
                    embedder=embedder,
                    is_final_attempt=is_final_attempt,
                )
            return await run_push_webhook_task(
                payload,
                settings=active_settings,
                is_final_attempt=is_final_attempt,
            )

        return self.run_coro(_dispatch())


_WORKER_RUNTIME = _WorkerAsyncRuntime()
atexit.register(_WORKER_RUNTIME.shutdown)


async def run_pr_webhook_task(
    payload: Mapping[str, Any],
    *,
    settings: Settings | None = None,
    http_client: httpx.AsyncClient | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    redis_client: redis_async.Redis | None = None,
    qdrant_store: QdrantZeroCodeStore | None = None,
    embedder: GeminiEmbedder | None = None,
    llm_factory: LLMFactory | None = None,
    api_base_url: str = "https://api.github.com",
    is_final_attempt: bool = False,
) -> dict[str, str]:
    """Execute Sprint 3 AI PR review when Gemini credentials exist, else Week 1 runner."""
    active_settings = settings if settings is not None else get_settings()
    coords = extract_pr_coords(payload)
    repo_full_name = payload.get("repo_full_name")
    has_repo_slug = isinstance(repo_full_name, str) and bool(repo_full_name.strip())
    if coords is not None and has_repo_slug and has_github_credentials(active_settings):
        if active_settings.has_active_llm_api_key or llm_factory is not None:
            sprint3_runner = Sprint3PRReviewRunner(
                settings=active_settings,
                http_client=http_client,
                session_factory=session_factory,
                redis_client=redis_client,
                qdrant_store=qdrant_store,
                embedder=embedder,
                llm_factory=llm_factory,
                api_base_url=api_base_url,
            )
            return await sprint3_runner.execute(payload, is_final_attempt=is_final_attempt)
        runner = Week1PRReviewRunner(
            settings=active_settings,
            http_client=http_client,
            session_factory=session_factory,
            redis_client=redis_client,
            api_base_url=api_base_url,
        )
        return await runner.execute(payload, is_final_attempt=is_final_attempt)

    if (
        coords is not None
        and has_repo_slug
        and active_settings.app_env == "production"
        and not has_github_credentials(active_settings)
    ):
        raise GitHubAuthConfigError(
            "GitHub App credentials are required in production worker mode."
        )

    return await execute_pr_review_lifecycle(
        payload,
        settings=active_settings,
        check_entry_supersede=False,
    )


@_typed_task(celery_app, name="codesight.webhook.handle_pr_event")
def handle_pr_event(payload: dict[str, Any]) -> dict[str, str]:
    """Handle pull_request webhook events through the Day 3-6 Supersede + Review spine."""
    trace_id = get_trace_id() or "-"
    active_settings = get_settings()
    coords = extract_pr_coords(payload)
    repo_full_name = payload.get("repo_full_name")
    has_repo_slug = isinstance(repo_full_name, str) and bool(repo_full_name.strip())

    # In configured live mode, delegate entry-supersede to pooled async runner so DB audit
    # ('superseded' / 'processed') is recorded without opening per-task sync Redis sockets.
    if coords is not None and has_repo_slug and has_github_credentials(active_settings):
        req = getattr(handle_pr_event, "request", None)
        is_worker_delivery = not bool(getattr(req, "called_directly", True)) and bool(
            getattr(req, "id", None)
        )
        current_retries = int(getattr(req, "retries", 0) or 0)
        is_final_attempt = (not is_worker_delivery) or (current_retries >= MAX_TASK_RETRIES)
        return _WORKER_RUNTIME.run_pr_webhook_task_sync(
            payload,
            settings=active_settings,
            is_final_attempt=is_final_attempt,
        )

    if is_pr_event_superseded(payload):
        logger.info(
            "webhook_pr_superseded",
            trace_id=trace_id,
            repo=str(payload.get("repo_full_name", "")),
            pr_number=payload.get("pr_number"),
            head_sha=str(payload.get("head_sha", "")),
        )
        return {"status": "superseded", "trace_id": trace_id}
    return _WORKER_RUNTIME.run_pr_webhook_task_sync(payload, settings=active_settings)


def _dispatch_push_task(task_obj: Any, payload: dict[str, Any]) -> dict[str, str]:
    trace_id = get_trace_id() or "-"
    active_settings = get_settings()
    coords = extract_push_coords(payload)
    if coords is not None and (
        can_run_live_push_sync(active_settings) or active_settings.app_env == "production"
    ):
        req = getattr(task_obj, "request", None)
        is_worker_delivery = not bool(getattr(req, "called_directly", True)) and bool(
            getattr(req, "id", None)
        )
        current_retries = int(getattr(req, "retries", 0) or 0)
        is_final_attempt = (not is_worker_delivery) or (current_retries >= MAX_TASK_RETRIES)
        return _WORKER_RUNTIME.run_push_webhook_task_sync(
            payload,
            settings=active_settings,
            is_final_attempt=is_final_attempt,
        )

    log_push_stub_fallback(payload, settings=active_settings, trace_id=trace_id)
    return {"status": "accepted", "trace_id": trace_id}


_PUSH_TASK_SETTINGS = get_settings()


@_typed_task(
    celery_app,
    name="codesight.webhook.handle_push_event",
    soft_time_limit=_PUSH_TASK_SETTINGS.index_task_soft_time_limit_seconds,
    time_limit=_PUSH_TASK_SETTINGS.index_task_hard_time_limit_seconds,
)
def handle_push_event(payload: dict[str, Any]) -> dict[str, str]:
    """Handle default-branch push webhook events via FR-07 IncrementalBranchSyncer."""
    return _dispatch_push_task(handle_push_event, payload)


@_typed_task(
    celery_app,
    name="codesight.indexing.sync_default_branch",
    soft_time_limit=_PUSH_TASK_SETTINGS.index_task_soft_time_limit_seconds,
    time_limit=_PUSH_TASK_SETTINGS.index_task_hard_time_limit_seconds,
)
def sync_default_branch(payload: dict[str, Any]) -> dict[str, str]:
    """Named Celery entrypoint for incremental default-branch Qdrant synchronization."""
    return _dispatch_push_task(sync_default_branch, payload)
