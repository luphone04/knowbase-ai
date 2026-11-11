"""Gemini token usage and estimated cost of chat replies and whole conversations.

Same accounting as the evaluation (app.evaluation.usage), which it reuses:
reported token counts only, never estimated from text; unknown counts are None,
never zero; Gemini's embedding responses report no tokens, so query embeddings
are counted as requests without usage; failed HTTP attempts before a request
succeeded are disclosed (`unmeasured_attempts`), never priced or assumed free.

Each reply stores the pricing snapshot that applied when it was saved
(`token_usage["pricing"]`), and is priced from it only, so later price changes
never alter a past reply. Replies saved before snapshots were recorded keep
their token counts but have no cost.

A conversation's totals add up its replies and its failed questions (a turn
that failed at Gemini saves no message, but its trace records the calls it
made). The total cost covers every request whose cost is known: when some
aren't (no reported usage, no price, retried attempts), it is a lower bound and
`cost_complete` is false.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.evaluation.usage import Pricer, Tally, embedding_tally, generation_tally


def _usd(value: Decimal) -> float:
    return float(round(value, 10))


@dataclass
class _Group:
    """A Tally plus how many of its requests have a known cost."""

    tally: Tally = field(default_factory=Tally)
    costed: int = 0

    def add(self, other: "_Group") -> None:
        self.tally.add(other.tally)
        self.costed += other.costed

    def as_json(self) -> dict[str, Any]:
        t = self.tally
        measured = t.requests == 0 or t.without_usage < t.requests

        def tokens(value: int) -> int | None:
            return value if measured else None

        return {
            "requests": t.requests,
            "requests_without_usage": t.without_usage,
            "unmeasured_attempts": t.unmeasured_attempts,
            "usage_complete": t.without_usage == 0 and t.unmeasured_attempts == 0,
            "input_tokens": tokens(t.input_tokens),
            "cached_input_tokens": tokens(t.cached_input_tokens),
            "output_tokens": tokens(t.output_tokens),
            "thinking_tokens": tokens(t.thinking_tokens),
            "total_tokens": tokens(t.total_tokens),
            # The cost of the requests that have one (None if none does); `cost_complete`
            # says whether that is every request, so a partial sum reads as a lower bound.
            "cost_usd": _usd(t.cost) if t.requests == 0 or self.costed else None,
            "cost_complete": self.costed == t.requests and t.unmeasured_attempts == 0,
        }


def _group(tally: Tally) -> _Group:
    costed = tally.requests if tally.unpriced == 0 and tally.without_usage == 0 else 0
    return _Group(tally, costed)


@dataclass
class _Entry:
    embedding: _Group
    generation: _Group
    embedding_model: str | None
    chat_model: str | None
    priced: bool


def _entry(usage: Mapping[str, Any], *, embedding_model: str, chat_model: str) -> _Entry:
    snapshot = usage.get("pricing")
    pricer = Pricer(snapshot if isinstance(snapshot, Mapping) and snapshot else None)
    embedding, generation = _Group(), _Group()
    used_embedding = used_chat = None
    if "query_embeddings" in usage:
        used_embedding = str(usage.get("embedding_model") or embedding_model)
        embedding = _group(embedding_tally(usage, used_embedding, pricer))
    if "generation_requests" in usage:
        used_chat = str(usage.get("model") or chat_model)
        generation = _group(generation_tally(usage, used_chat, pricer))
    return _Entry(
        embedding,
        generation,
        used_embedding if embedding.tally.requests else None,
        used_chat if generation.tally.requests else None,
        pricer.snapshot is not None,
    )


def has_usage_record(usage: Mapping[str, Any] | None) -> bool:
    """False for a reply saved without usage (or a user message): shown as not available."""
    if not usage:
        return False
    return "query_embeddings" in usage or "generation_requests" in usage


def _pricing_info(usage: Mapping[str, Any]) -> dict[str, Any] | None:
    snapshot = usage.get("pricing")
    if not isinstance(snapshot, Mapping) or not snapshot:
        return None
    return {key: snapshot.get(key) for key in ("tier_label", "verified_on", "source_url")}


def _issues(embedding: _Group, generation: _Group, unpriced_requests: int) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if unpriced_requests:
        issues.append({"code": "cost_unavailable", "requests": unpriced_requests})
    for kind, group in (("embedding", embedding), ("generation", generation)):
        if group.tally.without_usage:
            issues.append(
                {"code": "usage_missing", "kind": kind, "requests": group.tally.without_usage}
            )
        if group.tally.unmeasured_attempts:
            issues.append(
                {
                    "code": "unmeasured_attempts",
                    "kind": kind,
                    "attempts": group.tally.unmeasured_attempts,
                }
            )
    return issues


def _unpriced_measured(group: _Group) -> int:
    """Requests that reported usage but couldn't be priced (no snapshot or no price)."""
    t = group.tally
    return t.requests - t.without_usage - group.costed if t.requests > t.without_usage else 0


def message_usage(
    usage: Mapping[str, Any] | None, *, embedding_model: str, chat_model: str
) -> dict[str, Any] | None:
    """One reply's usage and estimated cost; None when the reply has no usage record."""
    if usage is None or not has_usage_record(usage):
        return None
    entry = _entry(usage, embedding_model=embedding_model, chat_model=chat_model)
    total = _Group()
    total.add(entry.embedding)
    total.add(entry.generation)
    unpriced = _unpriced_measured(entry.embedding) + _unpriced_measured(entry.generation)
    return {
        "pricing": _pricing_info(usage),
        "embedding": {**entry.embedding.as_json(), "model": entry.embedding_model},
        "generation": {**entry.generation.as_json(), "model": entry.chat_model},
        "total": total.as_json(),
        "issues": _issues(entry.embedding, entry.generation, unpriced),
    }


def conversation_usage(
    replies: Iterable[Mapping[str, Any]],
    failed_turns: Iterable[Mapping[str, Any]],
    *,
    embedding_model: str,
    chat_model: str,
) -> dict[str, Any]:
    """Totals over a conversation's replies (their `token_usage`) and failed questions
    (their traces' `token_usage`)."""
    embedding, generation = _Group(), _Group()
    reply_count = with_usage = failed = 0
    unpriced = 0
    pricing: dict[str, Any] | None = None
    for kind, rows in (("reply", replies), ("failed", failed_turns)):
        for usage in rows:
            if kind == "reply":
                reply_count += 1
            if not has_usage_record(usage):
                continue
            if kind == "reply":
                with_usage += 1
            else:
                failed += 1
            entry = _entry(usage, embedding_model=embedding_model, chat_model=chat_model)
            embedding.add(entry.embedding)
            generation.add(entry.generation)
            unpriced += _unpriced_measured(entry.embedding) + _unpriced_measured(entry.generation)
            pricing = _pricing_info(usage) or pricing  # the most recent snapshot
    total = _Group()
    total.add(embedding)
    total.add(generation)
    return {
        "replies": reply_count,
        "replies_with_usage": with_usage,
        "failed_questions": failed,
        "pricing": pricing,
        "embedding": embedding.as_json(),
        "generation": generation.as_json(),
        "total": total.as_json(),
        "issues": _issues(embedding, generation, unpriced),
    }
