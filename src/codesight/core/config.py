"""Application configuration with dual-topology defaults and SecretStr protection."""

from functools import lru_cache
from typing import Literal, Self
from urllib.parse import quote_plus

from pydantic import Field, SecretStr, model_validator
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
    api_port: int = Field(default=8082, ge=1, le=65535)

    # PostgreSQL 16 (host port 5433 avoids DocuMind 5432; 127.0.0.1 matches loopback Docker binds)
    postgres_host: str = "127.0.0.1"
    postgres_port: int = Field(default=5433, ge=1, le=65535)
    postgres_user: str = "codesight"
    postgres_password: SecretStr = SecretStr("codesight_dev_password")
    postgres_db: str = "codesight"

    # Redis 7 (host port 6381 avoids FlintFlow 6379 & DocuMind 6380)
    redis_host: str = "127.0.0.1"
    redis_port: int = Field(default=6381, ge=1, le=65535)
    redis_db: int = Field(default=0, ge=0, le=15)
    redis_password: SecretStr | None = None

    # Qdrant (HTTP 6333 / gRPC 6334)
    qdrant_host: str = "127.0.0.1"
    qdrant_http_port: int = Field(default=6333, ge=1, le=65535)
    qdrant_grpc_port: int = Field(default=6334, ge=1, le=65535)
    qdrant_collection_name: str = "codesight_ast_index"

    # Health probe timeout
    health_timeout_seconds: float = Field(default=1.5, gt=0.0, le=60.0)

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

    @model_validator(mode="after")
    def _validate_production_secrets(self) -> Self:
        if (
            self.app_env == "production"
            and self.postgres_password.get_secret_value() == "codesight_dev_password"
        ):
            raise ValueError("POSTGRES_PASSWORD must not use the dev default in production.")
        return self

    @property
    def has_github_app_credentials(self) -> bool:
        """Return True when both GITHUB_APP_ID and GITHUB_PRIVATE_KEY are non-empty."""
        return bool(
            self.github_app_id is not None
            and self.github_app_id.strip()
            and self.github_private_key is not None
            and self.github_private_key.get_secret_value().strip()
        )

    @property
    def has_github_token(self) -> bool:
        """Return True when a non-empty GITHUB_TOKEN PAT is configured."""
        return bool(self.github_token is not None and self.github_token.get_secret_value().strip())

    @property
    def has_github_credentials(self) -> bool:
        """Return True when either GitHub App or PAT credentials are configured."""
        return self.has_github_app_credentials or self.has_github_token

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
