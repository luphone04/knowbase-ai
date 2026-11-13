"""Daily budgets for evaluation runs (ADR 0014), on the PostgreSQL rate-limit counters.

A run reserves everything it may use when it is created, all or nothing: one
run slot for its workspace, one query embedding per question (from the
installation-wide DAILY_QUERY_EMBEDDING_LIMIT that chat and search share), and
one generation per question per answer mode (DAILY_EVALUATION_GENERATION_LIMIT,
installation-wide: every workspace shares one Gemini project). Whatever a run
didn't use is given back when it ends; the run slot is not.

**Additional retry reservations:** when a question is evaluated again because an
earlier attempt never saved its results (the worker crashed or lost its lease),
that earlier attempt may already have called Gemini. Before the retry makes any
call, it reserves one more attempt's worth (one query embedding and one
generation per answer mode), all or nothing, in the current UTC day. These are
recorded on the run and never given back, so the counters can only over-count
(when the interrupted attempt stopped before calling Gemini), never under-count.
The original reservation's unused slots are still given back as before, except
when a run ends on a question that never saved: its interrupted attempt may have
spent that question's original slots, so one attempt's worth is held back.
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.api.v1.gemini_calls import DAY_SECONDS, INSTALLATION, query_embedding_rule
from app.auth.rate_limit import RateLimiter, RateLimitExceeded, RateLimitRule
from app.config import Settings

logger = logging.getLogger(__name__)


def run_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule("evaluation-runs-daily", settings.daily_evaluation_run_limit, DAY_SECONDS)


def generation_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule(
        "evaluation-generations-daily", settings.daily_evaluation_generation_limit, DAY_SECONDS
    )


@dataclass(frozen=True)
class Reservation:
    query_embeddings: int
    generations: int
    windows: dict[str, str]  # rule name -> window start (ISO), for giving slots back

    def as_json(self) -> dict[str, Any]:
        return {
            "query_embeddings": self.query_embeddings,
            "generations": self.generations,
            "windows": self.windows,
        }


def _take(
    limiter: RateLimiter, wanted: list[tuple[RateLimitRule, str, int]], now: datetime | None
) -> dict[str, str]:
    """Reserve each (rule, identifier, amount), or none: the windows used, by rule name."""
    taken: list[tuple[RateLimitRule, str, int, datetime]] = []
    try:
        for rule, identifier, amount in wanted:
            if amount > 0:
                window = limiter.reserve_many(rule, identifier, amount, now=now)
                taken.append((rule, identifier, amount, window))
    except RateLimitExceeded:
        for rule, identifier, amount, window in taken:
            limiter.release_many(rule, identifier, amount, window_start=window)
        raise
    return {rule.name: window.isoformat() for rule, _, _, window in taken}


def reserve(
    limiter: RateLimiter,
    settings: Settings,
    *,
    workspace_id: str,
    query_embeddings: int,
    generations: int,
    now: datetime | None = None,
) -> Reservation:
    """Take every slot the run may use, or none (raises RateLimitExceeded)."""
    windows = _take(
        limiter,
        [
            (run_rule(settings), workspace_id, 1),
            (query_embedding_rule(settings), INSTALLATION, query_embeddings),
            (generation_rule(settings), INSTALLATION, generations),
        ],
        now,
    )
    return Reservation(query_embeddings=query_embeddings, generations=generations, windows=windows)


def reserve_retry(
    limiter: RateLimiter,
    settings: Settings,
    *,
    generations: int,
    query_embeddings: int = 1,
    now: datetime | None = None,
) -> dict[str, Any]:
    """An additional retry reservation: `query_embeddings` (one, or two for a follow-up
    with diagnostics) and `generations`, or none.

    Raises RateLimitExceeded when today's budget can't cover them; nothing is then taken.
    """
    windows = _take(
        limiter,
        [
            (query_embedding_rule(settings), INSTALLATION, query_embeddings),
            (generation_rule(settings), INSTALLATION, generations),
        ],
        now,
    )
    return {"query_embeddings": query_embeddings, "generations": generations, "windows": windows}


def release_retry(limiter: RateLimiter, settings: Settings, reservation: dict[str, Any]) -> None:
    """Give back a retry reservation that was never used (the run was lost before any call)."""
    windows = reservation.get("windows", {})
    for rule, amount in (
        (query_embedding_rule(settings), reservation.get("query_embeddings", 0)),
        (generation_rule(settings), reservation.get("generations", 0)),
    ):
        if amount and rule.name in windows:
            window = datetime.fromisoformat(windows[rule.name])
            limiter.release_many(rule, INSTALLATION, int(amount), window_start=window)


def retry_totals(reservation: dict[str, Any]) -> dict[str, Any]:
    """The additional retry reservations on a run's budget, added up, and what was held back."""
    retries = reservation.get("retry_reservations", [])
    held = reservation.get("held_from_original", {})
    return {
        "count": len(retries),
        "query_embeddings": sum(int(r.get("query_embeddings", 0)) for r in retries),
        "generations": sum(int(r.get("generations", 0)) for r in retries),
        "held_from_original": {
            "query_embeddings": int(held.get("query_embeddings", 0)),
            "generation_requests": int(held.get("generation_requests", 0)),
        },
    }


def give_back_unused(
    limiter: RateLimiter, settings: Settings, reservation: dict[str, Any], used: dict[str, int]
) -> None:
    """Return the reserved query embeddings and generations a finished run didn't use.

    Only the original reservation counts here; additional retry reservations are
    kept (they stand for calls an interrupted attempt may have made).
    """
    windows = reservation.get("windows", {})
    for rule, reserved, spent in (
        (
            query_embedding_rule(settings),
            reservation.get("query_embeddings", 0),
            used.get("query_embeddings", 0),
        ),
        (
            generation_rule(settings),
            reservation.get("generations", 0),
            used.get("generation_requests", 0),
        ),
    ):
        unused = int(reserved) - int(spent)
        if unused > 0 and rule.name in windows:
            limiter.release_many(
                rule, INSTALLATION, unused, window_start=datetime.fromisoformat(windows[rule.name])
            )
            logger.info("Gave back %d unused %s slots", unused, rule.name)
