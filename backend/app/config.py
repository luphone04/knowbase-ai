"""Backend configuration, read from environment variables.

Every field maps to an upper-case variable in `.env.example`. A unit test keeps
the two in sync.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SCHEMA_EMBEDDING_DIMENSIONS = 768


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    app_secret_key: SecretStr
    jwt_secret: SecretStr
    access_token_expire_minutes: int = Field(default=480, gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    database_url: str

    # The app must start without a key (SPEC.md §2.4); AI features report it as missing.
    gemini_api_key: SecretStr = SecretStr("")
    gemini_chat_model: str = "gemini-3.8-flash"
    gemini_thinking_level: Literal["low", "medium", "high"] = "low"
    gemini_embedding_model: str = "gemini-embedding-001"
    embedding_dimensions: int = SCHEMA_EMBEDDING_DIMENSIONS
    gemini_embedding_batch_size: int = Field(default=8, ge=1, le=100)

    upload_dir: str = "/app/data/uploads"
    max_upload_mb: int = Field(default=10, gt=0)
    max_crawl_pages: int = Field(default=20, gt=0)
    max_crawl_depth: int = Field(default=2, ge=0)
    crawl_delay_seconds: float = Field(default=1, ge=1)
    crawl_timeout_seconds: float = Field(default=10, gt=0)

    frontend_origin: str = "http://localhost:3000"

    daily_chat_request_limit: int = Field(default=30, ge=0)
    daily_embedding_text_limit: int = Field(default=300, ge=0)

    worker_poll_seconds: float = Field(default=5, gt=0)

    @field_validator("embedding_dimensions")
    @classmethod
    def _matches_schema(cls, value: int) -> int:
        if value != SCHEMA_EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"must be {SCHEMA_EMBEDDING_DIMENSIONS}: the schema stores "
                f"vector({SCHEMA_EMBEDDING_DIMENSIONS}); another size needs a migration "
                "and a full re-index"
            )
        return value

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()
