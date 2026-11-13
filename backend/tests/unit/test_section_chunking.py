"""section-v1 chunking (Experiment 005): headings, minimum fill, and per-workspace selection.

paragraph-v1 stays the default and must not change at all.
"""

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from app.config import Settings
from app.ingestion.chunker import ChunkerConfig, chunk_document
from app.ingestion.cleaner import build_clean_document
from app.ingestion.parsers import (
    ParsedDocument,
    _looks_like_heading,
    _looks_like_section_heading,
    _pdf_page_blocks,
    parse_document,
    parse_document_sections,
)
from app.ingestion.pipeline import IngestionWorker
from tests.fakes.documents import make_pdf

ASIA_CEMENT = uuid.uuid4()  # other workspaces: never section-v1 unless assigned
NORTHSTAR = uuid.uuid4()
EXPERIMENT = uuid.uuid4()

FILLER = "Employees follow this rule in every country where the company operates."


POLICY_PAGE = [
    "2.3 Anticorruption",
    *[f"{FILLER} Clause {i} of the anticorruption section." for i in range(5)],
    "",  # extracted text separates blocks with blank lines; headings start a block
    "Our Expectations",
    "• Never offer personal advantages to a purchasing employee.",
    *[f"• Report every attempted bribe immediately, case {i}." for i in range(4)],
    "",
    "Our Expectations",
    "• Never accept money or gift cards as a gift.",
    *[f"• Evaluate the value of each invitation before accepting it, case {i}." for i in range(6)],
    "",
    "2.4 Gifts, Hospitality, and Benefits",
    *[f"{FILLER} Clause {i} of the gifts section." for i in range(5)],
]
"""Numbered sections, each with a rules box, as in the Asia Cement Code of Conduct (one
page of extracted text; the PDF test builder can't write the bullet character)."""


def kinds(heading_test: Any, lines: list[str]) -> list[str]:
    """Block kinds the PDF parser makes from one page's extracted lines."""
    return [b.kind for b in _pdf_page_blocks("\n".join(lines), 1, heading_test)]


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (
            ["2.4 Gifts, Hospitality, and Benefits", "We are prohibited from it."],
            ["heading", "paragraph"],
        ),
        (["1. Introduction", "One of the objectives is this."], ["heading", "paragraph"]),
        (["Our Expectations", "• Never accept money."], ["heading", "paragraph"]),
        (["Warranty", "Every laptop has two years of cover."], ["heading", "paragraph"]),
        # Still conservative: wrapped sentences and numbered clauses stay text.
        (
            ["3.3 Except as expressly permitted below, employees must", "not accept gifts."],
            ["paragraph"],
        ),
        (["2.4 gifts and other things", "Next sentence."], ["paragraph"]),  # lower-case title
        (["Returns are accepted for", "• Unused items only."], ["paragraph"]),  # ends on "for"
        (["Ends with a period.", "• Next."], ["paragraph"]),
    ],
)
def test_section_headings_find_numbered_and_boxed_titles_only(
    lines: list[str], expected: list[str]
) -> None:
    assert kinds(_looks_like_section_heading, lines) == expected


@pytest.mark.parametrize(
    "lines",
    [
        ["2.4 Gifts, Hospitality, and Benefits", "We are prohibited from it."],
        ["Our Expectations", "• Never accept money."],
    ],
)
def test_paragraph_v1_parsing_is_unchanged(lines: list[str]) -> None:
    assert kinds(_looks_like_heading, lines) == ["paragraph"]  # as before: not headings


def test_a_real_pdf_with_numbered_headings_parses_by_strategy() -> None:
    data = make_pdf([["2.4 Gifts, Hospitality, and Benefits", "We are prohibited from it."]])
    assert [b.kind for b in parse_document_sections("pdf", data).blocks] == ["heading", "paragraph"]
    assert [b.kind for b in parse_document("pdf", data).blocks] == ["paragraph"]


def test_non_pdf_files_parse_the_same_under_both_strategies() -> None:
    text = b"# Returns\n\n2.4 Gifts\nOur Expectations\n- item"
    assert parse_document_sections("md", text) == parse_document("md", text)


def test_fingerprints_and_minimum_fill() -> None:
    paragraph, section = ChunkerConfig(), ChunkerConfig(strategy="section-v1")
    assert paragraph.fingerprint == "paragraph-v1:t1800:m2400:o250"  # unchanged, stored today
    assert section.fingerprint == "section-v1:t1800:m2400:o250:f0.15"
    assert (paragraph.min_fill, section.min_fill) == (0.4, 0.15)
    with pytest.raises(ValueError, match="unknown chunking strategy"):
        ChunkerConfig(strategy="semantic-v9")  # type: ignore[arg-type]


