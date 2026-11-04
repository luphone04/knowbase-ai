"""A scripted stand-in for `google.genai.Client`, returning real SDK types.

The real Gemini providers run unchanged against it: classification, retries,
validation, and normalisation are all exercised without network access.
"""

import hashlib
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np
from google.genai import errors, types

from app.providers.gemini import GeminiCalls, GeminiEmbeddingProvider, GeminiGenerationProvider
from app.providers.health import ProviderHealth
from app.providers.retry import RetryPolicy

DIMENSIONS = 768
Scripted = Any  # a response, an exception to raise, or a callable(contents) -> response


@dataclass
class RecordedCall:
    method: str
    model: str
    contents: Any
    config: Any


class FakeModels:
    def __init__(self) -> None:
        self.calls: list[RecordedCall] = []
        self.generate_script: deque[Scripted] = deque()
        self.embed_script: deque[Scripted] = deque()
        self.default_embed: Callable[[Any], types.EmbedContentResponse] | None = None

    def generate_content(self, *, model: str, contents: Any, config: Any) -> Any:
        self.calls.append(RecordedCall("generate_content", model, contents, config))
        return self._play(self.generate_script, contents, None)

    def embed_content(self, *, model: str, contents: Any, config: Any) -> Any:
        self.calls.append(RecordedCall("embed_content", model, contents, config))
        return self._play(self.embed_script, contents, self.default_embed)

    @staticmethod
    def _play(script: deque[Scripted], contents: Any, default: Callable[[Any], Any] | None) -> Any:
        if script:
            item = script.popleft()
        elif default is not None:
            item = default
        else:
            raise AssertionError("unexpected extra API call")
        if isinstance(item, BaseException):
            raise item
        return item(contents) if callable(item) else item


class FakeGeminiClient:
    """Only `models` exists. Touching any other client surface (batches, files,
    caches, tunings, ...) fails: the providers must never use them."""

    def __init__(self) -> None:
        self.models = FakeModels()
        self.forbidden_access: list[str] = []

    def __getattr__(self, name: str) -> Any:
        self.forbidden_access.append(name)
        raise AssertionError(f"provider touched client.{name}")


# --- providers wired to the fake, with a recorded (not real) sleep --------------


@dataclass
class Harness:
    client: FakeGeminiClient
    generation: GeminiGenerationProvider
    embedding: GeminiEmbeddingProvider
    sleeps: list[float]
    health: ProviderHealth

    @property
    def calls(self) -> list[RecordedCall]:
        return self.client.models.calls


def make_harness(
    *,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 20.0,
    max_elapsed: float | None = None,
    batch_size: int = 8,
    chat_model: str = "gemini-3.5-flash-lite",
    embedding_model: str = "gemini-embedding-001",
) -> Harness:
    client = FakeGeminiClient()
    sleeps: list[float] = []
    health = ProviderHealth()
    now = [0.0]  # a fake clock that only sleeping advances

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    calls = GeminiCalls(
        RetryPolicy(max_retries, base_delay, max_delay, max_elapsed),
        sleep=sleep,
        jitter=lambda delay: delay,  # deterministic backoff in tests
        health=health,
        clock=lambda: now[0],
    )
    generation = GeminiGenerationProvider(
        client,  # type: ignore[arg-type]  # duck-typed stand-in for genai.Client
        model=chat_model,
        thinking_level="low",
        max_output_tokens=1200,
        calls=calls,
    )
    embedding = GeminiEmbeddingProvider(
        client,  # type: ignore[arg-type]
        model=embedding_model,
        dimensions=DIMENSIONS,
        batch_size=batch_size,
        calls=calls,
    )
    return Harness(client, generation, embedding, sleeps, health)


# --- response builders --------------------------------------------------------------


def generation_response(
    text: str | None = "ready",
    *,
    finish_reason: types.FinishReason | None = types.FinishReason.STOP,
    usage: tuple[int, int, int | None, int] | None = (12, 1, None, 13),
    thought: str | None = None,
    no_candidates: bool = False,
    block_reason: types.BlockedReason | None = None,
) -> types.GenerateContentResponse:
    parts: list[types.Part] = []
    if thought is not None:
        parts.append(types.Part(text=thought, thought=True))
    if text is not None:
        parts.append(types.Part(text=text))
    usage_metadata = None
    if usage is not None:
        prompt, output, thinking, total = usage
        usage_metadata = types.GenerateContentResponseUsageMetadata(
            prompt_token_count=prompt,
            candidates_token_count=output,
            thoughts_token_count=thinking,
            total_token_count=total,
        )
    candidates = None
    if not no_candidates:
        candidates = [
            types.Candidate(
                content=types.Content(role="model", parts=parts), finish_reason=finish_reason
            )
        ]
    feedback = (
        types.GenerateContentResponsePromptFeedback(block_reason=block_reason)
        if block_reason
        else None
    )
    return types.GenerateContentResponse(
        candidates=candidates, usage_metadata=usage_metadata, prompt_feedback=feedback
    )


def raw_vector(text: str, dimensions: int = DIMENSIONS, norm: float = 0.59) -> list[float]:
    """A deterministic, distinct, non-unit vector per text (like Gemini's raw 768-d output)."""
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
    values = np.random.default_rng(seed).normal(size=dimensions)
    return [float(x) for x in values / np.linalg.norm(values) * norm]


def embedding_response(vectors: Sequence[Sequence[float] | None]) -> types.EmbedContentResponse:
    return types.EmbedContentResponse(
        embeddings=[types.ContentEmbedding(values=None if v is None else list(v)) for v in vectors]
    )


def per_input_embeddings(contents: Any) -> types.EmbedContentResponse:
    """One raw vector per input text, like gemini-embedding-001."""
    return embedding_response([raw_vector(text) for text in contents])


# --- error builders -------------------------------------------------------------------


def api_error(
    code: int, status: str, message: str = "provider detail", details: Sequence[dict[str, Any]] = ()
) -> errors.APIError:
    body = {"error": {"code": code, "status": status, "message": message, "details": list(details)}}
    error_class = errors.ServerError if code >= 500 else errors.ClientError
    return error_class(code, body)


def rate_limit_error(retry_delay: str | None = None) -> errors.APIError:
    details: list[dict[str, Any]] = [
        {
            "@type": "type.googleapis.com/google.rpc.QuotaFailure",
            "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}],
        }
    ]
    if retry_delay is not None:
        details.append(
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry_delay}
        )
    return api_error(429, "RESOURCE_EXHAUSTED", "You exceeded your current quota", details)


def daily_quota_error() -> errors.APIError:
    return api_error(
        429,
        "RESOURCE_EXHAUSTED",
        "You exceeded your current quota",
        [
            {
                "@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}],
            },
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "3600s"},
        ],
    )


def unavailable_error(code: int = 503) -> errors.APIError:
    return api_error(code, "UNAVAILABLE", "The model is overloaded. Please try again later.")


def deadline_error() -> errors.APIError:
    return api_error(504, "DEADLINE_EXCEEDED", "Deadline expired before operation could complete.")


def invalid_key_error() -> errors.APIError:
    return api_error(
        400,
        "INVALID_ARGUMENT",
        "API key not valid. Please pass a valid API key.",
        [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}],
    )
