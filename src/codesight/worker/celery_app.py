"""Celery application bootstrap with automatic trace_id signal propagation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, ParamSpec, TypeVar, cast

import structlog
from celery import Celery
from celery.signals import (
    before_task_publish,
    setup_logging,
    task_postrun,
    task_prerun,
    worker_process_init,
)

from codesight.core.config import Settings, get_settings
from codesight.core.logging import (
    bind_trace_id,
    clear_trace_context,
    configure_logging,
    get_trace_id,
)

_P = ParamSpec("_P")
_R = TypeVar("_R")

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


def create_celery_app(settings: Settings | None = None) -> Celery:
    """Create and configure the CodeSight-AI Celery application."""
    active_settings = settings if settings is not None else get_settings()
    app = Celery(
        "codesight",
        broker=active_settings.redis_url,
        backend=active_settings.redis_url,
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
    )
    return app


def _typed_task(
    app_instance: Celery,
    *,
    name: str,
) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    def _decorator(fn: Callable[_P, _R]) -> Callable[_P, _R]:
        registered = app_instance.task(name=name)(fn)
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
