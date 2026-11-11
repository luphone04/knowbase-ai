"""Gemini Developer API prices, kept in one place, for cost *estimates*.

An estimate is what the recorded token counts would cost at Google's published
**Paid tier, Standard** rates. It is not what Google billed: this app is meant
for the free tier (no charge), and token metadata says nothing about invoices,
credits, or account-level pricing. Vertex AI prices are a different product
and are not used.

Source: Google's official Gemini Developer API pricing page,
https://aistudio.google.com/docs/pricing (https://ai.google.dev/gemini-api/docs/pricing
redirects there). The page carries no "last updated" date; VERIFIED_ON is the
day the rates below were checked against it.

Billing rules applied (from the same page):

- Generation input: prompt tokens at the input rate, except the part served
  from a context cache (Gemini's implicit caching), which is billed at the
  context caching rate. `promptTokenCount` already includes cached tokens.
- Generation output: response tokens plus thinking tokens at the output rate.
  The page labels the row "Output price (including thinking tokens)". Gemini
  reports thoughts separately (`thoughtsTokenCount`), so they are added once.
  `totalTokenCount` already sums the others and is never priced.
- Embeddings: prompt tokens at the embedding input rate. There is no output.

A model not in PRICES has no price: its cost is unavailable, never guessed.

Each evaluation run stores `snapshot()` when it starts, and its costs are
computed from that snapshot only. Editing this table changes the estimates of
later runs, never of runs already recorded.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

SOURCE_URL = "https://aistudio.google.com/docs/pricing"
VERIFIED_ON = "2026-11-11"
TIER = "paid_standard"
TIER_LABEL = "Gemini Developer API, Paid tier, Standard"
CURRENCY = "USD"
MILLION = Decimal(1_000_000)

ModelKind = Literal["generation", "embedding"]


@dataclass(frozen=True)
class ModelPrice:
    """USD per 1,000,000 tokens."""

    kind: ModelKind
    input_per_million: Decimal
    output_per_million: Decimal | None = None  # generation only; includes thinking tokens
    cached_input_per_million: Decimal | None = None  # generation only

    def as_json(self) -> dict[str, Any]:
        """Rates as strings, so a stored snapshot keeps them exactly."""
        return {
            "kind": self.kind,
            "input_per_million": str(self.input_per_million),
            "output_per_million": _text(self.output_per_million),
            "cached_input_per_million": _text(self.cached_input_per_million),
        }


# Paid tier, Standard, verified on VERIFIED_ON. Text, image, video, and audio input share
# one rate for gemini-3.5-flash-lite, and it has no prompt-length price tiers.
PRICES: dict[str, ModelPrice] = {
    "gemini-3.5-flash-lite": ModelPrice(
        kind="generation",
        input_per_million=Decimal("0.30"),
        output_per_million=Decimal("2.50"),
        cached_input_per_million=Decimal("0.03"),
    ),
    "gemini-embedding-001": ModelPrice(kind="embedding", input_per_million=Decimal("0.15")),
}


def _text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decimal(value: object) -> Decimal | None:
    if not isinstance(value, str):
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() and number >= 0 else None


def snapshot(models: Iterable[str]) -> dict[str, Any]:
    """The pricing that applies to `models`, as JSON, to store with a run."""
    wanted = sorted(set(models))
    return {
        "tier": TIER,
        "tier_label": TIER_LABEL,
        "currency": CURRENCY,
        "unit": "per 1,000,000 tokens",
        "source_url": SOURCE_URL,
        "verified_on": VERIFIED_ON,
        "thinking_tokens": "billed at the output rate",
        "models": {name: PRICES[name].as_json() for name in wanted if name in PRICES},
        "unpriced_models": [name for name in wanted if name not in PRICES],
    }


def price_in(pricing: Mapping[str, Any], model: str) -> ModelPrice | None:
    """A model's price from a stored snapshot; None if it has none (or it can't be read)."""
    models = pricing.get("models")
    entry = models.get(model) if isinstance(models, Mapping) else None
    if not isinstance(entry, Mapping):
        return None
    kind = entry.get("kind")
    rate = _decimal(entry.get("input_per_million"))
    if kind not in ("generation", "embedding") or rate is None:
        return None
    output = _decimal(entry.get("output_per_million"))
    cached = _decimal(entry.get("cached_input_per_million"))
    if kind == "generation" and output is None:
        return None
    return ModelPrice(kind, rate, output, cached)


def generation_cost(
    price: ModelPrice,
    *,
    prompt_tokens: int,
    output_tokens: int,
    thinking_tokens: int,
    cached_tokens: int = 0,
) -> Decimal | None:
    """USD for one generation's reported tokens, or None if the price can't cover them."""
    if price.kind != "generation" or price.output_per_million is None:
        return None
    cached = min(max(cached_tokens, 0), prompt_tokens)
    if cached and price.cached_input_per_million is None:
        return None
    cached_cost = cached * (price.cached_input_per_million or Decimal(0))
    input_cost = (prompt_tokens - cached) * price.input_per_million
    output_cost = (output_tokens + thinking_tokens) * price.output_per_million
    return (input_cost + cached_cost + output_cost) / MILLION


def embedding_cost(price: ModelPrice, *, input_tokens: int) -> Decimal | None:
    """USD for one embedding request's reported prompt tokens."""
    if price.kind != "embedding":
        return None
    return input_tokens * price.input_per_million / MILLION
