"""Unit tests for FastAPI gateway, TraceIdMiddleware, and concurrent /health probes."""

import asyncio
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from codesight.api.health import probe_postgres, probe_qdrant, probe_redis
from codesight.api.main import TraceIdMiddleware, create_app
from codesight.core.config import Settings
from codesight.core.logging import clear_trace_context, get_trace_id


async def _ok_probe(_: Settings) -> None:
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_health_ok_and_trace_header_precedence() -> None:
    clear_trace_context()
    settings = Settings(_env_file=None)
    app = create_app(
        settings=settings,
        probes={"postgres": _ok_probe, "redis": _ok_probe, "qdrant": _ok_probe},
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp_gh = await client.get("/health", headers={"X-GitHub-Delivery": "delivery-guid-99"})
        resp_req = await client.get(
            "/health",
            headers={"X-Request-ID": "req-1", "X-GitHub-Delivery": "delivery-2"},
        )
        resp_trace = await client.get(
            "/health",
            headers={
                "X-Trace-ID": "trace-top",
                "X-Request-ID": "req-1",
                "X-GitHub-Delivery": "delivery-2",
            },
        )

    assert resp_gh.status_code == 200
    assert resp_gh.headers["x-trace-id"] == "delivery-guid-99"
    body = resp_gh.json()
    assert body["status"] == "ok"
    assert body["trace_id"] == "delivery-guid-99"
    assert set(body["checks"].keys()) == {"postgres", "redis", "qdrant"}
    assert resp_req.headers["x-trace-id"] == "req-1"
    assert resp_trace.headers["x-trace-id"] == "trace-top"
    assert get_trace_id() is None


@pytest.mark.asyncio
async def test_health_degraded_503_on_timeout_and_scrubs_secrets() -> None:
    secret_pw = "super_secret_pg_pw"
    settings = Settings(
        _env_file=None,
        postgres_password=SecretStr(secret_pw),
        health_timeout_seconds=0.05,
    )

    async def _slow_probe(_: Settings) -> None:
        await asyncio.sleep(0.5)

    async def _leaky_probe(s: Settings) -> None:
        raise ConnectionError(f"failed connecting to {s.postgres_dsn}")

    app = create_app(
        settings=settings,
        probes={"postgres": _leaky_probe, "redis": _slow_probe, "qdrant": _ok_probe},
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health", headers={"X-Trace-ID": "bad\r\nheader"})

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["postgres"]["status"] == "down"
    assert body["checks"]["postgres"]["detail"] == "ConnectionError"
    assert secret_pw not in (body["checks"]["postgres"]["detail"] or "")
    assert body["checks"]["redis"]["status"] == "down"
    assert body["checks"]["redis"]["detail"] == "TimeoutError"
    assert body["checks"]["qdrant"]["status"] == "up"
    assert len(resp.headers["x-trace-id"]) == 32
    assert resp.headers["x-trace-id"].isalnum()
    assert get_trace_id() is None


@pytest.mark.asyncio
async def test_middleware_clears_trace_context_when_route_raises() -> None:
    app = create_app(
        settings=Settings(_env_file=None),
        probes={"postgres": _ok_probe, "redis": _ok_probe, "qdrant": _ok_probe},
    )

    @app.get("/boom")
    async def _boom() -> None:
        assert get_trace_id() == "req-boom-1"
        raise RuntimeError("kaboom")

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/boom", headers={"X-Request-ID": "req-boom-1"})

    assert resp.status_code == 500
    assert get_trace_id() is None


@pytest.mark.asyncio
async def test_middleware_passes_through_non_http_scope() -> None:
    seen_types: list[str] = []

    async def _inner_app(scope: Any, receive: Any, send: Any) -> None:
        seen_types.append(scope["type"])

    middleware = TraceIdMiddleware(_inner_app)

    async def _noop_receive() -> dict[str, str]:
        return {"type": "lifespan.startup"}

    async def _noop_send(_: Any) -> None:
        return None

    await middleware({"type": "lifespan"}, _noop_receive, _noop_send)
    assert seen_types == ["lifespan"]
    assert get_trace_id() is None


@pytest.mark.asyncio
async def test_default_probes_execute_expected_client_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(_env_file=None)
    calls: list[str] = []

    class _FakePgConn:
        async def fetchval(self, query: str) -> int:
            calls.append(f"pg:{query}")
            return 1

        async def close(self) -> None:
            calls.append("pg:close")

    async def _fake_pg_connect(**kwargs: Any) -> _FakePgConn:
        assert kwargs["dsn"] == settings.postgres_dsn
        assert kwargs["timeout"] == settings.health_timeout_seconds
        return _FakePgConn()

    class _FakeRedis:
        async def ping(self) -> bool:
            calls.append("redis:ping")
            return True

        async def aclose(self) -> None:
            calls.append("redis:close")

    def _fake_redis_from_url(url: str, *, socket_timeout: float) -> _FakeRedis:
        assert url == settings.redis_url
        assert socket_timeout == settings.health_timeout_seconds
        return _FakeRedis()

    class _FakeResponse:
        def raise_for_status(self) -> None:
            calls.append("qdrant:status_ok")

    class _FakeHttpClient:
        def __init__(self, *, timeout: float) -> None:
            assert timeout == settings.health_timeout_seconds

        async def __aenter__(self) -> "_FakeHttpClient":
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def get(self, url: str) -> _FakeResponse:
            calls.append(f"qdrant:{url}")
            return _FakeResponse()

    monkeypatch.setattr("codesight.api.health.asyncpg.connect", _fake_pg_connect)
    monkeypatch.setattr("codesight.api.health.redis_async.from_url", _fake_redis_from_url)
    monkeypatch.setattr("codesight.api.health.httpx.AsyncClient", _FakeHttpClient)

    await probe_postgres(settings)
    await probe_redis(settings)
    await probe_qdrant(settings)

    assert calls == [
        "pg:SELECT 1",
        "pg:close",
        "redis:ping",
        "redis:close",
        f"qdrant:{settings.qdrant_http_url}/readyz",
        "qdrant:status_ok",
    ]
