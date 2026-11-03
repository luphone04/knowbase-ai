"""Gemini implementation of the provider interfaces (google-genai SDK).

Free-tier only: the client targets the Gemini Developer API (AI Studio key),
never Vertex AI. The only methods called are `models.generate_content` and
`models.embed_content`, always with the configured model. No grounding tools,
no batch API, and no other model is tried when one fails.

Logs carry model names, counts, finish reasons, token usage, latency, attempts,
and error categories. Never prompts, texts, vectors, or keys.
"""

import json
import logging
import time
from collections.abc import Callable, Sequence
from functools import partial
from typing import Any

import httpx
from google import genai
from google.genai import errors, types

from app.providers.errors import (
    IncompleteGeneration,
    InvalidProviderResponse,
    ProviderConfigurationError,
    ProviderError,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderRequestError,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.providers.health import ProviderHealth, provider_health
from app.providers.interfaces import (
    DOCUMENT_TASK,
    QUERY_TASK,
    EmbeddingResult,
    EmbeddingTask,
    GenerationResult,
    TokenUsage,
)
from app.providers.retry import RetryPolicy, call_with_retries, equal_jitter
from app.providers.vectors import normalize_embedding

try:  # The SDK can use either HTTP stack; handle errors from both.
    import httpx2

    _TRANSPORT_ERRORS: tuple[type[Exception], ...] = (httpx.TransportError, httpx2.TransportError)
    _READ_TIMEOUTS: tuple[type[Exception], ...] = (
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx2.ReadTimeout,
        httpx2.WriteTimeout,
    )
except ImportError:  # pragma: no cover - httpx2 is optional
    _TRANSPORT_ERRORS = (httpx.TransportError,)
    _READ_TIMEOUTS = (httpx.ReadTimeout, httpx.WriteTimeout)

_TIMEOUT_MESSAGE = (
    "Gemini did not finish within the request timeout (GEMINI_REQUEST_TIMEOUT_SECONDS). "
    "The service is slow right now; try again later, or raise the timeout."
)

logger = logging.getLogger(__name__)

_RETRY_INFO = "type.googleapis.com/google.rpc.RetryInfo"
_QUOTA_FAILURE = "type.googleapis.com/google.rpc.QuotaFailure"
_ERROR_INFO = "type.googleapis.com/google.rpc.ErrorInfo"


# --- error classification -------------------------------------------------------


def _error_details(exc: errors.APIError) -> list[dict[str, Any]]:
    body = exc.details if isinstance(exc.details, dict) else {}
    error = body.get("error", body)
    details = error.get("details", []) if isinstance(error, dict) else []
    return [d for d in details if isinstance(d, dict)]


def _retry_delay_seconds(details: list[dict[str, Any]]) -> float | None:
    for detail in details:
        if detail.get("@type") == _RETRY_INFO:
            raw = str(detail.get("retryDelay", "")).removesuffix("s")
            try:
                return max(0.0, float(raw))
            except ValueError:
                return None
    return None


def _quota_ids(details: list[dict[str, Any]]) -> tuple[str, ...]:
    ids: list[str] = []
    for detail in details:
        if detail.get("@type") == _QUOTA_FAILURE:
            for violation in detail.get("violations", []):
                if isinstance(violation, dict) and violation.get("quotaId"):
                    ids.append(str(violation["quotaId"]))
    return tuple(ids)


def _reasons(details: list[dict[str, Any]]) -> set[str]:
    return {str(d.get("reason")) for d in details if d.get("@type") == _ERROR_INFO}


def classify_gemini_error(exc: Exception) -> ProviderError | None:
    """Map an SDK or transport exception to a ProviderError, or None if unrecognised."""
    if isinstance(exc, errors.APIError):
        details = _error_details(exc)
        common: dict[str, Any] = {"status_code": exc.code, "provider_status": exc.status}

        if exc.code == 429:
            quota_ids = _quota_ids(details)
            if any("PerDay" in quota_id for quota_id in quota_ids):
                return ProviderQuotaExhausted(
                    "Gemini's free-tier daily quota is used up. It resets at midnight "
                    "Pacific time; try again then. No paid model or tier is used instead.",
                    quota_ids=quota_ids,
                    **common,
                )
            return ProviderRateLimited(
                "Gemini's free-tier rate limit was reached. Wait a moment and try again.",
                retry_after_seconds=_retry_delay_seconds(details),
                quota_ids=quota_ids,
                **common,
            )
        if exc.code in (408, 499):
            # The request timed out or was cancelled in transit (not our deadline).
            return ProviderUnavailable(
                "Gemini did not receive the request in time. Try again shortly.", **common
            )
        if exc.code == 504 or exc.status == "DEADLINE_EXCEEDED":
            return ProviderTimeout(_TIMEOUT_MESSAGE, **common)
        if exc.code >= 500:
            return ProviderUnavailable(
                "Gemini is temporarily unavailable. Try again shortly.", **common
            )
        if exc.code in (401, 403) or "API_KEY_INVALID" in _reasons(details):
            return ProviderConfigurationError(
                "Gemini rejected the API key. Check GEMINI_API_KEY in the backend .env.",
                **common,
            )
        if exc.code == 404:
            return ProviderConfigurationError(
                "The configured Gemini model was not found for this key. "
                "Check GEMINI_CHAT_MODEL and GEMINI_EMBEDDING_MODEL.",
                **common,
            )
        if exc.status == "FAILED_PRECONDITION":
            return ProviderConfigurationError(
                "Gemini refused this request for the project (for example, the free tier "
                "is not available here). No paid fallback is attempted.",
                **common,
            )
        return ProviderRequestError("Gemini rejected the request as invalid.", **common)

    if isinstance(exc, (json.JSONDecodeError, errors.UnknownApiResponseError)):
        # For example an HTML page from a proxy instead of Gemini's JSON.
        return InvalidProviderResponse("Gemini returned a response that could not be read.")
    if isinstance(exc, _READ_TIMEOUTS):
        return ProviderTimeout(_TIMEOUT_MESSAGE)
    if isinstance(exc, _TRANSPORT_ERRORS):
        return ProviderUnavailable(
            "Gemini could not be reached (timeout or network error). Try again shortly."
        )
    return None


# --- shared call handling ---------------------------------------------------------


class GeminiCalls:
    """Retries, health bookkeeping, and failure logging for every Gemini request."""

    def __init__(
        self,
        policy: RetryPolicy,
        *,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[float], float] = equal_jitter,
        health: ProviderHealth = provider_health,
    ) -> None:
        self.policy = policy
        self._sleep = sleep
        self._jitter = jitter
        self._health = health

    def run[T](self, operation: str, call: Callable[[], T]) -> tuple[T, int]:
        try:
            result, attempts = call_with_retries(
                operation,
                call,
                classify=classify_gemini_error,
                policy=self.policy,
                sleep=self._sleep,
                jitter=self._jitter,
            )
        except ProviderError as exc:
            self._health.record_failure(exc.category)
            logger.error(
                "Gemini %s failed: category=%s http=%s status=%s attempts=%d quota_ids=%s",
                operation,
                exc.category,
                exc.status_code,
                exc.provider_status,
                exc.attempts,
                ",".join(exc.quota_ids) or "-",
            )
            raise
        self._health.record_success()
        return result, attempts


