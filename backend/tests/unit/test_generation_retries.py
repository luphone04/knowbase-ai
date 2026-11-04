"""Generation and embeddings retry separately (GEMINI_GENERATION_MAX_RETRIES, GEMINI_MAX_RETRIES).

The providers are built by the real factory, so these tests cover the wiring
the application uses, against a scripted stand-in for the Gemini client.
"""

from collections.abc import Callable, Iterator

import pytest
from google.genai import errors

from app.config import Settings, get_settings
from app.providers import factory
from app.providers.errors import (
    ProviderConfigurationError,
    ProviderError,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.providers.health import provider_health
from tests.fakes.gemini import (
    FakeGeminiClient,
    daily_quota_error,
    deadline_error,
    generation_response,
    invalid_key_error,
    per_input_embeddings,
    rate_limit_error,
    unavailable_error,
)

FAKE_KEY = "test-gemini-key-not-real-0123456789"
SettingsFactory = Callable[..., Settings]


@pytest.fixture
def settings_with(monkeypatch: pytest.MonkeyPatch) -> Iterator[SettingsFactory]:
    def apply(**env: str) -> Settings:
        # Tiny backoff, so the embedding retry below doesn't slow the suite.
        env = {
            "GEMINI_API_KEY": FAKE_KEY,
            "GEMINI_RETRY_BASE_DELAY_SECONDS": "0.01",
            "GEMINI_RETRY_MAX_DELAY_SECONDS": "0.02",
            **env,
        }
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        get_settings.cache_clear()
        return get_settings()

    provider_health.reset()
    yield apply
    provider_health.reset()
    get_settings.cache_clear()


def generations(client: FakeGeminiClient) -> int:
    return sum(call.method == "generate_content" for call in client.models.calls)


def embeddings(client: FakeGeminiClient) -> int:
    return sum(call.method == "embed_content" for call in client.models.calls)


def test_the_defaults_give_generation_one_attempt_and_embeddings_three_retries(
    settings_with: SettingsFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GEMINI_GENERATION_MAX_RETRIES", raising=False)
    monkeypatch.delenv("GEMINI_MAX_RETRIES", raising=False)
    settings = settings_with()
    client = FakeGeminiClient()
    generation = factory.build_generation_provider(settings, client)  # type: ignore[arg-type]
    embedding = factory.build_embedding_provider(settings, client)  # type: ignore[arg-type]
    assert generation._calls.policy.max_retries == 0
    assert embedding._calls.policy.max_retries == 3


def test_generation_uses_its_own_setting_not_the_embedding_one(
    settings_with: SettingsFactory,
) -> None:
    settings = settings_with(GEMINI_GENERATION_MAX_RETRIES="1", GEMINI_MAX_RETRIES="4")
    assert factory.generation_retry_policy(settings).max_retries == 1
    assert factory.retry_policy(settings).max_retries == 4
    # The rest of the policy (backoff, overall deadline) is shared.
    assert factory.generation_retry_policy(settings).max_elapsed_seconds == 120


def test_zero_generation_retries_still_make_exactly_one_request(
    settings_with: SettingsFactory,
) -> None:
    client = FakeGeminiClient()
    client.models.generate_script.append(generation_response("Laptops: 14 days [S1]."))
    provider = factory.build_generation_provider(
        settings_with(GEMINI_GENERATION_MAX_RETRIES="0"),
        client,  # type: ignore[arg-type]
    )
    result = provider.generate("Can I return a laptop?")
    assert result.text == "Laptops: 14 days [S1]." and result.attempts == 1
    assert generations(client) == 1


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (unavailable_error(), ProviderUnavailable),
        (unavailable_error(500), ProviderUnavailable),
        (deadline_error(), ProviderTimeout),
        (rate_limit_error(), ProviderRateLimited),
        (daily_quota_error(), ProviderQuotaExhausted),
        (invalid_key_error(), ProviderConfigurationError),
    ],
    ids=["503", "500", "504", "rate-limit", "daily-quota", "bad-key"],
)
def test_with_zero_retries_a_failure_is_one_request_and_classified_as_before(
    settings_with: SettingsFactory, failure: errors.APIError, expected: type[ProviderError]
) -> None:
    client = FakeGeminiClient()
    # A second scripted reply would succeed: it must never be requested.
    client.models.generate_script.extend([failure, generation_response("ok")])
    provider = factory.build_generation_provider(
        settings_with(GEMINI_GENERATION_MAX_RETRIES="0", GEMINI_MAX_RETRIES="3"),
        client,  # type: ignore[arg-type]
    )
    with pytest.raises(expected) as excinfo:
        provider.generate("Can I return a laptop?")
    assert generations(client) == 1 and excinfo.value.attempts == 1
    assert provider_health.snapshot().last_error_category == expected.category


def test_a_generation_retry_setting_above_zero_still_retries(
    settings_with: SettingsFactory,
) -> None:
    client = FakeGeminiClient()
    client.models.generate_script.extend([unavailable_error(), generation_response("ok")])
    provider = factory.build_generation_provider(
        settings_with(GEMINI_GENERATION_MAX_RETRIES="1"),
        client,  # type: ignore[arg-type]
    )
    assert provider.generate("q").attempts == 2
    assert generations(client) == 2


def test_embeddings_keep_their_retries_when_generation_has_none(
    settings_with: SettingsFactory,
) -> None:
    client = FakeGeminiClient()
    client.models.embed_script.extend([unavailable_error(), per_input_embeddings])
    provider = factory.build_embedding_provider(
        settings_with(GEMINI_GENERATION_MAX_RETRIES="0", GEMINI_MAX_RETRIES="3"),
        client,  # type: ignore[arg-type]
    )
    result = provider.embed_documents(["a chunk of text"])
    assert result.attempts == 2 and len(result.vectors) == 1
    assert embeddings(client) == 2


def test_embedding_retries_stay_bounded(settings_with: SettingsFactory) -> None:
    client = FakeGeminiClient()
    client.models.embed_script.extend([unavailable_error()] * 10)
    provider = factory.build_embedding_provider(
        settings_with(GEMINI_MAX_RETRIES="2"),
        client,  # type: ignore[arg-type]
    )
    with pytest.raises(ProviderUnavailable) as excinfo:
        provider.embed_query("question")
    assert embeddings(client) == 3 and excinfo.value.attempts == 3
