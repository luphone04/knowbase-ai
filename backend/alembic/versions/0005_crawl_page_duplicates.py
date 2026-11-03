"""Separate a page's own document from the one it duplicates.

`crawl_pages.document_url` now always means the document the page owns.
Duplicates point at the other page's document through the new
`duplicate_of`, so removing a duplicate can never delete that document.

Revision ID: 0005_crawl_page_duplicates
Revises: 0004_crawler
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_crawl_page_duplicates"
down_revision: str | None = "0004_crawler"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("crawl_pages", sa.Column("duplicate_of", sa.Text(), nullable=True))
    op.execute(
        "UPDATE crawl_pages SET duplicate_of = document_url, document_url = NULL "
        "WHERE status = 'duplicate'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE crawl_pages SET document_url = duplicate_of "
        "WHERE status = 'duplicate' AND document_url IS NULL"
    )
    op.drop_column("crawl_pages", "duplicate_of")
