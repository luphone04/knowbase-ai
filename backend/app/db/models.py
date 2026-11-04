import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    MetaData,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    func,
    text,
    true,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.config import SCHEMA_EMBEDDING_DIMENSIONS

# Stable constraint names, so Alembic autogenerate produces reviewable diffs.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

WORKSPACE_ROLES = ("owner", "admin", "member")
# Ingestion methods. A new source type adds its kind here, a worker handler
# (IngestionWorker._handlers), and a migration widening ck_sources_kind.
SOURCE_KINDS = ("file",)
# "deleting" extends SPEC.md's four states: deletion runs as a worker job.
SOURCE_STATUSES = ("pending", "processing", "ready", "failed", "deleting")
FILE_TYPES = ("pdf", "docx", "txt", "md")
JOB_TYPES = ("ingest", "reindex", "delete")
JOB_STATUSES = ("queued", "running", "succeeded", "failed")
RETRIEVAL_MODES = ("vector", "hybrid")
MESSAGE_ROLES = ("user", "assistant")
# An assistant message either answered with validated citations or abstained.
MESSAGE_OUTCOMES = ("answered", "abstained")
# Why it abstained: nothing usable was retrieved; the model said the excerpts were
# not enough; or the answer had no valid citation, so it was not shown.
ABSTAIN_REASONS = ("no_evidence", "model_insufficient", "no_valid_citation")


def _sql_in(column: str, values: tuple[str, ...]) -> str:
    """`column IN ('a', 'b')`. (A Python tuple renders a one-item list as ('a',).)"""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        primary_key=True, default=uuid.uuid4, server_default=text("gen_random_uuid()")
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


def _updated_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


def _jsonb() -> Mapped[dict[str, Any]]:
    return mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))


