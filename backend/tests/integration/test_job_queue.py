"""The PostgreSQL job queue: atomic claiming, retries, stale-lock recovery, fencing, heartbeat."""

import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, tzinfo
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db.models import IngestionJob, Source
from app.db.session import get_engine, get_sessionmaker
from app.db.tenancy import bind_workspace
from app.worker.queue import (
    SUPERSEDED_MESSAGE,
    claim_next_job,
    engine_heartbeat,
    enqueue_job,
    recover_stale_jobs,
    requeue_or_supersede,
)
from tests.fakes.documents import RETURNS_POLICY
from tests.integration.conftest import Tenant, WorkerHarness, make_worker

pytestmark = pytest.mark.integration


def sql(statement: str, **params: object) -> list[Any]:
    with get_engine().begin() as connection:
        result = connection.execute(text(statement), params)
        return list(result) if result.returns_rows else []


def add_source(db: Session, tenant: Tenant, name: str) -> Source:
    bind_workspace(db, tenant.workspace_id)
    source = Source(
        workspace_id=tenant.workspace_id,
        kind="file",
        display_name=name,
        original_filename=name,
        dedupe_key=name,
        status="pending",
    )
    db.add(source)
    db.flush()
    return source


def queue_jobs(db: Session, tenant: Tenant, count: int) -> list[uuid.UUID]:
    ids = []
    for i in range(count):
        source = add_source(db, tenant, f"s{i}.md")
        job = enqueue_job(
            db,
            workspace_id=tenant.workspace_id,
            source_id=source.id,
            job_type="ingest",
            storage_key=None,
            max_attempts=3,
        )
        ids.append(job.id)
    db.commit()
    return ids


# --- claiming -------------------------------------------------------------------------------------


def test_claim_takes_the_oldest_due_job_and_commits_it(tenant_a: Tenant, db: Session) -> None:
    first, second = queue_jobs(db, tenant_a, 2)
    sql("UPDATE ingestion_jobs SET next_run_at = now() + interval '1 hour' WHERE id = :j", j=first)

    claimed = claim_next_job(get_sessionmaker(), "w1")
    assert claimed is not None and claimed.id == second  # the due one, not the future one
    row = sql(
        "SELECT status, attempts, lock_token, locked_by, locked_at"
        " FROM ingestion_jobs WHERE id = :j",
        j=second,
    )[0]
    assert (row.status, row.attempts, row.locked_by) == ("running", 1, "w1")
    assert row.lock_token == claimed.lock_token and row.locked_at is not None
    assert claim_next_job(get_sessionmaker(), "w1") is None  # nothing else is due


def test_concurrent_workers_never_claim_the_same_job(tenant_a: Tenant, db: Session) -> None:
    ids = set(queue_jobs(db, tenant_a, 30))
    claimed: list[uuid.UUID] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def drain(worker_number: int) -> None:
        start.wait()
        while (job := claim_next_job(get_sessionmaker(), f"w{worker_number}")) is not None:
            with lock:
                claimed.append(job.id)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(drain, range(8)))
    assert sorted(claimed) == sorted(ids)  # every job exactly once
    assert len(set(claimed)) == len(claimed)


def test_one_waiting_job_per_source(tenant_a: Tenant, db: Session) -> None:
    source = add_source(db, tenant_a, "a.md")
    kwargs: dict[str, Any] = {
        "workspace_id": tenant_a.workspace_id,
        "source_id": source.id,
        "storage_key": None,
        "max_attempts": 3,
    }
    first = enqueue_job(db, job_type="ingest", **kwargs)
    again = enqueue_job(db, job_type="delete", **kwargs)
    db.commit()
    assert again.id == first.id and again.job_type == "delete"  # updated, not duplicated

    claim_next_job(get_sessionmaker(), "w1")  # now running
    newer = enqueue_job(db, job_type="ingest", **kwargs)
    db.commit()
    assert newer.id != first.id  # a running job doesn't block a new waiting one


