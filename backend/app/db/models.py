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
SOURCE_KINDS = ("website", "file")
# "deleting" extends SPEC.md's four states: deletion runs as a worker job.
SOURCE_STATUSES = ("pending", "processing", "ready", "failed", "deleting")
FILE_TYPES = ("pdf", "docx", "txt", "md")
JOB_TYPES = ("ingest", "reindex", "delete")
JOB_STATUSES = ("queued", "running", "succeeded", "failed")
CRAWL_PAGE_STATUSES = ("indexed", "unchanged", "duplicate", "skipped", "failed")
CRAWL_DISCOVERY = ("start", "sitemap", "link")


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
    """One uploaded file or one website root."""

    __tablename__ = "sources"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_sources_workspace_and_id"),
        CheckConstraint(f"kind IN {SOURCE_KINDS}", name="kind"),
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
    """Extracted text of one file (or one web page)."""

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
    __table_args__ = (PrimaryKeyConstraint("workspace_id", "content_hash", "model", "dimensions"),)

    content_hash: Mapped[str] = mapped_column(Text)
    model: Mapped[str] = mapped_column(Text)
    dimensions: Mapped[int]
    embedding: Mapped[Any] = mapped_column(Vector(SCHEMA_EMBEDDING_DIMENSIONS))
    created_at: Mapped[datetime] = _created_at()


# --- website crawling (Milestone 5) ------------------------------------------------------


class CrawlPage(WorkspaceScoped, Base):
    """The latest crawl outcome for one URL of a website source.

    Pages themselves are `documents` (keyed by `canonical_url`); this table is
    the crawl's own record: what was discovered, fetched, skipped, or failed,
    and why. A URL keeps one row across crawls; `last_job_id` tells whether
    the latest crawl saw it.
    """

    __tablename__ = "crawl_pages"
    __table_args__ = (
        ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["sources.workspace_id", "sources.id"],
            ondelete="CASCADE",
            name="fk_crawl_pages_source_same_workspace",
        ),
        UniqueConstraint("source_id", "url", name="uq_crawl_pages_source_url"),
        CheckConstraint(f"status IN {CRAWL_PAGE_STATUSES}", name="status"),
        CheckConstraint(f"discovered_via IN {CRAWL_DISCOVERY}", name="discovered_via"),
        CheckConstraint("depth >= 0", name="depth"),
        Index("ix_crawl_pages_workspace_source_status", "workspace_id", "source_id", "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_id: Mapped[uuid.UUID]
    # Normalised URL as discovered (before redirects).
    url: Mapped[str] = mapped_column(Text)
    final_url: Mapped[str | None] = mapped_column(Text)
    # The document this page is indexed as (documents.canonical_url), if any.
    document_url: Mapped[str | None] = mapped_column(Text)
    depth: Mapped[int]
    discovered_via: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    # Machine-readable cause for skipped/failed/duplicate pages (e.g. "robots", "not_found").
    reason: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    http_status: Mapped[int | None]
    content_type: Mapped[str | None] = mapped_column(Text)
    title: Mapped[str | None] = mapped_column(Text)
    content_hash: Mapped[str | None] = mapped_column(Text)
    last_job_id: Mapped[uuid.UUID | None]
    first_seen_at: Mapped[datetime] = _created_at()
    fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # When the page's chunks were last (re)built, and when its text last changed.
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    changed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = _updated_at()
