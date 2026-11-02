import pytest

from app.ingestion.cleaner import CleanDocument, build_clean_document, clean_text, sha256_text
from app.ingestion.parsers import Block, ParsedDocument, UnsupportedDocument


def doc(
    *blocks: Block, title: str | None = "Title", file_type: str = "md", min_chars: int = 1
) -> CleanDocument:
    return build_clean_document(
        ParsedDocument(title, list(blocks)),
        file_type=file_type,
        fallback_title="fallback",
        min_chars=min_chars,
    )


def test_whitespace_is_normalised() -> None:
    assert clean_text("  many\t\tspaces  here  ", keep_line_breaks=False) == "many spaces here"


def test_line_breaks_kept_or_joined() -> None:
    text = "line one\nline two\n\n\n\nline three"
    assert clean_text(text, keep_line_breaks=True) == "line one\nline two\n\nline three"
    assert clean_text(text, keep_line_breaks=False) == "line one line two line three"


def test_ligatures_and_control_characters_are_normalised() -> None:
    assert clean_text("eﬃcient \u0007bell", keep_line_breaks=False) == "efficient bell"


def test_document_text_offsets_are_exact() -> None:
    clean = doc(Block("heading", "Returns"), Block("paragraph", "  Within  30 days.  "))
    assert clean.text == "Returns\n\nWithin 30 days."
    for block, (start, end) in zip(clean.blocks, clean.offsets, strict=True):
        assert clean.text[start:end] == block.text


def test_empty_blocks_are_dropped() -> None:
    clean = doc(Block("paragraph", "   "), Block("paragraph", "Real text."))
    assert [b.text for b in clean.blocks] == ["Real text."]


def test_content_hash_is_deterministic_and_content_sensitive() -> None:
    a = doc(Block("paragraph", "Same text."))
    b = doc(Block("paragraph", "Same   text."))  # same after cleaning
    c = doc(Block("paragraph", "Different text."))
    assert a.content_hash == b.content_hash == sha256_text("Same text.")
    assert a.content_hash != c.content_hash


def test_scanned_pdf_gets_the_no_ocr_message() -> None:
    with pytest.raises(UnsupportedDocument, match="OCR"):
        doc(file_type="pdf", min_chars=50)


def test_too_little_text_is_rejected_with_numbers() -> None:
    with pytest.raises(UnsupportedDocument, match=r"\(5 characters; at least 50"):
        doc(Block("paragraph", "short"), min_chars=50)


def test_title_falls_back_to_the_file_name() -> None:
    assert doc(Block("paragraph", "text"), title=None).title == "fallback"
