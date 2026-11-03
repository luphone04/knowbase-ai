"""Text extraction from PDF, DOCX, TXT, and Markdown (SPEC.md §7.4).

Each parser returns blocks (headings, paragraphs, tables) in reading order,
with the PDF page number on each block. Uploaded bytes are untrusted, so any
failure inside a third-party parser becomes UnsupportedDocument, a clear,
permanent job error. Nothing is ever executed, and no OCR is attempted:
image-only PDFs are reported as having no extractable text.
"""

import io
import re
from dataclasses import dataclass, field
from typing import Any, Literal

import docx
from docx.table import Table
from docx.text.paragraph import Paragraph
from pypdf import PdfReader

BlockKind = Literal["heading", "paragraph", "table"]

_MAX_META_LENGTH = 200
# Linear time: "[ \t]+" then "\S" can split a line only one way, so there is no
# backtracking. (The earlier "(.+?)\s*#*\s*$" was cubic on long runs of spaces.)
_MARKDOWN_HEADING = re.compile(r"(#{1,6})[ \t]+(\S.*)")
# Longer lines are never headings; they are read as paragraph text.
MAX_HEADING_LINE_CHARS = 1000
_FENCE = re.compile(r"^\s*(```|~~~)")
_SENTENCE_END = (".", ":", ";", ",", "!", "?", "-")
_CONTINUATION_WORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "for",
        "to",
        "and",
        "or",
        "but",
        "with",
        "by",
        "at",
        "from",
        "as",
        "is",
        "are",
        "was",
        "were",
        "that",
        "this",
    ]
)


