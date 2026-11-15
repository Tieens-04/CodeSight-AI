"""Unit tests for Settings configuration and structured JSON logging."""

import pytest
from pydantic import SecretStr

from codesight.core.config import Settings, get_settings


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
    assert s.postgres_host == "localhost"
    assert s.postgres_port == 5433
    assert s.redis_host == "localhost"
    assert s.redis_port == 6381
    assert s.redis_url == "redis://localhost:6381/0"
    assert s.qdrant_http_port == 6333
    assert s.qdrant_grpc_port == 6334
    assert s.api_port == 8082
    assert s.qdrant_http_url == "http://localhost:6333"
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
