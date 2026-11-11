"""Chat reply and conversation usage: reported tokens only, priced from each reply's snapshot."""

from decimal import Decimal
from typing import Any

import pytest

from app.chat.usage import conversation_usage, has_usage_record, message_usage
from app.providers import pricing

CHAT = "gemini-3.5-flash-lite"
EMBEDDING = "gemini-embedding-001"
SNAPSHOT = pricing.snapshot([CHAT, EMBEDDING])
MODELS = {"embedding_model": EMBEDDING, "chat_model": CHAT}


def usd(value: Decimal) -> float:
    return float(round(value, 10))


def reply(**overrides: Any) -> dict[str, Any]:
    """A reply's stored token_usage, as the chat service saves it now."""
    usage: dict[str, Any] = {
        "query_embeddings": 1,
        "embedding_model": EMBEDDING,
        "embedding_input_tokens": None,  # Gemini reports none for embeddings
        "embedding_attempts": 1,
        "generation_requests": 1,
        "model": CHAT,
        "prompt_tokens": 1000,
        "output_tokens": 50,
        "thinking_tokens": 30,
        "total_tokens": 1080,
        "cached_tokens": 400,
        "generation_attempts": 1,
        "pricing": SNAPSHOT,
    }
    usage.update(overrides)
    return usage


# 600 input at $0.30, 400 cached at $0.03, (50 output + 30 thinking) at $2.50, per 1M.
REPLY_COST = (600 * Decimal("0.30") + 400 * Decimal("0.03") + 80 * Decimal("2.50")) / 1_000_000


def test_a_reply_prices_cached_input_and_thinking_tokens() -> None:
    usage = message_usage(reply(), **MODELS)
    assert usage is not None
    generation = usage["generation"]
    assert generation["model"] == CHAT and generation["requests"] == 1
    assert (generation["input_tokens"], generation["cached_input_tokens"]) == (1000, 400)
    assert (generation["output_tokens"], generation["thinking_tokens"]) == (50, 30)
    assert generation["total_tokens"] == 1080
    assert generation["cost_usd"] == usd(REPLY_COST)
    assert generation["cost_complete"] is True
    assert usage["pricing"] == {
        "tier_label": pricing.TIER_LABEL,
        "verified_on": pricing.VERIFIED_ON,
        "source_url": pricing.SOURCE_URL,
    }


def test_unreported_embedding_tokens_stay_unknown_and_make_the_total_a_lower_bound() -> None:
    usage = message_usage(reply(), **MODELS)
    assert usage is not None
    embedding = usage["embedding"]
    assert embedding["requests"] == 1 and embedding["requests_without_usage"] == 1
    assert embedding["input_tokens"] is None and embedding["cost_usd"] is None  # never 0
    total = usage["total"]
    assert total["requests"] == 2
    assert total["input_tokens"] == 1000  # the answer's; the embedding's are unknown
    assert total["cost_usd"] == usd(REPLY_COST)
    assert total["cost_complete"] is False and total["usage_complete"] is False
    assert usage["issues"] == [{"code": "usage_missing", "kind": "embedding", "requests": 1}]


def test_reported_embedding_tokens_are_priced() -> None:
    usage = message_usage(reply(embedding_input_tokens=12), **MODELS)
    assert usage is not None
    assert usage["embedding"]["cost_usd"] == usd(12 * Decimal("0.15") / 1_000_000)
    assert usage["total"]["cost_complete"] is True and usage["issues"] == []


def test_each_reply_keeps_the_prices_it_was_saved_with() -> None:
    old = pricing.snapshot([CHAT, EMBEDDING])
    old["models"][CHAT] = {**old["models"][CHAT], "input_per_million": "1.00"}
    usage = message_usage(reply(pricing=old, cached_tokens=0), **MODELS)
    assert usage is not None
    expected = (1000 * Decimal("1.00") + 80 * Decimal("2.50")) / 1_000_000
    assert usage["generation"]["cost_usd"] == usd(expected)


