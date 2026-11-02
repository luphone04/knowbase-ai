"""Backend configuration, read from environment variables.

Every field maps to an upper-case variable in `.env.example`. A unit test keeps
the two in sync.
"""

from functools import lru_cache
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SCHEMA_EMBEDDING_DIMENSIONS = 768
PLACEHOLDER_SECRET = "replace-with-random-secret"
MIN_SECRET_LENGTH = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    app_env: Literal["development", "test", "production"] = "development"
    app_secret_key: SecretStr
    jwt_secret: SecretStr
    access_token_expire_minutes: int = Field(default=480, gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # Send the auth cookie only over HTTPS. Required in production; off for local HTTP.
    cookie_secure: bool = False
    # Host header allowlist (comma-separated) against DNS-rebinding and Host spoofing.
    allowed_hosts: str = "localhost,127.0.0.1"

    auth_rate_limit_window_seconds: int = Field(default=900, gt=0)
    login_rate_limit_per_ip: int = Field(default=20, gt=0)
    login_failure_limit_per_email: int = Field(default=5, gt=0)
    register_rate_limit_per_ip: int = Field(default=5, gt=0)

    database_url: str

    # The app must start without a key (SPEC.md §2.4); AI features report it as missing.
    gemini_api_key: SecretStr = SecretStr("")
    gemini_chat_model: str = "gemini-3.8-flash"
    gemini_thinking_level: Literal["low", "medium", "high"] = "low"
    gemini_embedding_model: str = "gemini-embedding-001"
    embedding_dimensions: int = SCHEMA_EMBEDDING_DIMENSIONS
    gemini_embedding_batch_size: int = Field(default=8, ge=1, le=100)
    # Includes thinking tokens (SPEC.md §2.3).
    gemini_max_output_tokens: int = Field(default=1200, ge=1, le=65536)
    # Sent to Gemini as the server deadline too (X-Server-Timeout). 90 s leaves room for
    # slow periods: a one-token reply took 50.7 s during M3 live testing.
    gemini_request_timeout_seconds: float = Field(default=90, gt=0, le=300)
    # Bounded retries for 429 rate limits and 5xx/network errors only.
    gemini_max_retries: int = Field(default=3, ge=0, le=5)
    gemini_retry_base_delay_seconds: float = Field(default=1, gt=0, le=10)
    gemini_retry_max_delay_seconds: float = Field(default=20, gt=0, le=120)

    upload_dir: str = "/app/data/uploads"
    max_upload_mb: int = Field(default=10, gt=0, le=100)
    # Extracted text below this length is rejected (e.g. scanned PDFs without OCR).
    min_document_chars: int = Field(default=50, ge=1)
    chunk_target_chars: int = Field(default=1800, ge=200)
    chunk_max_chars: int = Field(default=2400, ge=200)
    chunk_overlap_chars: int = Field(default=250, ge=0)
    max_crawl_pages: int = Field(default=20, gt=0)
    max_crawl_depth: int = Field(default=2, ge=0)
    crawl_delay_seconds: float = Field(default=1, ge=1)
    crawl_timeout_seconds: float = Field(default=10, gt=0)

    frontend_origin: str = "http://localhost:3000"

    daily_chat_request_limit: int = Field(default=30, ge=0)
    daily_embedding_text_limit: int = Field(default=300, ge=0)

    worker_poll_seconds: float = Field(default=5, gt=0)
    job_max_attempts: int = Field(default=3, ge=1, le=10)
    # A running job's lock is renewed every heartbeat; a lock older than the lease
    # belongs to a crashed worker and the job is recovered.
    job_lease_seconds: int = Field(default=120, ge=30)
    job_heartbeat_seconds: int = Field(default=30, ge=5)
    job_retry_base_seconds: int = Field(default=30, ge=1)

    @field_validator("app_secret_key", "jwt_secret")
    @classmethod
    def _random_secret(cls, value: SecretStr) -> SecretStr:
        secret = value.get_secret_value()
        if secret == PLACEHOLDER_SECRET or len(secret) < MIN_SECRET_LENGTH:
            raise ValueError(
                f"must be a random value of at least {MIN_SECRET_LENGTH} characters, "
                "not the .env.example placeholder"
            )
        return value

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

    @field_validator("frontend_origin")
    @classmethod
    def _origin_format(cls, value: str) -> str:
        # Browsers send Origin as scheme://host[:port]; compare against that exact form.
        parts = urlsplit(value.strip())
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("must be an http(s) origin such as http://localhost:3000")
        if parts.path not in ("", "/") or parts.query or parts.fragment:
            raise ValueError("must be an origin only, without a path, query, or fragment")
        return f"{parts.scheme}://{parts.netloc}".lower()

    @model_validator(mode="after")
    def _production_requires_secure_cookies(self) -> Self:
        if self.app_env == "production" and not self.cookie_secure:
            raise ValueError("COOKIE_SECURE must be true when APP_ENV=production")
        return self

    @model_validator(mode="after")
    def _chunk_sizes_are_consistent(self) -> Self:
        if not self.chunk_overlap_chars < self.chunk_target_chars <= self.chunk_max_chars:
            raise ValueError(
                "chunk settings need CHUNK_OVERLAP_CHARS < CHUNK_TARGET_CHARS <= CHUNK_MAX_CHARS"
            )
        return self

    @model_validator(mode="after")
    def _heartbeat_beats_the_lease(self) -> Self:
        if self.job_heartbeat_seconds * 2 > self.job_lease_seconds:
            raise ValueError("JOB_HEARTBEAT_SECONDS must be at most half of JOB_LEASE_SECONDS")
        return self

    @model_validator(mode="after")
    def _retry_delays_are_ordered(self) -> Self:
        if self.gemini_retry_max_delay_seconds < self.gemini_retry_base_delay_seconds:
            raise ValueError(
                "GEMINI_RETRY_MAX_DELAY_SECONDS must be at least GEMINI_RETRY_BASE_DELAY_SECONDS"
            )
        return self

    @property
    def gemini_configured(self) -> bool:
        return bool(self.gemini_api_key.get_secret_value().strip())

    @property
    def secret_values(self) -> list[str]:
        """Values that must never appear in logs (used by the log redaction filter)."""
        candidates = (self.gemini_api_key, self.jwt_secret, self.app_secret_key)
        return [s.get_secret_value() for s in candidates if s.get_secret_value().strip()]

    @property
    def allowed_host_list(self) -> list[str]:
        return [host.strip() for host in self.allowed_hosts.split(",") if host.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
