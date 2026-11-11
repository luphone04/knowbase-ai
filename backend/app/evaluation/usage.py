"""Gemini token usage and estimated cost of an evaluation run, from its stored results.

What is counted: the Gemini calls whose results the run saved.

- Query embeddings: one per question, embedded once and shared by every
  retrieval configuration. Its usage is stored on the `vector` row only, so
  it is counted once per question, never once per configuration.
- Generations: one per answer mode that called Gemini, on that mode's row.
  Vector and hybrid answers are separate calls and are counted separately.
- Not counted: retry reservations (budget held back, not calls), and calls an
  interrupted attempt made before its results were lost (unknowable; the run's
  retry reservations bound them).

Missing usage is never counted as zero. A request whose response carried no
token counts, or not enough of them, is reported in `requests_without_usage`;
its tokens and cost are left out, and the totals are marked incomplete
(`usage_complete: false`). When no request of a group reported usage, its
token counts are None. A generation is measured only with a reported prompt
count; a missing output or thinking count is zero only when the reported
total proves it (Gemini leaves zero counts out of its JSON).

Gemini reports no token counts for embeddings (`models.embed_content` returns
none on the Developer API), so query embeddings are counted as requests, all
without usage: their tokens and cost are None, and a run's total is a lower
bound covering the answers. Reported embedding counts would be used if a
provider ever supplied them.

Requests, attempts, and reservations are different things. `requests` are
logical calls (one per question's embedding, one per answer). A request that
succeeded after retries also made failed HTTP attempts: Gemini reports no
token counts for those, and whether they are billed isn't known, so they are
neither counted as tokens nor assumed free. They are disclosed as
`unmeasured_attempts`, which also marks the totals incomplete. Retry
reservations (budget) are separate again and are never usage.

Costs use the pricing snapshot stored when the run started (app.providers.pricing),
never the current table. A model the snapshot has no price for, or a run that
started before snapshots were recorded, has no cost (None), never a guess.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.providers import pricing as gemini_pricing
from app.providers.pricing import ModelPrice


@dataclass(frozen=True)
class UsageRow:
    question_key: str
    variant: str
    token_usage: Mapping[str, Any]


def _count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _usd(value: Decimal) -> float:
    return float(round(value, 10))


@dataclass
class Tally:
    """Requests and reported tokens of one group of calls, and their cost so far."""

    requests: int = 0
    without_usage: int = 0
    unpriced: int = 0  # requests whose model has no price in the snapshot
    # Failed HTTP attempts before a request succeeded: no token counts exist for them.
    unmeasured_attempts: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    total_tokens: int = 0
    cost: Decimal = field(default_factory=Decimal)

    def add(self, other: "Tally") -> None:
        self.requests += other.requests
        self.without_usage += other.without_usage
        self.unpriced += other.unpriced
        self.unmeasured_attempts += other.unmeasured_attempts
        self.input_tokens += other.input_tokens
        self.cached_input_tokens += other.cached_input_tokens
        self.output_tokens += other.output_tokens
        self.thinking_tokens += other.thinking_tokens
        self.total_tokens += other.total_tokens
        self.cost += other.cost

    def as_json(self, *, priced: bool) -> dict[str, Any]:
        """`priced`: the run has a pricing snapshot. Unknown values are None, never 0."""
        measured = self.requests == 0 or self.without_usage < self.requests

        def tokens(value: int) -> int | None:
            return value if measured else None

        cost_known = priced and measured and self.unpriced == 0
        return {
            "requests": self.requests,
            "requests_without_usage": self.without_usage,
            "unmeasured_attempts": self.unmeasured_attempts,
            "usage_complete": self.without_usage == 0 and self.unmeasured_attempts == 0,
            "input_tokens": tokens(self.input_tokens),
            "cached_input_tokens": tokens(self.cached_input_tokens),
            "output_tokens": tokens(self.output_tokens),
            "thinking_tokens": tokens(self.thinking_tokens),
            "total_tokens": tokens(self.total_tokens),
            "cost_usd": _usd(self.cost) if cost_known else None,
        }


@dataclass
class Pricer:
    snapshot: Mapping[str, Any] | None

    def price(self, model: str) -> ModelPrice | None:
        return gemini_pricing.price_in(self.snapshot, model) if self.snapshot else None


def _unmeasured_attempts(attempts: object, requests: int) -> int:
    """HTTP attempts beyond one per request; 0 when attempts weren't recorded."""
    recorded = _count(attempts)
    return max(recorded - requests, 0) if recorded is not None else 0


def _generation_counts(usage: Mapping[str, Any]) -> tuple[int, int, int, int] | None:
    """(prompt, output, thinking, total) if every count is known, else None.

    The prompt count must be reported. Gemini's JSON leaves zero counts out, so a
    missing output or thinking count is taken as zero only when the reported
    total equals the prompt plus the reported parts; otherwise it is unknown.
    """
    prompt = _count(usage.get("prompt_tokens"))
    if prompt is None:
        return None
    output = _count(usage.get("output_tokens"))
    thinking = _count(usage.get("thinking_tokens"))
    total = _count(usage.get("total_tokens"))
    if output is None or thinking is None:
        if total is None or total != prompt + (output or 0) + (thinking or 0):
            return None
        output, thinking = output or 0, thinking or 0
    return prompt, output, thinking, total if total is not None else prompt + output + thinking


