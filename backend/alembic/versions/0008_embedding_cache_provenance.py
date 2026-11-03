"""Record which source first embedded each cached vector (audit M4).

Deleting a source then also removes the cache rows its jobs created, even for
text that was never published (an older version, or batches of a job that
failed), once no chunk uses them. Rows written before this migration keep
source_id NULL and are still removed by content hash as before.

No foreign key: the column is provenance, not ownership. Other sources may
reuse the row, and it must outlive the source while a chunk still uses it.

Revision ID: 0008_embedding_cache_provenance
Revises: 0007_query_traces
Create Date: 2026-11-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_embedding_cache_provenance"
down_revision: str | None = "0007_query_traces"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("embedding_cache", sa.Column("source_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_embedding_cache_workspace_source",
        "embedding_cache",
        ["workspace_id", "source_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_embedding_cache_workspace_source", table_name="embedding_cache")
    op.drop_column("embedding_cache", "source_id")
