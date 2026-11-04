"""What API routes share around a Gemini call: daily slots and error responses.

Routes that call Gemini on a user's behalf (retrieval search, chat) reserve
daily slots before the call and give them back when Gemini refused it, so a
refused call costs nothing. Provider failures become explicit error responses,
never an answer.
"""

from datetime import datetime

from app.auth.rate_limit import RateLimiter, RateLimitExceeded, RateLimitRule
from app.config import Settings
from app.core.errors import AppError
from app.providers.errors import (
    IncompleteGeneration,
    ProviderError,
    ProviderNotConfigured,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderUnavailable,
)

DAY_SECONDS = 24 * 3600
INSTALLATION = "installation"


def query_embedding_rule(settings: Settings) -> RateLimitRule:
    """Query embeddings for the whole installation (one shared Gemini project)."""
    return RateLimitRule(
        "query-embeddings-daily", settings.daily_query_embedding_limit, DAY_SECONDS
    )


class DailySlots:
    """One slot from each (rule, identifier) pair, taken together or not at all."""

    def __init__(self, limiter: RateLimiter, buckets: list[tuple[RateLimitRule, str]]) -> None:
        self._limiter = limiter
        self._buckets = buckets
        self._windows: list[tuple[RateLimitRule, str, datetime]] = []

    def take(self) -> None:
        for rule, identifier in self._buckets:
            try:
                window = self._limiter.reserve(rule, identifier)
            except RateLimitExceeded:
                self.give_back()
                raise
            self._windows.append((rule, identifier, window))

    def give_back(self) -> None:
        for rule, identifier, window in self._windows:
            self._limiter.release_many(rule, identifier, 1, window_start=window)
        self._windows.clear()


def provider_failure(exc: ProviderError) -> AppError:
    if isinstance(exc, ProviderNotConfigured):
        return AppError(503, "ai_not_configured", exc.message)
    if isinstance(exc, ProviderQuotaExhausted | ProviderRateLimited):
        return AppError(429, "ai_quota", exc.message)
    if isinstance(exc, ProviderUnavailable):
        return AppError(503, "ai_unavailable", exc.message)
    if isinstance(exc, IncompleteGeneration):
        return AppError(502, "ai_incomplete", exc.message)
    return AppError(502, "ai_error", exc.message)
