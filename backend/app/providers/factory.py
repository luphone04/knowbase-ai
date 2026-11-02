"""Builds the configured providers. The only place that picks an implementation.

Callers depend on GenerationProvider / EmbeddingProvider. Tests pass fakes
straight to the code under test, or override these getters, so no
application code changes when the provider is swapped or mocked.
"""

from functools import lru_cache

from google import genai
from google.genai import types

from app.config import Settings, get_settings
from app.providers.errors import ProviderNotConfigured
from app.providers.gemini import GeminiCalls, GeminiEmbeddingProvider, GeminiGenerationProvider
from app.providers.interfaces import EmbeddingProvider, GenerationProvider
from app.providers.retry import RetryPolicy


def retry_policy(settings: Settings) -> RetryPolicy:
    return RetryPolicy(
        max_retries=settings.gemini_max_retries,
        base_delay_seconds=settings.gemini_retry_base_delay_seconds,
        max_delay_seconds=settings.gemini_retry_max_delay_seconds,
    )


def build_gemini_client(settings: Settings) -> genai.Client:
    """A client for the Gemini Developer API. Raises ProviderNotConfigured without a key."""
    api_key = settings.gemini_api_key.get_secret_value().strip()
    if not api_key:
        raise ProviderNotConfigured()
    return genai.Client(
        api_key=api_key,
        # Explicit: GOOGLE_GENAI_USE_VERTEXAI in the environment must never switch
        # this app to Vertex AI, a separately billed Google Cloud service.
        vertexai=False,
        http_options=types.HttpOptions(
            timeout=round(settings.gemini_request_timeout_seconds * 1000),  # milliseconds
            # Exactly one SDK attempt per call; app.providers.retry owns retries,
            # so they can't multiply.
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )


def build_generation_provider(
    settings: Settings, client: genai.Client | None = None
) -> GeminiGenerationProvider:
    return GeminiGenerationProvider(
        client if client is not None else build_gemini_client(settings),
        model=settings.gemini_chat_model,
        thinking_level=settings.gemini_thinking_level,
        max_output_tokens=settings.gemini_max_output_tokens,
        calls=GeminiCalls(retry_policy(settings)),
    )


def build_embedding_provider(
    settings: Settings, client: genai.Client | None = None
) -> GeminiEmbeddingProvider:
    return GeminiEmbeddingProvider(
        client if client is not None else build_gemini_client(settings),
        model=settings.gemini_embedding_model,
        dimensions=settings.embedding_dimensions,
        batch_size=settings.gemini_embedding_batch_size,
        calls=GeminiCalls(retry_policy(settings)),
    )


@lru_cache
def get_generation_provider() -> GenerationProvider:
    return build_generation_provider(get_settings())


@lru_cache
def get_embedding_provider() -> EmbeddingProvider:
    return build_embedding_provider(get_settings())