def _elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


# --- generation ---------------------------------------------------------------------


def _token_usage(metadata: types.GenerateContentResponseUsageMetadata | None) -> TokenUsage:
    if metadata is None:
        return TokenUsage()
    return TokenUsage(
        prompt_tokens=metadata.prompt_token_count,
        output_tokens=metadata.candidates_token_count,
        thinking_tokens=metadata.thoughts_token_count,
        total_tokens=metadata.total_token_count,
    )


def _enum_name(value: object) -> str:
    return str(getattr(value, "name", value))


def _finish_reason_and_text(response: types.GenerateContentResponse) -> tuple[str, str]:
    feedback = response.prompt_feedback
    if feedback is not None and feedback.block_reason is not None:
        return f"BLOCKED_{_enum_name(feedback.block_reason)}", ""
    if not response.candidates:
        return "NO_CANDIDATES", ""
    candidate = response.candidates[0]
    reason = _enum_name(candidate.finish_reason) if candidate.finish_reason else "UNSPECIFIED"
    parts = candidate.content.parts if candidate.content and candidate.content.parts else []
    # Thought summaries are not part of the answer.
    text = "".join(part.text for part in parts if part.text and not part.thought)
    return reason, text


class GeminiGenerationProvider:
    def __init__(
        self,
        client: genai.Client,
        *,
        model: str,
        thinking_level: str,
        max_output_tokens: int,
        calls: GeminiCalls,
    ) -> None:
        self._client = client
        self._model = model
        self._thinking_level = types.ThinkingLevel[thinking_level.upper()]
        self._max_output_tokens = max_output_tokens
        self._calls = calls

    @property
    def model(self) -> str:
        return self._model

    def generate(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        max_output_tokens: int | None = None,
    ) -> GenerationResult:
        if not prompt.strip():
            raise ValueError("prompt must not be empty")
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            max_output_tokens=max_output_tokens or self._max_output_tokens,
            thinking_config=types.ThinkingConfig(thinking_level=self._thinking_level),
            # One request per call: the SDK's automatic function calling could make
            # extra model calls if tools were ever attached.
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            # The SDK would otherwise resend the request (each a billed, quota-counted
            # call) while the model returns a continuation token. A cut-off answer
            # is reported as incomplete instead.
            automatic_continuation=False,
        )
        started = time.perf_counter()
        response, attempts = self._calls.run(
            "generate",
            lambda: self._client.models.generate_content(
                model=self._model, contents=prompt, config=config
            ),
        )
        latency_ms = _elapsed_ms(started)
        usage = _token_usage(response.usage_metadata)
        finish_reason, text = _finish_reason_and_text(response)
        logger.info(
            "Gemini generate model=%s finish=%s latency_ms=%d attempts=%d "
            "prompt_tokens=%s output_tokens=%s thinking_tokens=%s total_tokens=%s",
            self._model,
            finish_reason,
            latency_ms,
            attempts,
            usage.prompt_tokens,
            usage.output_tokens,
            usage.thinking_tokens,
            usage.total_tokens,
        )
        if finish_reason != "STOP":
            raise IncompleteGeneration(finish_reason, usage, latency_ms)
        if not text.strip():
            raise IncompleteGeneration("EMPTY_RESPONSE", usage, latency_ms)
        return GenerationResult(
            text=text,
            model=self._model,
            finish_reason=finish_reason,
            usage=usage,
            latency_ms=latency_ms,
            attempts=attempts,
        )


