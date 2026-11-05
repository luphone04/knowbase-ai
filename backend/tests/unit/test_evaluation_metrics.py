"""Evaluation metrics on small hand-computed examples (ADR 0014)."""

import math
from dataclasses import replace

import pytest

from app.db.models import EVALUATION_VARIANTS
from app.evaluation.metrics import (
    ResultRow,
    SourceRef,
    abstention_correct,
    chance_hit_rate,
    citation_score,
    latency_stats,
    nearest_rank,
    rank_sources,
    retrieval_score,
    summarize,
    summarize_answers,
    summarize_retrieval,
)
from app.evaluation.variants import ANSWER_MODES, BY_KEY, VARIANTS
from app.retrieval.service import PRODUCTION_LEXICAL

RETURNS = SourceRef("s1", "returns-policy.md", "returns-policy.md")
WARRANTY = SourceRef("s2", "warranty-policy.md", "warranty-policy.md")
SHIPPING = SourceRef("s3", "shipping-policy.md", "shipping-policy.md")
THESIS = SourceRef("s9", None, "thesis_report.pdf")  # a workspace file outside the dataset


# --- source ranking --------------------------------------------------------------------------


def test_chunks_collapse_to_sources_at_their_best_rank() -> None:
    ranked = rank_sources([WARRANTY, WARRANTY, THESIS, RETURNS, WARRANTY])
    assert [(r.rank, r.source.name) for r in ranked] == [
        (1, "warranty-policy.md"),
        (2, "thesis_report.pdf"),
        (3, "returns-policy.md"),
    ]
    assert ranked[0].chunk_ranks == (1, 2, 5)
    assert rank_sources([]) == []


def test_duplicate_chunks_cannot_inflate_recall() -> None:
    """Three chunks of one expected source still recall one of two expected sources."""
    score = retrieval_score(
        rank_sources([RETURNS, RETURNS, RETURNS]), ["returns-policy.md", "warranty-policy.md"]
    )
    assert score is not None
    assert score.recall == 0.5


# --- retrieval scores ------------------------------------------------------------------------


def test_hit_recall_and_reciprocal_rank() -> None:
    ranked = rank_sources([THESIS, SHIPPING, RETURNS, THESIS, WARRANTY])
    score = retrieval_score(ranked, ["returns-policy.md", "warranty-policy.md"])
    assert score is not None
    assert score.hit is True
    assert score.recall == 1.0
    assert score.reciprocal_rank == pytest.approx(1 / 3)  # returns is the 3rd distinct source


def test_a_miss_scores_zero() -> None:
    score = retrieval_score(rank_sources([THESIS, SHIPPING]), ["returns-policy.md"])
    assert score is not None
    assert (score.hit, score.recall, score.reciprocal_rank) == (False, 0.0, 0.0)


def test_unanswerable_questions_have_no_retrieval_score() -> None:
    assert retrieval_score(rank_sources([RETURNS]), []) is None


def test_sources_outside_the_dataset_are_never_relevant() -> None:
    score = retrieval_score(rank_sources([THESIS]), ["thesis_report.pdf"])
    assert score is not None and score.hit is False  # a key of None matches nothing


# --- citations -------------------------------------------------------------------------------


def test_citation_precision_and_recall() -> None:
    expected = ["returns-policy.md", "warranty-policy.md"]
    score = citation_score([RETURNS, SHIPPING, RETURNS], expected)
    assert score is not None
    assert score.precision == 0.5  # 2 distinct cited sources, 1 expected
    assert score.recall == 0.5  # 1 of 2 expected sources cited


def test_citation_scores_are_undefined_without_citations_or_expected_sources() -> None:
    assert citation_score([], ["returns-policy.md"]) is None  # an abstention cites nothing
    assert citation_score([RETURNS], []) is None  # unanswerable: nothing is relevant


# --- abstention ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outcome", "answerable", "correct"),
    [
        ("answered", True, True),
        ("abstained", True, False),
        ("answered", False, False),
        ("abstained", False, True),
        ("provider_error", True, None),
        ("provider_error", False, None),  # a failure is never a successful abstention
    ],
)
def test_abstention_correctness(outcome: str, answerable: bool, correct: bool | None) -> None:
    assert abstention_correct(outcome, answerable) is correct


# --- chance and latency ----------------------------------------------------------------------


def test_chance_hit_rate_is_hypergeometric() -> None:
    # 10 chunks, the expected source has 2, top 5: 1 - C(8,5)/C(10,5) = 1 - 56/252.
    assert chance_hit_rate(10, 2, 5) == pytest.approx(1 - 56 / 252)
    assert chance_hit_rate(10, 4, 5) == pytest.approx(1 - math.comb(6, 5) / math.comb(10, 5))
    assert chance_hit_rate(4, 1, 5) == 1.0  # the top 5 holds every chunk
    assert chance_hit_rate(10, 0, 5) is None
    assert chance_hit_rate(0, 0, 5) is None


