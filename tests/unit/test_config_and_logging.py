"""Unit tests for Settings configuration and structured JSON logging."""

import io
import json
import logging
from pathlib import Path

import pytest
import structlog
from pydantic import SecretStr

from codesight.core.config import Settings, get_settings
from codesight.core.logging import (
    bind_trace_id,
    clear_trace_context,
    configure_logging,
    get_trace_id,
    sanitize_trace_id,
)


def test_settings_canonical_host_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "POSTGRES_HOST",
        "POSTGRES_PORT",
        "REDIS_HOST",
        "REDIS_PORT",
        "REDIS_PASSWORD",
        "QDRANT_HOST",
        "QDRANT_HTTP_PORT",
        "QDRANT_GRPC_PORT",
        "API_PORT",
        "GITHUB_WEBHOOK_SECRET",
        "GEMINI_API_KEY",
    ):
        monkeypatch.delenv(key, raising=False)

    s = Settings(_env_file=None)
    assert s.postgres_host == "127.0.0.1"
    assert s.postgres_port == 5433
    assert s.redis_host == "127.0.0.1"
    assert s.redis_port == 6381
    assert s.redis_url == "redis://127.0.0.1:6381/0"
    assert s.qdrant_http_port == 6333
    assert s.qdrant_grpc_port == 6334
    assert s.api_port == 8082
    assert s.qdrant_http_url == "http://127.0.0.1:6333"
    assert s.github_webhook_secret is None
    assert s.gemini_api_key is None


def test_settings_docker_override_and_special_char_dsn_encoding() -> None:
    s = Settings(
        _env_file=None,
        postgres_host="codesight-postgres",
        postgres_port=5432,
        postgres_user="cs:user@1",
        postgres_password=SecretStr("p@ss:w/rd#1%"),
        postgres_db="codesight",
        redis_host="codesight-redis",
        redis_port=6379,
        redis_password=SecretStr("r#dis@pw"),
    )
    assert (
        s.postgres_dsn
        == "postgresql://cs%3Auser%401:p%40ss%3Aw%2Frd%231%25@codesight-postgres:5432/codesight"
    )
    assert s.sqlalchemy_async_dsn.startswith(
        "postgresql+asyncpg://cs%3Auser%401:p%40ss%3Aw%2Frd%231%25@codesight-postgres:5432/"
    )
    assert s.redis_url == "redis://:r%23dis%40pw@codesight-redis:6379/0"
    assert "p@ss:w/rd#1%" not in repr(s)
    assert "p@ss:w/rd#1%" not in str(s)
    assert "r#dis@pw" not in repr(s)
    assert "r#dis@pw" not in str(s)


def test_get_settings_cache_can_clear() -> None:
    get_settings.cache_clear()
    first = get_settings()
    second = get_settings()
    assert first is second
    get_settings.cache_clear()
    assert get_settings() is not first


def test_sanitize_trace_id_accepts_valid_and_rejects_hostile_inputs() -> None:
    assert sanitize_trace_id("  gh-delivery_123.abc  ") == "gh-delivery_123.abc"
    for bad in [None, "", "   ", "evil\r\ninjected: true", "x" * 129, "bad space", "a/b"]:
        cleaned = sanitize_trace_id(bad)
        assert len(cleaned) == 32 and cleaned.isalnum()


def test_configure_logging_emits_json_with_trace_id_for_structlog_and_stdlib() -> None:
    buf = io.StringIO()
    clear_trace_context()
    configure_logging(level="INFO", service_name="codesight-test", stream=buf)

    structlog.get_logger("codesight.startup").info("booting")

    tid = bind_trace_id("trace-abc-001")
    assert tid == "trace-abc-001"
    assert get_trace_id() == "trace-abc-001"

    structlog.get_logger("codesight.unit").info("custom_event", answer=42)
    logging.getLogger("uvicorn.error").warning("stdlib_warning")

    clear_trace_context()
    assert get_trace_id() is None
    assert logging.getLogger("uvicorn.access").propagate is False

    generated = bind_trace_id()
    assert len(generated) == 32 and generated.isalnum()
    clear_trace_context()

    lines = [json.loads(line) for line in buf.getvalue().strip().splitlines()]
    assert len(lines) == 3
    assert lines[0]["event"] == "booting"
    assert lines[0]["trace_id"] == "-"
    assert lines[1]["event"] == "custom_event"
    assert lines[1]["answer"] == 42
    assert lines[1]["trace_id"] == "trace-abc-001"
    assert lines[1]["service"] == "codesight-test"
    assert lines[1]["logger"] == "codesight.unit"
    assert "timestamp" in lines[1]
    assert lines[2]["event"] == "stdlib_warning"
    assert lines[2]["trace_id"] == "trace-abc-001"
    assert lines[2]["service"] == "codesight-test"
    assert lines[2]["level"] == "warning"


def test_configure_logging_quiets_httpx_info_unless_debug() -> None:
    buf_info = io.StringIO()
    clear_trace_context()
    configure_logging(level="INFO", service_name="codesight-test", stream=buf_info)

    logging.getLogger("httpx").info("HTTP Request: GET http://codesight-qdrant:6333/readyz")
    logging.getLogger("httpcore").info("connect_tcp.started")
    logging.getLogger("httpx").warning("httpx_degraded_retry")

    info_lines = [json.loads(line) for line in buf_info.getvalue().strip().splitlines()]
    assert len(info_lines) == 1
    assert info_lines[0]["event"] == "httpx_degraded_retry"
    assert info_lines[0]["level"] == "warning"

    buf_debug = io.StringIO()
    configure_logging(level="DEBUG", service_name="codesight-test", stream=buf_debug)
    logging.getLogger("httpx").info("HTTP Request: GET http://codesight-qdrant:6333/readyz")

    debug_lines = [json.loads(line) for line in buf_debug.getvalue().strip().splitlines()]
    assert len(debug_lines) == 1
    assert "HTTP Request" in debug_lines[0]["event"]


def test_settings_rejects_dev_password_in_production_and_invalid_ports() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="POSTGRES_PASSWORD"):
        Settings(_env_file=None, app_env="production")

    prod_ok = Settings(
        _env_file=None,
        app_env="production",
        postgres_password=SecretStr("non-default-prod-secret"),
    )
    assert prod_ok.qdrant_collection_name == "codesight_ast_index"

    with pytest.raises(ValidationError):
        Settings(_env_file=None, api_port=0)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, health_timeout_seconds=-1.0)


def test_settings_loads_live_eval_overrides_from_dotenv_without_shell_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for key in ("CODESIGHT_LIVE_PY_REPO", "CODESIGHT_LIVE_TS_REPO", "CODESIGHT_QDRANT_CONTAINER"):
        monkeypatch.delenv(key, raising=False)

    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "CODESIGHT_LIVE_PY_REPO=org/dotenv-py\n"
        "CODESIGHT_LIVE_TS_REPO=org/dotenv-ts\n"
        "CODESIGHT_QDRANT_CONTAINER=codesight-qdrant-custom\n",
        encoding="utf-8",
    )
    loaded = Settings(_env_file=dotenv)
    assert loaded.codesight_live_py_repo == "org/dotenv-py"
    assert loaded.codesight_live_ts_repo == "org/dotenv-ts"
    assert loaded.codesight_qdrant_container == "codesight-qdrant-custom"
