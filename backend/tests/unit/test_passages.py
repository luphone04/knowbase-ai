"""Mapping evidence quotations to chunks, and passage-level retrieval scores."""

from app.evaluation.metrics import passage_score, summarize_documents
from app.evaluation.passages import (
    ChunkText,
    evidence_groups,
    map_evidence,
    relevant_chunks,
)

QUOTE = "Never accept money or gift cards as a gift."


def chunk(
    cid: str, index: int, content: str, *, key: str = "cobc", pages: tuple[int, int] = (9, 9)
) -> ChunkText:
    return ChunkText(cid, key, index, pages[0], pages[1], content)


def test_a_quotation_inside_one_chunk_is_matched() -> None:
    chunks = [chunk("a", 0, "Intro."), chunk("b", 1, f"Rules. • {QUOTE} More rules.")]
    match = map_evidence(QUOTE, source_key="cobc", page=9, chunks=chunks)
    assert (match.status, match.chunk_ids, match.level) == ("matched", ("b",), "exact")


def test_overlapping_chunks_that_both_hold_it_are_all_relevant() -> None:
    chunks = [chunk("a", 3, f"End of one. {QUOTE}"), chunk("b", 4, f"{QUOTE} Start of two.")]
    match = map_evidence(QUOTE, source_key="cobc", page=9, chunks=chunks)
    assert match.status == "matched" and match.chunk_ids == ("a", "b")


def test_typography_and_pdf_spacing_are_tolerated_and_labelled() -> None:
    curly = chunk("a", 0, "Employees’ duties: never accept   money or gift cards as a gift.")
    assert (
        map_evidence(
            "Employees' duties: never accept money or gift cards as a gift.",
            source_key="cobc",
            page=9,
            chunks=[curly],
        ).level
        == "exact"
    )
    spaced = chunk("b", 0, "Never accept mo ney or gift cards as a gi ft.")
    match = map_evidence(QUOTE, source_key="cobc", page=9, chunks=[spaced])
    assert (match.status, match.level) == ("matched", "spacing")


def test_a_quotation_across_a_chunk_boundary_is_split_not_matched() -> None:
    chunks = [
        chunk("a", 0, "Section text. Never accept money or gift"),
        chunk("b", 1, "cards as a gift. Next section."),
    ]
    match = map_evidence(QUOTE, source_key="cobc", page=9, chunks=chunks)
    assert match.status == "split" and match.chunk_ids == ()
    assert "spans chunks 0 and 1" in (match.detail or "")


def test_other_pages_separate_chunks_and_other_sources_are_never_guessed() -> None:
    wrong_page = chunk("a", 0, QUOTE, pages=(3, 3))
    assert map_evidence(QUOTE, source_key="cobc", page=9, chunks=[wrong_page]).status == (
        "ambiguous"
    )
    apart = [chunk("a", 1, QUOTE), chunk("b", 7, QUOTE)]
    assert map_evidence(QUOTE, source_key="cobc", page=9, chunks=apart).status == "ambiguous"
    elsewhere = chunk("a", 0, QUOTE, key="acp")
    assert map_evidence(QUOTE, source_key="cobc", page=9, chunks=[elsewhere]).status == "missing"
    assert map_evidence(QUOTE, source_key="cobc", page=9, chunks=[]).status == "missing"


def test_a_question_has_relevant_chunks_only_if_all_its_evidence_matched() -> None:
    matched = {"status": "matched", "chunk_ids": ["a", "b"]}
    other = {"status": "matched", "chunk_ids": ["c"]}
    assert relevant_chunks({"evidence": [matched, other]}) == {"a", "b", "c"}
    assert evidence_groups({"evidence": [matched, other]}) == [{"a", "b"}, {"c"}]
    split = {"status": "split", "chunk_ids": []}
    assert relevant_chunks({"evidence": [matched, split]}) is None
    assert evidence_groups({"evidence": [matched, split]}) is None
    assert relevant_chunks(None) is None and relevant_chunks({"evidence": []}) is None


def test_passage_scores_need_the_chunk_not_just_the_document() -> None:
    evidence = [{"e1", "e1-overlap"}, {"e2"}]
    score = passage_score(["x", "e1-overlap", "y", "z", "w"], evidence)
    assert score is not None
    assert (score.hit, score.recall, score.reciprocal_rank) == (True, 0.5, 0.5)
    miss = passage_score(["same-document-other-chunk", "x"], evidence)
    assert miss is not None and (miss.hit, miss.recall, miss.reciprocal_rank) == (False, 0, 0)
    assert passage_score(["e1"], None) is None  # unmapped: no score, not a zero


def test_document_level_results_are_summarized_separately() -> None:
    sources = [{"rank": 1, "source_key": "acp"}, {"rank": 2, "source_key": "cobc"}]
    summary = summarize_documents(
        [("vector", "AC-001", sources), ("vector", "AC-002", [])],
        expected={"AC-001": ["cobc"], "AC-002": ["terms"]},
        variants=["vector", "hybrid"],
    )
    assert summary["vector"] == {
        "questions": 2,
        "hit_rate_at_5": 0.5,
        "recall_at_5": 0.5,
        "mrr": 0.25,
    }
    assert summary["hybrid"]["questions"] == 0 and summary["hybrid"]["mrr"] is None
