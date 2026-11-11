"""Token usage and estimated cost of an evaluation run (app.evaluation.usage)."""

from decimal import Decimal
from typing import Any

import pytest

from app.evaluation.usage import UsageRow, usage_summary
from app.providers import pricing
from app.providers.pricing import PRICES, ModelPrice

CHAT = "gemini-3.5-flash-lite"
EMBED = "gemini-embedding-001"
VARIANTS = ("vector", "hybrid", "hybrid_or", "hybrid_english", "hybrid_english_or")
SNAPSHOT = pricing.snapshot([CHAT, EMBED])


def usd(
    tokens_in: int, tokens_out: int = 0, rate_in: str = "0.30", rate_out: str = "2.50"
) -> float:
    cost = (tokens_in * Decimal(rate_in) + tokens_out * Decimal(rate_out)) / 1_000_000
    return float(round(cost, 10))


def embedding(tokens: int | None = 8, *, requests: int = 1) -> dict[str, Any]:
    return {
        "query_embeddings": requests,
        "embedding_model": EMBED,
        "embedding_input_tokens": tokens,
    }


def generation(
    prompt: int | None = 1000,
    output: int | None = 50,
    thinking: int | None = 30,
    *,
    total: int | None = -1,
    cached: int | None = None,
    model: str = CHAT,
    requests: int = 1,
) -> dict[str, Any]:
    if total == -1:
        total = None if prompt is None else prompt + (output or 0) + (thinking or 0)
    return {
        "generation_requests": requests,
        "model": model,
        "prompt_tokens": prompt,
        "output_tokens": output,
        "thinking_tokens": thinking,
        "total_tokens": total,
        "cached_tokens": cached,
    }


def question_rows(
    key: str,
    *,
    embed: dict[str, Any] | None = None,
    answers: dict[str, dict[str, Any]] | None = None,
) -> list[UsageRow]:
    """One row per configuration, as the runner stores them."""
    answers = answers or {}
    rows = []
    for variant in VARIANTS:
        usage: dict[str, Any] = dict(embed or embedding()) if variant == "vector" else {}
        usage.update(answers.get(variant, {}))
        rows.append(UsageRow(key, variant, usage))
    return rows


def summarize(rows: list[UsageRow], snapshot: dict[str, Any] | None = SNAPSHOT) -> dict[str, Any]:
    return usage_summary(rows, pricing=snapshot, embedding_model=EMBED, chat_model=CHAT)


def test_a_shared_query_embedding_is_counted_once_per_question() -> None:
    rows = question_rows("q01", embed=embedding(8)) + question_rows("q02", embed=embedding(12))
    summary = summarize(rows)
    assert summary["embedding"]["requests"] == 2  # not 10: five configurations share each
    assert summary["embedding"]["input_tokens"] == 20
    assert summary["embedding"]["cost_usd"] == usd(20, rate_in="0.15")
    assert summary["by_question"]["q01"]["embedding"]["input_tokens"] == 8
    assert summary["by_question"]["q01"]["embedding"]["requests"] == 1


def test_each_answer_mode_is_a_separate_generation() -> None:
    rows = question_rows(
        "q01",
        answers={"vector": generation(1000, 50, 30), "hybrid": generation(1200, 60, 20)},
    )
    summary = summarize(rows)
    gen = summary["generation"]
    assert gen["requests"] == 2
    assert (gen["input_tokens"], gen["output_tokens"], gen["thinking_tokens"]) == (2200, 110, 50)
    assert gen["total_tokens"] == 1080 + 1280
    # Thinking tokens are billed as output, once.
    assert gen["cost_usd"] == usd(2200, 160)
    answers = summary["by_question"]["q01"]["answers"]
    assert answers["vector"]["cost_usd"] == usd(1000, 80)
    assert answers["hybrid"]["cost_usd"] == usd(1200, 80)
    assert answers["vector"]["model"] == CHAT


