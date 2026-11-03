"""The durable job queue: PostgreSQL rows, no Redis or Celery (SPEC.md §4.2).

Lifecycle: queued -> running -> succeeded | failed, or back to queued for a
retry or a deferral.

- **Claim** in one short transaction with `FOR UPDATE SKIP LOCKED`, committed
  before any slow work: two workers can never take the same job, and no lock
  is held while Gemini is called.
- **Fencing token:** each claim writes a fresh `lock_token`, and every later
  write to the job requires it. A worker that stalled past its lease (and lost
  the job to recovery) can't overwrite the new owner's results.
- **Heartbeat:** `renew_lease` refreshes `locked_at` while a job runs. A
  running job whose lease lapsed belongs to a crashed worker;
  `recover_stale_jobs` requeues it, or fails it if no attempts are left.
- **One waiting job per source** (a partial unique index). A newer upload
  updates the waiting job instead of piling up another.

Claiming and recovery span all workspaces by design. They are the explicit,
auditable `SKIP_WORKSPACE_SCOPE` statements below. All processing happens in
a session bound to the job's workspace.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, Connection, Engine, func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, aliased, sessionmaker

from app.db.models import IngestionJob, Source
from app.db.tenancy import SKIP_WORKSPACE_SCOPE

logger = logging.getLogger(__name__)

RECOVERED_MESSAGE = "The worker stopped unexpectedly while processing this job; it was retried."
ABANDONED_MESSAGE = (
    "The worker stopped unexpectedly while processing this job, and no attempts are left."
)
SUPERSEDED_MESSAGE = "Superseded by a newer request for this source."
DELETE_ABANDONED_MESSAGE = (
    "Deleting this source did not finish (the worker stopped repeatedly). Delete it again."
)


@dataclass(frozen=True)
class ClaimedJob:
    id: uuid.UUID
    workspace_id: uuid.UUID
    source_id: uuid.UUID
    job_type: str
    attempts: int
    max_attempts: int
    lock_token: uuid.UUID
    storage_key: str | None


def enqueue_job(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    source_id: uuid.UUID,
    job_type: str,
    storage_key: str | None,
    max_attempts: int,
) -> IngestionJob | None:
    """Create the source's waiting job, or update the one already waiting. Caller commits.

    A waiting `delete` job is never turned back into an ingest or reindex: then
    nothing changes and None is returned (the caller reports "being deleted").
    """
    statement = (
        insert(IngestionJob)
        .values(
            workspace_id=workspace_id,
            source_id=source_id,
            job_type=job_type,
            status="queued",
            storage_key=storage_key,
            max_attempts=max_attempts,
        )
        .on_conflict_do_update(
            index_elements=["source_id"],
            index_where=text("status = 'queued'"),
            set_={
                "job_type": job_type,
                "storage_key": storage_key,
                "attempts": 0,
                "max_attempts": max_attempts,
                "next_run_at": func.now(),
                "last_error": None,
                "result": {},
            },
            where=None if job_type == "delete" else IngestionJob.job_type != "delete",
        )
        .returning(IngestionJob)
    )
    job: IngestionJob | None = db.scalars(
        statement, execution_options={"populate_existing": True}
    ).one_or_none()
    return job


def claim_next_job(session_factory: sessionmaker[Session], worker_id: str) -> ClaimedJob | None:
    """Atomically take the oldest due job, or return None. Commits before returning."""
    token = uuid.uuid4()
    other = aliased(IngestionJob)
    busy_source = (
        select(other.id)
        .where(other.source_id == IngestionJob.source_id, other.status == "running")
        .exists()
    )
    next_due = (
        select(IngestionJob.id)
        .where(
            IngestionJob.status == "queued",
            IngestionJob.next_run_at <= func.now(),
            # One job per source at a time: a source with a running job waits, so two
            # workers can never process (or publish) the same source concurrently.
            ~busy_source,
        )
        .order_by(IngestionJob.next_run_at, IngestionJob.created_at)
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    statement = (
        update(IngestionJob)
        .where(IngestionJob.id == next_due)
        .values(
            status="running",
            attempts=IngestionJob.attempts + 1,
            locked_at=func.now(),
            lock_token=token,
            locked_by=worker_id,
            started_at=func.coalesce(IngestionJob.started_at, func.now()),
            finished_at=None,
        )
        .returning(
            IngestionJob.id,
            IngestionJob.workspace_id,
            IngestionJob.source_id,
            IngestionJob.job_type,
            IngestionJob.attempts,
            IngestionJob.max_attempts,
            IngestionJob.storage_key,
        )
        # Cross-workspace by design: the queue serves every workspace.
        .execution_options(**SKIP_WORKSPACE_SCOPE, synchronize_session=False)
    )
    with session_factory() as session:
        row = session.execute(statement).one_or_none()
        session.commit()
    if row is None:
        return None
    return ClaimedJob(
        id=row.id,
        workspace_id=row.workspace_id,
        source_id=row.source_id,
        job_type=row.job_type,
        attempts=row.attempts,
        max_attempts=row.max_attempts,
        lock_token=token,
        storage_key=row.storage_key,
    )


def renew_lease(connection: Connection, job: ClaimedJob) -> bool:
    """Heartbeat. False means the job is no longer ours (recovered, finished, or deleted)."""
    # Runs on a plain Connection (not an ORM session): matched by id and token only.
    result = connection.execute(
        update(IngestionJob)
        .where(
            IngestionJob.id == job.id,
            IngestionJob.lock_token == job.lock_token,
            IngestionJob.status == "running",
        )
        .values(locked_at=func.now())
    )
    return result.rowcount == 1


def lock_owned_job(db: Session, job: ClaimedJob) -> IngestionJob | None:
    """Lock the job row for a final write, if this worker still owns it (fencing)."""
    return db.scalars(
        select(IngestionJob)
        .where(
            IngestionJob.id == job.id,
            IngestionJob.lock_token == job.lock_token,
            IngestionJob.status == "running",
        )
        .with_for_update()
    ).one_or_none()


def has_other_queued_job(db: Session, row: IngestionJob) -> bool:
    # Filtered by the job's own workspace explicitly, so this is correct both in a
    # workspace-bound session and in the unbound recovery session.
    return (
        db.scalar(
            select(IngestionJob.id)
            .where(
                IngestionJob.workspace_id == row.workspace_id,
                IngestionJob.source_id == row.source_id,
                IngestionJob.status == "queued",
                IngestionJob.id != row.id,
            )
            .execution_options(**SKIP_WORKSPACE_SCOPE)
        )
        is not None
    )


def requeue_or_supersede(
    db: Session,
    row: IngestionJob,
    *,
    run_at: datetime | ColumnElement[datetime],
    message: str,
    count_attempt: bool,
) -> bool:
    """Put a running job back in the queue. If a newer job for the same source is
    already waiting, this one is obsolete: mark it failed as superseded instead.
    Returns True if requeued."""
    if has_other_queued_job(db, row):
        row.status = "failed"
        row.last_error = SUPERSEDED_MESSAGE
        row.finished_at = datetime.now(UTC)
        row.locked_at = row.lock_token = row.locked_by = None
        return False
    row.status = "queued"
    row.next_run_at = run_at
    row.last_error = message
    row.locked_at = row.lock_token = row.locked_by = None
    if not count_attempt:
        row.attempts = max(0, row.attempts - 1)
    return True


def recover_stale_jobs(session_factory: sessionmaker[Session], lease_seconds: int) -> int:
    """Requeue (or fail) running jobs whose lease lapsed: their worker died.

    Both the staleness cutoff and the requeue time use the database clock, the
    one `claim_next_job` compares against. With the worker's clock, a skew of
    a few milliseconds could leave a recovered job "not yet due".

    Each job is recovered in its own short transaction that locks the source
    first and then the job, the same order as the API and the worker, so
    recovery can't deadlock with an upload. Rows locked by someone else are
    skipped and picked up on the next pass.
    """
    cutoff = func.now() - timedelta(seconds=lease_seconds)
    with session_factory() as session:
        candidates = session.execute(
            select(IngestionJob.id, IngestionJob.source_id, IngestionJob.workspace_id)
            .where(IngestionJob.status == "running", IngestionJob.locked_at < cutoff)
            # Cross-workspace by design: recovery serves every workspace.
            .execution_options(**SKIP_WORKSPACE_SCOPE)
        ).all()
    recovered = 0
    for job_id, source_id, workspace_id in candidates:
        with session_factory() as session:
            source = session.scalars(
                select(Source)
                .where(Source.id == source_id, Source.workspace_id == workspace_id)
                .with_for_update(skip_locked=True)
                .execution_options(**SKIP_WORKSPACE_SCOPE)
            ).one_or_none()
            row = session.scalars(
                select(IngestionJob)
                .where(
                    IngestionJob.id == job_id,
                    IngestionJob.status == "running",
                    IngestionJob.locked_at < cutoff,
                )
                .with_for_update(skip_locked=True)
                .execution_options(**SKIP_WORKSPACE_SCOPE)
            ).one_or_none()
            if row is None or source is None:
                continue  # already handled, or locked by an API request right now
            if row.attempts >= row.max_attempts:
                row.status = "failed"
                row.last_error = ABANDONED_MESSAGE
                row.finished_at = datetime.now(UTC)
                row.locked_at = row.lock_token = row.locked_by = None
                if row.job_type == "delete":
                    # Don't strand the source in "deleting": let an owner delete it again.
                    source.status = "failed"
                    source.error_message = DELETE_ABANDONED_MESSAGE
                elif source.status == "deleting":
                    pass
                elif has_other_queued_job(session, row):
                    source.status = "pending"  # a newer request will run next
                else:
                    source.status = "failed"
                    source.error_message = ABANDONED_MESSAGE
            elif (
                requeue_or_supersede(
                    session, row, run_at=func.now(), message=RECOVERED_MESSAGE, count_attempt=True
                )
                and source.status != "deleting"
            ):
                source.status = "pending"
            session.commit()
            recovered += 1
            logger.warning(
                "Recovered stale job id=%s workspace=%s status=%s attempts=%d/%d",
                row.id,
                row.workspace_id,
                row.status,
                row.attempts,
                row.max_attempts,
            )
    return recovered


def engine_heartbeat(engine: Engine, job: ClaimedJob) -> bool:
    with engine.begin() as connection:
        return renew_lease(connection, job)
