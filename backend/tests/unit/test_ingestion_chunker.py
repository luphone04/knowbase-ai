"""Paragraph-aware chunking: boundaries, overlap, long paragraphs, determinism, metadata."""

import pytest

from app.ingestion.chunker import ChunkDraft, ChunkerConfig, chunk_document
from app.ingestion.cleaner import CleanDocument, build_clean_document, sha256_text
from app.ingestion.parsers import Block, ParsedDocument

CONFIG = ChunkerConfig(target=1800, maximum=2400, overlap=250)


def make(*blocks: Block) -> CleanDocument:
    return build_clean_document(
        ParsedDocument("Doc", list(blocks)), file_type="md", fallback_title="Doc", min_chars=1
    )


def sentences(n: int, prefix: str = "Sentence") -> str:
    return " ".join(f"{prefix} {i} is a complete sentence about returns." for i in range(n))


def check_invariants(
    document: CleanDocument, chunks: list[ChunkDraft], config: ChunkerConfig
) -> None:
    for index, chunk in enumerate(chunks):
        assert chunk.index == index
        assert chunk.content == document.text[chunk.char_start : chunk.char_end]
        assert len(chunk.content) <= config.maximum
        assert chunk.content == chunk.content.strip()
        assert chunk.content_hash == sha256_text(chunk.content)
    # Every character of the document is covered by some chunk.
    covered: set[int] = set()
    for chunk in chunks:
        covered.update(range(chunk.char_start, chunk.char_end))
    gaps = [
        i for i in range(len(document.text)) if i not in covered and not document.text[i].isspace()
    ]
    assert gaps == []


def test_small_document_is_one_chunk() -> None:
    document = make(Block("heading", "Returns"), Block("paragraph", "Thirty days."))
    (chunk,) = chunk_document(document, CONFIG)
    assert chunk.content == "Returns\n\nThirty days."
    assert (chunk.char_start, chunk.char_end) == (0, len(document.text))
    assert chunk.heading == "Returns"


def test_empty_document_has_no_chunks() -> None:
    empty = CleanDocument("t", [], [], "", sha256_text(""), {})
    assert chunk_document(empty, CONFIG) == []


def test_paragraphs_are_packed_up_to_the_target_without_splitting() -> None:
    paragraphs = [Block("paragraph", sentences(6, f"P{i}")) for i in range(12)]  # ~300 chars each
    document = make(*paragraphs)
    chunks = chunk_document(document, CONFIG)
    check_invariants(document, chunks, CONFIG)
    assert len(chunks) > 1
    for chunk in chunks[:-1]:
        assert len(chunk.content) <= CONFIG.target
    # Chunk ends fall on paragraph ends, never mid-paragraph.
    ends = {end for _, end in document.offsets}
    assert all(chunk.char_end in ends for chunk in chunks)


def test_overlap_repeats_the_previous_tail_from_a_sentence_boundary() -> None:
    document = make(*[Block("paragraph", sentences(6, f"P{i}")) for i in range(12)])
    chunks = chunk_document(document, CONFIG)
    for previous, chunk in zip(chunks, chunks[1:], strict=False):
        overlap = previous.char_end - chunk.char_start
        assert 0 < overlap <= CONFIG.overlap
        assert chunk.content.startswith(("P", "Sentence"))  # begins at a sentence start
        assert previous.content.endswith(chunk.content[:overlap])


def test_zero_overlap_means_disjoint_chunks() -> None:
    config = ChunkerConfig(target=600, maximum=800, overlap=0)
    document = make(*[Block("paragraph", sentences(4, f"P{i}")) for i in range(8)])
    chunks = chunk_document(document, config)
    check_invariants(document, chunks, config)
    for previous, chunk in zip(chunks, chunks[1:], strict=False):
        assert chunk.char_start > previous.char_end


def test_long_paragraph_is_split_at_sentence_ends_near_the_target() -> None:
    long = sentences(200)  # ~10,000 chars in one paragraph
    document = make(Block("paragraph", long))
    chunks = chunk_document(document, CONFIG)
    check_invariants(document, chunks, CONFIG)
    assert len(chunks) >= 5
    for chunk in chunks:
        assert chunk.content.endswith(".")  # never ends mid-sentence
        assert len(chunk.content) <= CONFIG.target + 50


def test_paragraph_that_fits_the_maximum_is_never_split() -> None:
    paragraph = sentences(40)[:2100]
    paragraph = paragraph[: paragraph.rfind(".") + 1]
    document = make(Block("paragraph", "Intro."), Block("paragraph", paragraph))
    assert any(paragraph in chunk.content for chunk in chunk_document(document, CONFIG))


