"""The retrieval service and POST /retrieval/search, on real PostgreSQL + pgvector.

Query embeddings come from a fixed fake provider (no Gemini). Chunks are
either hand-built (exact distances) or the real demo corpus, uploaded and
indexed through the normal worker pipeline.
"""

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from app.api.v1.retrieval import get_retriever
from app.config import get_settings
from app.db.session import get_engine
from app.main import app
from app.providers.errors import (
    ProviderNotConfigured,
    ProviderQuotaExhausted,
    ProviderUnavailable,
)
from app.retrieval.service import Retriever
from tests.fakes.retrieval import FixedQueryProvider, add_source, basis
from tests.integration.conftest import Tenant, WorkerHarness, new_client

pytestmark = pytest.mark.integration

SEARCH = "/api/v1/retrieval/search"
DEMO_STORE = Path(__file__).resolve().parents[3] / "sample_data" / "demo_store"


@pytest.fixture
def provider() -> Iterator[FixedQueryProvider]:
    fake = FixedQueryProvider(vector=basis((0, 1)))
    app.dependency_overrides[get_retriever] = lambda: Retriever(get_settings(), lambda: fake)
    yield fake
    app.dependency_overrides.pop(get_retriever, None)


def search(tenant: Tenant, query: str, **body: object) -> Any:
    response = tenant.client.post(SEARCH, json={"query": query, **body})
    assert response.status_code == 200, response.text
    return response.json()


def chunk_ids(result: Any) -> list[str]:
    return [hit["chunk_id"] for hit in result["results"]]


# --- modes -----------------------------------------------------------------------------------


def test_vector_mode_returns_nearest_chunks_only(
    tenant_a: Tenant, provider: FixedQueryProvider
) -> None:
    ids = add_source(
        tenant_a.workspace_id,
        "guide.md",
        [(f"chunk {i}", basis((0, 10 - i), (1, i))) for i in range(8)],
    )
    result = search(tenant_a, "anything at all here", mode="vector")
    assert result["mode"] == "vector"
    assert chunk_ids(result) == [str(i) for i in ids[:5]]  # nearest first, top 5
    first = result["results"][0]
    assert first["vector_rank"] == 1 and first["lexical_rank"] is None
    assert first["score"] == first["vector_distance"]
    assert result["lexical_candidates"] == 0
    assert provider.queries == ["anything at all here"]  # one query embedding


def test_hybrid_mode_fuses_both_rankings(tenant_a: Tenant, provider: FixedQueryProvider) -> None:
    near, both, far = add_source(
        tenant_a.workspace_id,
        "faq.md",
        [
            ("nearest but unrelated words", basis((0, 1))),
            ("second nearest, NorthCare Plus twice: NorthCare Plus", basis((0, 3), (1, 1))),
            ("far away, mentions NorthCare Plus", basis((1, 1))),
        ],
    )
    result = search(tenant_a, "NorthCare Plus", mode="hybrid")
    hits = result["results"]
    # Vector 2 + lexical 1 beats vector 1 alone (near) and vector 3 + lexical 2 (far).
    assert chunk_ids(result) == [str(both), str(far), str(near)]
    top = hits[0]
    assert (top["vector_rank"], top["lexical_rank"]) == (2, 1)
    assert top["score"] == pytest.approx(1 / 62 + 1 / 61)
    by_id = {h["chunk_id"]: h for h in hits}
    assert by_id[str(near)]["lexical_rank"] is None  # vector-only
    assert by_id[str(far)]["lexical_rank"] == 2
    assert by_id[str(far)]["score"] == pytest.approx(1 / 63 + 1 / 62)
    assert result["lexical_candidates"] == 2 and result["vector_candidates"] == 3


def test_hybrid_and_vector_results_are_separately_observable(
    tenant_a: Tenant, provider: FixedQueryProvider
) -> None:
    add_source(
        tenant_a.workspace_id,
        "a.md",
        [("tent warranty", basis((1, 1))), ("unrelated", basis((0, 1)))],
    )
    vector = search(tenant_a, "tent", mode="vector")
    hybrid = search(tenant_a, "tent", mode="hybrid")
    assert chunk_ids(vector) != chunk_ids(hybrid)  # lexical evidence changes the order
    assert vector["trace_id"] != hybrid["trace_id"]


def test_follow_up_questions_use_the_previous_question(
    tenant_a: Tenant, provider: FixedQueryProvider
) -> None:
    add_source(tenant_a.workspace_id, "a.md", [("laptop returns", basis((0, 1)))])
    result = search(tenant_a, "And the warranty?", previous_question="How do I return a laptop?")
    assert result["contextualized"] is True
    assert result["retrieval_query"] == "How do I return a laptop? And the warranty?"
    assert result["query"] == "And the warranty?"
    assert provider.queries == ["How do I return a laptop? And the warranty?"]


def test_an_empty_workspace_returns_no_results(
    tenant_a: Tenant, provider: FixedQueryProvider
) -> None:
    result = search(tenant_a, "anything")
    assert result["results"] == []


