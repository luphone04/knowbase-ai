"""Retries, quota handling, error classification, and no paid fallback (no network)."""

import json
import logging

import httpx
import pytest
from google.genai import errors

from app.providers.errors import (
    InvalidProviderResponse,
    ProviderConfigurationError,
    ProviderError,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderRequestError,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.providers.retry import RetryPolicy, equal_jitter
from tests.fakes.gemini import (
    api_error,
    daily_quota_error,
    deadline_error,
    generation_response,
    invalid_key_error,
    make_harness,
    per_input_embeddings,
    rate_limit_error,
    unavailable_error,
)

PROMPT = "a prompt"


# --- retryable errors ------------------------------------------------------------


def test_temporary_503_is_retried_with_exponential_backoff() -> None:
    h = make_harness()
    h.client.models.generate_script.extend(
        [unavailable_error(), unavailable_error(), generation_response("ok")]
    )
    result = h.generation.generate(PROMPT)
    assert result.text == "ok"
    assert result.attempts == 3
    assert h.sleeps == [1.0, 2.0]


@pytest.mark.parametrize("code", [500, 502, 503])
def test_overload_and_server_errors_are_retryable(code: int) -> None:
    h = make_harness()
    h.client.models.generate_script.extend([unavailable_error(code), generation_response("ok")])
    assert h.generation.generate(PROMPT).attempts == 2


def test_rate_limit_429_is_retried_within_the_limit() -> None:
    h = make_harness()
    h.client.models.generate_script.extend([rate_limit_error(), generation_response("ok")])
    assert h.generation.generate(PROMPT).attempts == 2
    assert h.sleeps == [1.0]


def test_provider_retry_delay_is_respected_when_reasonable() -> None:
    h = make_harness()
    h.client.models.generate_script.extend([rate_limit_error("7s"), generation_response("ok")])
    h.generation.generate(PROMPT)
    assert h.sleeps == [7.0]


def test_connection_failures_are_retried() -> None:
    h = make_harness()
    h.client.models.generate_script.extend(
        [httpx.ConnectError("refused"), httpx.ConnectTimeout("no route"), generation_response("ok")]
    )
    assert h.generation.generate(PROMPT).attempts == 3


@pytest.mark.parametrize(
    "error",
    [
        deadline_error(),
        unavailable_error(504),
        httpx.ReadTimeout("slow"),
        httpx.WriteTimeout("slow"),
    ],
    ids=["504-deadline-exceeded", "504-gateway-timeout", "read-timeout", "write-timeout"],
)
def test_deadline_exceeded_is_not_retried(error: Exception) -> None:
    """Re-sending with the same deadline restarts the same wait (seen live in M3:
    4 x 504 at 30 s, then the same request succeeded in 50.7 s without a deadline)."""
    h = make_harness()
    h.client.models.generate_script.extend([error, generation_response("ok")])
    with pytest.raises(ProviderTimeout) as excinfo:
        h.generation.generate(PROMPT)
    assert len(h.calls) == 1 and h.sleeps == []
    assert excinfo.value.category == "timeout"
    assert "GEMINI_REQUEST_TIMEOUT_SECONDS" in excinfo.value.message
    assert isinstance(excinfo.value, ProviderUnavailable)  # still an availability problem
    assert h.health.snapshot().last_error_category == "timeout"


@pytest.mark.parametrize(
    "error",
    [
        api_error(408, "DEADLINE_EXCEEDED", "Request Timeout"),
        api_error(408, "", "Request Timeout"),
        api_error(499, "CANCELLED", "The operation was cancelled."),
    ],
    ids=["408-deadline", "408-plain", "499-cancelled"],
)
def test_request_timeouts_in_transit_are_retried(error: Exception) -> None:
    h = make_harness()
    h.client.models.generate_script.extend([error, generation_response("ok")])
    assert h.generation.generate(PROMPT).attempts == 2


@pytest.mark.parametrize(
    "error",
    [
        json.JSONDecodeError("Expecting value", "<html>proxy error</html>", 0),
        errors.UnknownApiResponseError("unexpected response shape"),
    ],
    ids=["not-json", "unknown-shape"],
)
def test_unreadable_responses_are_a_clear_provider_error(error: Exception) -> None:
    h = make_harness()
    h.client.models.generate_script.extend([error, generation_response("ok")])
    with pytest.raises(InvalidProviderResponse) as excinfo:
        h.generation.generate(PROMPT)
    assert excinfo.value.message == "Gemini returned a response that could not be read."
    assert len(h.calls) == 1


def test_embedding_calls_are_retried_too() -> None:
    h = make_harness()
    h.client.models.embed_script.extend([unavailable_error(), per_input_embeddings])
    result = h.embedding.embed_documents(["x"])
    assert result.attempts == 2 and result.requests == 1


# --- retries are bounded -------------------------------------------------------------


def test_persistent_503_stops_after_the_retry_limit() -> None:
    h = make_harness(max_retries=3)
    h.client.models.generate_script.extend([unavailable_error()] * 10)
    with pytest.raises(ProviderUnavailable) as excinfo:
        h.generation.generate(PROMPT)
    assert len(h.calls) == 4  # 1 attempt + 3 retries, never more
    assert excinfo.value.attempts == 4
    assert h.sleeps == [1.0, 2.0, 4.0]


def test_persistent_rate_limit_stops_after_the_retry_limit() -> None:
    h = make_harness(max_retries=2)
    h.client.models.generate_script.extend([rate_limit_error()] * 10)
    with pytest.raises(ProviderRateLimited) as excinfo:
        h.generation.generate(PROMPT)
    assert len(h.calls) == 3
    assert excinfo.value.category == "rate_limited"


def test_zero_retries_means_exactly_one_call() -> None:
    h = make_harness(max_retries=0)
    h.client.models.generate_script.extend([unavailable_error()] * 3)
    with pytest.raises(ProviderUnavailable):
        h.generation.generate(PROMPT)
    assert len(h.calls) == 1 and h.sleeps == []


def test_backoff_is_capped() -> None:
    policy = RetryPolicy(max_retries=5, base_delay_seconds=1, max_delay_seconds=4)
    assert [policy.backoff(n) for n in range(1, 6)] == [1, 2, 4, 4, 4]


def test_jitter_stays_within_half_to_full_delay() -> None:
    samples = [equal_jitter(8.0) for _ in range(500)]
    assert min(samples) >= 4.0 and max(samples) <= 8.0


def test_provider_asking_for_a_long_wait_fails_fast() -> None:
    h = make_harness(max_delay=20)
    h.client.models.generate_script.extend([rate_limit_error("60s"), generation_response("ok")])
    with pytest.raises(ProviderRateLimited):
        h.generation.generate(PROMPT)
    assert len(h.calls) == 1 and h.sleeps == []  # doesn't block a request for a minute


# --- quota exhaustion ------------------------------------------------------------------


def test_daily_quota_exhaustion_fails_once_with_a_clear_message() -> None:
    h = make_harness()
    h.client.models.generate_script.extend([daily_quota_error()] * 5)
    with pytest.raises(ProviderQuotaExhausted) as excinfo:
        h.generation.generate(PROMPT)
    error = excinfo.value
    assert len(h.calls) == 1 and h.sleeps == []  # retrying before the reset is pointless
    assert error.category == "quota_exhausted"
    assert error.status_code == 429
    assert "daily quota" in error.message and "midnight Pacific" in error.message
    assert "No paid model or tier" in error.message
    assert error.quota_ids == ("GenerateRequestsPerDayPerProjectPerModel-FreeTier",)


def test_quota_errors_are_recorded_for_the_status_endpoint() -> None:
    h = make_harness()
    h.client.models.generate_script.append(daily_quota_error())
    with pytest.raises(ProviderQuotaExhausted):
        h.generation.generate(PROMPT)
    assert h.health.snapshot().last_error_category == "quota_exhausted"

    h.client.models.generate_script.append(generation_response("ok"))
    h.generation.generate(PROMPT)
    assert h.health.snapshot().last_error_category is None  # a success clears it


# --- non-retryable errors -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (api_error(400, "INVALID_ARGUMENT", "bad request"), ProviderRequestError),
        (invalid_key_error(), ProviderConfigurationError),
        (api_error(403, "PERMISSION_DENIED"), ProviderConfigurationError),
        (api_error(401, "UNAUTHENTICATED"), ProviderConfigurationError),
        (api_error(404, "NOT_FOUND", "models/x is not found"), ProviderConfigurationError),
        (
            api_error(400, "FAILED_PRECONDITION", "free tier unavailable in your region"),
            ProviderConfigurationError,
        ),
        (api_error(413, "INVALID_ARGUMENT", "payload too large"), ProviderRequestError),
    ],
    ids=["invalid", "bad-key", "403", "401", "unknown-model", "precondition", "too-large"],
)
def test_invalid_requests_are_never_retried(
    error: Exception, expected: type[ProviderError]
) -> None:
    h = make_harness()
    h.client.models.generate_script.extend([error] * 5)
    with pytest.raises(expected) as excinfo:
        h.generation.generate(PROMPT)
    assert len(h.calls) == 1 and h.sleeps == []
    assert excinfo.value.attempts == 1


