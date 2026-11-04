"""Context building (SPEC.md §9.6): retrieved chunks -> numbered source excerpts.

- Chunks are taken in retrieval order (the top RETRIEVAL_TOP_N fused chunks).
- Empty chunks are unusable and skipped.
- A chunk that is the same text as, contained in, or nearly identical to an
  excerpt already taken *from the same document* is skipped.
- The excerpts share a character budget (CHAT_CONTEXT_MAX_CHARS). An excerpt that
  doesn't fit is cut at a word boundary; once less than MIN_EXCERPT_CHARS is
  left, no more are added.
- Excerpts are numbered S1, S2, ... in that order. The mapping from id to chunk
  stays on the server and is what citations are validated against.
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from app.retrieval.service import RetrievedChunk

MIN_EXCERPT_CHARS = 200
NEAR_DUPLICATE_JACCARD = 0.9
_WORDS = re.compile(r"\w+")
_ELLIPSIS = " …"


@dataclass(frozen=True)
class SourceExcerpt:
    citation_id: str  # "S1", "S2", ...
    text: str  # what the model sees (possibly cut to fit the budget)
    truncated: bool
    rank: int  # the chunk's rank in the retrieval results
    chunk_id: uuid.UUID
    chunk_index: int
    document_id: uuid.UUID
    document_title: str
    source_id: uuid.UUID
    source_name: str
    filename: str | None
    heading: str | None
    page_start: int | None
    page_end: int | None


def _normalized(text: str) -> str:
    return " ".join(text.casefold().split())


def _shingles(text: str) -> set[tuple[str, ...]]:
    words = _WORDS.findall(text.casefold())
    return {tuple(words[i : i + 3]) for i in range(max(1, len(words) - 2))}


def near_duplicate(a: str, b: str) -> bool:
    """Same text, one inside the other, or 3-word shingles at least 90% shared."""
    na, nb = _normalized(a), _normalized(b)
    if na == nb or na in nb or nb in na:
        return True
    sa, sb = _shingles(a), _shingles(b)
    return len(sa & sb) / len(sa | sb) >= NEAR_DUPLICATE_JACCARD


def _cut(text: str, limit: int) -> str:
    """At most `limit` characters, ending at a word boundary when there is one."""
    if len(text) <= limit:
        return text
    cut = text[: limit - len(_ELLIPSIS)]
    boundary = cut.rfind(" ")
    return (cut[:boundary] if boundary > limit // 2 else cut).rstrip() + _ELLIPSIS


def build_context(results: Sequence[RetrievedChunk], *, max_chars: int) -> list[SourceExcerpt]:
    excerpts: list[SourceExcerpt] = []
    kept: list[tuple[uuid.UUID, str]] = []  # (document id, full chunk text)
    remaining = max_chars
    for result in results:
        hit = result.hit
        content = hit.content.strip()
        if not content:
            continue
        if any(doc == hit.document_id and near_duplicate(content, other) for doc, other in kept):
            continue
        if remaining < MIN_EXCERPT_CHARS:
            break
        text = _cut(content, remaining)
        remaining -= len(text)
        kept.append((hit.document_id, content))
        excerpts.append(
            SourceExcerpt(
                citation_id=f"S{len(excerpts) + 1}",
                text=text,
                truncated=text != content,
                rank=result.rank,
                chunk_id=hit.chunk_id,
                chunk_index=hit.chunk_index,
                document_id=hit.document_id,
                document_title=hit.document_title,
                source_id=hit.source_id,
                source_name=hit.source_name,
                filename=hit.filename,
                heading=hit.heading,
                page_start=hit.page_start,
                page_end=hit.page_end,
            )
        )
    return excerpts
