import pytest

from app.ingestion.parsers import UnsupportedDocument, parse_document
from tests.fakes.documents import encrypt_pdf, make_docx, make_pdf

# --- PDF -----------------------------------------------------------------------------------


def test_pdf_text_is_extracted_page_by_page_with_page_numbers() -> None:
    data = make_pdf(
        [
            ["Returns Policy", "Laptops can be returned within 30 days."],
            ["Shipping takes three to five business days."],
        ],
        title="Northstar Policies",
    )
    parsed = parse_document("pdf", data)
    assert parsed.title == "Northstar Policies"  # from PDF metadata
    assert [(b.kind, b.text, b.page) for b in parsed.blocks] == [
        ("heading", "Returns Policy", 1),
        ("paragraph", "Laptops can be returned within 30 days.", 1),
        ("paragraph", "Shipping takes three to five business days.", 2),
    ]
    assert parsed.metadata["page_count"] == 2
    assert parsed.metadata["pages_without_text"] == []


def test_pdf_wrapped_lines_join_into_one_paragraph() -> None:
    parsed = parse_document(
        "pdf", make_pdf([["The warranty covers defects in", "materials for two years."]])
    )
    assert [b.text for b in parsed.blocks] == [
        "The warranty covers defects in materials for two years."
    ]


def test_pdf_pages_without_text_are_recorded_not_invented() -> None:
    parsed = parse_document("pdf", make_pdf([["Real text on page one."], []]))
    assert parsed.metadata["pages_without_text"] == [2]
    assert parse_document("pdf", make_pdf([[], []])).blocks == []  # no OCR, no fake text


def test_password_protected_pdf_is_rejected() -> None:
    with pytest.raises(UnsupportedDocument, match="Password-protected"):
        parse_document("pdf", encrypt_pdf(make_pdf([["secret"]])))


@pytest.mark.parametrize(
    "data",
    [b"%PDF-1.7\nthis is not a real pdf", b"%PDF-", make_pdf([["x"]])[:200]],
    ids=["garbage", "header-only", "truncated"],
)
def test_malformed_pdf_is_rejected_clearly(data: bytes) -> None:
    with pytest.raises(UnsupportedDocument, match="could not be read"):
        parse_document("pdf", data)


# --- DOCX ----------------------------------------------------------------------------------


def test_docx_keeps_headings_paragraphs_and_tables_in_reading_order() -> None:
    data = make_docx(
        [
            ("heading", ("Warranty", 1)),
            ("paragraph", "Two years of cover."),
            ("table", [["Item", "Cover"], ["Laptop", "2 years"]]),
            ("heading", ("Exclusions", 2)),
            ("paragraph", "Water damage is not covered."),
        ],
        title="Warranty policy",
    )
    parsed = parse_document("docx", data)
    assert parsed.title == "Warranty policy"
    assert [(b.kind, b.text, b.level) for b in parsed.blocks] == [
        ("heading", "Warranty", 1),
        ("paragraph", "Two years of cover.", None),
        ("table", "Item | Cover\nLaptop | 2 years", None),
        ("heading", "Exclusions", 2),
        ("paragraph", "Water damage is not covered.", None),
    ]
    assert parsed.metadata["table_count"] == 1


def test_docx_title_falls_back_to_first_heading() -> None:
    parsed = parse_document(
        "docx", make_docx([("heading", ("Product guide", 1)), ("paragraph", "x")])
    )
    assert parsed.title == "Product guide"


def test_malformed_docx_is_rejected_clearly() -> None:
    from tests.fakes.documents import make_zip

    broken = make_zip({"[Content_Types].xml": b"<bad", "word/document.xml": b"<not xml"})
    with pytest.raises(UnsupportedDocument, match="could not be read"):
        parse_document("docx", broken)


# --- TXT / Markdown --------------------------------------------------------------------


def test_text_paragraphs_split_on_blank_lines_and_keep_line_breaks() -> None:
    parsed = parse_document(
        "txt", b"Opening hours\r\n\r\nMonday to Friday\nnine to five\n\n\n\nClosed Sundays"
    )
    assert [b.text for b in parsed.blocks] == [
        "Opening hours",
        "Monday to Friday\nnine to five",
        "Closed Sundays",
    ]
    assert parsed.title == "Opening hours"


def test_text_with_bom_decodes() -> None:
    assert parse_document("txt", "﻿Hello".encode()).blocks[0].text == "Hello"


def test_invalid_utf8_is_reported() -> None:
    with pytest.raises(UnsupportedDocument, match="UTF-8"):
        parse_document("txt", b"\xff\xfe broken")


def test_markdown_headings_become_heading_blocks() -> None:
    parsed = parse_document("md", b"# Returns\nIntro line\n## Damaged boxes ##\nStill accepted.")
    assert [(b.kind, b.text, b.level) for b in parsed.blocks] == [
        ("heading", "Returns", 1),
        ("paragraph", "Intro line", None),
        ("heading", "Damaged boxes", 2),
        ("paragraph", "Still accepted.", None),
    ]
    assert parsed.title == "Returns"