def test_percentiles_use_the_nearest_rank() -> None:
    values = list(range(1, 21))  # 1..20
    assert nearest_rank(values, 95) == 19
    assert nearest_rank(values, 50) == 10
    assert nearest_rank([7], 95) == 7
    stats = latency_stats([30, None, 10, 20])
    assert stats == {"count": 3, "min": 10, "median": 20, "p95": 30, "max": 30}
    assert latency_stats([None, None]) is None


# --- variants --------------------------------------------------------------------------------


def test_variants_match_the_schema_and_only_production_modes_answer() -> None:
    assert tuple(v.key for v in VARIANTS) == EVALUATION_VARIANTS
    assert BY_KEY["vector"].mode == "vector"
    assert BY_KEY["hybrid"].lexical == PRODUCTION_LEXICAL
    assert not BY_KEY["hybrid"].experimental and not BY_KEY["vector"].experimental
    experiments = [v for v in VARIANTS if v.experimental]
    assert {(v.lexical.config, v.lexical.match) for v in experiments} == {
        ("simple", "any"),
        ("english", "all"),
        ("english", "any"),
    }
    assert all(v.mode == "hybrid" for v in experiments)
    assert set(ANSWER_MODES) == {"vector", "hybrid"}


# --- summaries -------------------------------------------------------------------------------

BASE = ResultRow(
    question_key="q",
    answerable=True,
    variant="hybrid",
    retrieval_error=False,
    hit_at_5=True,
    recall_at_5=1.0,
    reciprocal_rank=1.0,
    lexical_candidates=3,
    top_vector_distance=0.2,
    embedding_latency_ms=100,
    retrieval_latency_ms=10,
    answer_evaluated=False,
    outcome=None,
    abstain_reason=None,
    citation_precision=None,
    citation_recall=None,
    abstention_correct=None,
    answer_latency_ms=None,
    generation_latency_ms=None,
)


def test_retrieval_summary_uses_answerable_questions_only() -> None:
    rows = [
        replace(BASE, question_key="q1", hit_at_5=True, recall_at_5=1.0, reciprocal_rank=1.0),
        replace(BASE, question_key="q2", hit_at_5=True, recall_at_5=0.5, reciprocal_rank=0.5),
        replace(BASE, question_key="q3", hit_at_5=False, recall_at_5=0.0, reciprocal_rank=0.0),
        replace(
            BASE,
            question_key="u1",
            answerable=False,
            hit_at_5=None,
            recall_at_5=None,
            reciprocal_rank=None,
            lexical_candidates=0,
        ),
        replace(
            BASE,
            question_key="q4",
            retrieval_error=True,
            hit_at_5=None,
            recall_at_5=None,
            reciprocal_rank=None,
            lexical_candidates=None,
            retrieval_latency_ms=None,
        ),
    ]
    summary = summarize_retrieval(rows, chance={"q1": 0.8, "q2": 0.6, "q3": 1.0, "u1": None})
    assert summary["questions"] == 3  # q4 failed, u1 is unanswerable
    assert summary["errors"] == 1
    assert summary["hit_rate_at_5"] == pytest.approx(2 / 3)
    assert summary["recall_at_5"] == pytest.approx(0.5)
    assert summary["mrr"] == pytest.approx(0.5)
    assert summary["chance_hit_rate_at_5"] == pytest.approx(0.8)
    assert summary["no_lexical_candidates"] == {"answerable": 0, "unanswerable": 1}
    assert summary["unanswerable_questions"] == 1
    assert summary["latency_ms"]["count"] == 4


def test_retrieval_summary_without_measurements_is_not_zero() -> None:
    summary = summarize_retrieval([], chance={})
    assert summary["hit_rate_at_5"] is None
    assert summary["mrr"] is None
    assert summary["latency_ms"] is None
    assert summary["no_lexical_candidates"] == {"answerable": None, "unanswerable": None}


def answered(**changes: object) -> ResultRow:
    defaults: dict[str, object] = {
        "answer_evaluated": True,
        "outcome": "answered",
        "citation_precision": 1.0,
        "citation_recall": 1.0,
        "abstention_correct": True,
        "answer_latency_ms": 1000,
        "generation_latency_ms": 900,
    }
    return replace(BASE, **{**defaults, **changes})  # type: ignore[arg-type]