class UnsupportedDocument(Exception):
    """The file can't be indexed. `message` is safe to show the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Block:
    kind: BlockKind
    text: str
    page: int | None = None
    level: int | None = None


@dataclass
class ParsedDocument:
    title: str | None
    blocks: list[Block]
    metadata: dict[str, Any] = field(default_factory=dict)


def _markdown_heading(line: str) -> tuple[int, str] | None:
    """(level, text) for an ATX heading line, else None.

    An optional closing sequence of "#" is dropped when it is preceded by a
    space, as in CommonMark: "# Title ##" gives "Title", "# C#" stays "C#".
    """
    if not line.startswith("#") or len(line) > MAX_HEADING_LINE_CHARS:
        return None
    match = _MARKDOWN_HEADING.match(line)
    if match is None:
        return None
    text = match.group(2).rstrip()
    without_closing = text.rstrip("#")
    if without_closing != text and (not without_closing or without_closing[-1] in " \t"):
        text = without_closing.rstrip()
    return (len(match.group(1)), text) if text else None


def _short(value: object) -> str | None:
    text = str(value).strip() if value else ""
    return text[:_MAX_META_LENGTH] or None


def parse_document(file_type: str, data: bytes) -> ParsedDocument:
    if file_type == "pdf":
        return _parse_pdf(data)
    if file_type == "docx":
        return _parse_docx(data)
    if file_type in ("txt", "md"):
        return _parse_text(data, markdown=file_type == "md")
    raise UnsupportedDocument(f"Unsupported file type: {file_type}.")


# --- PDF -------------------------------------------------------------------------------


def _looks_like_heading(line: str, next_line: str | None) -> bool:
    """Conservative heuristic for PDF headings (PDFs carry no structure tags here).

    A short capitalised line with no closing punctuation, that doesn't end on a
    word that usually continues a sentence, followed by a line that starts a new
    sentence. Missing a heading only loses a label; mistaking a wrapped line for
    a heading would split a sentence, so ambiguous lines stay text.
    """
    if next_line is None or not 0 < len(line) <= 70 or line.endswith(_SENTENCE_END):
        return False
    last_word = line.rsplit(" ", 1)[-1].lower()
    return (
        line[0].isupper()
        and (next_line[0].isupper() or next_line[0].isdigit())
        and last_word not in _CONTINUATION_WORDS
    )


def _pdf_page_blocks(text: str, page: int) -> list[Block]:
    blocks: list[Block] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            # PDF line breaks are visual wrapping, not structure.
            blocks.append(Block("paragraph", " ".join(paragraph), page))
            paragraph.clear()

    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if not line:
            flush()
            continue
        following = next((later for later in lines[index + 1 :] if later), None)
        if not paragraph and _looks_like_heading(line, following):
            blocks.append(Block("heading", line, page))
        else:
            paragraph.append(line)
    flush()
    return blocks


def _parse_pdf(data: bytes) -> ParsedDocument:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise UnsupportedDocument("Password-protected PDFs are not supported.")
        page_count = len(reader.pages)
    except UnsupportedDocument:
        raise
    except Exception as exc:  # untrusted input: any parser failure means "can't read"
        raise UnsupportedDocument("The PDF could not be read; it may be damaged.") from exc

    blocks: list[Block] = []
    empty_pages: list[int] = []
    unreadable_pages: list[int] = []
    for number in range(1, page_count + 1):
        try:
            text = reader.pages[number - 1].extract_text() or ""
        except Exception:
            unreadable_pages.append(number)
            continue
        page_blocks = _pdf_page_blocks(text, number)
        if not page_blocks:
            empty_pages.append(number)
        blocks.extend(page_blocks)

    info = reader.metadata
    pdf_title = _short(info.title) if info else None
    first_heading = next((b.text for b in blocks if b.kind == "heading"), None)
    return ParsedDocument(
        title=pdf_title or first_heading,
        blocks=blocks,
        metadata={
            "page_count": page_count,
            "pages_without_text": empty_pages,
            "unreadable_pages": unreadable_pages,
            "pdf_title": pdf_title,
            "pdf_author": _short(info.author) if info else None,
        },
    )


# --- DOCX ------------------------------------------------------------------------------


def _heading_level(style_name: str) -> int | None:
    if style_name == "Title":
        return 0
    match = re.fullmatch(r"Heading ([1-9])", style_name)
    return int(match.group(1)) if match else None


def _table_text(table: Table) -> str:
    rows = []
    for row in table.rows:
        cells: list[str] = []
        for cell in row.cells:
            text = " ".join(cell.text.split())
            if not cells or cells[-1] != text:  # merged cells repeat their text
                cells.append(text)
        if any(cells):
            rows.append(" | ".join(cells))
    return "\n".join(rows)


def _parse_docx(data: bytes) -> ParsedDocument:
    try:
        document = docx.Document(io.BytesIO(data))
    except Exception as exc:
        raise UnsupportedDocument(
            "The Word document could not be read; it may be damaged."
        ) from exc

    blocks: list[Block] = []
    title_style: str | None = None
    paragraphs = tables = 0
    try:
        for child in document.element.body.iterchildren():
            tag = child.tag.rsplit("}", 1)[-1]
            if tag == "p":
                paragraph = Paragraph(child, document)
                text = paragraph.text.strip()
                if not text:
                    continue
                level = _heading_level(paragraph.style.name if paragraph.style else "")
                if level == 0 and title_style is None:
                    title_style = text
                if level is None:
                    paragraphs += 1
                    blocks.append(Block("paragraph", text))
                else:
                    blocks.append(Block("heading", text, level=max(level, 1)))
            elif tag == "tbl":
                text = _table_text(Table(child, document))
                if text:
                    tables += 1
                    blocks.append(Block("table", text))
        core_title = _short(document.core_properties.title)
        author = _short(document.core_properties.author)
    except Exception as exc:
        raise UnsupportedDocument(
            "The Word document could not be read; it may be damaged."
        ) from exc

    first_heading = next((b.text for b in blocks if b.kind == "heading"), None)
    return ParsedDocument(
        title=core_title or title_style or first_heading,
        blocks=blocks,
        metadata={"paragraph_count": paragraphs, "table_count": tables, "docx_author": author},
    )


# --- TXT / Markdown ------------------------------------------------------------------


def _parse_text(data: bytes, *, markdown: bool) -> ParsedDocument:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UnsupportedDocument(
            f"The file is not valid UTF-8 text (invalid byte at position {exc.start})."
        ) from exc
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    blocks: list[Block] = []
    paragraph: list[str] = []
    in_fence = False

    def flush() -> None:
        if paragraph:
            blocks.append(Block("paragraph", "\n".join(paragraph)))
            paragraph.clear()

    for line in lines:
        if markdown and _FENCE.match(line):
            in_fence = not in_fence
            paragraph.append(line)
            if not in_fence:
                flush()  # a code block stays one unit
            continue
        if in_fence:
            paragraph.append(line)
            continue
        heading = _markdown_heading(line) if markdown else None
        if heading:
            flush()
            blocks.append(Block("heading", heading[1], level=heading[0]))
        elif not line.strip():
            flush()
        else:
            paragraph.append(line)
    flush()

    if markdown:
        title = next((b.text for b in blocks if b.kind == "heading" and b.level == 1), None)
    else:
        first = next((b.text.split("\n", 1)[0].strip() for b in blocks), "")
        title = first if 0 < len(first) <= 120 else None
    return ParsedDocument(title=title, blocks=blocks, metadata={"line_count": len(lines)})
