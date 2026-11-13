"""Experiment 005 golden tests on the four real Asia Cement PDFs.

The PDFs are user-uploaded documents and are not in the repository. Point
ASIA_CEMENT_PDF_DIR at a folder holding them (the benchmark file names; SHA-256
checked) to run these tests; otherwise they are skipped. The fixture holds only
SHA-256 hashes of chunk text, never document text.

Only development questions are read: the held-out evidence stays sealed.
"""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.evaluation.dataset import load_asia_cement_dev
from app.evaluation.passages import ChunkText, map_evidence
from app.ingestion.chunker import ChunkDraft, ChunkerConfig, chunk_document
from app.ingestion.cleaner import build_clean_document
from app.ingestion.parsers import parse_document, parse_document_sections

FIXTURE = json.loads(
    (
        Path(__file__).resolve().parents[1] / "fixtures" / "asia_cement" / "chunk_hashes.json"
    ).read_text()
)
PDF_DIR = Path(os.environ.get("ASIA_CEMENT_PDF_DIR", "/nonexistent"))
BENCHMARK = load_asia_cement_dev()  # the public development split
DEV = [q for q in BENCHMARK.questions if q.split == "dev"]


def _pdf(key: str) -> bytes | None:
    path = PDF_DIR / BENCHMARK.file_name(key)
    if not path.is_file():
        return None
    data = path.read_bytes()
    return data if hashlib.sha256(data).hexdigest() == BENCHMARK.source_hashes[key] else None


AVAILABLE = all(_pdf(key) is not None for key in BENCHMARK.source_hashes)
pytestmark = pytest.mark.skipif(
    not AVAILABLE, reason="set ASIA_CEMENT_PDF_DIR to the four Asia Cement PDFs to run these"
)


def chunks(key: str, strategy: str) -> list[ChunkDraft]:
    data = _pdf(key)
    assert data is not None
    parse: Any = parse_document_sections if strategy == "section-v1" else parse_document
    clean = build_clean_document(
        parse("pdf", data), file_type="pdf", fallback_title="t", min_chars=50
    )
    return chunk_document(clean, ChunkerConfig(strategy=strategy))  # type: ignore[arg-type]


def test_paragraph_v1_reproduces_the_72_indexed_chunks_byte_for_byte() -> None:
    total = 0
    for key, expected in FIXTURE["documents"].items():
        produced = chunks(key, "paragraph-v1")
        assert [c.content_hash for c in produced] == expected["paragraph_v1"], key
        total += len(produced)
    assert total == 72
    assert FIXTURE["chunker"]["paragraph_v1"] == ChunkerConfig().fingerprint


def test_section_v1_makes_87_chunks() -> None:
    counts = {key: len(chunks(key, "section-v1")) for key in FIXTURE["documents"]}
    assert counts == {"acp": 20, "bpcc": 17, "cobc": 40, "terms": 10}
    for key, expected in FIXTURE["documents"].items():
        assert [c.content_hash for c in chunks(key, "section-v1")] == expected["section_v1"], key
    # The Business Partner Code has no numbered or boxed headings: it is unchanged.
    assert (
        FIXTURE["documents"]["bpcc"]["section_v1"] == FIXTURE["documents"]["bpcc"]["paragraph_v1"]
    )


def test_the_gift_rules_become_one_focused_chunk() -> None:
    code = chunks("cobc", "section-v1")
    holding = [c for c in code if "Never accept money or gift cards as a gift." in c.content]
    assert len(holding) == 1
    gift = holding[0]
    assert len(gift.content) == 523 and (gift.page_start, gift.page_end) == (9, 9)
    assert gift.heading == "Our Expectations" and gift.content.startswith("Our Expectations")
    quotes = {
        f"{q.key} Q{i}": e.quote
        for q in DEV
        if q.key in ("AC-001", "AC-014", "AC-017")
        for i, e in enumerate(q.evidence, 1)
        if e.source_key == "cobc" and e.quote in gift.content
    }
    assert sorted(quotes) == ["AC-001 Q1", "AC-014 Q2", "AC-017 Q1", "AC-017 Q2"]
    # Neither neighbouring section is mixed in, unlike paragraph-v1's cobc#10.
    assert "2.3 Anticorruption" not in gift.content and "2.2 Health" not in gift.content
    assert "2.4 Gifts, Hospitality, and Benefits" not in gift.content
    old = [c for c in chunks("cobc", "paragraph-v1") if "Never accept money or gift" in c.content]
    assert len(old) == 1 and "2.3 Anticorruption" in old[0].content  # the mixed chunk it replaces


def mapped(strategy: str) -> dict[tuple[str, int], Any]:
    texts = [
        ChunkText(f"{key}#{c.index}", key, c.index, c.page_start, c.page_end, c.content)
        for key in FIXTURE["documents"]
        for c in chunks(key, strategy)
    ]
    return {
        (q.key, i): map_evidence(e.quote, source_key=e.source_key, page=e.page, chunks=texts)
        for q in DEV
        for i, e in enumerate(q.evidence, 1)
    }


def test_all_28_development_quotations_map_to_section_v1_chunks() -> None:
    result = mapped("section-v1")
    assert len(result) == 28  # development evidence only; held-out is never read
    assert {key for key, _ in result} <= {q.key for q in DEV}
    assert all(m.status == "matched" for m in result.values())
    spacing = sorted(key for key, m in result.items() if m.level == "spacing")
    assert spacing == [("AC-012", 1)]  # PDF spacing ("non -cash"), as under paragraph-v1
    # The targets of the experiment land in focused chunks.
    assert result[("AC-001", 1)].chunk_ids == ("cobc#14",)
    assert result[("AC-017", 1)].chunk_ids == result[("AC-017", 2)].chunk_ids == ("cobc#14",)
    assert result[("AC-014", 1)].chunk_ids == ("cobc#15",)  # 2.4 Gifts body
    assert result[("AC-014", 3)].chunk_ids == ("bpcc#13",)  # unchanged Business Partner chunk


def test_paragraph_v1_mapping_is_unchanged() -> None:
    result = mapped("paragraph-v1")
    assert all(m.status == "matched" for m in result.values())
    assert result[("AC-001", 1)].chunk_ids == ("cobc#10",)  # as in Experiments 003 and 004
