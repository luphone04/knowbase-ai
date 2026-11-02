import uuid

from fastapi import APIRouter

from app.api.dependencies import CurrentAuth, DbSession
from app.api.v1.schemas import JobOut
from app.api.v1.sources import job_out
from app.core.errors import AppError
from app.db.models import IngestionJob

router = APIRouter(prefix="/jobs", tags=["jobs"])


@router.get("/{job_id}")
def get_job(job_id: uuid.UUID, _: CurrentAuth, db: DbSession) -> JobOut:
    """Status, attempts, counts, and a safe error message. Poll while queued or running."""
    job = db.get(IngestionJob, job_id)  # scoped to the caller's workspace
    if job is None:
        raise AppError(404, "not_found", "Job not found.")
    return job_out(job)
