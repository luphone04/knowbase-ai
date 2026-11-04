"""Retrieval inspection (SPEC.md §9, §17.11): run a search and see ranks and sources.

M6 makes retrieval observable before answer generation exists. Each search
embeds the query with Gemini (one request), so searches are limited per
workspace per day (DAILY_RETRIEVAL_REQUEST_LIMIT) and for the whole
installation (DAILY_QUERY_EMBEDDING_LIMIT). A slot is spent only when Gemini is
really called: not without a key, not for an empty workspace, and it is given
back when Gemini refuses the call. Results and traces come only from the
caller's workspace.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import CurrentAuth, DbSession, RateLimiterDep, SettingsDep
from app.api.v1.gemini_calls import (
    DAY_SECONDS,
    INSTALLATION,
    DailySlots,
    provider_failure,
    query_embedding_rule,
)
from app.api.v1.schemas import RetrievalHitOut, RetrievalSearchIn, RetrievalSearchOut, TraceOut
from app.auth.rate_limit import RateLimitRule
from app.config import Settings
from app.core.errors import AppError
from app.db.models import QueryTrace
from app.providers import factory
from app.providers.errors import REFUSED_BEFORE_COUNTING, ProviderError, ProviderNotConfigured
from app.retrieval.service import Retriever

router = APIRouter(prefix="/retrieval", tags=["retrieval"])


def get_retriever(settings: SettingsDep) -> Retriever:
    return Retriever(settings, factory.get_embedding_provider)


RetrieverDep = Annotated[Retriever, Depends(get_retriever)]


def retrieval_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule(
        "retrieval-requests-daily", settings.daily_retrieval_request_limit, DAY_SECONDS
    )


@router.post("/search")
def search(
    body: RetrievalSearchIn,
    auth: CurrentAuth,
    db: DbSession,
    settings: SettingsDep,
    limiter: RateLimiterDep,
    retriever: RetrieverDep,
) -> RetrievalSearchOut:
    """Top RETRIEVAL_TOP_N chunks for a question, in `vector` or `hybrid` mode, plus a trace."""
    if not settings.gemini_configured:
        raise provider_failure(ProviderNotConfigured())  # before spending a daily slot
    embed = retriever.has_indexed_chunks(db, workspace_id=auth.workspace_id)
    # End the read: no connection or transaction is held while Gemini embeds the query.
    db.commit()
    slots = DailySlots(
        limiter,
        [
            (retrieval_rule(settings), str(auth.workspace_id)),
            (query_embedding_rule(settings), INSTALLATION),
        ],
    )
    if embed:
        slots.take()
    try:
        result = retriever.retrieve(
            db,
            workspace_id=auth.workspace_id,
            question=body.query,
            mode=body.mode,
            previous_question=body.previous_question,
            embed=embed,
        )
    except ProviderError as exc:
        if isinstance(exc, REFUSED_BEFORE_COUNTING):
            slots.give_back()  # Gemini refused it, so it cost nothing
        raise provider_failure(exc) from exc
    db.commit()  # the trace
    return RetrievalSearchOut(
        mode=result.mode,
        query=result.question,
        retrieval_query=result.retrieval_query,
        contextualized=result.contextualized,
        trace_id=result.trace_id,
        latency_ms=result.latency_ms,
        vector_candidates=result.vector_candidates,
        lexical_candidates=result.lexical_candidates,
        results=[
            RetrievalHitOut(
                rank=item.rank,
                score=item.score,
                vector_rank=item.vector_rank,
                vector_distance=item.vector_distance,
                lexical_rank=item.lexical_rank,
                lexical_score=item.lexical_score,
                chunk_id=item.hit.chunk_id,
                chunk_index=item.hit.chunk_index,
                content=item.hit.content,
                heading=item.hit.heading,
                page_start=item.hit.page_start,
                page_end=item.hit.page_end,
                document_id=item.hit.document_id,
                document_title=item.hit.document_title,
                source_id=item.hit.source_id,
                source_name=item.hit.source_name,
                filename=item.hit.filename,
            )
            for item in result.results
        ],
    )


@router.get("/traces/{trace_id:uuid}")
def get_trace(trace_id: uuid.UUID, _: CurrentAuth, db: DbSession) -> TraceOut:
    """A stored retrieval trace: query, retrieved chunk ids, per-list ranks and scores, latency."""
    trace = db.get(QueryTrace, trace_id)  # scoped to the caller's workspace
    if trace is None:
        raise AppError(404, "not_found", "Trace not found.")
    return TraceOut(
        id=trace.id,
        retrieval_mode=trace.retrieval_mode,
        query_text=trace.query_text,
        retrieved_chunk_ids=trace.retrieved_chunk_ids,
        retrieval_scores=trace.retrieval_scores,
        latency_ms=trace.latency_ms,
        token_usage=trace.token_usage,
        created_at=trace.created_at,
    )
