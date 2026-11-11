"""GeminiEmbeddingProvider against a scripted SDK client (no network)."""

import logging
import math

import numpy as np
import pytest
from google.genai import types

from app.providers.errors import InvalidProviderResponse
from tests.fakes.gemini import (
    DIMENSIONS,
    embedding_response,
    make_harness,
    per_input_embeddings,
    rate_limit_error,
    raw_vector,
    unavailable_error,
)

TEXTS = ["Returns within 30 days.", "Shipping takes 3-5 days.", "Warranty lasts two years."]


def norm(vector: list[float]) -> float:
    return math.sqrt(sum(x * x for x in vector))


def test_documents_get_one_normalised_768_vector_each() -> None:
    h = make_harness()
    h.client.models.default_embed = per_input_embeddings
    result = h.embedding.embed_documents(TEXTS)

    assert len(result.vectors) == len(TEXTS)
    assert all(len(v) == DIMENSIONS for v in result.vectors)
    assert all(abs(norm(v) - 1.0) < 1e-9 for v in result.vectors)
    assert (result.model, result.dimensions, result.task_type) == (
        "gemini-embedding-001",
        768,
        "RETRIEVAL_DOCUMENT",
    )
    assert (result.requests, result.attempts) == (1, 1)


def test_document_request_uses_document_task_and_768_dimensions() -> None:
    h = make_harness(embedding_model="configured-embedding-model")
    h.client.models.default_embed = per_input_embeddings
    h.embedding.embed_documents(TEXTS)
    (call,) = h.calls
    config: types.EmbedContentConfig = call.config
    assert call.method == "embed_content"
    assert call.model == "configured-embedding-model"
    assert call.contents == TEXTS  # one list request, not one call per text
    assert config.task_type == "RETRIEVAL_DOCUMENT"
    assert config.output_dimensionality == 768


def test_query_uses_query_task_and_returns_one_vector() -> None:
    h = make_harness()
    h.client.models.default_embed = per_input_embeddings
    result = h.embedding.embed_query("Can I return a laptop?")
    (call,) = h.calls
    assert call.config.task_type == "RETRIEVAL_QUERY"
    assert call.config.output_dimensionality == 768
    assert call.contents == ["Can I return a laptop?"]
    assert result.task_type == "RETRIEVAL_QUERY"
    assert len(result.vectors) == 1 and abs(norm(result.vectors[0]) - 1) < 1e-9


