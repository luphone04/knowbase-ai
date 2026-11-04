"""Retrieval results built in memory, for chat tests that need no database."""

import uuid

from app.chat.context import SourceExcerpt
from app.retrieval.search import SearchHit
from app.retrieval.service import RetrievedChunk

DOCUMENT = uuid.UUID("00000000-0000-0000-0000-0000000000d1")


def retrieved(
    content: str,
    *,
    rank: int = 1,
    document_id: uuid.UUID = DOCUMENT,
    title: str = "Returns Policy",
    filename: str | None = "returns-policy.md",
    page: int | None = None,
) -> RetrievedChunk:
    hit = SearchHit(
        rank=rank,
        score=0.1,
        chunk_id=uuid.uuid4(),
        chunk_index=rank - 1,
        content=content,
        heading="Standard returns",
        page_start=page,
        page_end=page,
        document_id=document_id,
        document_title=title,
        source_id=uuid.uuid4(),
        source_name=filename or title,
        filename=filename,
    )
    return RetrievedChunk(
        rank=rank,
        score=0.1,
        vector_rank=rank,
        vector_distance=0.1,
        lexical_rank=None,
        lexical_score=None,
        hit=hit,
    )


def excerpt(text: str, citation_id: str = "S1", **kwargs: object) -> SourceExcerpt:
    chunk = retrieved(text, **kwargs).hit  # type: ignore[arg-type]
    return SourceExcerpt(
        citation_id=citation_id,
        text=text,
        truncated=False,
        rank=1,
        chunk_id=chunk.chunk_id,
        chunk_index=chunk.chunk_index,
        document_id=chunk.document_id,
        document_title=chunk.document_title,
        source_id=chunk.source_id,
        source_name=chunk.source_name,
        filename=chunk.filename,
        heading=chunk.heading,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
    )
