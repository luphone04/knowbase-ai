"""Text normalisation and the canonical document text (SPEC.md §7.4).

The cleaned text is what gets hashed, stored, chunked, and cited, so cleaning
must be deterministic: the same input always yields the same text and hash.
That is what makes "unchanged content" detectable.
"""

import bisect
import hashlib
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from app.ingestion.parsers import Block, ParsedDocument, UnsupportedDocument

BLOCK_SEPARATOR = "\n\n"
_SPACES = re.compile(r"[ \t\f\v  -​  　]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clean_text(text: str, *, keep_line_breaks: bool) -> str:
    # NFKC folds PDF ligatures and full-width forms (e.g. "ﬁ" -> "fi") for search.
    text = unicodedata.normalize("NFKC", text).replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")
    lines = [_SPACES.sub(" ", line).strip() for line in text.split("\n")]
    if keep_line_breaks:
        return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()
    return " ".join(line for line in lines if line)


@dataclass(frozen=True)
class CleanDocument:
    """Blocks joined by blank lines, with each block's exact [start, end) in `text`."""

    title: str
    blocks: list[Block]
    offsets: list[tuple[int, int]]
    text: str
    content_hash: str
    metadata: dict[str, Any]

    def block_at(self, position: int) -> int:
        """Index of the block containing (or nearest before) a text position."""
        starts = [start for start, _ in self.offsets]
        return max(0, bisect.bisect_right(starts, position) - 1)


def build_clean_document(
    parsed: ParsedDocument, *, file_type: str, fallback_title: str, min_chars: int
) -> CleanDocument:
    blocks: list[Block] = []
    for block in parsed.blocks:
        # Tables and plain-text/Markdown paragraphs keep their line structure;
        # headings and PDF paragraphs are single lines.
        keep = block.kind == "table" or (block.kind == "paragraph" and file_type in ("txt", "md"))
        text = clean_text(block.text, keep_line_breaks=keep)
        if text:
            blocks.append(Block(block.kind, text, block.page, block.level))

    offsets: list[tuple[int, int]] = []
    position = 0
    for block in blocks:
        offsets.append((position, position + len(block.text)))
        position += len(block.text) + len(BLOCK_SEPARATOR)
    text = BLOCK_SEPARATOR.join(block.text for block in blocks)

    if len(text) < min_chars:
        if file_type == "pdf":
            raise UnsupportedDocument(
                "No extractable text was found. Scanned or image-only PDFs need OCR, "
                "which is not supported."
            )
        if file_type == "html":
            raise UnsupportedDocument(
                f"The page has too little readable text to index ({len(text)} characters; "
                f"at least {min_chars} are needed)."
            )
        raise UnsupportedDocument(
            f"The document has too little text to index ({len(text)} characters; "
            f"at least {min_chars} are needed)."
        )

    title = clean_text(parsed.title or "", keep_line_breaks=False)[:200] or fallback_title
    return CleanDocument(
        title=title,
        blocks=blocks,
        offsets=offsets,
        text=text,
        content_hash=sha256_text(text),
        metadata=parsed.metadata,
    )