def chunks(heading_test: Any, config: ChunkerConfig) -> list[Any]:
    parsed = ParsedDocument(
        title="Code", blocks=_pdf_page_blocks("\n".join(POLICY_PAGE), 1, heading_test), metadata={}
    )
    clean = build_clean_document(parsed, file_type="pdf", fallback_title="t", min_chars=50)
    return chunk_document(clean, config)


def test_section_v1_keeps_each_section_and_rules_box_apart() -> None:
    section = chunks(_looks_like_section_heading, ChunkerConfig(strategy="section-v1"))
    starts = [c.content.split("\n\n", 1)[0] for c in section]
    assert starts == [
        "2.3 Anticorruption",
        "Our Expectations",
        "Our Expectations",
        "2.4 Gifts, Hospitality, and Benefits",
    ]
    gift_box = section[2]
    assert "Never accept money or gift cards as a gift." in gift_box.content
    assert "Anticorruption" not in gift_box.content and "2.4 Gifts" not in gift_box.content
    assert gift_box.heading == "Our Expectations"
    # Exact slices of the document text, as always.
    assert all(c.content_hash for c in section)
    paragraph = chunks(_looks_like_heading, ChunkerConfig())
    assert len(paragraph) < len(section)
    assert any("Anticorruption" in c.content and "gift cards" in c.content for c in paragraph)


def worker_settings(assignments: dict[str, str]) -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://u:p@localhost/db",
        jwt_secret="j" * 40,
        app_secret_key="a" * 40,
        ingestion_workspace_chunkers=assignments,
    )


def chunking(settings: Settings, workspace: uuid.UUID, file_type: str) -> tuple[str, str]:
    worker = IngestionWorker.__new__(IngestionWorker)  # no database or provider needed
    worker.settings = settings
    worker.chunker = ChunkerConfig()
    worker.section_chunker = ChunkerConfig(strategy="section-v1")
    worker._parse_target = parse_document
    worker._section_parse_target = parse_document_sections
    config, parse = IngestionWorker.chunking_for(worker, workspace, file_type)
    return config.strategy, parse.__name__


def test_only_the_assigned_workspace_uses_section_v1_and_only_for_pdfs() -> None:
    settings = worker_settings({str(EXPERIMENT): "section-v1"})
    assert chunking(settings, EXPERIMENT, "pdf") == ("section-v1", "parse_document_sections")
    assert chunking(settings, EXPERIMENT, "md") == ("paragraph-v1", "parse_document")
    assert chunking(settings, EXPERIMENT, "docx") == ("paragraph-v1", "parse_document")
    for other in (ASIA_CEMENT, NORTHSTAR, uuid.uuid4()):
        assert chunking(settings, other, "pdf") == ("paragraph-v1", "parse_document")
    assert chunking(worker_settings({}), ASIA_CEMENT, "pdf") == ("paragraph-v1", "parse_document")


def test_the_setting_accepts_workspace_ids_and_known_strategies_only() -> None:
    assert worker_settings({}).ingestion_workspace_chunkers == {}
    with pytest.raises(ValidationError):
        worker_settings({str(EXPERIMENT): "semantic-v9"})
    with pytest.raises(ValidationError):
        worker_settings({"exp005@example.com": "section-v1"})


def test_a_box_under_the_minimum_fill_joins_the_next_section_instead_of_standing_alone() -> None:
    short_box = [*POLICY_PAGE]
    start = short_box.index("• Never accept money or gift cards as a gift.") + 1
    end = short_box.index("2.4 Gifts, Hospitality, and Benefits") - 1
    del short_box[start:end]  # leave a box of one bullet (well under 270 characters)
    parsed = ParsedDocument(
        title="Code",
        blocks=_pdf_page_blocks("\n".join(short_box), 1, _looks_like_section_heading),
        metadata={},
    )
    clean = build_clean_document(parsed, file_type="pdf", fallback_title="t", min_chars=50)
    section = chunk_document(clean, ChunkerConfig(strategy="section-v1"))
    box = next(c for c in section if "gift cards" in c.content)
    assert len(box.content) > 270 and "2.4 Gifts, Hospitality, and Benefits" in box.content