def test_requeue_is_superseded_by_a_newer_waiting_job(tenant_a: Tenant, db: Session) -> None:
    source = add_source(db, tenant_a, "a.md")
    common: dict[str, Any] = {
        "workspace_id": tenant_a.workspace_id,
        "source_id": source.id,
        "job_type": "ingest",
        "storage_key": None,
        "max_attempts": 3,
    }
    enqueue_job(db, **common)
    db.commit()
    running = claim_next_job(get_sessionmaker(), "w1")
    assert running is not None
    enqueue_job(db, **common)  # a newer upload arrives while it runs
    db.commit()

    row = db.get_one(IngestionJob, running.id)
    requeued = requeue_or_supersede(
        db, row, run_at=datetime.now(UTC), message="retry", count_attempt=True
    )
    db.commit()
    assert requeued is False and row.status == "failed" and row.last_error == SUPERSEDED_MESSAGE


# --- crash recovery -------------------------------------------------------------------------------


def test_stale_running_jobs_are_requeued(tenant_a: Tenant, db: Session) -> None:
    (job_id,) = queue_jobs(db, tenant_a, 1)
    claimed = claim_next_job(get_sessionmaker(), "crashed-worker")
    assert claimed is not None
    sql(
        "UPDATE ingestion_jobs SET locked_at = now() - interval '10 minutes' WHERE id = :j",
        j=job_id,
    )

    assert recover_stale_jobs(get_sessionmaker(), lease_seconds=120) == 1
    row = sql(
        "SELECT status, attempts, lock_token, last_error FROM ingestion_jobs WHERE id = :j",
        j=job_id,
    )[0]
    assert row.status == "queued" and row.lock_token is None and row.attempts == 1
    assert "stopped unexpectedly" in row.last_error


def test_fresh_locks_are_left_alone(tenant_a: Tenant, db: Session) -> None:
    queue_jobs(db, tenant_a, 1)
    claim_next_job(get_sessionmaker(), "busy-worker")
    assert recover_stale_jobs(get_sessionmaker(), lease_seconds=120) == 0
    assert sql("SELECT status FROM ingestion_jobs")[0].status == "running"


def test_stale_job_out_of_attempts_fails_with_its_source(tenant_a: Tenant, db: Session) -> None:
    (job_id,) = queue_jobs(db, tenant_a, 1)
    claim_next_job(get_sessionmaker(), "crashed-worker")
    sql(
        "UPDATE ingestion_jobs SET attempts = max_attempts,"
        " locked_at = now() - interval '1 hour' WHERE id = :j",
        j=job_id,
    )
    recover_stale_jobs(get_sessionmaker(), lease_seconds=120)
    row = sql("SELECT status, last_error, source_id FROM ingestion_jobs WHERE id = :j", j=job_id)[0]
    assert row.status == "failed" and "no attempts are left" in row.last_error
    assert sql("SELECT status FROM sources WHERE id = :s", s=row.source_id)[0].status == "failed"


def test_worker_restart_recovers_and_finishes_an_abandoned_job(
    tenant_a: Tenant, worker: WorkerHarness
) -> None:
    body = tenant_a.client.post(
        "/api/v1/sources/files", files={"file": ("returns.md", RETURNS_POLICY.encode())}
    ).json()
    crashed = claim_next_job(get_sessionmaker(), "worker-that-died")  # claimed, never finished
    assert crashed is not None
    sql("UPDATE ingestion_jobs SET locked_at = now() - interval '5 minutes'")

    restarted = make_worker()
    assert restarted.worker.recover() == 1
    assert restarted.run_all() == 1
    row = sql("SELECT status, attempts FROM ingestion_jobs WHERE id = :j", j=body["job"]["id"])[0]
    assert (row.status, row.attempts) == ("succeeded", 2)
    assert sql("SELECT status FROM sources")[0].status == "ready"


# --- fencing and heartbeat ------------------------------------------------------------------------