# --- embeddings ---------------------------------------------------------------------


class GeminiEmbeddingProvider:
    """gemini-embedding-001 with per-input embeddings, batched, validated, and normalised."""

    def __init__(
        self,
        client: genai.Client,
        *,
        model: str,
        dimensions: int,
        batch_size: int,
        calls: GeminiCalls,
    ) -> None:
        self._client = client
        self._model = model
        self._dimensions = dimensions
        self._batch_size = batch_size
        self._calls = calls

    @property
    def model(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dimensions

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingResult:
        return self._embed(texts, DOCUMENT_TASK)

    def embed_query(self, text: str) -> EmbeddingResult:
        return self._embed([text], QUERY_TASK)

    def _embed(self, texts: Sequence[str], task: EmbeddingTask) -> EmbeddingResult:
        if isinstance(texts, str):
            raise TypeError("texts must be a sequence of strings, not a single string")
        if not texts:
            raise ValueError("at least one text is required")
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise ValueError("texts must be non-empty strings")

        config = types.EmbedContentConfig(task_type=task, output_dimensionality=self._dimensions)
        vectors: list[list[float]] = []
        attempts = 0
        requests = 0
        started = time.perf_counter()
        for offset in range(0, len(texts), self._batch_size):
            batch = list(texts[offset : offset + self._batch_size])
            response, batch_attempts = self._calls.run(
                f"embed {task}",
                partial(
                    self._client.models.embed_content,
                    model=self._model,
                    contents=batch,
                    config=config,
                ),
            )
            attempts += batch_attempts
            requests += 1
            embeddings = response.embeddings or []
            if len(embeddings) != len(batch):
                raise InvalidProviderResponse(
                    f"Expected one embedding per input ({len(batch)}), got {len(embeddings)}."
                )
            vectors.extend(normalize_embedding(e.values, self._dimensions) for e in embeddings)

        latency_ms = _elapsed_ms(started)
        logger.info(
            "Gemini embed model=%s task=%s texts=%d requests=%d attempts=%d "
            "dimensions=%d latency_ms=%d",
            self._model,
            task,
            len(texts),
            requests,
            attempts,
            self._dimensions,
            latency_ms,
        )
        return EmbeddingResult(
            vectors=vectors,
            model=self._model,
            dimensions=self._dimensions,
            task_type=task,
            latency_ms=latency_ms,
            requests=requests,
            attempts=attempts,
        )
