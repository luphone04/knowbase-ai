"""Add crawl_pages: per-URL crawl outcomes for website sources.

Website pages themselves reuse `documents` (one per canonical URL) and
`chunks`; nothing in the M4 tables changes.

Revision ID: 0004_crawler
Revises: 0003_ingestion
Create Date: 2026-11-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_crawler"
down_revision: str | None = "0003_ingestion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index("ix_crawl_pages_workspace_source_status", table_name="crawl_pages")
    op.drop_table("crawl_pages")