def embedding_tally(usage: Mapping[str, Any], model: str, pricer: Pricer) -> Tally:
    """The query embedding recorded on a row (the `vector` row), if any."""
    tally = Tally(requests=_count(usage.get("query_embeddings")) or 0)
    if tally.requests == 0:
        return tally
    tally.unmeasured_attempts = _unmeasured_attempts(
        usage.get("embedding_attempts"), tally.requests
    )
    tokens = _count(usage.get("embedding_input_tokens"))
    price = pricer.price(model)
    if price is None or price.kind != "embedding":
        tally.unpriced = tally.requests
    if tokens is None:
        tally.without_usage = tally.requests
        return tally
    tally.input_tokens = tally.total_tokens = tokens
    if price is not None:
        tally.cost = gemini_pricing.embedding_cost(price, input_tokens=tokens) or Decimal(0)
    return tally


def generation_tally(usage: Mapping[str, Any], model: str, pricer: Pricer) -> Tally:
    """The answer generation recorded on a row, if any."""
    tally = Tally(requests=_count(usage.get("generation_requests")) or 0)
    if tally.requests == 0:
        return tally
    tally.unmeasured_attempts = _unmeasured_attempts(
        usage.get("generation_attempts"), tally.requests
    )
    price = pricer.price(model)
    if price is None or price.kind != "generation":
        tally.unpriced = tally.requests
    counts = _generation_counts(usage)
    if counts is None:  # no usage metadata, or not enough of it to trust
        tally.without_usage = tally.requests
        return tally
    tally.input_tokens, tally.output_tokens, tally.thinking_tokens, tally.total_tokens = counts
    # cachedContentTokenCount is absent when nothing came from a cache. Reading it as 0
    # changes no token count; it only prices the whole prompt at the full input rate.
    tally.cached_input_tokens = min(_count(usage.get("cached_tokens")) or 0, tally.input_tokens)
    if price is not None:
        cost = gemini_pricing.generation_cost(
            price,
            prompt_tokens=tally.input_tokens,
            output_tokens=tally.output_tokens,
            thinking_tokens=tally.thinking_tokens,
            cached_tokens=tally.cached_input_tokens,
        )
        if cost is None:
            tally.unpriced = tally.requests
        else:
            tally.cost = cost
    return tally


def _rates(pricer: Pricer, model: str) -> dict[str, Any] | None:
    price = pricer.price(model)
    return price.as_json() if price is not None else None


def usage_summary(
    rows: Iterable[UsageRow],
    *,
    pricing: Mapping[str, Any] | None,
    embedding_model: str | None,
    chat_model: str | None,
) -> dict[str, Any]:
    """Tokens and estimated cost: per operation, per model, per question, and in total.

    `embedding_model` and `chat_model` (the run's config) name the model for rows
    that didn't record one.
    """
    pricer = Pricer(pricing or None)
    priced = pricer.snapshot is not None
    embedding, generation = Tally(), Tally()
    by_model: dict[tuple[str, str], Tally] = {}
    questions: dict[str, dict[str, Any]] = {}

    for row in rows:
        usage = row.token_usage or {}
        question = questions.setdefault(
            row.question_key,
            {"embedding": Tally(), "generation": Tally(), "answers": {}, "models": set()},
        )
        if "query_embeddings" in usage:
            model = str(usage.get("embedding_model") or embedding_model or "unknown")
            tally = embedding_tally(usage, model, pricer)
            if tally.requests:
                embedding.add(tally)
                question["embedding"].add(tally)
                by_model.setdefault(("embedding", model), Tally()).add(tally)
                question["embedding_model"] = model
        if "generation_requests" in usage:
            model = str(usage.get("model") or chat_model or "unknown")
            tally = generation_tally(usage, model, pricer)
            generation.add(tally)
            question["generation"].add(tally)
            question["answers"][row.variant] = {
                **tally.as_json(priced=priced),
                "model": model if tally.requests else None,
            }
            if tally.requests:
                by_model.setdefault(("generation", model), Tally()).add(tally)
                question["models"].add(model)

    total = Tally()
    total.add(embedding)
    total.add(generation)
    issues: list[dict[str, Any]] = []
    if not priced and total.requests:
        issues.append({"code": "pricing_not_recorded"})
    for (kind, model), tally in sorted(by_model.items()):
        if priced and tally.unpriced:
            issues.append({"code": "model_unpriced", "kind": kind, "model": model})
    for kind, tally in (("embedding", embedding), ("generation", generation)):
        if tally.without_usage:
            issues.append({"code": "usage_missing", "kind": kind, "requests": tally.without_usage})
        if tally.unmeasured_attempts:
            issues.append(
                {"code": "unmeasured_attempts", "kind": kind, "attempts": tally.unmeasured_attempts}
            )

    total_json = total.as_json(priced=priced)
    embedding_json = embedding.as_json(priced=priced)
    generation_json = generation.as_json(priced=priced)
    total_json["embedding_cost_usd"] = embedding_json["cost_usd"]
    total_json["generation_cost_usd"] = generation_json["cost_usd"]
    return {
        "pricing": dict(pricing) if pricing else None,
        "embedding": embedding_json,
        "generation": generation_json,
        "total": total_json,
        "models": [
            {
                "model": model,
                "kind": kind,
                **tally.as_json(priced=priced),
                "rates": _rates(pricer, model),
            }
            for (kind, model), tally in sorted(by_model.items())
        ],
        "by_question": {
            key: _question_json(question, priced=priced) for key, question in questions.items()
        },
        "issues": issues,
    }


def _question_json(question: dict[str, Any], *, priced: bool) -> dict[str, Any]:
    total = Tally()
    total.add(question["embedding"])
    total.add(question["generation"])
    return {
        "embedding": {
            **question["embedding"].as_json(priced=priced),
            "model": question.get("embedding_model"),
        },
        "generation": {
            **question["generation"].as_json(priced=priced),
            "models": sorted(question["models"]),
        },
        "answers": question["answers"],
        "total": total.as_json(priced=priced),
    }