def test_documents_and_queries_never_share_a_task_type() -> None:
    h = make_harness()
    h.client.models.default_embed = per_input_embeddings
    h.embedding.embed_documents(["a doc"])
    h.embedding.embed_query("a question")
    assert [c.config.task_type for c in h.calls] == ["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]


def test_each_input_keeps_its_own_vector_in_order() -> None:
    h = make_harness()
    h.client.models.default_embed = per_input_embeddings
    result = h.embedding.embed_documents(TEXTS)
    for text, vector in zip(TEXTS, result.vectors, strict=True):
        expected = np.asarray(raw_vector(text))
        expected = expected / np.linalg.norm(expected)
        assert np.allclose(vector, expected)
    assert len({tuple(v) for v in result.vectors}) == len(TEXTS)


def test_normalisation_fixes_length_but_keeps_direction() -> None:
    raw = raw_vector("x", norm=0.59)  # like real gemini-embedding-001 at 768 dims
    h = make_harness()
    h.client.models.embed_script.append(embedding_response([raw]))
    vector = h.embedding.embed_documents(["x"]).vectors[0]
    assert abs(norm(raw) - 0.59) < 1e-9
    assert abs(norm(vector) - 1.0) < 1e-12
    cosine = float(np.dot(raw, vector) / (norm(raw) * norm(vector)))
    assert abs(cosine - 1.0) < 1e-12


def test_inputs_are_batched_by_the_configured_size() -> None:
    texts = [f"text {i}" for i in range(10)]
    h = make_harness(batch_size=4)
    h.client.models.default_embed = per_input_embeddings
    result = h.embedding.embed_documents(texts)
    assert [len(c.contents) for c in h.calls] == [4, 4, 2]
    assert result.requests == 3 and len(result.vectors) == 10
    flat = [text for call in h.calls for text in call.contents]
    assert flat == texts  # order preserved across batches


def test_fewer_embeddings_than_inputs_is_rejected() -> None:
    h = make_harness()
    h.client.models.embed_script.append(embedding_response([raw_vector("only one")]))
    with pytest.raises(InvalidProviderResponse, match="one embedding per input"):
        h.embedding.embed_documents(["first", "second"])


def test_an_aggregated_single_embedding_for_many_inputs_is_rejected() -> None:
    """gemini-embedding-2 style aggregation would silently merge chunks; refuse it."""
    h = make_harness()
    h.client.models.embed_script.append(embedding_response([raw_vector("merged")]))
    with pytest.raises(InvalidProviderResponse):
        h.embedding.embed_documents(TEXTS)


@pytest.mark.parametrize("dimensions", [767, 769, 1536, 3072])
def test_wrong_dimension_count_is_rejected(dimensions: int) -> None:
    h = make_harness()
    h.client.models.embed_script.append(embedding_response([raw_vector("x", dimensions)]))
    with pytest.raises(InvalidProviderResponse, match="768-dimension"):
        h.embedding.embed_documents(["x"])


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_values_are_rejected(bad: float) -> None:
    vector = raw_vector("x")
    vector[100] = bad
    h = make_harness()
    h.client.models.embed_script.append(embedding_response([vector]))
    with pytest.raises(InvalidProviderResponse, match="NaN or infinite"):
        h.embedding.embed_documents(["x"])


def test_zero_vector_is_rejected() -> None:
    h = make_harness()
    h.client.models.embed_script.append(embedding_response([[0.0] * DIMENSIONS]))
    with pytest.raises(InvalidProviderResponse, match="zero length"):
        h.embedding.embed_documents(["x"])


def test_missing_values_are_rejected() -> None:
    h = make_harness()
    h.client.models.embed_script.append(embedding_response([None]))
    with pytest.raises(InvalidProviderResponse):
        h.embedding.embed_documents(["x"])


def test_no_embeddings_field_is_rejected() -> None:
    h = make_harness()
    h.client.models.embed_script.append(types.EmbedContentResponse(embeddings=None))
    with pytest.raises(InvalidProviderResponse):
        h.embedding.embed_documents(["x"])


@pytest.mark.parametrize("texts", [[], [""], ["ok", "   "]])
def test_invalid_inputs_are_rejected_without_an_api_call(texts: list[str]) -> None:
    h = make_harness()
    with pytest.raises(ValueError):
        h.embedding.embed_documents(texts)
    assert h.calls == []


def test_a_bare_string_is_not_split_into_characters() -> None:
    h = make_harness()
    with pytest.raises(TypeError):
        h.embedding.embed_documents("a single string")
    assert h.calls == []


def test_blank_query_is_rejected_without_an_api_call() -> None:
    h = make_harness()
    with pytest.raises(ValueError):
        h.embedding.embed_query("  ")
    assert h.calls == []


def test_logs_record_counts_never_texts(caplog: pytest.LogCaptureFixture) -> None:
    h = make_harness()
    h.client.models.default_embed = per_input_embeddings
    with caplog.at_level(logging.DEBUG):
        h.embedding.embed_documents(["PRIVATE-CHUNK-TEXT"])
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "texts=1" in logged and "task=RETRIEVAL_DOCUMENT" in logged
    assert "PRIVATE-CHUNK-TEXT" not in logged


# --- token usage: Gemini reports none for embeddings -----------------------------------


def test_embeddings_use_the_public_sdk_method_only() -> None:
    h = make_harness()
    h.client.models.default_embed = per_input_embeddings
    h.embedding.embed_query("Can I return a laptop?")
    assert [call.method for call in h.calls] == ["embed_content"]
    assert h.client.forbidden_access == []  # no private API client, no other surface


def test_unreported_embedding_tokens_are_unknown_not_zero() -> None:
    h = make_harness(batch_size=2)
    h.client.models.default_embed = per_input_embeddings
    assert h.embedding.embed_query("a question").input_tokens is None
    assert h.embedding.embed_documents(TEXTS).input_tokens is None


def test_a_successful_call_after_retries_keeps_requests_and_attempts_apart() -> None:
    h = make_harness()
    h.client.models.embed_script.extend(
        [unavailable_error(), unavailable_error(), embedding_response([raw_vector("q")])]
    )
    result = h.embedding.embed_query("q")
    assert (result.requests, result.attempts) == (1, 3)  # one logical request, three attempts
    assert result.input_tokens is None
    assert len(h.sleeps) == 2


def test_attempts_are_added_up_across_batches_with_retries() -> None:
    h = make_harness(batch_size=1)
    h.client.models.embed_script.extend(
        [
            embedding_response([raw_vector("a")]),
            rate_limit_error(),
            embedding_response([raw_vector("b")]),
        ]
    )
    result = h.embedding.embed_documents(["a", "b"])
    assert (result.requests, result.attempts, result.input_tokens) == (2, 3, None)
