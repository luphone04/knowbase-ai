from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"
# Variables in .env.example that belong to other services, not the backend Settings.
NON_BACKEND_VARIABLES = {
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "API_HOST_PORT",
    "NEXT_PUBLIC_API_BASE_URL",
}

REQUIRED = {
    "app_secret_key": "test-app-secret",
    "jwt_secret": "test-jwt-secret",
    "database_url": "postgresql+psycopg://user:pass@localhost:5432/db",
}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep variables from the container environment out of these tests."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def make_settings(**overrides: object) -> Settings:
    return Settings(**{**REQUIRED, **overrides})  # type: ignore[arg-type]


def test_defaults_match_spec() -> None:
    settings = make_settings()
    assert settings.gemini_chat_model == "gemini-3.8-flash"
    assert settings.gemini_embedding_model == "gemini-embedding-001"
    assert settings.gemini_thinking_level == "low"
    assert settings.embedding_dimensions == 768
    assert settings.gemini_embedding_batch_size == 8
    assert settings.max_upload_mb == 10
    assert settings.max_crawl_pages == 20
    assert settings.daily_chat_request_limit == 30


def test_required_settings_are_enforced() -> None:
    with pytest.raises(ValidationError) as excinfo:
        Settings()
    missing = {error["loc"][0] for error in excinfo.value.errors()}
    assert missing == set(REQUIRED)


def test_reads_upper_case_environment_variables(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name.upper(), value)
    monkeypatch.setenv("GEMINI_THINKING_LEVEL", "high")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "768")
    monkeypatch.setenv("CRAWL_DELAY_SECONDS", "1")
    settings = Settings()
    assert settings.gemini_thinking_level == "high"
    assert settings.embedding_dimensions == 768
    assert settings.crawl_delay_seconds == 1.0


def test_every_env_example_value_parses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The committed template must load as-is (environment values are strings)."""
    if not ENV_EXAMPLE.is_file():
        pytest.skip(".env.example is outside the image")
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            monkeypatch.setenv(name.strip(), value.strip())
    Settings()


def test_app_starts_without_gemini_key() -> None:
    assert make_settings().gemini_configured is False
    assert make_settings(gemini_api_key="k").gemini_configured is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("gemini_thinking_level", "minimal"),  # rejected by gemini-3.8-flash
        ("embedding_dimensions", 1536),  # schema is vector(768)
        ("crawl_delay_seconds", 0.5),  # SPEC.md §7.3: at least 1 second
        ("max_upload_mb", 0),
    ],
)
def test_rejects_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_secrets_are_masked() -> None:
    settings = make_settings(gemini_api_key="super-secret-key")
    rendered = repr(settings) + settings.model_dump_json()
    for secret in ("super-secret-key", "test-app-secret", "test-jwt-secret"):
        assert secret not in rendered


@pytest.mark.skipif(not ENV_EXAMPLE.is_file(), reason=".env.example is outside the image")
def test_env_example_matches_settings() -> None:
    names = {
        line.split("=", 1)[0].strip()
        for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    fields = {name.upper() for name in Settings.model_fields}
    assert names - NON_BACKEND_VARIABLES - fields == set(), "unknown variables in .env.example"
    assert fields - names == set(), "settings missing from .env.example"
