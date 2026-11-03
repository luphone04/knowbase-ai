"""Deterministic, paragraph-aware chunking (SPEC.md §7.5).

Every chunk is an exact slice of the document text, so
`content == text[char_start:char_end]` always holds and offsets are true
positions, not estimates.

1. Blocks (headings, paragraphs, tables) are the units. A block longer than
   `maximum - overlap` is split at sentence ends, then at spaces, never
   mid-word unless a "word" itself is too long.
2. Units are packed in order until the next one would push the chunk past
   `target`.
3. A heading starts a new chunk once the current one is reasonably full, and
   a chunk never ends on a heading that has text after it.
4. Each new chunk repeats up to `overlap` characters of the previous chunk,
   starting at a sentence or word boundary, for continuity. There is no
   overlap across a section break (a chunk that starts with a heading).
"""

import re
from dataclasses import dataclass

from app.ingestion.cleaner import CleanDocument, sha256_text

# A sentence end: punctuation, optional closing quotes/brackets (kept with the
# sentence), then whitespace (the gap). Group "gap" is the whitespace only.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])[\"')\]]*(?P<gap>\s+)")
_WORD_BREAK = re.compile(r"(?P<gap>\s+)")
_MIN_FILL_BEFORE_HEADING = 0.4  # of target


@dataclass(frozen=True)
class ChunkerConfig:
    target: int = 1800
    maximum: int = 2400
    overlap: int = 250

    def __post_init__(self) -> None:
        if not 0 <= self.overlap < self.target <= self.maximum:
            raise ValueError("chunker needs 0 <= overlap < target <= maximum")

    @property
    def fingerprint(self) -> str:
        """Changes whenever chunk boundaries could change; stored with each document."""
        return f"paragraph-v1:t{self.target}:m{self.maximum}:o{self.overlap}"


@dataclass(frozen=True)
class ChunkDraft:
    index: int
    content: str
    content_hash: str
    char_start: int
    char_end: int
    heading: str | None
    page_start: int | None
    page_end: int | None


@dataclass(frozen=True)
class _Unit:
    start: int
    end: int
    block: int
    is_heading: bool


def _split_long(text: str, start: int, end: int, limit: int) -> list[tuple[int, int]]:
    """Split text[start:end] into pieces of at most `limit`, preferring sentence ends."""
    pieces: list[tuple[int, int]] = []
    cursor = start
    while end - cursor > limit:
        window_end = cursor + limit
        cut: re.Match[str] | None = None
        for pattern in (_SENTENCE_BREAK, _WORD_BREAK):
            for match in pattern.finditer(text, cursor + 1, window_end + 1):
                if match.start("gap") <= window_end:
                    cut = match  # the last boundary inside the window wins
            if cut is not None:
                break
        if cut is None:  # one enormous "word": cut it
            pieces.append((cursor, window_end))
            cursor = window_end
            continue
        pieces.append((cursor, cut.start("gap")))
        cursor = cut.end("gap")
    pieces.append((cursor, end))
    return [(a, b) for a, b in pieces if b > a]


def _units(document: CleanDocument, config: ChunkerConfig) -> list[_Unit]:
    """Blocks that fit in a chunk (with overlap) stay whole; longer ones are split
    into pieces sized to the target, so chunks land near `target`, not `maximum`."""
    fits = config.maximum - config.overlap
    piece = max(1, config.target - config.overlap)
    units: list[_Unit] = []
    for index, (block, (start, end)) in enumerate(
        zip(document.blocks, document.offsets, strict=True)
    ):
        heading = block.kind == "heading"
        pieces = (
            [(start, end)] if end - start <= fits else _split_long(document.text, start, end, piece)
        )
        for piece_start, piece_end in pieces:
            units.append(_Unit(piece_start, piece_end, index, heading and piece_start == start))
    return units


def _overlap_start(text: str, previous_start: int, previous_end: int, overlap: int) -> int:
    """Where the repeated tail of the previous chunk begins, at a clean boundary."""
    floor = max(previous_start + 1, previous_end - overlap)
    for pattern in (_SENTENCE_BREAK, _WORD_BREAK):
        match = pattern.search(text, floor, previous_end)
        if match is not None and match.end("gap") < previous_end:
            return match.end("gap")
    return previous_end  # no clean boundary: no overlap


def chunk_document(document: CleanDocument, config: ChunkerConfig) -> list[ChunkDraft]:
    text = document.text
    units = _units(document, config)
    min_fill = int(config.target * _MIN_FILL_BEFORE_HEADING)
    chunks: list[ChunkDraft] = []
    previous: tuple[int, int] | None = None
    i = 0
    while i < len(units):
        start = units[i].start
        if previous is not None and config.overlap and not units[i].is_heading:
            overlap_start = _overlap_start(text, previous[0], previous[1], config.overlap)
            # Only a real overlap (inside the previous chunk) moves the start back;
            # otherwise the chunk begins exactly at its first unit.
            if overlap_start < previous[1] and units[i].end - overlap_start <= config.maximum:
                start = overlap_start

        end = units[i].end
        j = i + 1
        while j < len(units):
            unit = units[j]
            if unit.end - start > config.target:
                break
            if unit.is_heading and end - start >= min_fill:
                break
            end = unit.end
            j += 1
        # A chunk of nothing but headings is useless for retrieval: pull in the text
        # that follows, up to the hard maximum.
        while (
            j < len(units)
            and all(u.is_heading for u in units[i:j])
            and units[j].end - start <= config.maximum
        ):
            end = units[j].end
            j += 1
        # Don't strand a heading at the end of a chunk when its section follows.
        if j - 1 > i and units[j - 1].is_heading and j < len(units):
            j -= 1
            end = units[j - 1].end

        first_block = document.block_at(start)
        last_block = document.block_at(end - 1)
        chunks.append(
            ChunkDraft(
                index=len(chunks),
                content=text[start:end],
                content_hash=sha256_text(text[start:end]),
                char_start=start,
                char_end=end,
                heading=document.heading_at(first_block),
                page_start=document.blocks[first_block].page,
                page_end=document.blocks[last_block].page,
            )
        )
        previous = (start, end)
        i = j
    return chunks
