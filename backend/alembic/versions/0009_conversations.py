"""Add conversations and messages, and link query_traces to them (SPEC.md §6.1, M7).

Every key into these tables is tenant-safe: a conversation references its
owner's membership (workspace_id, user_id), a message its conversation, and a
trace its conversation and message, each through (workspace_id, id). Existing
traces (M6 searches) have NULL ids and are unaffected.

Revision ID: 0009_conversations
Revises: 0008_embedding_cache_provenance
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_conversations"
down_revision: str | None = "0008_embedding_cache_provenance"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "char_length(btrim(title)) BETWEEN 1 AND 200",
            name=op.f("ck_conversations_title_length"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["workspace_members.workspace_id", "workspace_members.user_id"],
            name="fk_conversations_member_same_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_conversations_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_conversations_workspace_and_id"),
    )
    op.create_index(
        "ix_conversations_workspace_user_updated",
        "conversations",
        ["workspace_id", "user_id", "updated_at"],
        unique=False,
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("abstain_reason", sa.Text(), nullable=True),
        sa.Column(
            "citations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "token_usage",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "CASE WHEN outcome = 'abstained' "
            "THEN coalesce(abstain_reason IN "
            "('no_evidence', 'model_insufficient', 'no_valid_citation'), false) "
            "ELSE abstain_reason IS NULL END",
            name=op.f("ck_messages_abstain_reason"),
        ),
        sa.CheckConstraint(
            "CASE role WHEN 'user' THEN outcome IS NULL "
            "WHEN 'assistant' THEN coalesce(outcome IN ('answered', 'abstained'), false) "
            "ELSE true END",
            name=op.f("ck_messages_outcome"),
        ),
        sa.CheckConstraint("role IN ('user', 'assistant')", name=op.f("ck_messages_role")),
        sa.CheckConstraint("char_length(content) >= 1", name=op.f("ck_messages_content")),
        sa.CheckConstraint(
            "latency_ms IS NULL OR latency_ms >= 0", name=op.f("ck_messages_latency_ms")
        ),
        sa.CheckConstraint("position >= 1", name=op.f("ck_messages_position")),
        sa.ForeignKeyConstraint(
            ["workspace_id", "conversation_id"],
            ["conversations.workspace_id", "conversations.id"],
            name="fk_messages_conversation_same_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_messages_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint(
            "conversation_id", "position", name="uq_messages_conversation_position"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_messages_workspace_and_id"),
    )
    op.create_index(
        "ix_messages_conversation_created",
        "messages",
        ["conversation_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_query_traces_conversation_id", "query_traces", ["conversation_id"], unique=False
    )
    op.create_index("ix_query_traces_message_id", "query_traces", ["message_id"], unique=False)
    op.create_foreign_key(
        "fk_query_traces_message_same_workspace",
        "query_traces",
        "messages",
        ["workspace_id", "message_id"],
        ["workspace_id", "id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_query_traces_conversation_same_workspace",
        "query_traces",
        "conversations",
        ["workspace_id", "conversation_id"],
        ["workspace_id", "id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_query_traces_conversation_same_workspace", "query_traces", type_="foreignkey"
    )
    op.drop_constraint("fk_query_traces_message_same_workspace", "query_traces", type_="foreignkey")
    op.drop_index("ix_query_traces_message_id", table_name="query_traces")
    op.drop_index("ix_query_traces_conversation_id", table_name="query_traces")
    op.drop_index("ix_messages_conversation_created", table_name="messages")
    op.drop_table("messages")
    op.drop_index("ix_conversations_workspace_user_updated", table_name="conversations")
    op.drop_table("conversations")
