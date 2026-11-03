"""Retrieval inspection (SPEC.md §9, §17.11): run a search and see ranks and sources.

M6 makes retrieval observable before answer generation exists. Each search
embeds the query with Gemini (one request), so searches are limited per
workspace per day (DAILY_RETRIEVAL_REQUEST_LIMIT). Results and traces come
only from the caller's workspace.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.dependencies import CurrentAuth, DbSession, RateLimiterDep, SettingsDep
from app.api.v1.schemas import RetrievalHitOut, RetrievalSearchIn, RetrievalSearchOut, TraceOut
from app.auth.rate_limit import RateLimitRule
from app.config import Settings
from app.core.errors import AppError
from app.db.models import QueryTrace
from app.providers import factory
from app.providers.errors import (
    ProviderError,
    ProviderNotConfigured,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderUnavailable,
)
from app.retrieval.service import Retriever

router = APIRouter(prefix="/retrieval", tags=["retrieval"])

RETRIEVAL_WINDOW_SECONDS = 24 * 3600


def get_retriever(settings: SettingsDep) -> Retriever:
    return Retriever(settings, factory.get_embedding_provider)


RetrieverDep = Annotated[Retriever, Depends(get_retriever)]


def retrieval_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule(
        "retrieval-requests-daily", settings.daily_retrieval_request_limit, RETRIEVAL_WINDOW_SECONDS
    )


def _provider_failure(exc: ProviderError) -> AppError:
    if isinstance(exc, ProviderNotConfigured):
        return AppError(503, "ai_not_configured", exc.message)
    if isinstance(exc, ProviderQuotaExhausted | ProviderRateLimited):
        return AppError(429, "ai_quota", exc.message)
    if isinstance(exc, ProviderUnavailable):
        return AppError(503, "ai_unavailable", exc.message)
    return AppError(502, "ai_error", exc.message)


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
    limiter.reserve(retrieval_rule(settings), str(auth.workspace_id))
    try:
        result = retriever.retrieve(
            db,
            workspace_id=auth.workspace_id,
            question=body.query,
            mode=body.mode,
            previous_question=body.previous_question,
        )
    except ProviderError as exc:
        raise _provider_failure(exc) from exc
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