def test_markdown_code_fences_are_one_block_and_hide_headings() -> None:
    parsed = parse_document("md", b"Intro\n\n```bash\n# not a heading\n\necho hi\n```\nAfter")
    kinds = [(b.kind, b.text) for b in parsed.blocks]
    assert ("paragraph", "```bash\n# not a heading\n\necho hi\n```") in kinds
    assert all(kind != "heading" for kind, _ in kinds)


def test_unknown_type_is_rejected() -> None:
    with pytest.raises(UnsupportedDocument):
        parse_document("exe", b"MZ")


@pytest.mark.parametrize(
    ("lines", "expected_kinds"),
    [
        (["Warranty", "Every laptop has two years of cover."], ["heading", "paragraph"]),
        (["The warranty covers defects in", "materials for two years."], ["paragraph"]),
        (["Returns are accepted for", "Unused items only."], ["paragraph"]),  # ends on "for"
        (["Total: 30 days", "Within the window."], ["heading", "paragraph"]),
        (["Ends with a period.", "Next sentence."], ["paragraph"]),
    ],
)
def test_pdf_heading_heuristic_is_conservative(lines: list[str], expected_kinds: list[str]) -> None:
    parsed = parse_document("pdf", make_pdf([lines]))
    assert [b.kind for b in parsed.blocks] == expected_kinds


# --- Markdown headings: correctness and linear time (audit H1) ---------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("# Returns", (1, "Returns")),
        ("###### Six", (6, "Six")),
        ("## Closing hashes ##", (2, "Closing hashes")),
        ("# Trailing spaces   ", (1, "Trailing spaces")),
        ("# C#", (1, "C#")),  # "#" not preceded by a space is part of the text
        ("#\tTab separated", (1, "Tab separated")),
        ("#NoSpace", None),
        ("####### Seven", None),
        (" # Indented", None),
        ("# #", None),  # only a closing sequence: an empty heading
        ("#    ", None),
    ],
)
def test_markdown_heading_rules(line: str, expected: tuple[int, str] | None) -> None:
    from app.ingestion.parsers import _markdown_heading

    assert _markdown_heading(line) == expected


@pytest.mark.parametrize("spaces", [1_000, 20_000, 500_000])
def test_markdown_heading_parsing_is_linear_on_hostile_lines(spaces: int) -> None:
    """A heading with a long whitespace run was cubic: 20,000 spaces took ~3 hours."""
    import time

    hostile = ("# a" + " " * spaces + "b\n").encode()
    started = time.perf_counter()
    parsed = parse_document("md", hostile * 3)  # within MAX_DOCUMENT_CHARS
    assert time.perf_counter() - started < 2.0
    assert parsed.blocks  # still parsed (as paragraphs: too long to be headings)


def test_overlong_heading_lines_are_paragraphs() -> None:
    parsed = parse_document("md", ("# " + "word " * 300).encode())
    assert [b.kind for b in parsed.blocks] == ["paragraph"]


# --- resource limits for untrusted documents (audit H2) ------------------------------------


def test_pdf_page_limit_is_checked_before_extracting_text() -> None:
    from app.ingestion.parsers import ParseLimits

    pdf = make_pdf([["Page text."]] * 3)
    with pytest.raises(UnsupportedDocument, match="3 pages; the limit is 2"):
        parse_document("pdf", pdf, ParseLimits(max_pdf_pages=2))
    assert len(parse_document("pdf", pdf, ParseLimits(max_pdf_pages=3)).blocks) == 3


def test_a_small_pdf_that_expands_hugely_stops_at_the_character_limit() -> None:
    import time

    from app.ingestion.parsers import ParseLimits
    from tests.fakes.documents import make_shared_stream_pdf

    bomb = make_shared_stream_pdf(pages=50, chars_per_page=200_000)  # ~10 M characters
    assert len(bomb) < 10_000
    started = time.perf_counter()
    with pytest.raises(UnsupportedDocument, match="more than 300,000 characters"):
        parse_document("pdf", bomb, ParseLimits(max_chars=300_000))
    # Stopped after about 2 of the 50 pages, not after extracting all of them.
    assert time.perf_counter() - started < 5


def test_docx_and_text_character_limits() -> None:
    from app.ingestion.parsers import ParseLimits

    limits = ParseLimits(max_chars=1_000)
    docx_file = make_docx([("paragraph", "word " * 50)] * 10)  # 2,500 characters
    with pytest.raises(UnsupportedDocument, match="more than 1,000 characters"):
        parse_document("docx", docx_file, limits)
    with pytest.raises(UnsupportedDocument, match="more than 1,000 characters"):
        parse_document("txt", b"x" * 1_001, limits)
    assert parse_document("txt", b"x" * 1_000, limits).blocks
