"""Structured JSON logging and cross-boundary trace_id context management."""

from __future__ import annotations

import logging
import re
import sys
import uuid
from collections.abc import MutableMapping
from typing import Any, TextIO

import structlog

_TRACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def sanitize_trace_id(raw: str | None) -> str:
    """Validate an incoming trace ID or generate a fresh 32-char hex UUID."""
    if raw is not None:
        cleaned = raw.strip()
        if _TRACE_ID_PATTERN.fullmatch(cleaned):
            return cleaned
    return uuid.uuid4().hex


def bind_trace_id(trace_id: str | None = None) -> str:
    """Sanitize and bind a trace_id into structlog contextvars."""
    sanitized = sanitize_trace_id(trace_id)
    structlog.contextvars.bind_contextvars(trace_id=sanitized)
    return sanitized


def get_trace_id() -> str | None:
    """Return the currently bound trace_id, if any."""
    value = structlog.contextvars.get_contextvars().get("trace_id")
    return str(value) if value is not None else None


def clear_trace_context() -> None:
    """Clear all structlog contextvars bound to the current execution context."""
    structlog.contextvars.clear_contextvars()


def _make_defaults_processor(service_name: str) -> structlog.types.Processor:
    def _inject_defaults(
        _logger: Any,
        _method_name: str,
        event_dict: MutableMapping[str, Any],
    ) -> MutableMapping[str, Any]:
        event_dict.setdefault("service", service_name)
        event_dict.setdefault("trace_id", "-")
        return event_dict

    return _inject_defaults


def configure_logging(
    level: str = "INFO",
    service_name: str = "codesight-api",
    stream: TextIO | None = None,
) -> None:
    """Configure structlog and stdlib logging to emit single-line JSON records."""
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        _make_defaults_processor(service_name),
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )

    target_stream = stream if stream is not None else sys.stderr
    handler = logging.StreamHandler(target_stream)
    handler.setFormatter(formatter)

    normalized_level = level.upper()
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(normalized_level)

    uvicorn_access = logging.getLogger("uvicorn.access")
    uvicorn_access.handlers.clear()
    uvicorn_access.propagate = False

    for logger_name in ("uvicorn", "uvicorn.error", "celery", "httpx", "httpcore"):
        lib_logger = logging.getLogger(logger_name)
        lib_logger.handlers.clear()
        lib_logger.propagate = True

    noisy_http_level = logging.DEBUG if normalized_level == "DEBUG" else logging.WARNING
    for noisy_name in ("httpx", "httpcore"):
        logging.getLogger(noisy_name).setLevel(noisy_http_level)
