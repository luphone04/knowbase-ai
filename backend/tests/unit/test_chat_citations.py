import pytest

from app.chat.citations import LINK_REMOVED, check_answer, is_abstention
from app.chat.prompt import ABSTENTION

VALID = {"S1", "S2", "S3"}


def check(text: str, excerpt_text: str = "") -> tuple[str, list[str], list[str], int]:
    result = check_answer(text, valid_ids=VALID, excerpt_text=excerpt_text)
    return result.text, result.cited_ids, result.invalid_ids, result.removed_urls


def test_valid_citations_are_kept_in_order_of_first_use() -> None:
    text = "Laptops: 14 days [S2]. Others: 30 days [S1]. Again [S2]."
    assert check(text) == (text, ["S2", "S1"], [], 0)


def test_grouped_and_lower_case_citations_are_normalised() -> None:
    assert check("Both apply [S1, s3] and [S2;S1].")[:2] == (
        "Both apply [S1, S3] and [S2, S1].",
        ["S1", "S3", "S2"],
    )


def test_fabricated_ids_are_removed_from_a_group() -> None:
    assert check("Tents: 2 years [S1, S9].") == ("Tents: 2 years [S1].", ["S1"], ["S9"], 0)


def test_a_group_of_only_fabricated_ids_is_removed_with_its_space() -> None:
    assert check("Tents: 2 years [S7]. Laptops: 14 days [S1].") == (
        "Tents: 2 years. Laptops: 14 days [S1].",
        ["S1"],
        ["S7"],
        0,
    )


def test_an_answer_with_only_fabricated_citations_cites_nothing() -> None:
    text, cited, invalid, _ = check("Express shipping costs $25 [S4][S0].")
    assert (text, cited, invalid) == ("Express shipping costs $25.", [], ["S4", "S0"])


def test_an_answer_without_citations_cites_nothing() -> None:
    assert check("Express shipping costs $25.")[1:3] == ([], [])


def test_leading_zeros_name_the_same_id() -> None:
    assert check("Fine [S01].")[:2] == ("Fine [S1].", ["S1"])


def test_brackets_that_are_not_citations_are_left_alone() -> None:
    text = "See [note] and [Step 2] and [SKU S1-B]."
    assert check(text) == (text, [], [], 0)


def test_urls_not_in_the_excerpts_are_removed() -> None:
    excerpts = "Track parcels at https://track.example.com/northstar ."
    text, _, _, removed = check(
        "Track it at https://track.example.com/northstar. Or visit http://evil.example/x?a=1. [S1]",
        excerpts,
    )
    assert text == (
        f"Track it at https://track.example.com/northstar. Or visit {LINK_REMOVED}. [S1]"
    )
    assert removed == 1


@pytest.mark.parametrize(
    "text",
    [
        ABSTENTION,
        "I couldn’t find enough information in this knowledge base to answer that.",
        "Sorry, I could not find enough information in this knowledge base about drones.",
        "  i COULDN'T find enough   information in this knowledge base to answer that ",
    ],
)
def test_abstentions_are_recognised(text: str) -> None:
    assert is_abstention(text)


def test_an_answer_is_not_an_abstention() -> None:
    assert not is_abstention("Laptops can be returned within 14 days [S1].")
