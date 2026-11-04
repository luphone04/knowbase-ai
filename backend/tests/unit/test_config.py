from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import PLACEHOLDER_SECRET, Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[3] / ".env.example"
# Variables in .env.example that belong to other services, not the backend Settings.
NON_BACKEND_VARIABLES = {
    "POSTGRES_DB",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "API_HOST_PORT",
    "NEXT_PUBLIC_API_BASE_URL",
}

APP_SECRET = "unit-app-secret-0123456789abcdef0123"
JWT_SECRET = "unit-jwt-secret-0123456789abcdef0123"
REQUIRED = {
    "app_secret_key": APP_SECRET,
    "jwt_secret": JWT_SECRET,
    "database_url": "postgresql+psycopg://user:pass@localhost:5432/db",
}


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep variables from the container environment out of these tests."""
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)


def make_settings(**overrides: object) -> Settings:
    return Settings(**{**REQUIRED, **overrides})  # type: ignore[arg-type]


def env_example_values() -> dict[str, str]:
    values = {}
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            name, value = line.split("=", 1)
            values[name.strip()] = value.strip()
    return values


def test_defaults_match_spec() -> None:
    settings = make_settings()
    assert settings.gemini_chat_model == "gemini-3.8-flash"
    assert settings.gemini_embedding_model == "gemini-embedding-001"
    assert settings.gemini_thinking_level == "low"
    assert settings.embedding_dimensions == 768
    assert settings.gemini_embedding_batch_size == 8
    assert settings.max_upload_mb == 10
    assert settings.daily_chat_request_limit == 30
    assert settings.cookie_secure is False
    assert settings.allowed_host_list == ["localhost", "127.0.0.1"]


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
    monkeypatch.setenv("COOKIE_SECURE", "true")
    settings = Settings()
    assert settings.gemini_thinking_level == "high"
    assert settings.embedding_dimensions == 768
    assert settings.cookie_secure is True


def test_leftover_crawler_variables_in_an_old_env_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Website crawling was removed; a local .env written before that must still load."""
    for name, value in REQUIRED.items():
        monkeypatch.setenv(name.upper(), value)
    monkeypatch.setenv("MAX_CRAWL_PAGES", "20")
    monkeypatch.setenv("CRAWL_DELAY_SECONDS", "0.5")  # was a validation error
    monkeypatch.setenv("CRAWL_USER_AGENT", "anything at all")
    monkeypatch.setenv("DAILY_CRAWL_REQUEST_LIMIT", "-1")
    settings = Settings()
    assert not hasattr(settings, "max_crawl_pages")


def test_env_example_parses_once_secrets_are_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    if not ENV_EXAMPLE.is_file():
        pytest.skip(".env.example is outside the image")
    for name, value in env_example_values().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("APP_SECRET_KEY", APP_SECRET)
    monkeypatch.setenv("JWT_SECRET", JWT_SECRET)
    Settings()


def test_env_example_placeholders_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Copying .env.example without replacing the secrets must not start the app."""
    if not ENV_EXAMPLE.is_file():
        pytest.skip(".env.example is outside the image")
    for name, value in env_example_values().items():
        monkeypatch.setenv(name, value)
    with pytest.raises(ValidationError) as excinfo:
        Settings()
    assert {error["loc"][0] for error in excinfo.value.errors()} == {
        "app_secret_key",
        "jwt_secret",
    }


def test_app_starts_without_gemini_key() -> None:
    assert make_settings().gemini_configured is False
    assert make_settings(gemini_api_key="k").gemini_configured is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("gemini_thinking_level", "minimal"),  # rejected by gemini-3.8-flash
        ("embedding_dimensions", 1536),  # schema is vector(768)
        ("max_upload_mb", 0),
        ("rrf_k", 0),
        ("retrieval_top_n", 11),  # more than RETRIEVAL_CANDIDATES (10)
        ("jwt_secret", PLACEHOLDER_SECRET),
        ("jwt_secret", "too-short-secret"),
        ("app_secret_key", PLACEHOLDER_SECRET),
        ("frontend_origin", "http://localhost:3000/app"),  # path, not an origin
        ("frontend_origin", "localhost:3000"),  # no scheme
        ("frontend_origin", "ftp://localhost"),
        ("login_failure_limit_per_email", 0),
    ],
)
def test_rejects_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        make_settings(**{field: value})


def test_production_requires_secure_cookies() -> None:
    with pytest.raises(ValidationError, match="COOKIE_SECURE"):
        make_settings(app_env="production", cookie_secure=False)
    assert make_settings(app_env="production", cookie_secure=True).cookie_secure is True


def test_frontend_origin_is_normalised() -> None:
    settings = make_settings(frontend_origin="HTTP://LocalHost:3000/")
    assert settings.frontend_origin == "http://localhost:3000"


def test_secrets_are_masked() -> None:
    settings = make_settings(gemini_api_key="super-secret-key")
    rendered = repr(settings) + settings.model_dump_json()
    for secret in ("super-secret-key", APP_SECRET, JWT_SECRET):
        assert secret not in rendered


@pytest.mark.skipif(not ENV_EXAMPLE.is_file(), reason=".env.example is outside the image")
def test_env_example_matches_settings() -> None:
    names = set(env_example_values())
    fields = {name.upper() for name in Settings.model_fields}
    assert names - NON_BACKEND_VARIABLES - fields == set(), "unknown variables in .env.example"
    assert fields - names == set(), "settings missing from .env.example"


def test_token_lifetime_is_capped_at_a_week() -> None:
    assert make_settings(access_token_expire_minutes=10_080).access_token_expire_minutes == 10_080
    with pytest.raises(ValidationError, match="less than or equal to 10080"):
        make_settings(access_token_expire_minutes=10_081)


def test_the_two_secrets_must_differ() -> None:
    with pytest.raises(ValidationError, match="must be different"):
        make_settings(jwt_secret=APP_SECRET)
