"""Passage-level ground truth: which indexed chunks hold each evidence quotation.

A dataset with passage relevance (Asia Cement) cites evidence as source key +
PDF page + exact quotation. Chunk ids and boundaries change whenever a document
is re-parsed or re-chunked, so the quotations are mapped to the workspace's
current chunks when a run starts, and that mapping is stored with the run.

Matching is deterministic and never guesses:

- `matched`: the quotation occurs in one or more chunks of its own source that
  cover its PDF page. Several chunks are expected when chunk overlap repeats
  the text; every one of them holds the whole passage, so each counts.
  `level` says how: `exact` (whitespace collapsed, quotes and dashes unified,
  case-insensitive) or `spacing` (also ignoring spaces, for PDF text such as
  "sugge st" or "non -cash").
- `ambiguous`: the quotation occurs in chunks that are not one overlapping
  run (for example, the same sentence on two different pages), or only on pages
  other than the cited one. Not used for scoring.
- `split`: no chunk holds the whole quotation, but it continues from the end
  of one chunk into the next. Not used for scoring: neither chunk alone is the
  passage.
- `missing`: not found in its source at all (for example, lost in parsing).

Only `matched` evidence defines relevant chunks; a question whose evidence
isn't all matched has no passage-level score rather than a guessed one.
"""

import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

MatchStatus = Literal["matched", "ambiguous", "split", "missing"]
MIN_SPLIT_PART = 12  # characters (spaces ignored) on each side of a chunk boundary

_TRANSLATE = str.maketrans(
    {
        "‘": "'",
        "’": "'",
        "“": '"',
        "”": '"',
        "–": "-",
        "—": "-",
        "‐": "-",
        "‑": "-",
        "­": None,  # soft hyphen
        "•": " ",  # bullet
        "": " ",  # private-use bullet some PDFs emit
    }
)
_SPACE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Case-folded text with unified quotes and dashes and collapsed whitespace."""
    text = unicodedata.normalize("NFKC", text).translate(_TRANSLATE).casefold()
    return _SPACE.sub(" ", text).strip()


def compact(text: str) -> str:
    """`normalize` without any whitespace at all."""
    return _SPACE.sub("", normalize(text))


@dataclass(frozen=True)
class ChunkText:
    id: str
    source_key: str
    chunk_index: int
    page_start: int | None
    page_end: int | None
    content: str

    def covers(self, page: int) -> bool:
        if self.page_start is None:
            return True  # unknown pages can't rule a chunk out
        return self.page_start <= page <= (self.page_end or self.page_start)


@dataclass(frozen=True)
class EvidenceMatch:
    status: MatchStatus
    chunk_ids: tuple[str, ...]  # relevant chunks (matched only)
    level: str | None = None  # matched: "exact" or "spacing"
    detail: str | None = None  # why it isn't matched, with the chunks involved

    def as_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "chunk_ids": list(self.chunk_ids),
            "level": self.level,
            "detail": self.detail,
        }


def _contiguous(chunks: Sequence[ChunkText]) -> bool:
    indexes = sorted(c.chunk_index for c in chunks)
    return all(b - a == 1 for a, b in zip(indexes, indexes[1:], strict=False))


def _containing(quote: str, chunks: Sequence[ChunkText]) -> tuple[list[ChunkText], str | None]:
    exact = normalize(quote)
    found = [c for c in chunks if exact in normalize(c.content)]
    if found:
        return found, "exact"
    spaced = compact(quote)
    found = [c for c in chunks if spaced in compact(c.content)]
    return found, ("spacing" if found else None)


def _split(quote: str, chunks: Sequence[ChunkText]) -> tuple[ChunkText, ChunkText] | None:
    """Two consecutive chunks where the quotation runs from the end of one into the next."""
    target = compact(quote)
    ordered = sorted(chunks, key=lambda c: c.chunk_index)
    for first, second in zip(ordered, ordered[1:], strict=False):
        if second.chunk_index != first.chunk_index + 1:
            continue
        head, tail = compact(first.content), compact(second.content)
        for cut in range(len(target) - MIN_SPLIT_PART, MIN_SPLIT_PART - 1, -1):
            if head.endswith(target[:cut]) and target[cut:] in tail:
                return first, second
    return None


def map_evidence(
    quote: str, *, source_key: str, page: int | None, chunks: Iterable[ChunkText]
) -> EvidenceMatch:
    """Where one quotation lives among a workspace's chunks (all sources may be given)."""
    own = [c for c in chunks if c.source_key == source_key]
    if not own:
        return EvidenceMatch("missing", (), detail=f"no indexed chunks for source {source_key}")
    found, level = _containing(quote, own)
    if found:
        on_page = [c for c in found if page is None or c.covers(page)]
        if not on_page:
            pages = sorted({c.page_start for c in found if c.page_start is not None})
            return EvidenceMatch(
                "ambiguous",
                (),
                detail=f"found only on page(s) {pages}, not the cited page {page}",
            )
        if not _contiguous(on_page):
            indexes = sorted(c.chunk_index for c in on_page)
            return EvidenceMatch(
                "ambiguous", (), detail=f"found in separate chunks {indexes} of {source_key}"
            )
        ordered = sorted(on_page, key=lambda c: c.chunk_index)
        return EvidenceMatch("matched", tuple(c.id for c in ordered), level=level)
    pair = _split(quote, own)
    if pair is not None:
        return EvidenceMatch(
            "split",
            (),
            detail=(
                f"spans chunks {pair[0].chunk_index} and {pair[1].chunk_index} of {source_key}; "
                "no single chunk holds it"
            ),
        )
    return EvidenceMatch("missing", (), detail=f"not found in {source_key}")


def relevant_chunks(mapping: Mapping[str, Any] | None) -> set[str] | None:
    """A question's relevant chunk ids from its stored mapping; None unless all evidence matched."""
    if not mapping:
        return None
    items = mapping.get("evidence", [])
    if not items or any(item.get("status") != "matched" for item in items):
        return None
    return {chunk for item in items for chunk in item.get("chunk_ids", [])}


def evidence_groups(mapping: Mapping[str, Any] | None) -> list[set[str]] | None:
    """Each evidence passage's chunk ids (for passage recall); None unless all matched."""
    if relevant_chunks(mapping) is None:
        return None
    assert mapping is not None
    return [set(item["chunk_ids"]) for item in mapping["evidence"]]