def test_sentence_longer_than_the_limit_splits_at_spaces() -> None:
    words = " ".join(f"word{i}" for i in range(1000))  # no sentence ends at all
    document = make(Block("paragraph", words))
    chunks = chunk_document(document, CONFIG)
    check_invariants(document, chunks, CONFIG)
    assert all(not chunk.content.endswith("word") for chunk in chunks)


def test_giant_single_word_is_hard_cut_but_fully_covered() -> None:
    document = make(Block("paragraph", "x" * 6000))
    chunks = chunk_document(document, CONFIG)
    check_invariants(document, chunks, CONFIG)


def test_headings_start_new_chunks_and_are_never_stranded() -> None:
    blocks = []
    for section in range(4):
        blocks.append(Block("heading", f"Section {section}"))
        blocks.extend(Block("paragraph", sentences(6, f"S{section}P{i}")) for i in range(3))
    document = make(*blocks)
    chunks = chunk_document(document, CONFIG)
    check_invariants(document, chunks, CONFIG)
    for chunk in chunks:
        assert not chunk.content.endswith(tuple(f"Section {s}" for s in range(4)))
        assert chunk.heading is not None
    # A chunk that opens with a heading carries no overlap from the previous section.
    for chunk in chunks:
        if chunk.content.startswith("Section"):
            assert chunk.heading == chunk.content.split("\n", 1)[0]


def test_heading_is_never_a_chunk_on_its_own() -> None:
    document = make(Block("heading", "Returns"), Block("paragraph", sentences(80)))
    chunks = chunk_document(document, CONFIG)
    assert chunks[0].content.startswith("Returns\n\n")
    assert len(chunks[0].content) > len("Returns")


def test_quotes_stay_with_their_sentence_at_split_points() -> None:
    text = " ".join(f'Item {i} is labelled "final sale." Returns are refused.' for i in range(150))
    document = make(Block("paragraph", text))
    for chunk in chunk_document(document, CONFIG):
        assert chunk.content.count('"') % 2 == 0


def test_pdf_page_numbers_are_recorded() -> None:
    blocks = [Block("paragraph", sentences(8, f"Page{p}"), page=p) for p in (1, 2, 3, 4, 5, 6)]
    document = build_clean_document(
        ParsedDocument("Doc", blocks), file_type="pdf", fallback_title="Doc", min_chars=1
    )
    chunks = chunk_document(document, CONFIG)
    assert chunks[0].page_start == 1
    assert chunks[-1].page_end == 6
    for chunk in chunks:
        assert chunk.page_start is not None and chunk.page_end is not None
        assert chunk.page_start <= chunk.page_end


def test_chunking_is_deterministic() -> None:
    document = make(*[Block("paragraph", sentences(9, f"P{i}")) for i in range(20)])
    assert chunk_document(document, CONFIG) == chunk_document(document, CONFIG)


def test_fingerprint_changes_with_settings() -> None:
    assert CONFIG.fingerprint != ChunkerConfig(target=1000, maximum=2400, overlap=250).fingerprint


@pytest.mark.parametrize(
    ("target", "maximum", "overlap"), [(100, 50, 10), (100, 200, 100), (100, 200, -1)]
)
def test_inconsistent_settings_are_rejected(target: int, maximum: int, overlap: int) -> None:
    with pytest.raises(ValueError):
        ChunkerConfig(target=target, maximum=maximum, overlap=overlap)


def test_chunking_many_small_blocks_is_not_quadratic() -> None:
    """200,000 tiny paragraphs took ~5 s when block lookups rebuilt per chunk (audit M13)."""
    import time

    from app.ingestion.cleaner import build_clean_document
    from app.ingestion.parsers import parse_document

    parsed = parse_document("txt", b"a\n\n" * 200_000)
    document = build_clean_document(parsed, file_type="txt", fallback_title="t", min_chars=1)
    started = time.perf_counter()
    chunks = chunk_document(document, ChunkerConfig())
    assert time.perf_counter() - started < 2.0
    assert chunks and all(c.heading is None for c in chunks)


def test_heading_lookup_matches_the_nearest_preceding_heading() -> None:
    from app.ingestion.cleaner import build_clean_document
    from app.ingestion.parsers import parse_document

    text = "# One\n\n" + "para one. " * 300 + "\n\n# Two\n\n" + "para two. " * 300
    document = build_clean_document(
        parse_document("md", text.encode()), file_type="md", fallback_title="t", min_chars=1
    )
    chunks = chunk_document(document, ChunkerConfig())
    assert [c.heading for c in chunks][0] == "One"
    assert chunks[-1].heading == "Two"
