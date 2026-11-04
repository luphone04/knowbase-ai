"""Vector and full-text candidate search on real PostgreSQL + pgvector.

Chunks are inserted with hand-built vectors, so the expected cosine
distances are exact: e0 is distance 0 from a query of e0, (e0+e1)/sqrt(2)
about 0.29, and e1 exactly 1.
"""

import math
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from app.db import tenancy
from app.db.session import get_engine, get_sessionmaker
from app.db.tenancy import TenantScopeError, bind_workspace
from app.retrieval.search import lexical_search, vector_search
from tests.fakes.retrieval import MODEL, add_source, basis
from tests.integration.conftest import Tenant

pytestmark = pytest.mark.integration


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


def test_ties_at_the_cut_off_are_settled_by_chunk_id(tenant_a: Tenant, scoped: Session) -> None:
    """Which of several equally near chunks makes the top k must not depend on scan order."""
    (nearest,) = add_source(tenant_a.workspace_id, "near.md", [("nearest", basis((0, 1)))])
    tied = add_source(
        tenant_a.workspace_id,
        "dupes.md",
        [(f"same {i}", basis((0, 1), (1, 1))) for i in range(12)],
    )
    hits = vector_search(
        scoped,
        workspace_id=tenant_a.workspace_id,
        query_vector=basis((0, 1)),
        model=MODEL,
        limit=3,
    )
    assert [h.chunk_id for h in hits] == [nearest, *sorted(tied, key=str)[:2]]


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


def test_the_explicit_workspace_filters_isolate_on_their_own(
    tenant_a: Tenant, tenant_b: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Second barrier: with the session guard switched off, the SQL still filters."""
    (mine,) = add_source(tenant_a.workspace_id, "a.md", [("tent", basis((1, 1)))])
    add_source(tenant_b.workspace_id, "b.md", [("tent secret", basis((0, 1)))])
    event.remove(Session, "do_orm_execute", tenancy._apply_workspace_scope)
    try:
        with get_sessionmaker()() as unguarded:
            vector = vector_search(
                unguarded,
                workspace_id=tenant_a.workspace_id,
                query_vector=basis((0, 1)),
                model=MODEL,
                limit=10,
            )
            lexical = lexical_search(
                unguarded, workspace_id=tenant_a.workspace_id, query="tent", limit=10
            )
    finally:
        event.listen(Session, "do_orm_execute", tenancy._apply_workspace_scope)
    assert [h.chunk_id for h in vector] == [mine]
    assert [h.chunk_id for h in lexical] == [mine]


def test_vector_search_through_the_hnsw_index_still_fills_the_limit(
    tenant_a: Tenant, tenant_b: Tenant, scoped: Session
) -> None:
    """Another workspace's 60 chunks are all nearer than this one's 5. Through the HNSW
    index (ef_search 10), only iterative scanning finds this workspace's chunks."""
    add_source(
        tenant_b.workspace_id, "b.md", [(f"b{i}", basis((0, 1), (i + 1, 0.01))) for i in range(60)]
    )
    mine = add_source(
        tenant_a.workspace_id, "a.md", [(f"a{i}", basis((1, 1), (i + 100, 0.01))) for i in range(5)]
    )
    statements: list[tuple[str, Any]] = []

    def capture(conn: Any, cursor: Any, statement: str, params: Any, *args: Any) -> None:
        if "<=>" in statement:
            statements.append((statement, params))

    # Small tables favour the B-trees, so take them away (rolled back below) to force
    # the plan a large corpus gets: the HNSW scan, then the workspace filter.
    scoped.execute(text("DROP INDEX ix_chunks_workspace_document"))
    scoped.execute(text("ALTER TABLE chunks DROP CONSTRAINT uq_chunks_document_chunk_index"))
    scoped.execute(text("ANALYZE chunks"))
    scoped.execute(text("SET LOCAL enable_seqscan = off"))
    scoped.execute(text("SET LOCAL hnsw.ef_search = 10"))
    event.listen(get_engine(), "before_cursor_execute", capture)
    try:
        hits = vector_search(
            scoped,
            workspace_id=tenant_a.workspace_id,
            query_vector=basis((0, 1)),
            model=MODEL,
            limit=5,
        )
    finally:
        event.remove(get_engine(), "before_cursor_execute", capture)
    cursor = scoped.connection().connection.cursor()
    statement, params = statements[-1]
    cursor.execute("EXPLAIN " + statement, params)
    plan = "\n".join(row[0] for row in cursor.fetchall())
    cursor.execute("SET LOCAL hnsw.iterative_scan = off")
    cursor.execute(statement, params)
    without_iterative_scan = len(cursor.fetchall())
    scoped.rollback()

    assert "ix_chunks_embedding_hnsw" in plan, plan
    assert sorted(h.chunk_id for h in hits) == sorted(mine)
    assert without_iterative_scan < 5  # the setting is what fills the limit


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
    dashed = lexical_search(
        scoped, workspace_id=tenant_a.workspace_id, query="weighs -tent", limit=10
    )
    assert [h.chunk_id for h in dashed] == [tent]  # a dash is not negation in a question


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


def test_a_leading_dash_never_matches_chunks_without_the_word(
    tenant_a: Tenant, scoped: Session
) -> None:
    """As NOT, "-tent" matched every chunk lacking "tent" (at rank 0) and boosted it in hybrid."""
    tent, _, _ = add_source(
        tenant_a.workspace_id,
        "guide.md",
        [
            ("The Summit 2P tent weighs 1.6 kg.", basis((0, 1))),
            ("Gift cards never expire.", basis((1, 1))),
            ("Returns within 30 days.", basis((2, 1))),
        ],
    )
    for query in ("-tent", "--tent", "(-tent)"):
        hits = lexical_search(scoped, workspace_id=tenant_a.workspace_id, query=query, limit=10)
        assert [h.chunk_id for h in hits] == [tent], query
        assert all(h.score > 0 for h in hits)
    assert lexical_search(scoped, workspace_id=tenant_a.workspace_id, query="-", limit=10) == []


def test_hyphenated_words_still_match(tenant_a: Tenant, scoped: Session) -> None:
    (chunk,) = add_source(
        tenant_a.workspace_id, "a.md", [("Use the e-mail form for returns.", basis((0, 1)))]
    )
    hits = lexical_search(scoped, workspace_id=tenant_a.workspace_id, query="e-mail", limit=10)
    assert [h.chunk_id for h in hits] == [chunk]


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
