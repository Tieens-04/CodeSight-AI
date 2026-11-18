"""Application configuration with dual-topology defaults and SecretStr protection."""

from functools import lru_cache
from typing import Literal
from urllib.parse import quote_plus

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings defaulting to host-mapped ports and supporting Docker overrides."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Core application metadata
    app_name: str = "codesight-ai"
    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # FastAPI Gateway (host port 8082 avoids FlintFlow 8080 & DocuMind 8081)
    api_host: str = "0.0.0.0"
    api_port: int = 8082

    # PostgreSQL 16 (host port 5433 avoids DocuMind 5432)
    postgres_host: str = "localhost"
    postgres_port: int = 5433
    postgres_user: str = "codesight"
    postgres_password: SecretStr = SecretStr("codesight_dev_password")
    postgres_db: str = "codesight"

    # Redis 7 (host port 6381 avoids FlintFlow 6379 & DocuMind 6380)
    redis_host: str = "localhost"
    redis_port: int = 6381
    redis_db: int = 0
    redis_password: SecretStr | None = None

    # Qdrant (HTTP 6333 / gRPC 6334)
    qdrant_host: str = "localhost"
    qdrant_http_port: int = 6333
    qdrant_grpc_port: int = 6334

    # Health probe timeout
    health_timeout_seconds: float = 1.5

    # Week 1 E2E milestone smoke review behavior (set False when LangGraph AI pipeline takes over)
    week1_smoke_inline_comment: bool = True

    # Optional Phase 2+ secrets (kept None until configured)
    github_app_id: str | None = None
    github_private_key: SecretStr | None = None
    github_webhook_secret: SecretStr | None = None
    github_token: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    def _build_postgres_dsn(self, scheme: str) -> str:
        user = quote_plus(self.postgres_user)
        password = quote_plus(self.postgres_password.get_secret_value())
        return (
            f"{scheme}://{user}:{password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def postgres_dsn(self) -> str:
        """PostgreSQL DSN for asyncpg connections."""
        return self._build_postgres_dsn("postgresql")

    @property
    def sqlalchemy_async_dsn(self) -> str:
        """PostgreSQL DSN for SQLAlchemy 2.0 async engine."""
        return self._build_postgres_dsn("postgresql+asyncpg")

    @property
    def redis_url(self) -> str:
        """Redis broker and cache URL."""
        raw_pw = self.redis_password.get_secret_value() if self.redis_password else ""
        auth = f":{quote_plus(raw_pw)}@" if raw_pw else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    @property
    def qdrant_http_url(self) -> str:
        """Qdrant HTTP base URL."""
        return f"http://{self.qdrant_host}:{self.qdrant_http_port}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached Settings instance."""
    return Settings()
