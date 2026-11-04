"""Workspace-scoped candidate search (SPEC.md §9.2, §9.3).

Both searches run as ORM statements on a session bound to the workspace, so
the M2 tenancy guard filters them, *and* they filter `workspace_id`
explicitly: two independent barriers against cross-workspace results.
Chunks of a source that is being deleted are never returned.

- **Vector:** cosine distance (`<=>`) to the query embedding, nearest first.
  Only chunks embedded with the configured model are compared, so vectors
  from different models never mix. The SQL orders by distance alone, which
  is the shape pgvector's HNSW index can serve. Iterative index scans are
  enabled so that filtering by workspace can't silently return fewer than
  `limit` rows. Exact ties are ordered by chunk id afterwards.
  When an exact tie crosses the cut-off, the tied chunks are chosen by id
  too, so the top k never depends on scan order.
- **Lexical:** PostgreSQL full-text search on the generated `search_tsv`
  (`simple` configuration), `websearch_to_tsquery`, ranked by `ts_rank_cd`.
  This is PostgreSQL ranking, not BM25. "Quoted phrases" work, but a leading
  dash is not negation: questions are not search syntax, and "-tent" must not
  match every chunk *without* "tent". A query with no searchable words returns
  nothing.
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Row, func, select, text
from sqlalchemy.orm import Session

from app.db.models import Chunk, Document, Source

_HAS_WORD = re.compile(r"\w")
# A dash that starts a word (websearch_to_tsquery's NOT); hyphens inside words stay.
_NEGATION = re.compile(r"(?:^|(?<=[\s(\"]))-+")


@dataclass(frozen=True)
class SearchHit:
    rank: int  # 1-based within this search
    score: float  # vector: cosine distance (lower is closer); lexical: ts_rank_cd (higher)
    chunk_id: uuid.UUID
    chunk_index: int
    content: str
    heading: str | None
    page_start: int | None
    page_end: int | None
    document_id: uuid.UUID
    document_title: str
    source_id: uuid.UUID
    source_name: str
    filename: str | None


def _columns() -> tuple[Any, ...]:
    return (
        Chunk.id,
        Chunk.chunk_index,
        Chunk.content,
        Chunk.meta,
        Document.id.label("document_id"),
        Document.title,
        Source.id.label("source_id"),
        Source.display_name,
        Source.original_filename,
    )


def _scoped(statement: Any, workspace_id: uuid.UUID) -> Any:
    return (
        statement.join(Document, Document.id == Chunk.document_id)
        .join(Source, Source.id == Document.source_id)
        .where(
            Chunk.workspace_id == workspace_id,
            Document.workspace_id == workspace_id,
            Source.workspace_id == workspace_id,
            Source.status != "deleting",
        )
    )


def _hits(rows: Sequence[Row[Any]], scores: Sequence[float]) -> list[SearchHit]:
    hits = []
    for rank, (row, score) in enumerate(zip(rows, scores, strict=True), start=1):
        meta = row.meta or {}
        hits.append(
            SearchHit(
                rank=rank,
                score=float(score),
                chunk_id=row.id,
                chunk_index=row.chunk_index,
                content=row.content,
                heading=meta.get("heading"),
                page_start=meta.get("page_start"),
                page_end=meta.get("page_end"),
                document_id=row.document_id,
                document_title=row.title,
                source_id=row.source_id,
                source_name=row.display_name,
                filename=row.original_filename,
            )
        )
    return hits


def vector_search(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    query_vector: Sequence[float],
    model: str,
    limit: int,
) -> list[SearchHit]:
    """The `limit` chunks nearest to `query_vector` (a unit RETRIEVAL_QUERY embedding)."""
    distance = Chunk.embedding.cosine_distance(list(query_vector)).label("distance")
    candidates = _scoped(select(*_columns(), distance), workspace_id).where(
        Chunk.embedding_model == model
    )
    # Keep scanning the HNSW index until enough rows pass the workspace filter.
    db.execute(text("SET LOCAL hnsw.iterative_scan = strict_order"))
    # One extra row shows whether a tie crosses the cut-off.
    rows = list(db.execute(candidates.order_by(distance).limit(limit + 1)).all())
    if len(rows) > limit and rows[limit].distance == rows[limit - 1].distance:
        # Chunks exactly as near as the last one kept (identical text in several
        # documents): take the tied ones by id, not in whatever order the scan met them.
        boundary = rows[limit - 1].distance
        nearer = [row for row in rows if row.distance < boundary]
        tied = db.execute(
            candidates.where(distance == boundary).order_by(Chunk.id).limit(limit - len(nearer))
        ).all()
        rows = nearer + list(tied)
    rows = sorted(rows[:limit], key=lambda row: (row.distance, str(row.id)))
    return _hits(rows, [row.distance for row in rows])


def lexical_search(
    db: Session, *, workspace_id: uuid.UUID, query: str, limit: int
) -> list[SearchHit]:
    """The `limit` best full-text matches for `query` ("quoted phrases" are kept)."""
    query = _NEGATION.sub("", query)
    if not _HAS_WORD.search(query):
        return []
    tsquery = func.websearch_to_tsquery("simple", query)
    rank = func.ts_rank_cd(Chunk.search_tsv, tsquery).label("lexical_rank")
    statement = (
        _scoped(select(*_columns(), rank), workspace_id)
        .where(Chunk.search_tsv.op("@@")(tsquery))
        .order_by(rank.desc(), Chunk.id)
        .limit(limit)
    )
    rows = db.execute(statement).all()
    return _hits(rows, [row.lexical_rank for row in rows])