# --- the demo corpus through the real pipeline --------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("RMA number", "returns-policy.md"),
        ("NorthCare Plus", "warranty-policy.md"),
        ('"Summit 2P"', "product-guide.md"),
        ("express shipping", "shipping-policy.md"),
        ("gift cards expire", "support-faq.md"),
    ],
)
def test_exact_terms_in_the_demo_corpus_rank_their_document_first(
    tenant_a: Tenant, worker: WorkerHarness, provider: FixedQueryProvider, query: str, expected: str
) -> None:
    for path in sorted(DEMO_STORE.glob("*.md")):
        response = tenant_a.client.post(
            "/api/v1/sources/files", files={"file": (path.name, path.read_bytes())}
        )
        assert response.status_code == 202, response.text
    assert worker.run_all() == 5
    # All 5 chunks are vector candidates; the lexical match adds a second rank.
    result = search(tenant_a, query, mode="hybrid")
    assert result["results"][0]["filename"] == expected
    assert result["results"][0]["lexical_rank"] == 1


# --- traces ------------------------------------------------------------------------------------


def test_every_search_writes_a_trace(tenant_a: Tenant, provider: FixedQueryProvider) -> None:
    (chunk,) = add_source(tenant_a.workspace_id, "a.md", [("tent", basis((0, 1)))])
    result = search(tenant_a, "tent", mode="hybrid")
    trace = tenant_a.client.get(f"/api/v1/retrieval/traces/{result['trace_id']}").json()
    assert trace["retrieval_mode"] == "hybrid"
    assert trace["query_text"] == "tent"
    assert trace["retrieved_chunk_ids"] == [str(chunk)]
    scores = trace["retrieval_scores"]
    assert scores["rrf_k"] == 60 and scores["candidates"] == 10 and scores["top_n"] == 5
    assert scores["vector"][0]["rank"] == 1 and scores["lexical"][0]["rank"] == 1
    assert trace["token_usage"] == {"query_embeddings": 1}
    assert trace["latency_ms"] >= 0
    with get_engine().connect() as connection:
        owner = connection.execute(text("SELECT workspace_id FROM query_traces")).scalar_one()
    assert owner == tenant_a.workspace_id


# --- isolation, validation, limits, and provider errors -------------------------------------------


def test_searches_and_traces_are_workspace_isolated(
    tenant_a: Tenant, tenant_b: Tenant, provider: FixedQueryProvider
) -> None:
    add_source(tenant_b.workspace_id, "secret.md", [("tent secret", basis((0, 1)))])
    (mine,) = add_source(tenant_a.workspace_id, "mine.md", [("tent", basis((1, 1)))])
    result = search(tenant_a, "tent")
    assert chunk_ids(result) == [str(mine)]
    b_result = search(tenant_b, "tent")
    assert (
        tenant_a.client.get(f"/api/v1/retrieval/traces/{b_result['trace_id']}").status_code == 404
    )
    assert tenant_b.client.get(f"/api/v1/retrieval/traces/{result['trace_id']}").status_code == 404


def test_search_requires_authentication_and_origin(tenant_a: Tenant) -> None:
    assert new_client().post(SEARCH, json={"query": "tent"}).status_code == 401
    response = tenant_a.client.post(
        SEARCH, json={"query": "tent"}, headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 1001},
        {"query": "tent", "mode": "bm25"},
        {"query": "tent", "workspace_id": str(uuid.uuid4())},
    ],
    ids=["empty", "blank", "too-long", "unknown-mode", "client-workspace"],
)
def test_invalid_requests_are_rejected(
    tenant_a: Tenant, provider: FixedQueryProvider, body: dict[str, object]
) -> None:
    assert tenant_a.client.post(SEARCH, json=body).status_code == 422
    assert provider.queries == []  # rejected before any Gemini call


def test_searches_are_rate_limited_per_workspace(
    tenant_a: Tenant,
    tenant_b: Tenant,
    provider: FixedQueryProvider,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DAILY_RETRIEVAL_REQUEST_LIMIT", "1")
    get_settings.cache_clear()
    search(tenant_a, "tent")
    limited = tenant_a.client.post(SEARCH, json={"query": "tent"})
    assert limited.status_code == 429 and "Retry-After" in limited.headers
    assert len(provider.queries) == 1  # the limited request never reached Gemini
    search(tenant_b, "tent")  # another workspace has its own allowance


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (ProviderNotConfigured(), 503, "ai_not_configured"),
        (ProviderQuotaExhausted("Daily quota used up."), 429, "ai_quota"),
        (ProviderUnavailable("Gemini is temporarily unavailable."), 503, "ai_unavailable"),
    ],
)
def test_provider_failures_are_clear_errors(
    tenant_a: Tenant, provider: FixedQueryProvider, failure: Exception, status: int, code: str
) -> None:
    provider.failure = failure
    response = tenant_a.client.post(SEARCH, json={"query": "tent"})
    assert response.status_code == status
    assert response.json()["error"]["code"] == code


def test_a_mismatched_query_embedding_is_refused(
    tenant_a: Tenant, provider: FixedQueryProvider
) -> None:
    provider.model = "some-other-model"
    response = tenant_a.client.post(SEARCH, json={"query": "tent"})
    assert response.status_code == 502
    assert response.json()["error"]["code"] == "ai_error"