def test_retried_attempts_are_disclosed_not_priced() -> None:
    usage = message_usage(reply(generation_attempts=3, embedding_attempts=2), **MODELS)
    assert usage is not None
    assert usage["generation"]["requests"] == 1
    assert usage["generation"]["unmeasured_attempts"] == 2
    assert usage["generation"]["cost_usd"] == usd(REPLY_COST)  # the successful attempt's
    assert usage["generation"]["cost_complete"] is False
    assert usage["embedding"]["unmeasured_attempts"] == 1
    codes = [(i["code"], i["kind"]) for i in usage["issues"]]
    assert ("unmeasured_attempts", "generation") in codes
    assert ("unmeasured_attempts", "embedding") in codes


def test_a_reply_without_a_generation_call_costs_nothing_for_it() -> None:
    usage = message_usage({"query_embeddings": 0, "generation_requests": 0}, **MODELS)
    assert usage is not None
    assert usage["total"]["requests"] == 0
    assert usage["total"]["cost_usd"] == 0.0 and usage["total"]["cost_complete"] is True
    assert usage["generation"]["model"] is None and usage["pricing"] is None


def test_missing_generation_counts_are_not_zero() -> None:
    usage = message_usage(reply(thinking_tokens=None, total_tokens=None), **MODELS)
    assert usage is not None
    generation = usage["generation"]
    assert generation["requests_without_usage"] == 1
    assert generation["input_tokens"] is None and generation["cost_usd"] is None
    assert usage["total"]["cost_usd"] is None  # nothing in this reply has a known cost


def test_an_old_reply_without_prices_keeps_its_tokens_but_has_no_cost() -> None:
    old = reply()
    del old["pricing"]
    usage = message_usage(old, **MODELS)
    assert usage is not None
    assert usage["generation"]["input_tokens"] == 1000
    assert usage["generation"]["cost_usd"] is None
    assert usage["pricing"] is None
    assert {"code": "cost_unavailable", "requests": 1} in usage["issues"]


@pytest.mark.parametrize("stored", [{}, None, {"something": 1}])
def test_a_reply_without_a_usage_record_is_not_available(stored: dict[str, Any] | None) -> None:
    assert message_usage(stored, **MODELS) is None
    assert has_usage_record(stored) is False


def test_conversation_totals_add_replies_and_failed_questions() -> None:
    no_evidence = {"query_embeddings": 0, "generation_requests": 0}
    legacy: dict[str, Any] = {}
    failed = {
        "query_embeddings": 1,
        "embedding_model": EMBEDDING,
        "embedding_input_tokens": None,
        "generation_requests": 1,
        "generation_error": "unavailable",
        "pricing": SNAPSHOT,
    }
    totals = conversation_usage([reply(), reply(), no_evidence, legacy], [failed], **MODELS)
    assert totals["replies"] == 4 and totals["replies_with_usage"] == 3
    assert totals["failed_questions"] == 1
    assert totals["embedding"]["requests"] == 3  # two replies + the failed question
    assert totals["embedding"]["requests_without_usage"] == 3
    assert totals["generation"]["requests"] == 3
    assert totals["generation"]["requests_without_usage"] == 1  # the failed call
    assert totals["generation"]["input_tokens"] == 2000
    assert totals["generation"]["cached_input_tokens"] == 800
    assert totals["generation"]["thinking_tokens"] == 60
    assert totals["total"]["requests"] == 6
    assert totals["total"]["cost_usd"] == usd(2 * REPLY_COST)
    assert totals["total"]["cost_complete"] is False
    issues = {(i["code"], i.get("kind")): i for i in totals["issues"]}
    assert issues[("usage_missing", "embedding")]["requests"] == 3
    assert issues[("usage_missing", "generation")]["requests"] == 1
    assert totals["pricing"]["verified_on"] == pricing.VERIFIED_ON


def test_a_cut_off_answer_counts_its_reported_tokens() -> None:
    cut_off = {**reply(), "generation_error": "incomplete_generation"}
    totals = conversation_usage([], [cut_off], **MODELS)
    assert totals["replies"] == 0 and totals["failed_questions"] == 1
    assert totals["generation"]["output_tokens"] == 50
    assert totals["generation"]["cost_usd"] == usd(REPLY_COST)


def test_an_empty_conversation_has_nothing_to_count() -> None:
    totals = conversation_usage([], [], **MODELS)
    assert totals["replies"] == 0 and totals["total"]["requests"] == 0
    assert totals["total"]["cost_usd"] == 0.0 and totals["issues"] == []
