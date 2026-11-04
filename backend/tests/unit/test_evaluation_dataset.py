"""The evaluation dataset: the shipped file is valid and supported by the demo corpus."""

import json
from pathlib import Path
from typing import Any

import pytest

from app.evaluation.dataset import (
    DEFAULT_DATASET,
    MIN_ANSWERABLE,
    MIN_QUESTIONS,
    MIN_UNANSWERABLE,
    DatasetError,
    corpus_problems,
    load_dataset,
    parse_dataset,
)

DEMO_STORE = Path(__file__).resolve().parents[3] / "sample_data" / "demo_store"
needs_sample_data = pytest.mark.skipif(
    not DEMO_STORE.is_dir(),
    reason="sample_data/ is outside the backend image (runs on the host and in CI)",
)


def raw_dataset() -> dict[str, Any]:
    raw: dict[str, Any] = json.loads(DEFAULT_DATASET.read_bytes())
    return raw


def parse(raw: dict[str, Any]) -> None:
    parse_dataset(json.dumps(raw).encode())


def test_the_shipped_dataset_meets_the_minimums() -> None:
    dataset = load_dataset()
    assert len(dataset.questions) >= MIN_QUESTIONS
    assert dataset.answerable >= MIN_ANSWERABLE
    assert dataset.unanswerable >= MIN_UNANSWERABLE
    assert sorted(dataset.source_hashes) == [
        "product-guide.md",
        "returns-policy.md",
        "shipping-policy.md",
        "support-faq.md",
        "warranty-policy.md",
    ]
    categories = {q.category for q in dataset.questions}
    # Direct, paraphrased, exact-term, follow-up, and multi-document questions (brief §3).
    assert {"direct", "paraphrase", "exact_term", "follow_up", "multi_source"} <= categories
    assert any(len(q.expected_source_keys) > 1 for q in dataset.questions)
    # Every source is expected by at least one question.
    expected = {key for q in dataset.questions for key in q.expected_source_keys}
    assert expected == set(dataset.source_hashes)


def test_positions_follow_the_file_and_keys_are_unique() -> None:
    dataset = load_dataset()
    assert [q.position for q in dataset.questions] == list(range(1, len(dataset.questions) + 1))
    assert len({q.key for q in dataset.questions}) == len(dataset.questions)


@needs_sample_data
def test_the_dataset_is_supported_by_the_demo_documents() -> None:
    """Hashes match, every evidence quote is in its file, no absent term is in the corpus."""
    dataset = load_dataset()
    files = {path.name: path.read_bytes() for path in sorted(DEMO_STORE.glob("*.md"))}
    assert set(files) == set(dataset.source_hashes)
    assert corpus_problems(dataset, files) == []


def test_corpus_problems_reports_each_kind_of_mismatch() -> None:
    dataset = load_dataset()
    unrelated = b"unrelated text that mentions a student discount"
    files = {key: unrelated for key in dataset.source_hashes}
    del files["support-faq.md"]
    problems = corpus_problems(dataset, files)
    assert "support-faq.md: missing from the corpus" in problems
    assert "returns-policy.md: content differs from the recorded SHA-256" in problems
    assert any(p.startswith("q01: evidence not found in returns-policy.md") for p in problems)
    assert "u03: absent term 'student' appears in the corpus" in problems


def test_a_documented_no_is_answerable() -> None:
    """International shipping and opened fuel canisters are documented refusals, not gaps."""
    by_key = {q.key: q for q in load_dataset().questions}
    assert by_key["q10"].answerable and by_key["q10"].expected_source_keys == (
        "shipping-policy.md",
    )
    assert by_key["q11"].answerable


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda q: q[0].update(expected_source_keys=[]), "needs expected sources"),
        (lambda q: q[0].update(expected_source_keys=["missing.md"]), "unknown source key"),
        (lambda q: q[0].update(previous_question="Earlier?"), "only follow_up"),
        (lambda q: q[15].pop("previous_question"), "only follow_up"),
        (lambda q: q[-1].update(expected_source_keys=["returns-policy.md"]), "has no sources"),
        (lambda q: q[-1].update(absent_terms=[]), "lists its absent terms"),
        (lambda q: q[1].update(key="q01"), "duplicate key"),
        (lambda q: q[0].update(evidence=[]), "needs an answer and evidence"),
        (
            lambda q: q[0]["evidence"].append(
                {"source_key": "support-faq.md", "quote": "Gift cards never expire."}
            ),
            "evidence from a source that isn't expected",
        ),
    ],
)
def test_rule_violations_are_refused(change: Any, message: str) -> None:
    raw = raw_dataset()
    change(raw["questions"])
    with pytest.raises(DatasetError, match=message):
        parse(raw)


def test_minimum_counts_are_enforced() -> None:
    raw = raw_dataset()
    raw["questions"] = [q for q in raw["questions"] if not q["key"].startswith("u")]
    with pytest.raises(DatasetError, match="unanswerable questions are needed"):
        parse(raw)


def test_unknown_fields_and_categories_are_refused() -> None:
    raw = raw_dataset()
    raw["questions"][0]["relevant_chunk_ids"] = ["abc"]
    with pytest.raises(DatasetError, match="not valid"):
        parse(raw)
    raw = raw_dataset()
    raw["questions"][0]["category"] = "trivia"
    with pytest.raises(DatasetError, match="not valid"):
        parse(raw)


def test_any_edit_changes_the_dataset_hash() -> None:
    raw = raw_dataset()
    original = parse_dataset(json.dumps(raw).encode()).sha256
    raw["questions"][0]["expected_answer"] += " (edited)"
    assert parse_dataset(json.dumps(raw).encode()).sha256 != original