def test_answer_summary_confusion_matrix_and_denominators() -> None:
    rows = [
        answered(question_key="q1"),
        answered(question_key="q2", citation_precision=0.5, citation_recall=0.5),
        answered(
            question_key="q3",
            outcome="abstained",
            abstain_reason="model_insufficient",
            citation_precision=None,
            citation_recall=None,
            abstention_correct=False,
        ),
        answered(
            question_key="q4",
            outcome="provider_error",
            citation_precision=None,
            citation_recall=None,
            abstention_correct=None,
            answer_latency_ms=None,
            generation_latency_ms=None,
        ),
        answered(
            question_key="u1",
            answerable=False,
            outcome="abstained",
            abstain_reason="no_valid_citation",
            citation_precision=None,
            citation_recall=None,
            abstention_correct=True,
        ),
        answered(
            question_key="u2",
            answerable=False,
            citation_precision=None,
            citation_recall=None,
            abstention_correct=False,
        ),
        replace(BASE, question_key="q5"),  # not answered in this mode
    ]
    summary = summarize_answers(rows)
    assert summary["questions"] == 6
    assert summary["decided"] == 5  # the provider failure is not in the denominator
    assert summary["provider_errors"] == 1
    assert summary["confusion"] == {
        "answerable": {"answered": 2, "abstained": 1, "provider_error": 1},
        "unanswerable": {"answered": 1, "abstained": 1, "provider_error": 0},
    }
    assert summary["abstention_accuracy"] == pytest.approx(3 / 5)  # q1, q2, u1
    assert summary["citation_questions"] == 2
    assert summary["citation_precision"] == pytest.approx(0.75)
    assert summary["citation_recall"] == pytest.approx(0.75)
    assert summary["abstain_reasons"] == {"model_insufficient": 1, "no_valid_citation": 1}
    assert summary["latency_ms"]["count"] == 5  # the failed call has no answer latency


def test_answer_summary_for_a_mode_that_was_not_answered() -> None:
    summary = summarize_answers([replace(BASE, question_key="q1")])
    assert summary["questions"] == 0
    assert summary["abstention_accuracy"] is None
    assert summary["citation_precision"] is None
    assert summary["latency_ms"] is None


def test_run_summary_groups_by_configuration() -> None:
    rows = [
        replace(BASE, question_key="q1", variant="vector", top_vector_distance=0.1),
        replace(
            BASE,
            question_key="u1",
            variant="vector",
            answerable=False,
            hit_at_5=None,
            recall_at_5=None,
            reciprocal_rank=None,
            top_vector_distance=0.4,
        ),
        replace(BASE, question_key="q1", variant="hybrid", reciprocal_rank=0.5),
        answered(question_key="q1", variant="hybrid"),
    ]
    summary = summarize(
        rows, variants=["vector", "hybrid"], answer_modes=["hybrid"], chance={"q1": 0.5}
    )
    assert summary["questions_processed"] == 2
    assert set(summary["retrieval"]) == {"vector", "hybrid"}
    assert summary["retrieval"]["vector"]["mrr"] == 1.0
    assert summary["retrieval"]["hybrid"]["mrr"] == pytest.approx(0.75)
    assert set(summary["answers"]) == {"hybrid"}
    assert summary["nearest_vector_distance"]["answerable"]["median"] == 0.1
    assert summary["nearest_vector_distance"]["unanswerable"]["median"] == 0.4
    assert summary["embedding_latency_ms"]["count"] == 2


def test_failed_attempts_have_their_own_latency_and_never_mix_with_answers() -> None:
    rows = [
        answered(question_key="q1", answer_latency_ms=1000, generation_latency_ms=900),
        answered(
            question_key="q2",
            outcome="provider_error",
            citation_precision=None,
            citation_recall=None,
            abstention_correct=None,
            answer_latency_ms=None,
            generation_latency_ms=None,
            failed_attempt_ms=90_500,
            failed_generation_ms=90_000,
        ),
    ]
    summary = summarize_answers(rows)
    assert summary["latency_ms"] == {
        "count": 1,
        "min": 1000,
        "median": 1000,
        "p95": 1000,
        "max": 1000,
    }
    assert summary["generation_latency_ms"]["count"] == 1
    assert summary["failed_attempt_latency_ms"]["max"] == 90_500
    assert summary["failed_generation_latency_ms"]["count"] == 1


def test_failures_recorded_before_timing_existed_are_not_measured() -> None:
    """Runs from before failed-attempt timing (4 November 2026) have no such values."""
    old_failure = answered(
        outcome="provider_error",
        citation_precision=None,
        citation_recall=None,
        abstention_correct=None,
        answer_latency_ms=None,
        generation_latency_ms=None,
    )
    summary = summarize_answers([old_failure])
    assert summary["failed_attempt_latency_ms"] is None
    assert summary["failed_generation_latency_ms"] is None
    assert summary["latency_ms"] is None


def test_failed_embeddings_stay_out_of_embedding_latency() -> None:
    rows = [
        replace(BASE, question_key="q1", variant="vector", embedding_latency_ms=800),
        replace(
            BASE,
            question_key="q2",
            variant="vector",
            retrieval_error=True,
            embedding_latency_ms=5000,  # an older row that timed the failure here
            failed_embedding_ms=5000,
        ),
    ]
    summary = summarize(rows, variants=["vector"], answer_modes=[], chance={})
    assert summary["embedding_latency_ms"]["count"] == 1
    assert summary["embedding_latency_ms"]["max"] == 800
    assert summary["failed_embedding_latency_ms"]["max"] == 5000
