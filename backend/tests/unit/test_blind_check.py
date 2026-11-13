"""The blind held-out duplicate check, on synthetic datasets (no real held-out content).

The check against the real held-out split is in test_asia_followups_dataset.py and
reports counts only.
"""

import dataclasses

from app.evaluation.blind_check import DuplicateReport, heldout_duplicates
from app.evaluation.dataset import Dataset, DatasetQuestion, Evidence

HELD_QUESTION = "Must suppliers report a conflict of interest to the purchasing team?"
HELD_QUOTE = "Suppliers must disclose any conflict of interest to the purchasing team in writing."


def question(
    key: str,
    text: str,
    *,
    quotes: tuple[str, ...] = (),
    previous: str | None = None,
    split: str = "dev",
    corroborating: tuple[str, ...] = (),
    page: int = 3,
) -> DatasetQuestion:
    return DatasetQuestion(
        key=key,
        position=1,
        category="follow_up" if previous else "direct",
        question=text,
        previous_question=previous,
        expected_source_keys=("doc",),
        expected_answer="An answer.",
        evidence=tuple(Evidence("doc", q, page=page) for q in quotes),
        absent_terms=(),
        notes=None,
        split=split,
        corroborating_evidence=tuple(Evidence("doc", q, page=page) for q in corroborating),
    )


def dataset(*questions: DatasetQuestion) -> Dataset:
    return Dataset(
        name="synthetic",
        version="1",
        description="",
        corpus_directory="",
        source_hashes={"doc": "0" * 64},
        questions=questions,
        sha256="0" * 64,
        relevance="passage",
        run_split="dev",
    )


PROTECTED = dataset(
    question("AC-001", "How often is training required?", quotes=("Train every two years.",)),
    question("AC-002", HELD_QUESTION, quotes=(HELD_QUOTE,), split="heldout"),
)


def report(*candidates: DatasetQuestion) -> DuplicateReport:
    return heldout_duplicates(dataset(*candidates), PROTECTED)


def test_an_unrelated_dataset_passes() -> None:
    result = report(question("FU-001", "What about gifts to officials?", quotes=("No gifts.",)))
    assert result == DuplicateReport(1, 1, 0, 0) and result.passed


def test_exact_and_trivially_edited_questions_are_duplicates() -> None:
    edited = "  must SUPPLIERS report a conflict of interest to the purchasing team? "
    assert report(question("FU-001", HELD_QUESTION)).duplicate_questions == 1
    assert report(question("FU-001", edited)).duplicate_questions == 1
    # A held-out question reused as a candidate's previous question counts too.
    reused = question("FU-001", "And in writing?", previous=HELD_QUESTION)
    assert report(reused).duplicate_questions == 1
    assert report(reused).passed is False


def test_substantially_identical_quotations_are_duplicates() -> None:
    spaced = HELD_QUOTE.replace("purchasing", "purch asing").replace("-", " - ")
    one_word = HELD_QUOTE.replace("in writing", "by email")
    for quote in (HELD_QUOTE, HELD_QUOTE.upper(), spaced, one_word):
        assert report(question("FU-001", "Unrelated?", quotes=(quote,))).duplicate_quotations == 1


def test_the_same_topic_page_or_document_is_not_a_duplicate() -> None:
    same_topic = question(
        "FU-001",
        "Do contractors have to tell anyone about conflicts of interest?",
        quotes=("Contractors notify the compliance team about any personal interest.",),
        page=3,  # the held-out evidence's page too
    )
    result = report(same_topic)
    assert (result.duplicate_questions, result.duplicate_quotations) == (0, 0)
    # A short quotation inside the held-out one is overlap, not a substantially identical one.
    inside = question("FU-002", "Unrelated?", quotes=("disclose any conflict of interest",))
    assert report(inside).passed


def test_only_the_held_out_split_and_only_scored_evidence_are_compared() -> None:
    development = question("FU-001", "How often is training required?")  # a dev question
    assert report(development).passed
    corroborating = question("FU-002", "Unrelated?", corroborating=(HELD_QUOTE,))
    assert report(corroborating).passed  # reviewer-only evidence isn't scored or compared


def test_the_report_holds_counts_only() -> None:
    result = report(question("FU-001", HELD_QUESTION, quotes=(HELD_QUOTE,)))
    assert {f.name: type(getattr(result, f.name)) for f in dataclasses.fields(result)} == {
        "candidate_questions": int,
        "held_out_questions": int,
        "duplicate_questions": int,
        "duplicate_quotations": int,
    }
    assert HELD_QUESTION not in repr(result) and "AC-002" not in repr(result)
