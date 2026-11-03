"""Retire website crawling: drop crawl_pages and allow only file sources.

Owner decision 2026-11-03 (ADR 0008): Knowbase AI ingests uploaded files
only. The generic `sources.kind` column stays, so a future source type adds
its value in a new migration. The nullable `sources.source_url`,
`documents.canonical_url`, and `documents.fetched_at` columns stay too.

**This migration never deletes data silently.** If any website source or
crawl record exists, it stops with instructions. Delete those sources first
(DELETE /api/v1/sources/{id}), or purge them explicitly:

    alembic -x purge_website_sources=true upgrade head

The purge removes only website sources and what cascades from them (their
documents, chunks, crawl records, and jobs), plus cached embeddings that no
remaining chunk uses. File sources, documents, chunks, uploads, and their
cached embeddings are untouched.

Downgrade restores the website kind and an empty crawl_pages table; purged
website data is not restored (keep a database backup).

Revision ID: 0006_retire_website_crawling
Revises: 0005_crawl_page_duplicates
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0006_retire_website_crawling"
down_revision: str | None = "0005_crawl_page_duplicates"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PURGE_FLAG = "purge_website_sources"


def _purge_requested() -> bool:
    value = context.get_x_argument(as_dictionary=True).get(PURGE_FLAG, "")
    return value.strip().lower() in ("1", "true", "yes")


def upgrade() -> None:
    bind = op.get_bind()
    websites = bind.scalar(sa.text("SELECT count(*) FROM sources WHERE kind <> 'file'"))
    crawl_rows = bind.scalar(sa.text("SELECT count(*) FROM crawl_pages"))
    if (websites or crawl_rows) and not _purge_requested():
        raise RuntimeError(
            f"Refusing to retire website crawling: {websites} website source(s) and "
            f"{crawl_rows} crawl record(s) still exist. Delete those sources through "
            "DELETE /api/v1/sources/{id} first, or remove them explicitly with "
            f"`alembic -x {PURGE_FLAG}=true upgrade head` (back up the database first). "
            "File data is never touched."
        )
    if websites:
        # Cached vectors used only by website chunks go; any shared with a file stay.
        op.execute(
            """
            DELETE FROM embedding_cache e
            WHERE EXISTS (
                SELECT 1 FROM chunks c
                JOIN documents d ON d.id = c.document_id
                JOIN sources s ON s.id = d.source_id
                WHERE s.kind <> 'file'
                  AND c.workspace_id = e.workspace_id AND c.content_hash = e.content_hash
            )
            AND NOT EXISTS (
                SELECT 1 FROM chunks c
                JOIN documents d ON d.id = c.document_id
                JOIN sources s ON s.id = d.source_id
                WHERE s.kind = 'file'
                  AND c.workspace_id = e.workspace_id AND c.content_hash = e.content_hash
            )
            """
        )
        # Cascades to their documents, chunks, crawl records, and jobs.
        op.execute("DELETE FROM sources WHERE kind <> 'file'")

    op.drop_index("ix_crawl_pages_workspace_source_status", table_name="crawl_pages")
    op.drop_table("crawl_pages")
    op.drop_constraint(op.f("ck_sources_kind"), "sources", type_="check")
    op.create_check_constraint(op.f("ck_sources_kind"), "sources", "kind IN ('file')")


def downgrade() -> None:
    op.drop_constraint(op.f("ck_sources_kind"), "sources", type_="check")
    op.create_check_constraint(op.f("ck_sources_kind"), "sources", "kind IN ('website', 'file')")
    # crawl_pages as left by 0004 and 0005 (empty).
    op.create_table(
        "crawl_pages",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source_id", sa.Uuid(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("document_url", sa.Text(), nullable=True),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column("discovered_via", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("content_type", sa.Text(), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.Text(), nullable=True),
        sa.Column("last_job_id", sa.Uuid(), nullable=True),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("duplicate_of", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "discovered_via IN ('start', 'sitemap', 'link')",
            name=op.f("ck_crawl_pages_discovered_via"),
        ),
        sa.CheckConstraint(
            "status IN ('indexed', 'unchanged', 'duplicate', 'skipped', 'failed')",
            name=op.f("ck_crawl_pages_status"),
        ),
        sa.CheckConstraint("depth >= 0", name=op.f("ck_crawl_pages_depth")),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["sources.workspace_id", "sources.id"],
            name="fk_crawl_pages_source_same_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_crawl_pages_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_crawl_pages")),
        sa.UniqueConstraint("source_id", "url", name="uq_crawl_pages_source_url"),
    )
    op.create_index(
        "ix_crawl_pages_workspace_source_status",
        "crawl_pages",
        ["workspace_id", "source_id", "status"],
        unique=False,
    )
