"""Bounded exponential backoff for provider calls.

Only errors marked retryable (rate limits, 5xx, timeouts, network failures)
are retried, and only up to `max_retries` times. When the provider asks for a
longer wait than `max_delay`, the call fails at once instead of blocking the
request. Everything else (bad key, bad request, exhausted daily quota) fails
on the first attempt.
"""

import logging
import random
from collections.abc import Callable
from dataclasses import dataclass

from app.providers.errors import ProviderError

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    max_retries: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 20.0

    def backoff(self, retry_number: int) -> float:
        """Delay before retry n (1-based): base, 2*base, 4*base, ..., capped."""
        return float(min(self.max_delay_seconds, self.base_delay_seconds * 2 ** (retry_number - 1)))


def equal_jitter(delay: float) -> float:
    """Somewhere in [delay/2, delay], so concurrent callers don't retry in lockstep."""
    return delay / 2 + random.uniform(0, delay / 2)


def call_with_retries[T](
    operation: str,
    call: Callable[[], T],
    *,
    classify: Callable[[Exception], ProviderError | None],
    policy: RetryPolicy,
    sleep: Callable[[float], None],
    jitter: Callable[[float], float] = equal_jitter,
) -> tuple[T, int]:
    """Run `call`, retrying classified retryable errors. Returns (result, attempts).

    Exceptions that `classify` doesn't recognise are re-raised unchanged:
    unknown failures are bugs, not outages.
    """
    attempt = 0
    while True:
        attempt += 1
        try:
            return call(), attempt
        except Exception as exc:
            error = classify(exc)
            if error is None:
                raise
            error.attempts = attempt
            if not error.retryable or attempt > policy.max_retries:
                raise error from exc

            if error.retry_after_seconds is not None:
                delay = error.retry_after_seconds
            else:
                delay = jitter(policy.backoff(attempt))
            if delay > policy.max_delay_seconds:
                logger.warning(
                    "%s: provider asked to wait %.0fs (limit %.0fs); not retrying",
                    operation,
                    delay,
                    policy.max_delay_seconds,
                )
                raise error from exc

            logger.warning(
                "%s: %s (HTTP %s), retry %d/%d in %.1fs",
                operation,
                error.category,
                error.status_code,
                attempt,
                policy.max_retries,
                delay,
            )
            sleep(delay)
