"""Concurrent dependency health probes for PostgreSQL, Redis, and Qdrant."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Literal

import asyncpg
import httpx
import redis.asyncio as redis_async
import sqlalchemy as sa
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from codesight import __version__
from codesight.core.config import Settings
from codesight.core.logging import get_trace_id

HealthProbe = Callable[[Settings], Awaitable[None]]


class ServiceCheckResult(BaseModel):
    """Status of a single backing service dependency."""

    status: Literal["up", "down"]
    latency_ms: float
    detail: str | None = None


class HealthResponse(BaseModel):
    """Aggregated readiness payload for GET /health."""

    status: Literal["ok", "degraded"]
    version: str
    trace_id: str
    checks: dict[str, ServiceCheckResult]


async def probe_postgres(settings: Settings) -> None:
    """Verify PostgreSQL connectivity via SELECT 1."""
    conn = await asyncpg.connect(
        dsn=settings.postgres_dsn,
        timeout=settings.health_timeout_seconds,
    )
    try:
        await conn.fetchval("SELECT 1")
    finally:
        await conn.close()


async def probe_redis(settings: Settings) -> None:
    """Verify Redis connectivity via PING."""
    client = redis_async.from_url(
        settings.redis_url,
        socket_timeout=settings.health_timeout_seconds,
        socket_connect_timeout=settings.health_timeout_seconds,
    )
    try:
        await client.ping()
    finally:
        await client.aclose()


async def probe_qdrant(settings: Settings) -> None:
    """Verify Qdrant readiness via HTTP GET /readyz."""
    async with httpx.AsyncClient(timeout=settings.health_timeout_seconds) as client:
        response = await client.get(f"{settings.qdrant_http_url}/readyz")
        response.raise_for_status()


DEFAULT_PROBES: dict[str, HealthProbe] = {
    "postgres": probe_postgres,
    "redis": probe_redis,
    "qdrant": probe_qdrant,
}


async def _execute_probe(
    name: str,
    probe: HealthProbe,
    settings: Settings,
) -> tuple[str, ServiceCheckResult]:
    started = time.perf_counter()
    try:
        await asyncio.wait_for(probe(settings), timeout=settings.health_timeout_seconds)
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        return name, ServiceCheckResult(status="up", latency_ms=elapsed_ms)
    except Exception as exc:
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 2)
        detail = (
            f"HTTP {exc.response.status_code}"
            if isinstance(exc, httpx.HTTPStatusError)
            else type(exc).__name__
        )
        return name, ServiceCheckResult(status="down", latency_ms=elapsed_ms, detail=detail)


async def run_health_checks(
    settings: Settings,
    probes: Mapping[str, HealthProbe] | None = None,
    *,
    db_engine: AsyncEngine | None = None,
    redis_client: redis_async.Redis | None = None,
) -> HealthResponse:
    """Execute all backing service probes concurrently and aggregate readiness."""
    if probes is not None:
        active_probes: Mapping[str, HealthProbe] = probes
    else:
        pooled_probes: dict[str, HealthProbe] = dict(DEFAULT_PROBES)
        if db_engine is not None:

            async def _pooled_postgres(_: Settings) -> None:
                async with db_engine.connect() as conn:
                    await conn.execute(sa.text("SELECT 1"))

            pooled_probes["postgres"] = _pooled_postgres
        if redis_client is not None:

            async def _pooled_redis(_: Settings) -> None:
                await redis_client.ping()

            pooled_probes["redis"] = _pooled_redis
        active_probes = pooled_probes

    results = await asyncio.gather(
        *(_execute_probe(name, probe, settings) for name, probe in active_probes.items())
    )
    checks = dict(results)
    overall: Literal["ok", "degraded"] = (
        "ok" if all(item.status == "up" for item in checks.values()) else "degraded"
    )
    return HealthResponse(
        status=overall,
        version=__version__,
        trace_id=get_trace_id() or "-",
        checks=checks,
    )
