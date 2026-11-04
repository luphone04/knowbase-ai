"""Add evaluation questions, runs, and results (SPEC.md §6.1, M9, ADR 0014).

Questions are a shared, versioned dataset (no workspace). Runs and results are
workspace-owned: a result references its run through (workspace_id, id), so it
can never point at another workspace's run. A partial unique index allows one
active run per workspace. Only new tables: existing data is untouched.

Revision ID: 0010_evaluations
Revises: 0009_conversations
Create Date: 2026-11-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010_evaluations"
down_revision: str | None = "0009_conversations"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "evaluation_questions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("dataset_name", sa.Text(), nullable=False),
        sa.Column("dataset_version", sa.Text(), nullable=False),
        sa.Column("dataset_sha256", sa.Text(), nullable=False),
        sa.Column("question_key", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("previous_question", sa.Text(), nullable=True),
        sa.Column("expected_answer", sa.Text(), nullable=True),
        sa.Column(
            "relevant_source_keys",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("is_unanswerable", sa.Boolean(), nullable=False),
        sa.Column(
            "reference",
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
        sa.CheckConstraint(
            "category IN ('direct', 'paraphrase', 'exact_term', 'negative_policy', 'multi_source', "
            "'follow_up', 'out_of_scope', 'absent_detail')",
            name=op.f("ck_evaluation_questions_category"),
        ),
        sa.CheckConstraint(
            "is_unanswerable = (jsonb_array_length(relevant_source_keys) = 0)",
            name=op.f("ck_evaluation_questions_unanswerable_has_no_sources"),
        ),
        sa.CheckConstraint("position >= 1", name=op.f("ck_evaluation_questions_position")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluation_questions")),
        sa.UniqueConstraint(
            "dataset_name", "dataset_version", "question_key", name="uq_evaluation_questions_key"
        ),
    )
    op.create_table(
        "evaluation_runs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=True),
        sa.Column("dataset_name", sa.Text(), nullable=False),
        sa.Column("dataset_version", sa.Text(), nullable=False),
        sa.Column("dataset_sha256", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="queued", nullable=False),
        sa.Column(
            "config",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "corpus",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("questions_total", sa.Integer(), nullable=False),
        sa.Column("questions_done", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "summary",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "next_run_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lock_token", sa.Uuid(), nullable=True),
        sa.Column("locked_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name=op.f("ck_evaluation_runs_status"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_evaluation_runs_attempts")),
        sa.CheckConstraint(
            "questions_done >= 0 AND questions_done <= questions_total",
            name=op.f("ck_evaluation_runs_progress"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"],
            ["users.id"],
            name=op.f("fk_evaluation_runs_created_by_user_id_users"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_evaluation_runs_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluation_runs")),
        sa.UniqueConstraint("workspace_id", "id", name="uq_evaluation_runs_workspace_and_id"),
    )
    op.create_index(
        "ix_evaluation_runs_status_next_run_at",
        "evaluation_runs",
        ["status", "next_run_at"],
        unique=False,
    )
    op.create_index(
        "ix_evaluation_runs_workspace_created",
        "evaluation_runs",
        ["workspace_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_evaluation_runs_one_active_per_workspace",
        "evaluation_runs",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_table(
        "evaluation_results",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("question_id", sa.Uuid(), nullable=False),
        sa.Column("variant", sa.Text(), nullable=False),
        sa.Column("retrieval_mode", sa.Text(), nullable=False),
        sa.Column("retrieval_query", sa.Text(), nullable=False),
        sa.Column(
            "retrieved",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "retrieved_sources",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("vector_candidates", sa.Integer(), nullable=True),
        sa.Column("lexical_candidates", sa.Integer(), nullable=True),
        sa.Column("top_vector_distance", sa.Double(), nullable=True),
        sa.Column("hit_at_5", sa.Boolean(), nullable=True),
        sa.Column("recall_at_5", sa.Double(), nullable=True),
        sa.Column("reciprocal_rank", sa.Double(), nullable=True),
        sa.Column("embedding_latency_ms", sa.Integer(), nullable=True),
        sa.Column("retrieval_latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "answer_evaluated", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("abstain_reason", sa.Text(), nullable=True),
        sa.Column("answer", sa.Text(), nullable=True),
        sa.Column(
            "citations",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("citation_precision", sa.Double(), nullable=True),
        sa.Column("citation_recall", sa.Double(), nullable=True),
        sa.Column("abstention_correct", sa.Boolean(), nullable=True),
        sa.Column("answer_latency_ms", sa.Integer(), nullable=True),
        sa.Column(
            "token_usage",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error_stage", sa.Text(), nullable=True),
        sa.Column("error_category", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "CASE WHEN outcome = 'abstained' THEN coalesce(abstain_reason IN "
            "('no_evidence', 'model_insufficient', 'no_valid_citation'), false) "
            "ELSE abstain_reason IS NULL END",
            name=op.f("ck_evaluation_results_abstain_reason"),
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome IN ('answered', 'abstained', 'provider_error')",
            name=op.f("ck_evaluation_results_outcome"),
        ),
        sa.CheckConstraint(
            "retrieval_mode IN ('vector', 'hybrid')",
            name=op.f("ck_evaluation_results_retrieval_mode"),
        ),
        sa.CheckConstraint(
            "variant IN ('vector', 'hybrid', 'hybrid_or', 'hybrid_english', 'hybrid_english_or')",
            name=op.f("ck_evaluation_results_variant"),
        ),
        sa.CheckConstraint(
            "citation_precision IS NULL OR (citation_precision >= 0 AND citation_precision <= 1)",
            name=op.f("ck_evaluation_results_citation_precision"),
        ),
        sa.CheckConstraint(
            "citation_recall IS NULL OR (citation_recall >= 0 AND citation_recall <= 1)",
            name=op.f("ck_evaluation_results_citation_recall"),
        ),
        sa.CheckConstraint(
            "coalesce(embedding_latency_ms, 0) >= 0 AND coalesce(retrieval_latency_ms, 0) >= 0 "
            "AND coalesce(answer_latency_ms, 0) >= 0",
            name=op.f("ck_evaluation_results_latency"),
        ),
        sa.CheckConstraint(
            "recall_at_5 IS NULL OR (recall_at_5 >= 0 AND recall_at_5 <= 1)",
            name=op.f("ck_evaluation_results_recall_at_5"),
        ),
        sa.CheckConstraint(
            "reciprocal_rank IS NULL OR (reciprocal_rank >= 0 AND reciprocal_rank <= 1)",
            name=op.f("ck_evaluation_results_reciprocal_rank"),
        ),
        sa.ForeignKeyConstraint(
            ["question_id"],
            ["evaluation_questions.id"],
            name=op.f("fk_evaluation_results_question_id_evaluation_questions"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["evaluation_runs.workspace_id", "evaluation_runs.id"],
            name="fk_evaluation_results_run_same_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_evaluation_results_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_evaluation_results")),
        sa.UniqueConstraint("run_id", "question_id", "variant", name="uq_evaluation_results_item"),
    )
    op.create_index(
        "ix_evaluation_results_question_id", "evaluation_results", ["question_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_evaluation_results_question_id", table_name="evaluation_results")
    op.drop_table("evaluation_results")
    op.drop_index(
        "uq_evaluation_runs_one_active_per_workspace",
        table_name="evaluation_runs",
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.drop_index("ix_evaluation_runs_workspace_created", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_status_next_run_at", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
    op.drop_table("evaluation_questions")
