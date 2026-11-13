import re
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


_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _no_control_characters(value: str | None) -> str | None:
    # PostgreSQL text can't hold NUL, and no question needs control characters.
    # Rejected here, before a daily slot or a Gemini call is spent.
    if value is not None and _CONTROL_CHARACTERS.search(value):
        raise ValueError("must not contain control characters")
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
        _no_control_characters(value)
        return _not_blank(value)

    @field_validator("previous_question")
    @classmethod
    def _previous_question_is_text(cls, value: str | None) -> str | None:
        return _no_control_characters(value)


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


# --- conversations and chat (M7) ---------------------------------------------------------------


class ConversationCreateIn(StrictRequest):
    title: str | None = Field(default=None, max_length=200)

    @field_validator("title")
    @classmethod
    def _title_is_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _no_control_characters(value)
        return _not_blank(value).strip()


class MessageIn(StrictRequest):
    content: str = Field(min_length=1, max_length=1000)
    mode: Literal["vector", "hybrid"] = "hybrid"

    @field_validator("content")
    @classmethod
    def _content_not_blank(cls, value: str) -> str:
        _no_control_characters(value)
        return _not_blank(value)


class CitationOut(BaseModel):
    """A validated citation: the id used in the answer and the excerpt it points to."""

    id: str
    rank: int
    chunk_id: uuid.UUID
    chunk_index: int
    document_id: uuid.UUID
    document_title: str
    source_id: uuid.UUID
    source_name: str
    filename: str | None
    heading: str | None
    page_start: int | None
    page_end: int | None
    excerpt: str


class MessageOut(BaseModel):
    id: uuid.UUID
    position: int
    role: str
    content: str
    outcome: str | None  # assistant only: "answered" or "abstained"
    abstain_reason: str | None  # "no_evidence", "model_insufficient", "no_valid_citation"
    citations: list[CitationOut]
    token_usage: dict[str, Any]
    # Assistant only: tokens and estimated cost (app.chat.usage.message_usage); None when
    # the reply has no usage record.
    usage: dict[str, Any] | None = None
    latency_ms: int | None
    metadata: dict[str, Any]
    created_at: datetime


class ConversationUsageOut(BaseModel):
    """A conversation's tokens and estimated cost (app.chat.usage.conversation_usage)."""

    conversation_id: uuid.UUID
    replies: int  # assistant replies in the conversation
    replies_with_usage: int  # of those, the ones with a usage record
    failed_questions: int  # questions that failed at Gemini (no reply saved)
    pricing: dict[str, Any] | None  # tier, date, and source of the most recent snapshot
    embedding: dict[str, Any]
    generation: dict[str, Any]
    total: dict[str, Any]
    issues: list[dict[str, Any]]


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationDetailOut(ConversationOut):
    messages: list[MessageOut]


class TurnRetrievalOut(BaseModel):
    mode: str
    retrieval_query: str
    contextualized: bool
    trace_id: uuid.UUID
    results: int


class ChatTurnOut(BaseModel):
    conversation: ConversationOut
    user_message: MessageOut
    assistant_message: MessageOut
    retrieval: TurnRetrievalOut


# --- evaluation (M9, ADR 0014) ---------------------------------------------------------------


class EvaluationRunIn(StrictRequest):
    # Production modes to answer with (one generation per question each); [] = retrieval only.
    answer_modes: list[Literal["vector", "hybrid"]] = Field(default=["hybrid"], max_length=2)

    @field_validator("answer_modes")
    @classmethod
    def _unique_modes(cls, modes: list[str]) -> list[str]:
        if len(set(modes)) != len(modes):
            raise ValueError("each answer mode at most once")
        return [mode for mode in ("vector", "hybrid") if mode in modes]


class EvidenceOut(BaseModel):
    source_key: str
    quote: str
    page: int | None = None  # PDF page (passage datasets)
    section: str | None = None


class EvaluationQuestionOut(BaseModel):
    id: uuid.UUID
    key: str
    position: int
    category: str
    question: str
    previous_question: str | None
    answerable: bool
    expected_source_keys: list[str]
    expected_answer: str | None
    evidence: list[EvidenceOut]
    absent_terms: list[str]
    notes: str | None
    split: str | None = None  # "dev" or "heldout" (Asia Cement)
    required_facts: list[str] = Field(default_factory=list)


class DatasetOut(BaseModel):
    id: str = "northstar-demo"
    name: str
    version: str
    sha256: str
    description: str
    questions: int  # what one run evaluates
    answerable: int
    unanswerable: int
    source_keys: list[str]
    source_files: dict[str, str] | None = None  # key -> original file name (if not the key)
    relevance: Literal["source", "passage"] = "source"
    split: str | None = None  # the split a run evaluates (None: every question)
    held_out_questions: int = 0  # never run automatically
    # Passage datasets, once the corpus is ready: evidence quotations mapped to chunks.
    evidence_mapping: dict[str, Any] | None = None


class CorpusSourceOut(BaseModel):
    key: str
    status: str  # ready, missing, not_ready, content_differs
    chunks: int
    chunker: str | None = None  # the chunking strategy fingerprint its chunks were made with


class OtherSourceOut(BaseModel):
    name: str
    chunks: int
    status: str
    chunker: str | None = None


class CorpusOut(BaseModel):
    ready: bool
    sources: list[CorpusSourceOut]
    other_sources: list[OtherSourceOut]
    total_chunks: int
    indexing: int


class EvaluationLimitsOut(BaseModel):
    """What one run would use, and what is left today (UTC) in each budget."""

    query_embeddings_per_run: int
    generations_per_answer_mode: int
    runs_left: int
    query_embeddings_left: int
    generations_left: int
    question_interval_seconds: float


class EvaluationOverviewOut(BaseModel):
    dataset: DatasetOut
    corpus: CorpusOut
    limits: EvaluationLimitsOut
    questions: list[EvaluationQuestionOut]


class EvaluationRunOut(BaseModel):
    id: uuid.UUID
    status: str  # queued, running, succeeded, failed
    dataset_name: str
    dataset_version: str
    dataset_sha256: str
    questions_total: int
    questions_done: int
    answer_modes: list[str]
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    config: dict[str, Any]
    corpus: dict[str, Any]
    summary: dict[str, Any]


class EvaluationResultOut(BaseModel):
    variant: str
    retrieval_mode: str
    retrieval_query: str
    retrieved: list[dict[str, Any]]
    retrieved_sources: list[dict[str, Any]]
    vector_candidates: int | None
    lexical_candidates: int | None
    top_vector_distance: float | None
    hit_at_5: bool | None
    recall_at_5: float | None
    reciprocal_rank: float | None
    embedding_latency_ms: int | None
    retrieval_latency_ms: int | None
    answer_evaluated: bool
    outcome: str | None  # answered, abstained, provider_error
    abstain_reason: str | None
    answer: str | None
    citations: list[dict[str, Any]]
    citation_precision: float | None
    citation_recall: float | None
    abstention_correct: bool | None
    answer_latency_ms: int | None
    token_usage: dict[str, Any]
    details: dict[str, Any]
    error_stage: str | None
    error_category: str | None
    error_message: str | None


class EvaluationQuestionResultOut(BaseModel):
    question: EvaluationQuestionOut
    results: list[EvaluationResultOut]  # one per configuration evaluated so far


class EvaluationRunDetailOut(EvaluationRunOut):
    questions: list[EvaluationQuestionResultOut]
