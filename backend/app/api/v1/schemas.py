import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import AfterValidator, BaseModel, ConfigDict, EmailStr, Field, StringConstraints

# NIST SP 800-63B: length over composition rules; a cap keeps Argon2 input bounded.
PASSWORD_MIN_LENGTH = 12
PASSWORD_MAX_LENGTH = 128


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be only whitespace")
    return value


NewPassword = Annotated[
    str,
    Field(min_length=PASSWORD_MIN_LENGTH, max_length=PASSWORD_MAX_LENGTH),
    AfterValidator(_not_blank),
]
WorkspaceName = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)
]


class StrictRequest(BaseModel):
    # Unknown fields are rejected, so clients can't smuggle in e.g. "role" or "workspace_id".
    model_config = ConfigDict(extra="forbid")


class RegisterRequest(StrictRequest):
    email: EmailStr
    password: NewPassword
    workspace_name: WorkspaceName


class LoginRequest(StrictRequest):
    email: EmailStr
    password: str = Field(min_length=1, max_length=PASSWORD_MAX_LENGTH)


class WorkspaceUpdateRequest(StrictRequest):
    name: WorkspaceName


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    created_at: datetime


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    name: str
    role: str
    created_at: datetime


class SessionOut(BaseModel):
    user: UserOut
    workspace: WorkspaceOut


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    role: str
    joined_at: datetime


# --- sources and jobs (M4) -----------------------------------------------------------
# Storage keys and filesystem paths are deliberately absent from every response.


class JobOut(BaseModel):
    id: uuid.UUID
    source_id: uuid.UUID
    job_type: str
    status: str
    attempts: int
    max_attempts: int
    next_run_at: datetime
    last_error: str | None
    result: dict[str, Any]
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class JobSummaryOut(BaseModel):
    id: uuid.UUID
    job_type: str
    status: str
    last_error: str | None


class SourceOut(BaseModel):
    id: uuid.UUID
    kind: str
    display_name: str
    source_url: str | None
    settings: dict[str, Any]
    original_filename: str | None
    file_type: str | None
    file_size: int | None
    status: str
    error_message: str | None
    last_indexed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    document_count: int
    chunk_count: int
    latest_job: JobSummaryOut | None


class DocumentOut(BaseModel):
    id: uuid.UUID
    title: str
    canonical_url: str | None
    fetched_at: datetime | None
    content_hash: str
    chunk_count: int
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class SourceDetailOut(SourceOut):
    documents: list[DocumentOut]
    jobs: list[JobOut]
    # Website sources: crawl pages by status (indexed, unchanged, duplicate, skipped, failed).
    page_counts: dict[str, int]


class UploadOut(BaseModel):
    source: SourceOut
    job: JobOut | None
    unchanged: bool


# --- website sources (M5) ------------------------------------------------------------


class WebsiteIn(StrictRequest):
    url: str = Field(min_length=1, max_length=2048, description="Public http(s) URL to start at")
    # Optional, lower-only overrides of MAX_CRAWL_PAGES / MAX_CRAWL_DEPTH.
    max_pages: int | None = Field(default=None, ge=1)
    max_depth: int | None = Field(default=None, ge=0)


class WebsiteOut(BaseModel):
    source: SourceOut
    job: JobOut | None
    created: bool


class CrawlPageOut(BaseModel):
    id: uuid.UUID
    url: str
    final_url: str | None
    document_url: str | None
    depth: int
    discovered_via: str
    status: str
    reason: str | None
    message: str | None
    http_status: int | None
    content_type: str | None
    title: str | None
    content_hash: str | None
    in_latest_crawl: bool
    first_seen_at: datetime
    fetched_at: datetime | None
    indexed_at: datetime | None
    changed_at: datetime | None
