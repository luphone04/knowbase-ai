"""Client construction: key handling, free-tier endpoint only, no SDK retries."""

from collections.abc import Callable, Iterator

import pytest
from google import genai

from app.config import Settings, get_settings
from app.providers import factory
from app.providers.errors import ProviderNotConfigured

FAKE_KEY = "test-gemini-key-not-real-0123456789"

SettingsFactory = Callable[..., Settings]


@pytest.fixture
def settings_with(monkeypatch: pytest.MonkeyPatch) -> Iterator[SettingsFactory]:
    def apply(**env: str) -> Settings:
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        get_settings.cache_clear()
        return get_settings()

    yield apply
    get_settings.cache_clear()
    factory.get_generation_provider.cache_clear()
    factory.get_embedding_provider.cache_clear()


@pytest.mark.parametrize("key", ["", "   "])
def test_missing_key_is_reported_before_any_client_exists(
    settings_with: SettingsFactory, monkeypatch: pytest.MonkeyPatch, key: str
) -> None:
    def no_client(*args: object, **kwargs: object) -> None:
        raise AssertionError("a Gemini client must not be created without a key")

    monkeypatch.setattr(genai, "Client", no_client)  # the factory looks it up at call time
    settings = settings_with(GEMINI_API_KEY=key)
    assert settings.gemini_configured is False
    with pytest.raises(ProviderNotConfigured) as excinfo:
        factory.build_gemini_client(settings)
    assert "GEMINI_API_KEY" in str(excinfo.value)
    with pytest.raises(ProviderNotConfigured):
        factory.get_generation_provider()
    with pytest.raises(ProviderNotConfigured):
        factory.get_embedding_provider()


def test_client_targets_the_gemini_developer_api_never_vertex(
    settings_with: SettingsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Even if the environment asks the SDK for Vertex AI (separately billed), we don't use it.
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    client = factory.build_gemini_client(settings_with(GEMINI_API_KEY=FAKE_KEY))
    assert isinstance(client, genai.Client)
    assert client.vertexai is False


def test_client_has_a_timeout_and_no_sdk_side_retries(settings_with: SettingsFactory) -> None:
    client = factory.build_gemini_client(
        settings_with(GEMINI_API_KEY=FAKE_KEY, GEMINI_REQUEST_TIMEOUT_SECONDS="12.5")
    )
    options = client._api_client._http_options
    assert options.timeout == 12500  # milliseconds
    assert options.retry_options is not None and options.retry_options.attempts == 1


def test_key_comes_from_backend_settings_only(
    settings_with: SettingsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # GOOGLE_API_KEY would win inside the SDK if we relied on its env lookup; we never do.
    monkeypatch.setenv("GOOGLE_API_KEY", "some-other-key-from-the-environment")
    client = factory.build_gemini_client(settings_with(GEMINI_API_KEY=FAKE_KEY))
    assert client._api_client.api_key == FAKE_KEY


def test_providers_use_model_names_from_configuration(settings_with: SettingsFactory) -> None:
    settings = settings_with(
        GEMINI_API_KEY=FAKE_KEY,
        GEMINI_CHAT_MODEL="configured-chat",
        GEMINI_EMBEDDING_MODEL="configured-embed",
        GEMINI_EMBEDDING_BATCH_SIZE="3",
        GEMINI_MAX_RETRIES="1",
    )
    generation = factory.build_generation_provider(settings)
    embedding = factory.build_embedding_provider(settings)
    assert generation.model == "configured-chat"
    assert (embedding.model, embedding.dimensions) == ("configured-embed", 768)
    assert embedding._batch_size == 3
    assert generation._calls.policy.max_retries == 1


def test_retry_policy_comes_from_settings(settings_with: SettingsFactory) -> None:
    policy = factory.retry_policy(
        settings_with(
            GEMINI_MAX_RETRIES="2",
            GEMINI_RETRY_BASE_DELAY_SECONDS="0.5",
            GEMINI_RETRY_MAX_DELAY_SECONDS="8",
        )
    )
    assert (policy.max_retries, policy.base_delay_seconds, policy.max_delay_seconds) == (2, 0.5, 8)


def test_retry_settings_are_bounded(settings_with: SettingsFactory) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        settings_with(GEMINI_MAX_RETRIES="50")
    with pytest.raises(ValidationError):
        settings_with(GEMINI_RETRY_BASE_DELAY_SECONDS="10", GEMINI_RETRY_MAX_DELAY_SECONDS="5")
