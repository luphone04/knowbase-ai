"""Document-chunk embeddings for ingestion: cache, then budget, then Gemini.

1. **Cache.** Look up each chunk's content hash in `embedding_cache`, keyed by
   (workspace, content hash, model, dimensions). Unchanged or repeated text
   costs no Gemini call and no budget.
2. **Budget.** Only texts that really go to Gemini reserve the installation-wide
   daily limit (DAILY_EMBEDDING_TEXT_LIMIT). The reservation is atomic and made
   *before* the call. It is given back only when Gemini refused the call
   outright (429, bad key or request); after a 5xx or a timeout it is kept,
   because Gemini may have counted the request.
3. **Gemini.** Chunks are embedded with the existing M3 provider: RETRIEVAL_DOCUMENT,
   768 dimensions, validated and normalised there. Each finished batch is
   cached immediately, so a later failure or a budget deferral never wastes it.
"""

import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.auth.rate_limit import RateLimiter, RateLimitExceeded, RateLimitRule
from app.db.models import EmbeddingCacheEntry
from app.providers.errors import REFUSED_BEFORE_COUNTING, InvalidProviderResponse
from app.providers.interfaces import DOCUMENT_TASK, EmbeddingProvider

logger = logging.getLogger(__name__)

DAILY_WINDOW_SECONDS = 24 * 3600  # epoch-aligned, so each window starts at 00:00 UTC
BUDGET_SCOPE = "installation"


class EmbeddingDeferred(Exception):
    """Not a failure: try again at `run_at` (budget or provider daily quota reached)."""

    def __init__(self, run_at: datetime, reason: str) -> None:
        super().__init__(reason)
        self.run_at = run_at
        self.reason = reason


class EmbeddingDisabled(Exception):
    message = "Embedding is disabled (DAILY_EMBEDDING_TEXT_LIMIT=0), so nothing can be indexed."


@dataclass(frozen=True)
class BudgetReservation:
    count: int
    window_start: datetime


class EmbeddingBudget:
    """The SPEC.md §2.5 daily guardrail: texts sent to Gemini per UTC day, all workspaces.

    Installation-wide on purpose: Gemini's free-tier quota belongs to one Google
    project, shared by every workspace on this deployment.
    """

    def __init__(self, limiter: RateLimiter, daily_limit: int) -> None:
        self.daily_limit = daily_limit
        self._limiter = limiter
        self._rule = RateLimitRule("embedding-texts-daily", daily_limit, DAILY_WINDOW_SECONDS)

    def reserve(self, count: int) -> BudgetReservation:
        if self.daily_limit == 0:
            raise EmbeddingDisabled()
        try:
            window_start = self._limiter.reserve_many(self._rule, BUDGET_SCOPE, count)
        except RateLimitExceeded as exc:
            retry_after = int(exc.headers.get("Retry-After", DAILY_WINDOW_SECONDS))
            raise EmbeddingDeferred(
                datetime.now(UTC) + timedelta(seconds=retry_after),
                f"The daily embedding limit ({self.daily_limit} texts per day) has been reached. "
                "Indexing resumes automatically after 00:00 UTC; finished batches are kept.",
            ) from exc
        return BudgetReservation(count, window_start)

    def refund(self, reservation: BudgetReservation) -> None:
        self._limiter.release_many(
            self._rule, BUDGET_SCOPE, reservation.count, window_start=reservation.window_start
        )

    def remaining(self) -> int:
        return self._limiter.remaining(self._rule, BUDGET_SCOPE)

    @property
    def batch_cap(self) -> int:
        return max(1, self.daily_limit)


@dataclass(frozen=True)
class EmbeddingOutcome:
    vectors: dict[str, list[float]]  # content hash -> unit vector
    embedded: int  # texts sent to Gemini in this run
    reused: int  # texts served from the cache
    requests: int


def _batches(items: list[str], size: int) -> Iterator[list[str]]:
    for offset in range(0, len(items), size):
        yield items[offset : offset + size]


def embed_document_texts(
    session: Callable[[], AbstractContextManager[Session]],
    *,
    workspace_id: uuid.UUID,
    texts_by_hash: dict[str, str],
    model: str,
    dimensions: int,
    batch_size: int,
    budget: EmbeddingBudget,
    provider_factory: Callable[[], EmbeddingProvider],
    checkpoint: Callable[[], None] = lambda: None,
    source_id: uuid.UUID | None = None,
) -> EmbeddingOutcome:
    """Return a vector for every distinct text, calling Gemini only for uncached ones.

    `session` opens a session bound to `workspace_id`; the provider is only
    built if something actually needs embedding.
    """
    hashes = list(texts_by_hash)
    vectors: dict[str, list[float]] = {}
    with session() as db:
        for content_hash, embedding in db.execute(
            select(EmbeddingCacheEntry.content_hash, EmbeddingCacheEntry.embedding).where(
                EmbeddingCacheEntry.content_hash.in_(hashes),
                EmbeddingCacheEntry.model == model,
                EmbeddingCacheEntry.dimensions == dimensions,
            )
        ):
            vectors[content_hash] = [float(x) for x in embedding]
    reused = len(vectors)
    missing = [h for h in hashes if h not in vectors]
    if not missing:
        return EmbeddingOutcome(vectors, embedded=0, reused=reused, requests=0)

    provider = provider_factory()
    if provider.model != model or provider.dimensions != dimensions:
        # Never mix models or sizes in one index (SPEC.md §2.2).
        raise InvalidProviderResponse(
            "The embedding provider does not match the configured model and dimensions."
        )

    embedded = requests = 0
    for batch in _batches(missing, min(batch_size, budget.batch_cap)):
        checkpoint()  # stop between batches once the job's deadline has passed
        reservation = budget.reserve(len(batch))
        try:
            result = provider.embed_documents([texts_by_hash[h] for h in batch])
        except REFUSED_BEFORE_COUNTING:
            budget.refund(reservation)
            raise
        if (
            result.task_type != DOCUMENT_TASK
            or result.model != model
            or result.dimensions != dimensions
            or len(result.vectors) != len(batch)
        ):
            raise InvalidProviderResponse("The embedding result does not match the request.")
        rows: list[dict[str, Any]] = [
            {
                "workspace_id": workspace_id,
                "source_id": source_id,  # provenance: deleting this source cleans it up
                "content_hash": content_hash,
                "model": model,
                "dimensions": dimensions,
                "embedding": vector,
            }
            for content_hash, vector in zip(batch, result.vectors, strict=True)
        ]
        with session() as db:
            db.execute(insert(EmbeddingCacheEntry).values(rows).on_conflict_do_nothing())
            db.commit()
        vectors.update(zip(batch, result.vectors, strict=True))
        embedded += len(batch)
        requests += result.requests
    logger.info(
        "Embedded chunks workspace=%s new=%d cached=%d requests=%d model=%s",
        workspace_id,
        embedded,
        reused,
        requests,
        model,
    )
    return EmbeddingOutcome(vectors, embedded=embedded, reused=reused, requests=requests)
