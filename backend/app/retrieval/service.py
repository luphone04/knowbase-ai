"""One retrieval run: contextualize, embed the query, search, fuse, trace (SPEC.md §9).

    question (+ previous question for a follow-up)
        -> Gemini RETRIEVAL_QUERY embedding (one call)
        -> vector search (top RETRIEVAL_CANDIDATES)
        -> hybrid only: full-text search (top RETRIEVAL_CANDIDATES), then RRF
        -> top RETRIEVAL_TOP_N results, and a query_traces row

The two modes are separate code paths, so evaluation can compare them.
Vector-only returns the nearest chunks in distance order. Hybrid fuses the
two rankings by rank alone, never by adding raw scores.

The session passed in must be bound to the workspace (tenancy guard). The
searches also filter by workspace explicitly. The query is embedded before
the session's first statement, so a caller that has no transaction open holds
no database connection while Gemini works. The trace is added and flushed but
not committed: the caller owns the transaction (M7 saves a chat turn with it).
"""

import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db.models import Chunk, QueryTrace
from app.providers.errors import InvalidProviderResponse
from app.providers.interfaces import QUERY_TASK, EmbeddingProvider
from app.retrieval.followup import contextualize
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.search import SearchHit, lexical_search, vector_search

logger = logging.getLogger(__name__)

RetrievalMode = Literal["vector", "hybrid"]


@dataclass(frozen=True)
class RetrievedChunk:
    rank: int
    score: float  # vector mode: cosine distance (lower is closer); hybrid: RRF score
    vector_rank: int | None
    vector_distance: float | None
    lexical_rank: int | None
    lexical_score: float | None
    hit: SearchHit


@dataclass(frozen=True)
class RetrievalResult:
    mode: RetrievalMode
    question: str
    retrieval_query: str
    contextualized: bool
    results: list[RetrievedChunk]
    vector_candidates: int
    lexical_candidates: int
    trace_id: uuid.UUID
    latency_ms: int


class Retriever:
    def __init__(
        self, settings: Settings, embedding_provider_factory: Callable[[], EmbeddingProvider]
    ) -> None:
        self.settings = settings
        self._provider_factory = embedding_provider_factory

    def _embed_query(self, query: str) -> list[float]:
        provider = self._provider_factory()
        result = provider.embed_query(query)
        if (
            result.task_type != QUERY_TASK
            or result.model != self.settings.gemini_embedding_model
            or result.dimensions != self.settings.embedding_dimensions
            or len(result.vectors) != 1
        ):
            raise InvalidProviderResponse("The query embedding does not match the request.")
        return result.vectors[0]

    def has_indexed_chunks(self, db: Session, *, workspace_id: uuid.UUID) -> bool:
        """False when nothing could match, so a search needn't call Gemini at all."""
        return (
            db.scalar(
                select(Chunk.id)
                .where(
                    Chunk.workspace_id == workspace_id,
                    Chunk.embedding_model == self.settings.gemini_embedding_model,
                )
                .limit(1)
            )
            is not None
        )

    def retrieve(
        self,
        db: Session,
        *,
        workspace_id: uuid.UUID,
        question: str,
        mode: RetrievalMode,
        previous_question: str | None = None,
        embed: bool = True,
        conversation_id: uuid.UUID | None = None,
    ) -> RetrievalResult:
        """Search the workspace. With `embed=False` (nothing indexed) no Gemini call is made."""
        started = time.perf_counter()
        settings = self.settings
        query = contextualize(question, previous_question)
        vector_hits = (
            vector_search(
                db,
                workspace_id=workspace_id,
                query_vector=self._embed_query(query),
                model=settings.gemini_embedding_model,
                limit=settings.retrieval_candidates,
            )
            if embed
            else []
        )
        lexical_hits: list[SearchHit] = []
        if mode == "vector":
            results = [
                RetrievedChunk(
                    rank=hit.rank,
                    score=hit.score,
                    vector_rank=hit.rank,
                    vector_distance=hit.score,
                    lexical_rank=None,
                    lexical_score=None,
                    hit=hit,
                )
                for hit in vector_hits[: settings.retrieval_top_n]
            ]
        else:
            lexical_hits = (
                lexical_search(
                    db, workspace_id=workspace_id, query=query, limit=settings.retrieval_candidates
                )
                if embed
                else []
            )
            by_id = {hit.chunk_id: hit for hit in lexical_hits}
            by_id.update({hit.chunk_id: hit for hit in vector_hits})
            vector_by_id = {hit.chunk_id: hit for hit in vector_hits}
            lexical_by_id = {hit.chunk_id: hit for hit in lexical_hits}
            fused = reciprocal_rank_fusion(
                {
                    "vector": [hit.chunk_id for hit in vector_hits],
                    "lexical": [hit.chunk_id for hit in lexical_hits],
                },
                k=settings.rrf_k,
                limit=settings.retrieval_top_n,
            )
            results = [
                RetrievedChunk(
                    rank=item.rank,
                    score=item.score,
                    vector_rank=item.source_ranks.get("vector"),
                    vector_distance=(
                        vector_by_id[item.chunk_id].score if item.chunk_id in vector_by_id else None
                    ),
                    lexical_rank=item.source_ranks.get("lexical"),
                    lexical_score=(
                        lexical_by_id[item.chunk_id].score
                        if item.chunk_id in lexical_by_id
                        else None
                    ),
                    hit=by_id[item.chunk_id],
                )
                for item in fused
            ]

        latency_ms = round((time.perf_counter() - started) * 1000)
        trace = QueryTrace(
            workspace_id=workspace_id,
            conversation_id=conversation_id,
            retrieval_mode=mode,
            query_text=query,
            retrieved_chunk_ids=[str(r.hit.chunk_id) for r in results],
            retrieval_scores={
                "contextualized": query != " ".join(question.split()),
                "rrf_k": settings.rrf_k if mode == "hybrid" else None,
                "candidates": settings.retrieval_candidates,
                "top_n": settings.retrieval_top_n,
                "vector": [
                    {"chunk_id": str(h.chunk_id), "rank": h.rank, "distance": h.score}
                    for h in vector_hits
                ],
                "lexical": [
                    {"chunk_id": str(h.chunk_id), "rank": h.rank, "ts_rank_cd": h.score}
                    for h in lexical_hits
                ],
                "results": [
                    {"chunk_id": str(r.hit.chunk_id), "rank": r.rank, "score": r.score}
                    for r in results
                ],
            },
            latency_ms=latency_ms,
            token_usage={"query_embeddings": 1 if embed else 0},
        )
        db.add(trace)
        db.flush()  # assigns the id; the caller commits
        logger.info(
            "Retrieval workspace=%s mode=%s vector=%d lexical=%d results=%d latency_ms=%d trace=%s",
            workspace_id,
            mode,
            len(vector_hits),
            len(lexical_hits),
            len(results),
            latency_ms,
            trace.id,
        )
        return RetrievalResult(
            mode=mode,
            question=question,
            retrieval_query=query,
            contextualized=query != " ".join(question.split()),
            results=results,
            vector_candidates=len(vector_hits),
            lexical_candidates=len(lexical_hits),
            trace_id=trace.id,
            latency_ms=latency_ms,
        )
