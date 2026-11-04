"""Provider failures, each with a category the API and UI can act on.

Every `message` is written for end users. It never includes the provider's raw
error text, request content, or credentials. Diagnostic fields such as the
status code, provider status, quota ids, and the provider's own error message and
reasons are kept separately, for logs only.
"""

from app.providers.interfaces import TokenUsage


class ProviderError(Exception):
    category = "provider_error"
    retryable = False

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        provider_status: str | None = None,
        retry_after_seconds: float | None = None,
        quota_ids: tuple[str, ...] = (),
        provider_message: str | None = None,
        provider_reasons: tuple[str, ...] = (),
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.provider_status = provider_status
        self.retry_after_seconds = retry_after_seconds
        self.quota_ids = quota_ids
        # Google's error text and ErrorInfo reasons: needed to tell, say, a region or
        # billing precondition from a bad key. Logged, never shown to users.
        self.provider_message = provider_message
        self.provider_reasons = provider_reasons
        self.attempts = 0

    def __str__(self) -> str:
        return self.message


class ProviderNotConfigured(ProviderError):
    category = "not_configured"

    def __init__(self) -> None:
        super().__init__(
            "Gemini is not configured. Set GEMINI_API_KEY in the backend .env "
            "(a free Google AI Studio key; do not enable billing). "
            "Non-AI features keep working."
        )


class ProviderConfigurationError(ProviderError):
    """The key, model, or project was rejected. Retrying won't help."""

    category = "invalid_configuration"


class ProviderRequestError(ProviderError):
    """The provider refused the request itself (4xx). Retrying won't help."""

    category = "invalid_request"


class ProviderRateLimited(ProviderError):
    """A short-term free-tier rate limit (requests or tokens per minute)."""

    category = "rate_limited"
    retryable = True


class ProviderQuotaExhausted(ProviderError):
    """The daily free-tier quota is used up. Retrying before the reset only wastes calls."""

    category = "quota_exhausted"


class ProviderUnavailable(ProviderError):
    """5xx or network failure: probably temporary."""

    category = "unavailable"
    retryable = True


class ProviderTimeout(ProviderUnavailable):
    """The request didn't finish within the deadline (504 DEADLINE_EXCEEDED or read timeout).

    Not retried: re-sending with the same deadline restarts the same wait. Google's
    guidance for 504 is to raise the client deadline rather than retry.
    """

    category = "timeout"
    retryable = False


# Gemini rejected these before doing any work, so they used none of its quota and
# the app's own budget reservation for them can be given back. 5xx errors and
# timeouts are not here: Gemini may have counted those requests.
REFUSED_BEFORE_COUNTING: tuple[type[ProviderError], ...] = (
    ProviderRateLimited,
    ProviderQuotaExhausted,
    ProviderConfigurationError,
    ProviderRequestError,
)


class InvalidProviderResponse(ProviderError):
    """The provider answered, but the answer breaks the contract (count, size, values)."""

    category = "invalid_response"


class IncompleteGeneration(ProviderError):
    """Generation ended without a complete answer (finish reason other than STOP, or empty)."""

    category = "incomplete_generation"

    def __init__(self, finish_reason: str, usage: TokenUsage, latency_ms: int) -> None:
        super().__init__(
            "The model did not return a complete answer "
            f"(finish reason {finish_reason}). It will not be shown as a grounded answer."
        )
        self.finish_reason = finish_reason
        self.usage = usage
        self.latency_ms = latency_ms


QUOTA_CATEGORIES = frozenset({ProviderRateLimited.category, ProviderQuotaExhausted.category})
