import uuid

from app.chat.context import MIN_EXCERPT_CHARS, build_context, near_duplicate
from tests.fakes.chat import retrieved

LONG = "Unused items can be returned within 30 days of delivery for a full refund. " * 4


def test_excerpts_are_numbered_in_retrieval_order() -> None:
    results = [retrieved(f"chunk number {i} " + LONG, rank=i) for i in range(1, 4)]
    excerpts = build_context(results, max_chars=10_000)
    assert [e.citation_id for e in excerpts] == ["S1", "S2", "S3"]
    assert [e.chunk_id for e in excerpts] == [r.hit.chunk_id for r in results]
    assert excerpts[0].document_title == "Returns Policy"
    assert excerpts[0].filename == "returns-policy.md"
    assert not any(e.truncated for e in excerpts)


def test_empty_chunks_are_skipped_without_using_an_id() -> None:
    excerpts = build_context(
        [retrieved("  \n ", rank=1), retrieved(LONG, rank=2)], max_chars=10_000
    )
    assert [(e.citation_id, e.rank) for e in excerpts] == [("S1", 2)]


def test_no_results_give_no_excerpts() -> None:
    assert build_context([], max_chars=10_000) == []


def test_near_identical_chunks_of_one_document_are_kept_once() -> None:
    same_doc = [
        retrieved(LONG, rank=1),
        retrieved(LONG.upper(), rank=2),  # same text, other case
        retrieved(LONG[20:200], rank=3),  # contained in the first
        retrieved(LONG.replace("full refund", "full  refund."), rank=4),  # nearly identical
    ]
    excerpts = build_context(same_doc, max_chars=10_000)
    assert [e.rank for e in excerpts] == [1]


def test_the_same_text_in_another_document_is_kept() -> None:
    other = uuid.uuid4()
    excerpts = build_context(
        [retrieved(LONG, rank=1), retrieved(LONG, rank=2, document_id=other)], max_chars=10_000
    )
    assert [e.rank for e in excerpts] == [1, 2]


def test_different_chunks_of_one_document_are_not_duplicates() -> None:
    assert not near_duplicate(
        "Laptops can be returned within 14 days if the factory seal is intact.",
        "Refunds are issued within 5 business days after the warehouse inspects the item.",
    )


def test_the_character_budget_cuts_the_last_excerpt_and_stops() -> None:
    chunk = "word " * 200  # 1,000 characters
    results = [retrieved(f"{i} {chunk}", rank=i) for i in range(1, 6)]
    excerpts = build_context(results, max_chars=1_600 + MIN_EXCERPT_CHARS - 1)
    assert sum(len(e.text) for e in excerpts) <= 1_600 + MIN_EXCERPT_CHARS - 1
    assert [e.truncated for e in excerpts] == [False, True]
    assert excerpts[1].text.endswith(" …")
    assert not excerpts[1].text.endswith("wor …")  # cut at a word boundary


def test_a_first_chunk_larger_than_the_budget_is_cut_not_dropped() -> None:
    (only,) = build_context([retrieved("x" * 5_000)], max_chars=1_000)
    assert only.truncated and len(only.text) <= 1_000