def test_totals_add_embeddings_and_generations() -> None:
    rows = question_rows("q01", embed=embedding(10), answers={"hybrid": generation(1000, 50, 30)})
    summary = summarize(rows)
    total = summary["total"]
    assert total["requests"] == 2
    assert total["input_tokens"] == 1010
    assert (total["output_tokens"], total["thinking_tokens"]) == (50, 30)
    assert total["total_tokens"] == 10 + 1080
    assert total["embedding_cost_usd"] == usd(10, rate_in="0.15")
    assert total["generation_cost_usd"] == usd(1000, 80)
    assert total["cost_usd"] == pytest.approx(usd(10, rate_in="0.15") + usd(1000, 80))
    assert total["usage_complete"] is True and summary["issues"] == []
    question = summary["by_question"]["q01"]["total"]
    assert question["cost_usd"] == total["cost_usd"] and question["input_tokens"] == 1010


def test_cached_prompt_tokens_use_the_caching_rate() -> None:
    rows = question_rows("q01", answers={"hybrid": generation(3000, 0, 0, cached=1000)})
    gen = summarize(rows)["generation"]
    assert gen["cached_input_tokens"] == 1000
    expected = (2000 * Decimal("0.30") + 1000 * Decimal("0.03")) / 1_000_000
    assert gen["cost_usd"] == float(round(expected, 10))


def test_a_retrieval_only_run_has_no_generation_cost() -> None:
    summary = summarize(question_rows("q01") + question_rows("q02"))
    assert summary["generation"]["requests"] == 0
    assert summary["generation"]["cost_usd"] == 0.0  # no call: a real zero
    assert summary["generation"]["input_tokens"] == 0
    assert summary["total"]["requests"] == 2
    assert summary["by_question"]["q01"]["answers"] == {}


def test_missing_usage_is_never_counted_as_zero() -> None:
    rows = question_rows("q01", embed=embedding(8)) + question_rows(
        "q02", embed=embedding(None), answers={"hybrid": generation(None, None, None)}
    )
    summary = summarize(rows)
    emb = summary["embedding"]
    assert (emb["requests"], emb["requests_without_usage"]) == (2, 1)
    assert emb["usage_complete"] is False
    assert emb["input_tokens"] == 8  # what was reported; flagged incomplete
    gen = summary["generation"]
    assert (gen["requests"], gen["requests_without_usage"]) == (1, 1)
    assert gen["input_tokens"] is None and gen["cost_usd"] is None  # nothing reported
    assert summary["total"]["usage_complete"] is False
    assert {"code": "usage_missing", "kind": "embedding", "requests": 1} in summary["issues"]
    assert {"code": "usage_missing", "kind": "generation", "requests": 1} in summary["issues"]
    q02 = summary["by_question"]["q02"]
    assert q02["embedding"]["input_tokens"] is None and q02["embedding"]["cost_usd"] is None
    assert q02["total"]["cost_usd"] is None


def test_counts_omitted_beside_reported_usage_are_zero() -> None:
    """Gemini omits zero counts (no thinking, say) when usage metadata is present."""
    rows = question_rows("q01", answers={"hybrid": generation(500, 20, None, total=520)})
    gen = summarize(rows)["generation"]
    assert gen["thinking_tokens"] == 0 and gen["usage_complete"] is True
    assert gen["cost_usd"] == usd(500, 20)


def test_an_unpriced_model_has_no_cost() -> None:
    rows = question_rows("q01", answers={"hybrid": generation(model="gemini-9-ultra")})
    summary = summarize(rows)
    assert summary["generation"]["input_tokens"] == 1000  # tokens still shown
    assert summary["generation"]["cost_usd"] is None
    assert summary["total"]["cost_usd"] is None
    assert summary["embedding"]["cost_usd"] is not None  # the priced part keeps its cost
    assert {"code": "model_unpriced", "kind": "generation", "model": "gemini-9-ultra"} in (
        summary["issues"]
    )
    (unpriced,) = [m for m in summary["models"] if m["model"] == "gemini-9-ultra"]
    assert unpriced["rates"] is None and unpriced["cost_usd"] is None


