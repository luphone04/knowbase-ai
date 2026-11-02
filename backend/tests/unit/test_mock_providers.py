import math

from app.providers.interfaces import EmbeddingProvider, GenerationProvider
from tests.fakes.providers import MockEmbeddingProvider, MockGenerationProvider


def test_mocks_satisfy_the_provider_interfaces() -> None:
    generation: GenerationProvider = MockGenerationProvider()  # checked by mypy
    embedding: EmbeddingProvider = MockEmbeddingProvider()
    assert generation.generate("q").finish_reason == "STOP"
    assert embedding.dimensions == 768


def test_mock_embeddings_are_deterministic_unit_and_per_input() -> None:
    provider = MockEmbeddingProvider()
    first = provider.embed_documents(["a", "b"])
    again = provider.embed_documents(["a", "b"])
    assert first.vectors == again.vectors
    assert first.vectors[0] != first.vectors[1]
    assert all(math.isclose(math.sqrt(sum(x * x for x in v)), 1.0) for v in first.vectors)


def test_mock_records_task_types() -> None:
    provider = MockEmbeddingProvider()
    provider.embed_documents(["doc"])
    provider.embed_query("doc")
    assert [task for task, _ in provider.calls] == ["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]
    # Same text, same vector, so retrieval tests can find a document exactly.
    assert provider.embed_query("doc").vectors == provider.embed_documents(["doc"]).vectors


def test_mock_generation_is_labelled_as_a_mock() -> None:
    result = MockGenerationProvider().generate("question")
    assert result.model == "mock-generation"  # never mistaken for a live Gemini result
