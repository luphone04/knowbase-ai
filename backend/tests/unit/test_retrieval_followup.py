"""Follow-up contextualization: short or referring questions borrow the previous one."""

import pytest

from app.retrieval.followup import MAX_QUERY_CHARS, contextualize, is_follow_up

PREVIOUS = "How long do I have to return a Trailblazer 14 laptop?"


@pytest.mark.parametrize(
    "question",
    [
        "And the warranty?",  # short
        "What about tents?",
        "Does it come with a warranty that covers water damage to the screen?",  # "it"
        "Can I still send those back after the fourteen day return window closes?",
        "And if the factory seal on the box has been opened already by someone?",
    ],
)
def test_follow_ups_are_combined_with_the_previous_question(question: str) -> None:
    assert is_follow_up(question)
    assert contextualize(question, PREVIOUS) == f"{PREVIOUS} {question}"


def test_a_long_standalone_question_is_used_as_is() -> None:
    question = "How many business days does standard shipping take within the country normally?"
    assert not is_follow_up(question)
    assert contextualize(question, PREVIOUS) == question


def test_without_a_previous_question_nothing_is_added() -> None:
    assert contextualize("And the warranty?", None) == "And the warranty?"
    assert contextualize("And the warranty?", "   ") == "And the warranty?"


def test_whitespace_is_normalised_and_history_is_bounded() -> None:
    assert contextualize("  what   about\ttents? ", " previous \n question ") == (
        "previous question what about tents?"
    )
    long_previous = "word " * 1000
    assert len(contextualize("And it?", long_previous)) <= MAX_QUERY_CHARS
    assert contextualize("And it?", long_previous).endswith("And it?")  # current kept


def test_words_inside_other_words_are_not_references() -> None:
    # "item", "theres", "italic" contain reference words but aren't references.
    question = "Which item categories qualify for free standard shipping on large orders?"
    assert not is_follow_up(question)
