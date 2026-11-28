"""Application configuration with dual-topology defaults and SecretStr protection."""

from functools import lru_cache
from typing import Final, Literal, Self
from urllib.parse import quote_plus

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

GEMINI_MODEL_PRICING_USD_PER_1M: Final[dict[str, tuple[float, float]]] = {
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash-lite": (0.10, 0.40),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
    "gemini-3.8-flash": (0.75, 3.75),
    "gemini-4-hydrogen": (1.50, 7.50),
    "gemini-4-argon": (2.00, 10.00),
    "gemini-4-neon": (3.00, 15.00),
    "gemini-4-carbon": (2.00, 10.00),
}
ALLOWED_GEMINI_CRITIC_MODELS: Final[frozenset[str]] = frozenset(GEMINI_MODEL_PRICING_USD_PER_1M)
ALLOWED_GEMINI_AUDITOR_MODELS: Final[frozenset[str]] = ALLOWED_GEMINI_CRITIC_MODELS


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

    # Hybrid Code RAG indexing & Gemini embedding quota discipline
    index_embed_code_chunks: bool = False
    gemini_embed_tpm_budget: int = Field(default=25_000, ge=1_000)
    gemini_embed_concurrency: int = Field(default=2, ge=1, le=10)
    index_task_soft_time_limit_seconds: int = Field(default=300, ge=30)
    index_task_hard_time_limit_seconds: int = Field(default=360, ge=60)

    # Multi-agent LangGraph LLM orchestration (kept separate from embedding limits)
    llm_provider: Literal["gemini", "openai", "anthropic"] = "gemini"
    auditor_model: str = "gemini-2.5-flash"
    critic_model: str = "gemini-2.5-flash"
    llm_max_concurrency: int = Field(default=3, ge=1, le=10)
    langfuse_tracing_enabled: bool = True

    # Optional Phase 2+ secrets (kept None until configured)
    github_app_id: str | None = None
    github_private_key: SecretStr | None = None
    github_webhook_secret: SecretStr | None = None
    github_token: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: SecretStr | None = None
    langfuse_host: str = "https://cloud.langfuse.com"

    # Optional Milestone 2 live evaluation overrides
    codesight_live_py_repo: str | None = None
    codesight_live_ts_repo: str | None = None
    codesight_qdrant_container: str | None = None

    @model_validator(mode="after")
    def _validate_production_secrets(self) -> Self:
        if (
            self.app_env == "production"
            and self.postgres_password.get_secret_value() == "codesight_dev_password"
        ):
            raise ValueError("POSTGRES_PASSWORD must not use the dev default in production.")
        if self.index_task_hard_time_limit_seconds <= self.index_task_soft_time_limit_seconds:
            raise ValueError(
                "INDEX_TASK_HARD_TIME_LIMIT_SECONDS must exceed INDEX_TASK_SOFT_TIME_LIMIT_SECONDS."
            )
        cleaned_auditor = self.auditor_model.strip()
        cleaned_critic = self.critic_model.strip()
        if not cleaned_auditor:
            raise ValueError("AUDITOR_MODEL must not be empty.")
        if not cleaned_critic:
            raise ValueError("CRITIC_MODEL must not be empty.")
        self.auditor_model = cleaned_auditor
        self.critic_model = cleaned_critic
        if self.llm_provider == "gemini":
            if cleaned_auditor not in ALLOWED_GEMINI_AUDITOR_MODELS:
                raise ValueError(
                    "AUDITOR_MODEL must be a supported Gemini model "
                    "(e.g. 'gemini-2.5-flash', 'gemini-2.5-pro', 'gemini-3.5-flash-lite') "
                    "when LLM_PROVIDER is 'gemini'."
                )
            if cleaned_critic not in ALLOWED_GEMINI_CRITIC_MODELS:
                raise ValueError(
                    "CRITIC_MODEL must be a supported Gemini model "
                    "(e.g. 'gemini-2.5-flash', 'gemini-2.5-pro', 'gemini-3.5-flash-lite') "
                    "when LLM_PROVIDER is 'gemini'."
                )
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

    @property
    def has_gemini_api_key(self) -> bool:
        """Return True when a non-empty GEMINI_API_KEY is configured."""
        return bool(
            self.gemini_api_key is not None and self.gemini_api_key.get_secret_value().strip()
        )

    @property
    def has_active_llm_api_key(self) -> bool:
        """Return True when the API key required by the active `llm_provider` is configured."""
        if self.llm_provider == "gemini":
            return self.has_gemini_api_key
        if self.llm_provider == "openai":
            return bool(
                self.openai_api_key is not None and self.openai_api_key.get_secret_value().strip()
            )
        return bool(
            self.anthropic_api_key is not None and self.anthropic_api_key.get_secret_value().strip()
        )

    @property
    def has_langfuse_credentials(self) -> bool:
        """Return True when both LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are non-empty."""
        return bool(
            self.langfuse_public_key is not None
            and self.langfuse_public_key.strip()
            and self.langfuse_secret_key is not None
            and self.langfuse_secret_key.get_secret_value().strip()
        )

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
