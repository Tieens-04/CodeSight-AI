"""FastAPI gateway application and pure ASGI TraceIdMiddleware."""

from __future__ import annotations

import time
from collections.abc import Mapping

import structlog
from fastapi import FastAPI, Response, status
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from codesight import __version__
from codesight.api.health import HealthProbe, HealthResponse, run_health_checks
from codesight.core.config import Settings, get_settings
from codesight.core.logging import bind_trace_id, clear_trace_context, configure_logging

_TRACE_HEADER_PRIORITY: tuple[bytes, ...] = (
    b"x-trace-id",
    b"x-request-id",
    b"x-github-delivery",
)

logger = structlog.get_logger("codesight.api")


class TraceIdMiddleware:
    """Pure ASGI middleware extracting, binding, and cleaning up request trace_id."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers_map: dict[bytes, bytes] = {}
        for raw_key, raw_value in scope.get("headers", ()):
            lowered = raw_key.lower()
            if lowered not in headers_map:
                headers_map[lowered] = raw_value

        candidate: str | None = None
        for key in _TRACE_HEADER_PRIORITY:
            if key in headers_map:
                candidate = headers_map[key].decode("latin-1")
                break

        trace_id = bind_trace_id(candidate)
        response_status = status.HTTP_500_INTERNAL_SERVER_ERROR
        started = time.perf_counter()

        async def send_with_trace_id(message: Message) -> None:
            nonlocal response_status
            if message["type"] == "http.response.start":
                response_status = int(message["status"])
                raw_headers = [
                    (k, v)
                    for k, v in message.get("headers", ())
                    if k.lower() != b"x-trace-id"
                ]
                raw_headers.append((b"x-trace-id", trace_id.encode("ascii")))
                message["headers"] = raw_headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace_id)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000.0, 2)
            logger.info(
                "http_request_completed",
                method=scope.get("method", ""),
                path=scope.get("path", ""),
                status_code=response_status,
                duration_ms=duration_ms,
            )
            clear_trace_context()


def create_app(
    settings: Settings | None = None,
    probes: Mapping[str, HealthProbe] | None = None,
) -> FastAPI:
    """Create and configure the CodeSight-AI FastAPI application."""
    active_settings = settings if settings is not None else get_settings()
    configure_logging(level=active_settings.log_level, service_name="codesight-api")

    application = FastAPI(
        title="CodeSight-AI Gateway",
        version=__version__,
    )
    application.add_middleware(TraceIdMiddleware)

    @application.get("/health", response_model=HealthResponse)
    async def health_check(response: Response) -> HealthResponse:
        result = await run_health_checks(active_settings, probes=probes)
        if result.status != "ok":
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return result

    return application


app: FastAPI = create_app()