class WorkspaceScoped:
    """Rows owned by exactly one workspace.

    ORM queries on these models are filtered to the session's bound workspace
    automatically; see app.db.tenancy.
    """

    workspace_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("workspaces.id", ondelete="CASCADE"))


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # The application lower-cases emails; the database refuses anything else.
        CheckConstraint("email = lower(btrim(email))", name="email_normalized"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(Text, unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    is_active: Mapped[bool] = mapped_column(default=True, server_default=true())


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint("char_length(btrim(name)) BETWEEN 1 AND 100", name="name_length"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    owner_user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = _created_at()


class WorkspaceMember(WorkspaceScoped, Base):
    __tablename__ = "workspace_members"
    __table_args__ = (
        PrimaryKeyConstraint("workspace_id", "user_id"),
        CheckConstraint(f"role IN {WORKSPACE_ROLES}", name="role"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()


class RevokedToken(Base):
    """Access tokens ended by logout before they expire. Rows can go once expired."""

    __tablename__ = "revoked_tokens"

    jti: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime] = _created_at()


class RateLimitCounter(Base):
    """Fixed-window request counters. Keys are HMACs, never raw emails or IPs."""

    __tablename__ = "rate_limit_counters"
    __table_args__ = (
        PrimaryKeyConstraint("bucket_key", "window_start"),
        Index("ix_rate_limit_counters_window_start", "window_start"),
    )

    bucket_key: Mapped[str] = mapped_column(Text)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    count: Mapped[int]


# --- ingestion (Milestone 4) -----------------------------------------------------------
# Child tables reference their parent through (workspace_id, parent_id), so the
# database itself refuses a row that points into another workspace.


class Source(WorkspaceScoped, Base):
    """One ingested source: today, one uploaded file (`kind` "file")."""

    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_sources_workspace_and_id"),
        CheckConstraint(_sql_in("kind", SOURCE_KINDS), name="kind"),
        CheckConstraint(f"status IN {SOURCE_STATUSES}", name="status"),
        CheckConstraint(f"file_type IS NULL OR file_type IN {FILE_TYPES}", name="file_type"),
        # One source per file name (case-insensitive) per workspace: uploading the
        # same name again is a new version of that source, not a duplicate.
        Index(
            "uq_sources_workspace_dedupe_key",
            "workspace_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    # Unused by file sources; kept for a future source type that has a location.
    source_url: Mapped[str | None] = mapped_column(Text)
    original_filename: Mapped[str | None] = mapped_column(Text)
    # Generated by the server (UUIDs only); never derived from the upload's name.
    storage_key: Mapped[str | None] = mapped_column(Text)
    file_type: Mapped[str | None] = mapped_column(Text)
    file_size: Mapped[int | None]
    file_sha256: Mapped[str | None] = mapped_column(Text)
    dedupe_key: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="pending", server_default="pending")
    last_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    settings: Mapped[dict[str, Any]] = _jsonb()
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Document(WorkspaceScoped, Base):
    """Extracted text of one file. A source may hold several documents in future,
    keyed by `canonical_url` (NULL for a file's single document)."""

    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_documents_workspace_and_id"),
        ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["sources.workspace_id", "sources.id"],
            ondelete="CASCADE",
            name="fk_documents_source_same_workspace",
        ),
        Index(
            "uq_documents_source_canonical_url",
            "source_id",
            "canonical_url",
            unique=True,
            postgresql_where=text("canonical_url IS NOT NULL"),
        ),
        # Uploaded files: exactly one document per source, so re-ingesting can't duplicate it.
        Index(
            "uq_documents_one_per_file_source",
            "source_id",
            unique=True,
            postgresql_where=text("canonical_url IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_id: Mapped[uuid.UUID]
    title: Mapped[str] = mapped_column(Text)
    canonical_url: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text)
    text_content: Mapped[str] = mapped_column(Text)
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Chunk(WorkspaceScoped, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "document_id"],
            ["documents.workspace_id", "documents.id"],
            ondelete="CASCADE",
            name="fk_chunks_document_same_workspace",
        ),
        UniqueConstraint("document_id", "chunk_index", name="uq_chunks_document_chunk_index"),
        Index("ix_chunks_workspace_document", "workspace_id", "document_id"),
        Index(
            "ix_chunks_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
        Index("ix_chunks_search_tsv", "search_tsv", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    document_id: Mapped[uuid.UUID]
    chunk_index: Mapped[int]
    content: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(Text)
    # Unit-length RETRIEVAL_DOCUMENT vector; pgvector returns a numpy array.
    embedding: Mapped[Any] = mapped_column(Vector(SCHEMA_EMBEDDING_DIMENSIONS))
    # Stored per chunk so vectors from different models are never compared.
    embedding_model: Mapped[str] = mapped_column(Text)
    search_tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', coalesce(content, ''))", persisted=True)
    )
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = _created_at()


class IngestionJob(WorkspaceScoped, Base):
    """A durable unit of worker work. PostgreSQL is the queue (SPEC.md §4.2)."""

    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["sources.workspace_id", "sources.id"],
            ondelete="CASCADE",
            name="fk_ingestion_jobs_source_same_workspace",
        ),
        CheckConstraint(f"job_type IN {JOB_TYPES}", name="job_type"),
        CheckConstraint(f"status IN {JOB_STATUSES}", name="status"),
        CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="attempts"),
        Index("ix_ingestion_jobs_status_next_run_at", "status", "next_run_at", "created_at"),
        # At most one waiting job per source; a newer upload updates the waiting job.
        Index(
            "uq_ingestion_jobs_one_queued_per_source",
            "source_id",
            unique=True,
            postgresql_where=text("status = 'queued'"),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_id: Mapped[uuid.UUID]
    job_type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="queued", server_default="queued")
    attempts: Mapped[int] = mapped_column(default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(default=3, server_default="3")
    next_run_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Fencing token: rotated on every claim, so a stale worker can't commit results.
    lock_token: Mapped[uuid.UUID | None]
    locked_by: Mapped[str | None] = mapped_column(Text)
    # The stored upload this job processes (None for delete jobs).
    storage_key: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any]] = _jsonb()
    created_at: Mapped[datetime] = _created_at()
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmbeddingCacheEntry(WorkspaceScoped, Base):
    """A computed document embedding, reused for identical text in the same workspace.

    Written as soon as each batch is embedded, so finished work survives a failed
    or deferred job and costs no Gemini quota the second time.
    """

    __tablename__ = "embedding_cache"
    __table_args__ = (
        PrimaryKeyConstraint("workspace_id", "content_hash", "model", "dimensions"),
        Index("ix_embedding_cache_workspace_source", "workspace_id", "source_id"),
    )

    # The source whose job first embedded this text (provenance, not ownership:
    # other sources may reuse the row). Deleting that source removes the row
    # once no chunk uses it. NULL for rows created before migration 0008.
    source_id: Mapped[uuid.UUID | None]
    content_hash: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    dimensions: Mapped[int]
    embedding: Mapped[Any] = mapped_column(Vector(SCHEMA_EMBEDDING_DIMENSIONS))
    created_at: Mapped[datetime] = _created_at()


# --- retrieval (Milestone 6) --------------------------------------------------------------


class QueryTrace(WorkspaceScoped, Base):
    """One retrieval run: the query, what came back, the ranks and scores, and timing.

    Written for every search (SPEC.md §6.1 `query_traces`). A chat turn's trace
    also names its conversation and, once the turn is saved, the assistant message
    that used the evidence. A turn that failed at Gemini keeps a trace without one.
    """

    __tablename__ = "query_traces"
    __table_args__ = (
        CheckConstraint(_sql_in("retrieval_mode", RETRIEVAL_MODES), name="retrieval_mode"),
        CheckConstraint("latency_ms >= 0", name="latency_ms"),
        Index("ix_query_traces_workspace_created", "workspace_id", "created_at"),
        ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["conversations.workspace_id", "conversations.id"],
            ondelete="CASCADE",
            name="fk_query_traces_conversation_same_workspace",
        ),
        ForeignKeyConstraint(
            ["workspace_id", "message_id"],
            ["messages.workspace_id", "messages.id"],
            ondelete="CASCADE",
            name="fk_query_traces_message_same_workspace",
        ),
        Index("ix_query_traces_conversation_id", "conversation_id"),
        Index("ix_query_traces_message_id", "message_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID | None]
    message_id: Mapped[uuid.UUID | None]
    retrieval_mode: Mapped[str] = mapped_column(Text)
    # The text actually retrieved with (a follow-up includes the previous question).
    query_text: Mapped[str] = mapped_column(Text)
    retrieved_chunk_ids: Mapped[list[str]] = mapped_column(JSONB)
    # Per-list ranks and raw scores, the fused scores, and the settings used.
    retrieval_scores: Mapped[dict[str, Any]] = mapped_column(JSONB)
    latency_ms: Mapped[int]
    token_usage: Mapped[dict[str, Any]] = _jsonb()
    created_at: Mapped[datetime] = _created_at()


# --- chat (Milestone 7) ---------------------------------------------------------------------


class Conversation(WorkspaceScoped, Base):
    """One user's chat in one workspace. Only that user can read or continue it."""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_conversations_workspace_and_id"),
        # The owner must be a member of the workspace; leaving it removes their chats.
        ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspace_members.workspace_id", "workspace_members.user_id"],
            ondelete="CASCADE",
            name="fk_conversations_member_same_workspace",
        ),
        CheckConstraint("char_length(btrim(title)) BETWEEN 1 AND 200", name="title_length"),
        Index("ix_conversations_workspace_user_updated", "workspace_id", "user_id", "updated_at"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID]
    title: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Message(WorkspaceScoped, Base):
    """A question (`user`) or an answer or abstention (`assistant`).

    `position` orders a conversation's messages (1, 2, ...): a question and its
    answer are saved together, so their timestamps can be equal.
    """

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_messages_workspace_and_id"),
        ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["conversations.workspace_id", "conversations.id"],
            ondelete="CASCADE",
            name="fk_messages_conversation_same_workspace",
        ),
        UniqueConstraint("conversation_id", "position", name="uq_messages_conversation_position"),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        CheckConstraint(_sql_in("role", MESSAGE_ROLES), name="role"),
        CheckConstraint("position >= 1", name="position"),
        CheckConstraint("char_length(content) >= 1", name="content"),
        # CASE and coalesce keep these checks from evaluating to NULL, which would pass.
        CheckConstraint(
            "CASE role WHEN 'user' THEN outcome IS NULL "
            f"WHEN 'assistant' THEN coalesce({_sql_in('outcome', MESSAGE_OUTCOMES)}, false) "
            "ELSE true END",
            name="outcome",
        ),
        CheckConstraint(
            "CASE WHEN outcome = 'abstained' "
            f"THEN coalesce({_sql_in('abstain_reason', ABSTAIN_REASONS)}, false) "
            "ELSE abstain_reason IS NULL END",
            name="abstain_reason",
        ),
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latency_ms"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    conversation_id: Mapped[uuid.UUID]
    position: Mapped[int]
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(Text)
    abstain_reason: Mapped[str | None] = mapped_column(Text)
    # Validated citations only: id ("S1"), chunk, document, source, file, pages, excerpt.
    citations: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, default=list, server_default=text("'[]'::jsonb")
    )
    token_usage: Mapped[dict[str, Any]] = _jsonb()
    latency_ms: Mapped[int | None]
    # Retrieval mode, trace id, model, and what citation validation removed.
    meta: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = _created_at()
