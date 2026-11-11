"""The Gemini pricing table and cost arithmetic (app.providers.pricing)."""

from decimal import Decimal

import pytest

from app.providers import pricing
from app.providers.pricing import (
    PRICES,
    ModelPrice,
    embedding_cost,
    generation_cost,
    price_in,
    snapshot,
)

FLASH_LITE = PRICES["gemini-3.5-flash-lite"]
EMBEDDING = PRICES["gemini-embedding-001"]


def test_the_configured_models_have_the_verified_paid_standard_rates() -> None:
    assert ModelPrice("generation", Decimal("0.30"), Decimal("2.50"), Decimal("0.03")) == FLASH_LITE
    assert ModelPrice("embedding", Decimal("0.15")) == EMBEDDING
    assert pricing.TIER == "paid_standard"
    assert pricing.SOURCE_URL == "https://aistudio.google.com/docs/pricing"
    assert pricing.VERIFIED_ON == "2026-11-11"


def test_generation_cost_prices_input_and_output_per_million_tokens() -> None:
    cost = generation_cost(
        FLASH_LITE, prompt_tokens=1_000_000, output_tokens=1_000_000, thinking_tokens=0
    )
    assert cost == Decimal("0.30") + Decimal("2.50")
    small = generation_cost(FLASH_LITE, prompt_tokens=2000, output_tokens=100, thinking_tokens=0)
    assert small == (2000 * Decimal("0.30") + 100 * Decimal("2.50")) / 1_000_000


def test_thinking_tokens_are_billed_once_at_the_output_rate() -> None:
    """'Output price (including thinking tokens)': output + thoughts, never the total again."""
    with_thinking = generation_cost(
        FLASH_LITE, prompt_tokens=1000, output_tokens=100, thinking_tokens=400
    )
    assert with_thinking == (1000 * Decimal("0.30") + 500 * Decimal("2.50")) / 1_000_000
    # Pricing the reported total (1000 + 100 + 400) too would double count; it's never used.
    assert with_thinking == generation_cost(
        FLASH_LITE, prompt_tokens=1000, output_tokens=500, thinking_tokens=0
    )


def test_cached_prompt_tokens_are_billed_at_the_caching_rate() -> None:
    cost = generation_cost(
        FLASH_LITE, prompt_tokens=3000, output_tokens=0, thinking_tokens=0, cached_tokens=1000
    )
    assert cost == (2000 * Decimal("0.30") + 1000 * Decimal("0.03")) / 1_000_000
    # Cached tokens are part of the prompt count: never more than it.
    capped = generation_cost(
        FLASH_LITE, prompt_tokens=100, output_tokens=0, thinking_tokens=0, cached_tokens=500
    )
    assert capped == 100 * Decimal("0.03") / 1_000_000


def test_cached_tokens_without_a_caching_rate_have_no_price() -> None:
    no_cache_rate = ModelPrice("generation", Decimal("1"), Decimal("2"))
    assert (
        generation_cost(
            no_cache_rate, prompt_tokens=10, output_tokens=1, thinking_tokens=0, cached_tokens=5
        )
        is None
    )


def test_embedding_cost_prices_input_tokens() -> None:
    assert embedding_cost(EMBEDDING, input_tokens=1_000_000) == Decimal("0.15")
    assert embedding_cost(EMBEDDING, input_tokens=12) == 12 * Decimal("0.15") / 1_000_000


def test_a_price_of_the_wrong_kind_prices_nothing() -> None:
    assert embedding_cost(FLASH_LITE, input_tokens=10) is None
    assert generation_cost(EMBEDDING, prompt_tokens=10, output_tokens=1, thinking_tokens=0) is None


def test_a_snapshot_records_rates_exactly_and_lists_unpriced_models() -> None:
    snap = snapshot(["gemini-3.5-flash-lite", "gemini-embedding-001", "some-new-model"])
    assert snap["tier"] == "paid_standard" and snap["currency"] == "USD"
    assert snap["source_url"] == pricing.SOURCE_URL and snap["verified_on"] == pricing.VERIFIED_ON
    assert snap["models"]["gemini-3.5-flash-lite"] == {
        "kind": "generation",
        "input_per_million": "0.30",
        "output_per_million": "2.50",
        "cached_input_per_million": "0.03",
    }
    assert snap["models"]["gemini-embedding-001"]["input_per_million"] == "0.15"
    assert snap["unpriced_models"] == ["some-new-model"]
    assert price_in(snap, "gemini-3.5-flash-lite") == FLASH_LITE
    assert price_in(snap, "gemini-embedding-001") == EMBEDDING


def test_an_unknown_model_has_no_price() -> None:
    snap = snapshot(["gemini-3.5-flash-lite"])
    assert price_in(snap, "gemini-9-ultra") is None
    assert price_in({}, "gemini-3.5-flash-lite") is None


@pytest.mark.parametrize(
    "entry",
    [
        {"kind": "generation", "input_per_million": "0.30"},  # no output rate
        {"kind": "generation", "input_per_million": "abc", "output_per_million": "2.50"},
        {"kind": "generation", "input_per_million": "-1", "output_per_million": "2.50"},
        {"kind": "generation", "input_per_million": 0.3, "output_per_million": "2.50"},
        {"kind": "video", "input_per_million": "0.30"},
        "not a mapping",
    ],
)
def test_an_unreadable_snapshot_entry_has_no_price(entry: object) -> None:
    assert price_in({"models": {"m": entry}}, "m") is None


def test_a_snapshot_keeps_its_rates_when_the_table_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    snap = snapshot(["gemini-3.5-flash-lite"])
    monkeypatch.setitem(
        PRICES,
        "gemini-3.5-flash-lite",
        ModelPrice("generation", Decimal("9"), Decimal("99"), Decimal("1")),
    )
    assert price_in(snap, "gemini-3.5-flash-lite") == FLASH_LITE
    assert (
        snapshot(["gemini-3.5-flash-lite"])["models"]["gemini-3.5-flash-lite"]["input_per_million"]
        == "9"
    )
