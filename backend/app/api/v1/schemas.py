import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    StringConstraints,
    field_validator,
)

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
    content_hash: str
    chunk_count: int
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime


class SourceDetailOut(SourceOut):
    documents: list[DocumentOut]
    jobs: list[JobOut]


class UploadOut(BaseModel):
    source: SourceOut
    job: JobOut | None
    unchanged: bool


# --- retrieval (M6) ---------------------------------------------------------------------


class RetrievalSearchIn(StrictRequest):
    query: str = Field(min_length=1, max_length=1000)
    mode: Literal["vector", "hybrid"] = "hybrid"
    # The previous user question, for short follow-ups ("and the warranty?").
    previous_question: str | None = Field(default=None, max_length=1000)

    @field_validator("query")
    @classmethod
    def _query_not_blank(cls, value: str) -> str:
        return _not_blank(value)


class RetrievalHitOut(BaseModel):
    rank: int
    score: float  # vector mode: cosine distance; hybrid: RRF score
    vector_rank: int | None
    vector_distance: float | None
    lexical_rank: int | None
    lexical_score: float | None  # ts_rank_cd (PostgreSQL ranking, not BM25)
    chunk_id: uuid.UUID
    chunk_index: int
    content: str
    heading: str | None
    page_start: int | None
    page_end: int | None
    document_id: uuid.UUID
    document_title: str
    source_id: uuid.UUID
    source_name: str
    filename: str | None


class RetrievalSearchOut(BaseModel):
    mode: str
    query: str
    retrieval_query: str
    contextualized: bool
    trace_id: uuid.UUID
    latency_ms: int
    vector_candidates: int
    lexical_candidates: int
    results: list[RetrievalHitOut]


class TraceOut(BaseModel):
    id: uuid.UUID
    retrieval_mode: str
    query_text: str
    retrieved_chunk_ids: list[str]
    retrieval_scores: dict[str, Any]
    latency_ms: int
    token_usage: dict[str, Any]
    created_at: datetime