def test_a_worker_that_lost_its_job_cannot_publish(tenant_a: Tenant) -> None:
    tenant_a.client.post(
        "/api/v1/sources/files", files={"file": ("returns.md", RETURNS_POLICY.encode())}
    )
    stalled = make_worker()
    claim = claim_next_job(get_sessionmaker(), "stalled-worker")
    assert claim is not None
    # The stalled worker's lease lapses; recovery hands the job to someone else.
    sql("UPDATE ingestion_jobs SET locked_at = now() - interval '10 minutes'")
    recover_stale_jobs(get_sessionmaker(), lease_seconds=120)
    new_owner = claim_next_job(get_sessionmaker(), "new-worker")
    assert new_owner is not None and new_owner.lock_token != claim.lock_token

    stalled.worker.process(claim)  # finally wakes up and tries to finish

    row = sql("SELECT status, lock_token, locked_by FROM ingestion_jobs")[0]
    assert (row.status, row.lock_token, row.locked_by) == (
        "running",
        new_owner.lock_token,
        "new-worker",
    )
    assert sql("SELECT count(*) FROM chunks")[0][0] == 0  # nothing published by the stale worker
    assert engine_heartbeat(get_engine(), claim) is False  # its heartbeat is refused too
    assert engine_heartbeat(get_engine(), new_owner) is True


def test_heartbeat_keeps_a_long_job_alive(tenant_a: Tenant) -> None:
    tenant_a.client.post(
        "/api/v1/sources/files", files={"file": ("returns.md", RETURNS_POLICY.encode())}
    )
    harness = make_worker(env={"JOB_HEARTBEAT_SECONDS": "5", "JOB_LEASE_SECONDS": "30"})
    harness.worker.settings = harness.worker.settings.model_copy(
        update={"job_heartbeat_seconds": 0.05}
    )
    seen: list[datetime] = []
    original = harness.provider.embed_documents

    def slow(texts: Any) -> Any:  # a slow Gemini call
        for _ in range(4):
            time.sleep(0.1)
            seen.append(sql("SELECT locked_at FROM ingestion_jobs")[0].locked_at)
        return original(texts)

    harness.provider.embed_documents = slow  # type: ignore[method-assign]
    harness.run_all()
    assert len(set(seen)) > 1  # locked_at advanced while the job ran
    assert sql("SELECT status FROM ingestion_jobs")[0].status == "succeeded"
    assert recover_stale_jobs(get_sessionmaker(), lease_seconds=30) == 0


def test_heartbeat_thread_stops_when_the_job_ends(tenant_a: Tenant, worker: WorkerHarness) -> None:
    tenant_a.client.post(
        "/api/v1/sources/files", files={"file": ("returns.md", RETURNS_POLICY.encode())}
    )
    before = {t.name for t in threading.enumerate()}
    worker.run_all()
    time.sleep(0.05)
    assert not {t.name for t in threading.enumerate() if t.name.startswith("heartbeat-")} - before


def test_claim_session_is_cross_workspace_by_design(
    tenant_a: Tenant, tenant_b: Tenant, db: Session
) -> None:
    queue_jobs(db, tenant_a, 1)
    db.rollback()
    with get_sessionmaker()() as session_b:
        queue_jobs(session_b, tenant_b, 1)
    workspaces = set()
    while (job := claim_next_job(get_sessionmaker(), "w")) is not None:
        workspaces.add(job.workspace_id)
    assert workspaces == {tenant_a.workspace_id, tenant_b.workspace_id}


def test_recovery_uses_the_database_clock(
    tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker whose clock runs ahead of the database still gets recovered jobs at once."""
    import app.worker.queue as queue_module

    tenant_a.client.post(
        "/api/v1/sources/files", files={"file": ("returns.md", RETURNS_POLICY.encode())}
    )
    assert claim_next_job(get_sessionmaker(), "worker-that-died") is not None
    sql("UPDATE ingestion_jobs SET locked_at = now() - interval '5 minutes'")

    class FastClock(datetime):
        @classmethod
        def now(cls, tz: tzinfo | None = None) -> "FastClock":
            return cls.fromtimestamp((datetime.now(UTC) + timedelta(hours=1)).timestamp(), tz)

    monkeypatch.setattr(queue_module, "datetime", FastClock)
    assert recover_stale_jobs(get_sessionmaker(), lease_seconds=120) == 1
    assert claim_next_job(get_sessionmaker(), "worker-after-restart") is not None
