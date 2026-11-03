"""Vector and full-text candidate search on real PostgreSQL + pgvector.

Chunks are inserted with hand-built vectors, so the expected cosine
distances are exact: e0 is distance 0 from a query of e0, (e0+e1)/sqrt(2)
about 0.29, and e1 exactly 1.
"""

import math
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.session import get_engine, get_sessionmaker
from app.db.tenancy import TenantScopeError, bind_workspace
from app.retrieval.search import lexical_search, vector_search
from tests.integration.conftest import Tenant

pytestmark = pytest.mark.integration

MODEL = "gemini-embedding-001"
DIMS = 768


def basis(*weights: tuple[int, float]) -> list[float]:
    vector = [0.0] * DIMS
    for index, weight in weights:
        vector[index] = weight
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector]


def literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(v) for v in vector) + "]"


def add_source(
    workspace_id: uuid.UUID,
    name: str,
    chunks: list[tuple[str, list[float]]],
    *,
    status: str = "ready",
    model: str = MODEL,
    page: int | None = None,
) -> list[uuid.UUID]:
    """One file source with one document and the given (content, vector) chunks."""
    source_id, document_id = uuid.uuid4(), uuid.uuid4()
    chunk_ids = [uuid.uuid4() for _ in chunks]
    with get_engine().begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sources (id, workspace_id, kind, display_name, original_filename,"
                " file_type, status) VALUES (:id, :ws, 'file', :name, :name, 'md', :status)"
            ),
            {"id": source_id, "ws": workspace_id, "name": name, "status": status},
        )
        connection.execute(
            text(
                "INSERT INTO documents (id, workspace_id, source_id, title, content_hash,"
                " text_content) VALUES (:id, :ws, :src, :title, 'h', 't')"
            ),
            {"id": document_id, "ws": workspace_id, "src": source_id, "title": f"{name} title"},
        )
        for index, ((content, vector), chunk_id) in enumerate(zip(chunks, chunk_ids, strict=True)):
            connection.execute(
                text(
                    "INSERT INTO chunks (id, workspace_id, document_id, chunk_index, content,"
                    " content_hash, embedding, embedding_model, metadata) VALUES (:id, :ws,"
                    " :doc, :i, :content, :hash, CAST(:vec AS vector), :model,"
                    " CAST(:meta AS jsonb))"
                ),
                {
                    "id": chunk_id,
                    "ws": workspace_id,
                    "doc": document_id,
                    "i": index,
                    "content": content,
                    "hash": uuid.uuid4().hex,
                    "vec": literal(vector),
                    "model": model,
                    "meta": (
                        f'{{"heading": "Section {index}", "page_start": {page or "null"},'
                        f' "page_end": {page or "null"}}}'
                    ),
                },
            )
    return chunk_ids


@pytest.fixture
def scoped(tenant_a: Tenant) -> Iterator[Session]:
    with get_sessionmaker()() as session:
        bind_workspace(session, tenant_a.workspace_id)
        yield session


# --- vector search -------------------------------------------------------------------------


def test_vector_search_ranks_by_cosine_distance(tenant_a: Tenant, scoped: Session) -> None:
    exact, near, far = add_source(
        tenant_a.workspace_id,
        "returns.pdf",
        [("exact", basis((0, 1))), ("near", basis((0, 1), (1, 1))), ("far", basis((1, 1)))],
        page=2,
    )
    hits = vector_search(
        scoped,
        workspace_id=tenant_a.workspace_id,
        query_vector=basis((0, 1)),
        model=MODEL,
        limit=10,
    )
    assert [h.chunk_id for h in hits] == [exact, near, far]
    assert [h.rank for h in hits] == [1, 2, 3]
    assert hits[0].score == pytest.approx(0.0, abs=1e-6)
    assert hits[1].score == pytest.approx(1 - 1 / math.sqrt(2), abs=1e-6)
    assert hits[2].score == pytest.approx(1.0, abs=1e-6)
    top = hits[0]
    assert (top.content, top.heading, top.page_start, top.page_end) == ("exact", "Section 0", 2, 2)
    assert (top.source_name, top.filename, top.document_title) == (
        "returns.pdf",
        "returns.pdf",
        "returns.pdf title",
    )


def test_vector_search_respects_the_limit(tenant_a: Tenant, scoped: Session) -> None:
    add_source(tenant_a.workspace_id, "many.md", [(f"c{i}", basis((i, 1))) for i in range(15)])
    hits = vector_search(
        scoped,
        workspace_id=tenant_a.workspace_id,
        query_vector=basis((0, 1)),
        model=MODEL,
        limit=10,
    )
    assert len(hits) == 10


def test_vectors_from_another_model_are_never_compared(tenant_a: Tenant, scoped: Session) -> None:
    add_source(tenant_a.workspace_id, "old.md", [("old", basis((0, 1)))], model="other-model")
    (current,) = add_source(tenant_a.workspace_id, "new.md", [("new", basis((1, 1)))])
    hits = vector_search(
        scoped,
        workspace_id=tenant_a.workspace_id,
        query_vector=basis((0, 1)),
        model=MODEL,
        limit=10,
    )
    assert [h.chunk_id for h in hits] == [current]


def test_exact_ties_are_ordered_by_chunk_id(tenant_a: Tenant, scoped: Session) -> None:
    ids = add_source(
        tenant_a.workspace_id, "twins.md", [("a", basis((0, 1))), ("b", basis((0, 1)))]
    )
    hits = vector_search(
        scoped,
        workspace_id=tenant_a.workspace_id,
        query_vector=basis((0, 1)),
        model=MODEL,
        limit=10,
    )
    assert [h.chunk_id for h in hits] == sorted(ids, key=str)


