"""What the application depends on: provider-neutral interfaces and results.

Ingestion, retrieval, and chat code use these types only. Gemini is one
implementation (app.providers.gemini); tests use deterministic fakes. Swapping
or mocking the provider therefore never touches business logic.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

EmbeddingTask = Literal["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]
DOCUMENT_TASK: EmbeddingTask = "RETRIEVAL_DOCUMENT"
QUERY_TASK: EmbeddingTask = "RETRIEVAL_QUERY"


@dataclass(frozen=True)
class TokenUsage:
    """Token counts reported by the provider; None where it reported nothing.

    `output_tokens` excludes thinking: Gemini reports thoughts separately, and its
    total is prompt + tool-use prompt + thoughts + output. `cached_tokens` is the
    part of `prompt_tokens` served from a (possibly implicit) context cache.
    """

    prompt_tokens: int | None = None
    output_tokens: int | None = None
    thinking_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None


@dataclass(frozen=True)
class GenerationResult:
    text: str
    model: str
    finish_reason: str
    usage: TokenUsage
    latency_ms: int
    attempts: int


@dataclass(frozen=True)
class EmbeddingResult:
    """Unit-length vectors, one per input text, in input order.

    Model, dimensions, and task type travel with the vectors so they can be
    stored together. Vectors from different models or dimensions must never
    share an index.

    `input_tokens` is a provider-reported input token count. It is None when the
    provider reports none, which is always the case for Gemini embeddings: the
    count is unknown, never zero and never estimated from the text.
    """

    vectors: list[list[float]]
    model: str
    dimensions: int
    task_type: EmbeddingTask
    latency_ms: int
    requests: int
    attempts: int
    input_tokens: int | None = None


class GenerationProvider(Protocol):
    @property
    def model(self) -> str: ...

    def generate(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        max_output_tokens: int | None = None,
    ) -> GenerationResult:
        """Return a complete answer, or raise IncompleteGeneration / ProviderError."""
        ...


class EmbeddingProvider(Protocol):
    @property
    def model(self) -> str: ...

    @property
    def dimensions(self) -> int: ...

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingResult:
        """Embed content to be stored and searched (RETRIEVAL_DOCUMENT)."""
        ...

    def embed_query(self, text: str) -> EmbeddingResult:
        """Embed one search question (RETRIEVAL_QUERY); the result holds one vector."""
        ...
