"""The daily embedding guardrail (DAILY_EMBEDDING_TEXT_LIMIT) during ingestion."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from app.auth.rate_limit import RateLimiter, RateLimitExceeded, RateLimitRule
from app.config import get_settings
from app.db.session import get_engine
from app.ingestion.embedding import EmbeddingBudget
from tests.integration.conftest import Tenant, make_worker

pytestmark = pytest.mark.integration


def sql(statement: str, **params: object) -> list[Any]:
    with get_engine().begin() as connection:
        result = connection.execute(text(statement), params)
        return list(result) if result.returns_rows else []


def document(paragraphs: int) -> bytes:
    """Small chunks (CHUNK_* lowered below) so each paragraph is its own chunk."""
    return "\n\n".join(
        f"Paragraph {i} describes policy number {i} in enough words to be its own chunk." * 3
        for i in range(paragraphs)
    ).encode()


SMALL_CHUNKS = {
    "CHUNK_TARGET_CHARS": "250",
    "CHUNK_MAX_CHARS": "300",
    "CHUNK_OVERLAP_CHARS": "0",
}


@pytest.fixture(autouse=True)
def restore_settings() -> Any:
    yield
    import os

    for name, value in {
        "DAILY_EMBEDDING_TEXT_LIMIT": "300",
        "CHUNK_TARGET_CHARS": "1800",
        "CHUNK_MAX_CHARS": "2400",
        "CHUNK_OVERLAP_CHARS": "250",
    }.items():
        os.environ[name] = value
    get_settings.cache_clear()


def budget_used() -> int:
    rows = sql(
        "SELECT count FROM rate_limit_counters WHERE bucket_key LIKE 'embedding-texts-daily:%'"
    )
    return int(rows[0][0]) if rows else 0


def test_limit_defers_the_job_and_keeps_finished_batches(tenant_a: Tenant) -> None:
    harness = make_worker(env={"DAILY_EMBEDDING_TEXT_LIMIT": "5", **SMALL_CHUNKS})
    body = tenant_a.client.post(
        "/api/v1/sources/files", files={"file": ("p.txt", document(7))}
    ).json()
    harness.run_all()

    assert harness.provider.texts_embedded == 5  # never more than the limit
    assert budget_used() == 5
    job = sql("SELECT status, attempts, next_run_at, last_error FROM ingestion_jobs")[0]
    assert job.status == "queued" and job.attempts == 0  # deferred, not failed
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    assert abs((job.next_run_at - tomorrow).total_seconds()) < 5  # resumes after 00:00 UTC
    assert "daily embedding limit (5 texts per day)" in job.last_error
    assert sql("SELECT count(*) FROM embedding_cache")[0][0] == 5  # finished work is kept
    assert sql("SELECT count(*) FROM chunks")[0][0] == 0  # nothing half-published
    assert (
        sql("SELECT status FROM sources WHERE id = :s", s=body["source"]["id"])[0].status
        == "pending"
    )

    # A new day: the budget window rolls over and the job resumes.
    sql("DELETE FROM rate_limit_counters")
    sql("UPDATE ingestion_jobs SET next_run_at = now()")
    harness.run_all()
    assert harness.provider.texts_embedded == 7  # only the 2 missing texts were sent
    assert sql("SELECT count(*) FROM chunks")[0][0] == 7
    assert sql("SELECT status FROM ingestion_jobs")[0].status == "succeeded"


def test_cached_and_unchanged_content_never_uses_budget(tenant_a: Tenant) -> None:
    harness = make_worker(env=SMALL_CHUNKS)
    body = tenant_a.client.post(
        "/api/v1/sources/files", files={"file": ("p.txt", document(4))}
    ).json()
    harness.run_all()
    used = budget_used()
    assert used == 4

    tenant_a.client.post(f"/api/v1/sources/{body['source']['id']}/reindex")
    harness.run_all()
    assert budget_used() == used  # unchanged: no budget

    # Same paragraphs uploaded as a different file: all served from the cache.
    tenant_a.client.post("/api/v1/sources/files", files={"file": ("copy.txt", document(4))})
    harness.run_all()
    assert budget_used() == used
    assert harness.provider.texts_embedded == 4


def test_zero_limit_disables_embedding_with_a_clear_failure(tenant_a: Tenant) -> None:
    harness = make_worker(env={"DAILY_EMBEDDING_TEXT_LIMIT": "0"})
    tenant_a.client.post("/api/v1/sources/files", files={"file": ("p.txt", document(3))})
    harness.run_all()
    job = sql("SELECT status, last_error FROM ingestion_jobs")[0]
    assert job.status == "failed" and "DAILY_EMBEDDING_TEXT_LIMIT=0" in job.last_error
    assert harness.provider.calls == []


def test_budget_reservations_are_atomic_under_concurrency() -> None:
    limiter = RateLimiter(get_engine(), get_settings().app_secret_key.get_secret_value())
    rule = RateLimitRule("concurrent-budget", limit=30, window_seconds=86400)

    def take(_: int) -> bool:
        try:
            limiter.reserve_many(rule, "installation", 3)
        except RateLimitExceeded:
            return False
        return True

    with ThreadPoolExecutor(max_workers=10) as pool:
        granted = list(pool.map(take, range(20)))
    assert granted.count(True) == 10  # exactly 30 of 60 requested texts
    assert sql("SELECT count FROM rate_limit_counters")[0][0] == 30


def test_a_batch_larger_than_what_is_left_is_refused_whole() -> None:
    limiter = RateLimiter(get_engine(), get_settings().app_secret_key.get_secret_value())
    budget = EmbeddingBudget(limiter, daily_limit=10)
    budget.reserve(8)
    from app.ingestion.embedding import EmbeddingDeferred

    with pytest.raises(EmbeddingDeferred):
        budget.reserve(3)  # 8 + 3 > 10: nothing taken
    assert budget.remaining() == 2
    budget.reserve(2)
    assert budget.remaining() == 0