def test_a_run_without_a_pricing_snapshot_has_tokens_but_no_cost() -> None:
    rows = question_rows("q01", answers={"hybrid": generation()})
    summary = summarize(rows, snapshot=None)
    assert summary["pricing"] is None
    assert summary["generation"]["input_tokens"] == 1000
    assert summary["generation"]["cost_usd"] is None
    assert summary["total"]["cost_usd"] is None
    assert summary["issues"][0] == {"code": "pricing_not_recorded"}


def test_costs_come_from_the_runs_snapshot_not_the_current_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = pricing.snapshot([CHAT, EMBED])
    monkeypatch.setitem(PRICES, CHAT, ModelPrice("generation", Decimal("10"), Decimal("20")))
    rows = question_rows("q01", answers={"hybrid": generation(1000, 50, 30)})
    summary = summarize(rows, snapshot=recorded)
    assert summary["generation"]["cost_usd"] == usd(1000, 80)
    assert summary["pricing"]["models"][CHAT]["input_per_million"] == "0.30"
    # A run started after the change gets the new prices.
    newer = summarize(rows, snapshot=pricing.snapshot([CHAT, EMBED]))
    assert newer["generation"]["cost_usd"] == usd(1000, 80, rate_in="10", rate_out="20")


def test_refused_calls_count_nothing_and_unrefused_failures_are_incomplete() -> None:
    refused = question_rows(
        "q01",
        embed=embedding(None, requests=0),
        answers={"vector": {"generation_requests": 0}},
    )
    failed = question_rows(
        "q02",
        embed=embedding(9),
        answers={"vector": {"generation_requests": 1, "model": CHAT}},  # a 5xx: unknown usage
    )
    summary = summarize(refused + failed)
    assert summary["embedding"]["requests"] == 1 and summary["embedding"]["usage_complete"]
    assert summary["generation"]["requests"] == 1
    assert summary["generation"]["requests_without_usage"] == 1
    assert summary["by_question"]["q01"]["total"]["requests"] == 0
    assert summary["by_question"]["q01"]["total"]["cost_usd"] == 0.0


def test_an_incomplete_generation_with_reported_usage_is_priced() -> None:
    usage = {**generation(1000, 1190, 10), "generation_requests": 1}
    summary = summarize(question_rows("q01", answers={"hybrid": usage}))
    assert summary["generation"]["cost_usd"] == usd(1000, 1200)


def test_the_model_breakdown_lists_each_model_with_its_rates() -> None:
    rows = question_rows("q01", embed=embedding(8), answers={"hybrid": generation(1000, 50, 30)})
    models = {m["model"]: m for m in summarize(rows)["models"]}
    assert models[EMBED]["kind"] == "embedding" and models[EMBED]["input_tokens"] == 8
    assert models[EMBED]["rates"]["input_per_million"] == "0.15"
    assert models[CHAT]["kind"] == "generation" and models[CHAT]["output_tokens"] == 50
    assert models[CHAT]["rates"]["output_per_million"] == "2.50"


def test_rows_without_a_recorded_model_use_the_runs_models() -> None:
    """Results stored before models were recorded per row."""
    rows = [UsageRow("q01", "vector", {"query_embeddings": 1})]
    summary = usage_summary(rows, pricing=SNAPSHOT, embedding_model=EMBED, chat_model=CHAT)
    assert summary["models"][0]["model"] == EMBED
    assert summary["embedding"]["requests_without_usage"] == 1


# --- missing counts are unknown unless the response proves them zero ---------------------


def test_a_generation_without_a_prompt_count_is_unmeasured() -> None:
    """Other counts being present doesn't make a missing prompt count zero."""
    rows = question_rows("q01", answers={"hybrid": generation(None, 50, 30, total=1080)})
    gen = summarize(rows)["generation"]
    assert gen["requests_without_usage"] == 1 and gen["usage_complete"] is False
    assert gen["input_tokens"] is None and gen["cost_usd"] is None