def test_unknown_exceptions_are_bugs_and_propagate_unchanged() -> None:
    h = make_harness()
    h.client.models.generate_script.extend([RuntimeError("boom"), generation_response("ok")])
    with pytest.raises(RuntimeError, match="boom"):
        h.generation.generate(PROMPT)
    assert len(h.calls) == 1


# --- no paid fallback ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error",
    [daily_quota_error(), rate_limit_error(), unavailable_error(), api_error(404, "NOT_FOUND")],
    ids=["daily-quota", "rate-limit", "unavailable", "model-not-found"],
)
def test_failures_never_switch_model_endpoint_or_service(error: Exception) -> None:
    h = make_harness(max_retries=2)
    h.client.models.generate_script.extend([error] * 5)
    h.client.models.embed_script.extend([error] * 5)
    with pytest.raises(Exception):  # noqa: B017 - the category is covered elsewhere
        h.generation.generate(PROMPT)
    with pytest.raises(Exception):  # noqa: B017
        h.embedding.embed_documents(["x"])

    assert {c.model for c in h.calls if c.method == "generate_content"} == {"gemini-3.8-flash"}
    assert {c.model for c in h.calls if c.method == "embed_content"} == {"gemini-embedding-001"}
    assert {c.method for c in h.calls} <= {"generate_content", "embed_content"}
    # Never reached for batches, files, caches, tunings, or any other client surface.
    assert h.client.forbidden_access == []


# --- safe error text ---------------------------------------------------------------------------


def test_user_facing_message_never_contains_raw_provider_text() -> None:
    h = make_harness()
    raw = "internal detail: projects/123456/quota xyz"
    h.client.models.generate_script.append(api_error(400, "INVALID_ARGUMENT", raw))
    with pytest.raises(ProviderRequestError) as excinfo:
        h.generation.generate(PROMPT)
    assert raw not in str(excinfo.value)
    assert str(excinfo.value) == "Gemini rejected the request as invalid."


def test_failure_logs_have_category_and_status_but_not_prompt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    h = make_harness(max_retries=1)
    h.client.models.generate_script.extend([unavailable_error()] * 3)
    with caplog.at_level(logging.DEBUG), pytest.raises(ProviderUnavailable):
        h.generation.generate("PRIVATE-PROMPT")
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "category=unavailable" in logged and "http=503" in logged
    assert "PRIVATE-PROMPT" not in logged
