import pytest

from app.api.v1.gemini_calls import provider_failure
from app.providers.errors import (
    IncompleteGeneration,
    InvalidProviderResponse,
    ProviderConfigurationError,
    ProviderError,
    ProviderNotConfigured,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.providers.interfaces import TokenUsage


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (ProviderNotConfigured(), 503, "ai_not_configured"),
        (ProviderQuotaExhausted("Daily quota used up."), 429, "ai_quota"),
        (ProviderRateLimited("Slow down."), 429, "ai_quota"),
        (ProviderUnavailable("Down."), 503, "ai_unavailable"),
        (ProviderTimeout("Too slow."), 503, "ai_unavailable"),
        (IncompleteGeneration("MAX_TOKENS", TokenUsage(), 10), 502, "ai_incomplete"),
        (InvalidProviderResponse("Bad shape."), 502, "ai_error"),
        (ProviderConfigurationError("Bad key."), 502, "ai_error"),
    ],
)
def test_provider_failures_map_to_explicit_errors(
    failure: ProviderError, status: int, code: str
) -> None:
    error = provider_failure(failure)
    assert (error.status_code, error.code) == (status, code)
    assert error.message == failure.message  # written for users; no raw provider text
