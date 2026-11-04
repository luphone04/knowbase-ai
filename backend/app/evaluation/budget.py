"""Daily budgets for evaluation runs (ADR 0014), on the PostgreSQL rate-limit counters.

A run reserves everything it may use when it is created, all or nothing: one
run slot for its workspace, one query embedding per question (from the
installation-wide DAILY_QUERY_EMBEDDING_LIMIT that chat and search share), and
one generation per question per answer mode (DAILY_EVALUATION_GENERATION_LIMIT,
installation-wide: every workspace shares one Gemini project). Whatever a run
didn't use is given back when it ends; the run slot is not.
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


def reserve(
    limiter: RateLimiter,
    settings: Settings,
    *,
    workspace_id: str,
    query_embeddings: int,
    generations: int,
) -> Reservation:
    """Take every slot the run may use, or none (raises RateLimitExceeded)."""
    taken: list[tuple[RateLimitRule, str, int, datetime]] = []
    wanted = [
        (run_rule(settings), workspace_id, 1),
        (query_embedding_rule(settings), INSTALLATION, query_embeddings),
        (generation_rule(settings), INSTALLATION, generations),
    ]
    try:
        for rule, identifier, amount in wanted:
            if amount > 0:
                taken.append(
                    (rule, identifier, amount, limiter.reserve_many(rule, identifier, amount))
                )
    except RateLimitExceeded:
        for rule, identifier, amount, window in taken:
            limiter.release_many(rule, identifier, amount, window_start=window)
        raise
    return Reservation(
        query_embeddings=query_embeddings,
        generations=generations,
        windows={rule.name: window.isoformat() for rule, _, _, window in taken},
    )


def give_back_unused(
    limiter: RateLimiter, settings: Settings, reservation: dict[str, Any], used: dict[str, int]
) -> None:
    """Return the reserved query embeddings and generations a finished run didn't use."""
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