def test_sources_being_deleted_are_excluded(tenant_a: Tenant, scoped: Session) -> None:
    add_source(tenant_a.workspace_id, "doomed.md", [("x", basis((0, 1)))], status="deleting")
    assert (
        vector_search(
            scoped,
            workspace_id=tenant_a.workspace_id,
            query_vector=basis((0, 1)),
            model=MODEL,
            limit=10,
        )
        == []
    )
    assert lexical_search(scoped, workspace_id=tenant_a.workspace_id, query="x", limit=10) == []


def test_an_empty_workspace_returns_nothing(tenant_a: Tenant, scoped: Session) -> None:
    assert (
        vector_search(
            scoped,
            workspace_id=tenant_a.workspace_id,
            query_vector=basis((0, 1)),
            model=MODEL,
            limit=10,
        )
        == []
    )
    assert lexical_search(scoped, workspace_id=tenant_a.workspace_id, query="tent", limit=10) == []


# --- workspace isolation --------------------------------------------------------------------


def test_another_workspaces_chunks_are_never_returned(
    tenant_a: Tenant, tenant_b: Tenant, scoped: Session
) -> None:
    (mine,) = add_source(tenant_a.workspace_id, "a.md", [("Summit 2P tent", basis((1, 1)))])
    add_source(tenant_b.workspace_id, "b.md", [("Summit 2P tent secret", basis((0, 1)))])
    vector = vector_search(
        scoped,
        workspace_id=tenant_a.workspace_id,
        query_vector=basis((0, 1)),
        model=MODEL,
        limit=10,
    )
    lexical = lexical_search(scoped, workspace_id=tenant_a.workspace_id, query="tent", limit=10)
    assert [h.chunk_id for h in vector] == [mine]  # B's exact match is invisible
    assert [h.chunk_id for h in lexical] == [mine]


def test_asking_for_another_workspace_through_a_bound_session_returns_nothing(
    tenant_a: Tenant, tenant_b: Tenant, scoped: Session
) -> None:
    add_source(tenant_b.workspace_id, "b.md", [("tent", basis((0, 1)))])
    assert (
        vector_search(
            scoped,
            workspace_id=tenant_b.workspace_id,
            query_vector=basis((0, 1)),
            model=MODEL,
            limit=10,
        )
        == []
    )
    assert lexical_search(scoped, workspace_id=tenant_b.workspace_id, query="tent", limit=10) == []


def test_an_unbound_session_fails_closed(tenant_a: Tenant) -> None:
    add_source(tenant_a.workspace_id, "a.md", [("tent", basis((0, 1)))])
    with get_sessionmaker()() as unbound, pytest.raises(TenantScopeError):
        lexical_search(unbound, workspace_id=tenant_a.workspace_id, query="tent", limit=10)


# --- lexical search ----------------------------------------------------------------------------


def test_exact_product_terms_are_found(tenant_a: Tenant, scoped: Session) -> None:
    laptop, tent, _ = add_source(
        tenant_a.workspace_id,
        "guide.md",
        [
            ("The Trailblazer 14 laptop weighs 1.3 kg.", basis((0, 1))),
            ("The Summit 2P tent weighs 1.6 kg.", basis((1, 1))),
            ("Gift cards never expire.", basis((2, 1))),
        ],
    )
    hits = lexical_search(
        scoped, workspace_id=tenant_a.workspace_id, query="Trailblazer 14", limit=10
    )
    assert [h.chunk_id for h in hits] == [laptop]
    assert hits[0].score > 0
    phrase = lexical_search(
        scoped, workspace_id=tenant_a.workspace_id, query='"summit 2p"', limit=10
    )
    assert [h.chunk_id for h in phrase] == [tent]  # case-insensitive phrase
    either = lexical_search(
        scoped, workspace_id=tenant_a.workspace_id, query="weighs -tent", limit=10
    )
    assert [h.chunk_id for h in either] == [laptop]  # websearch negation


def test_more_matches_rank_higher(tenant_a: Tenant, scoped: Session) -> None:
    once, twice = add_source(
        tenant_a.workspace_id,
        "faq.md",
        [
            ("A warranty claim needs an order number.", basis((0, 1))),
            ("The warranty covers defects; warranty claims take 10 days.", basis((1, 1))),
        ],
    )
    hits = lexical_search(scoped, workspace_id=tenant_a.workspace_id, query="warranty", limit=10)
    assert [h.chunk_id for h in hits] == [twice, once]
    assert hits[0].score > hits[1].score


def test_queries_without_words_return_nothing(tenant_a: Tenant, scoped: Session) -> None:
    add_source(tenant_a.workspace_id, "a.md", [("tent", basis((0, 1)))])
    for query in ("", "   ", "?!", "-- ''"):
        assert (
            lexical_search(scoped, workspace_id=tenant_a.workspace_id, query=query, limit=10) == []
        )


def test_lexical_input_is_never_interpreted_as_sql(tenant_a: Tenant, scoped: Session) -> None:
    add_source(tenant_a.workspace_id, "a.md", [("tent", basis((0, 1)))])
    hostile = "tent'); DROP TABLE chunks; --"
    lexical_search(scoped, workspace_id=tenant_a.workspace_id, query=hostile, limit=10)
    with get_engine().connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM chunks")).scalar_one() == 1
