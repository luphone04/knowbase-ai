"""Deterministic in-memory providers for tests of code that *uses* AI.

They implement GenerationProvider / EmbeddingProvider without any network.
They live under tests/ on purpose: the application can't be configured to
use them, so a mock can never pass for the live Gemini integration
(SPEC.md §2.4).
"""

from collections.abc import Sequence
from dataclasses import dataclass, field

from app.providers.interfaces import (
    DOCUMENT_TASK,
    QUERY_TASK,
    EmbeddingResult,
    EmbeddingTask,
    GenerationResult,
    TokenUsage,
)
from app.providers.vectors import normalize_embedding
from tests.fakes.gemini import raw_vector

MOCK_DIMENSIONS = 768


@dataclass
class MockGenerationProvider:
    reply: str = "Mock answer [S1]."
    model: str = "mock-generation"
    prompts: list[str] = field(default_factory=list)

    def generate(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        max_output_tokens: int | None = None,
    ) -> GenerationResult:
        self.prompts.append(prompt)
        return GenerationResult(
            text=self.reply,
            model=self.model,
            finish_reason="STOP",
            usage=TokenUsage(
                prompt_tokens=len(prompt.split()), output_tokens=len(self.reply.split())
            ),
            latency_ms=0,
            attempts=1,
        )


@dataclass
class MockEmbeddingProvider:
    """Same text, same unit vector; different texts, different vectors.

    The vector doesn't depend on the task type, so a query identical to a
    document finds it exactly. Each call's task type is recorded so tests
    can assert that documents and queries used the right one.
    """

    model: str = "mock-embedding"
    dimensions: int = MOCK_DIMENSIONS
    calls: list[tuple[EmbeddingTask, list[str]]] = field(default_factory=list)
    # Raised (in order) by the next calls, to simulate provider failures.
    failures: list[Exception] = field(default_factory=list)

    @property
    def texts_embedded(self) -> int:
        return sum(len(texts) for _, texts in self.calls)

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingResult:
        return self._embed(list(texts), DOCUMENT_TASK)

    def embed_query(self, text: str) -> EmbeddingResult:
        return self._embed([text], QUERY_TASK)

    def _embed(self, texts: list[str], task: EmbeddingTask) -> EmbeddingResult:
        if self.failures:
            raise self.failures.pop(0)
        self.calls.append((task, texts))
        vectors = [
            normalize_embedding(raw_vector(t, self.dimensions), self.dimensions) for t in texts
        ]
        return EmbeddingResult(
            vectors=vectors,
            model=self.model,
            dimensions=self.dimensions,
            task_type=task,
            latency_ms=0,
            requests=1,
            attempts=1,
        )