def test_a_missing_output_or_thinking_count_is_zero_only_when_the_total_proves_it() -> None:
    proven = summarize(
        question_rows("q01", answers={"hybrid": generation(1000, None, 30, total=1030)})
    )
    assert proven["generation"]["output_tokens"] == 0
    assert proven["generation"]["usage_complete"] is True
    # The total doesn't add up: the missing count isn't zero, so it is unknown.
    unproven = summarize(
        question_rows("q01", answers={"hybrid": generation(1000, 50, None, total=1200)})
    )
    assert unproven["generation"]["requests_without_usage"] == 1
    assert unproven["generation"]["thinking_tokens"] is None
    # No total at all: nothing proves it.
    no_total = summarize(
        question_rows("q01", answers={"hybrid": generation(1000, 50, None, total=None)})
    )
    assert no_total["generation"]["requests_without_usage"] == 1


def test_reported_zeros_are_kept() -> None:
    rows = question_rows(
        "q01", embed=embedding(0), answers={"hybrid": generation(1000, 0, 0, total=None)}
    )
    summary = summarize(rows)
    assert summary["embedding"]["input_tokens"] == 0 and summary["embedding"]["usage_complete"]
    gen = summary["generation"]
    assert (gen["output_tokens"], gen["thinking_tokens"], gen["total_tokens"]) == (0, 0, 1000)
    assert gen["usage_complete"] is True and gen["cost_usd"] == usd(1000)


def test_a_missing_cached_count_changes_no_token_count() -> None:
    rows = question_rows("q01", answers={"hybrid": generation(1000, 50, 30, cached=None)})
    gen = summarize(rows)["generation"]
    assert gen["cached_input_tokens"] == 0 and gen["input_tokens"] == 1000
    assert gen["cost_usd"] == usd(1000, 80)  # the whole prompt at the full input rate


# --- retried attempts -------------------------------------------------------------------


def test_retried_attempts_of_successful_calls_are_disclosed_not_counted() -> None:
    rows = question_rows(
        "q01",
        embed={**embedding(8), "embedding_attempts": 3},
        answers={"hybrid": {**generation(1000, 50, 30), "generation_attempts": 2}},
    )
    summary = summarize(rows)
    emb, gen, total = summary["embedding"], summary["generation"], summary["total"]
    # Logical requests stay one each; the extra HTTP attempts are reported apart.
    assert (emb["requests"], emb["unmeasured_attempts"]) == (1, 2)
    assert (gen["requests"], gen["unmeasured_attempts"]) == (1, 1)
    assert total["requests"] == 2 and total["unmeasured_attempts"] == 3
    # Tokens are the successful responses' own, never multiplied by attempts.
    assert emb["input_tokens"] == 8 and gen["input_tokens"] == 1000
    assert emb["cost_usd"] == usd(8, rate_in="0.15") and gen["cost_usd"] == usd(1000, 80)
    # ...but the totals no longer claim to be complete.
    assert emb["usage_complete"] is False and gen["usage_complete"] is False
    assert total["usage_complete"] is False
    assert {"code": "unmeasured_attempts", "kind": "embedding", "attempts": 2} in summary["issues"]
    assert {"code": "unmeasured_attempts", "kind": "generation", "attempts": 1} in summary["issues"]
    q01 = summary["by_question"]["q01"]
    assert q01["embedding"]["unmeasured_attempts"] == 2
    assert q01["answers"]["hybrid"]["unmeasured_attempts"] == 1
    assert q01["total"]["usage_complete"] is False


def test_single_attempt_calls_stay_complete() -> None:
    rows = question_rows(
        "q01",
        embed={**embedding(8), "embedding_attempts": 1},
        answers={"hybrid": {**generation(), "generation_attempts": 1}},
    )
    summary = summarize(rows)
    assert summary["total"]["unmeasured_attempts"] == 0
    assert summary["total"]["usage_complete"] is True and summary["issues"] == []


def test_unrecorded_attempts_are_not_assumed() -> None:
    """Older rows recorded no embedding attempts: nothing is invented either way."""
    summary = summarize(question_rows("q01", embed=embedding(8)))
    assert summary["embedding"]["unmeasured_attempts"] == 0
