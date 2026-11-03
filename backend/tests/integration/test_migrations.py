"""Run the migration chain against a fresh, throwaway database."""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, inspect, text
from sqlalchemy.exc import DataError, IntegrityError

from app.db.migrations_support import installed_extensions
from app.db.models import Base

pytestmark = pytest.mark.integration


def migrate(connection: Connection, config: Config, target: str, *, down: bool = False) -> None:
    config.attributes["connection"] = connection
    if down:
        command.downgrade(config, target)
    else:
        command.upgrade(config, target)


def test_upgrade_head_enables_extensions(scratch_engine: Engine, alembic_config: Config) -> None:
    with scratch_engine.begin() as connection:
        assert installed_extensions(connection) == {}
        migrate(connection, alembic_config, "head")
        extensions = installed_extensions(connection)
        current = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()

    assert extensions == {"vector": "0.8.7", "pg_trgm": "1.6"}
    assert current == ScriptDirectory.from_config(alembic_config).get_current_head()


def test_upgrade_is_idempotent(scratch_engine: Engine, alembic_config: Config) -> None:
    with scratch_engine.begin() as connection:
        migrate(connection, alembic_config, "head")
        migrate(connection, alembic_config, "head")
        assert "vector" in installed_extensions(connection)


def test_downgrade_and_upgrade_round_trip(scratch_engine: Engine, alembic_config: Config) -> None:
    with scratch_engine.begin() as connection:
        migrate(connection, alembic_config, "head")
        migrate(connection, alembic_config, "base", down=True)
        assert installed_extensions(connection) == {}
        assert set(inspect(connection).get_table_names()) == {"alembic_version"}
        migrate(connection, alembic_config, "head")
        assert set(installed_extensions(connection)) == {"vector", "pg_trgm"}


def test_models_match_migrations(scratch_engine: Engine, alembic_config: Config) -> None:
    """Autogenerate finds nothing to do: the ORM models and the migrations agree."""
    with scratch_engine.begin() as connection:
        migrate(connection, alembic_config, "head")
        diff = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert diff == []


def test_auth_schema_constraints(scratch_engine: Engine, alembic_config: Config) -> None:
    with scratch_engine.begin() as connection:
        migrate(connection, alembic_config, "head")
        schema = inspect(connection)
        assert set(schema.get_table_names()) == {
            "alembic_version",
            "users",
            "workspaces",
            "workspace_members",
            "revoked_tokens",
            "rate_limit_counters",
            "sources",
            "documents",
            "chunks",
            "ingestion_jobs",
            "embedding_cache",
            "crawl_pages",
        }
        assert schema.get_pk_constraint("workspace_members")["constrained_columns"] == [
            "workspace_id",
            "user_id",
        ]
        assert {u["name"] for u in schema.get_unique_constraints("users")} == {"uq_users_email"}
        assert {c["name"] for c in schema.get_check_constraints("users")} == {
            "ck_users_email_normalized"
        }
        assert {c["name"] for c in schema.get_check_constraints("workspace_members")} == {
            "ck_workspace_members_role"
        }
        member_fks = {
            fk["referred_table"]: fk["options"].get("ondelete")
            for fk in schema.get_foreign_keys("workspace_members")
        }
        assert member_fks == {"workspaces": "CASCADE", "users": "CASCADE"}
        assert "ix_workspace_members_user_id" in {
            i["name"] for i in schema.get_indexes("workspace_members")
        }


USER_ID = "00000000-0000-0000-0000-000000000001"
OTHER_ID = "00000000-0000-0000-0000-000000000002"


@pytest.mark.parametrize(
    ("statement", "constraint"),
    [
        (
            "INSERT INTO users (id, email, password_hash) "
            f"VALUES ('{OTHER_ID}', 'Upper@Example.com', 'x')",
            "ck_users_email_normalized",
        ),
        (
            f"INSERT INTO workspaces (name, owner_user_id) VALUES ('   ', '{USER_ID}')",
            "ck_workspaces_name_length",
        ),
        (
            "INSERT INTO workspace_members (workspace_id, user_id, role) "
            f"SELECT id, '{USER_ID}', 'superuser' FROM workspaces",
            "ck_workspace_members_role",
        ),
    ],
    ids=["email-not-normalised", "blank-workspace-name", "unknown-role"],
)
def test_database_rejects_invalid_rows(
    scratch_engine: Engine, alembic_config: Config, statement: str, constraint: str
) -> None:
    """Defence in depth: the database enforces the rules even if app code doesn't."""
    with scratch_engine.begin() as connection:
        migrate(connection, alembic_config, "head")
        connection.execute(
            text(
                "INSERT INTO users (id, email, password_hash) "
                f"VALUES ('{USER_ID}', 'a@example.com', 'x');"
                f"INSERT INTO workspaces (name, owner_user_id) VALUES ('W', '{USER_ID}')"
            )
        )
    with scratch_engine.connect() as connection, pytest.raises(IntegrityError) as excinfo:
        connection.execute(text(statement))
    assert constraint in str(excinfo.value)


def test_ingestion_schema_indexes_and_tenant_safe_keys(
    scratch_engine: Engine, alembic_config: Config
) -> None:
    with scratch_engine.begin() as connection:
        migrate(connection, alembic_config, "head")
        indexes = {
            row.indexname: row.indexdef
            for row in connection.execute(
                text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'chunks'")
            )
        }
        assert "USING hnsw (embedding vector_cosine_ops)" in indexes["ix_chunks_embedding_hnsw"]
        assert "USING gin (search_tsv)" in indexes["ix_chunks_search_tsv"]
        column = connection.execute(
            text(
                "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
            )
        ).scalar_one()
        assert column == "vector(768)"
        fks = {
            fk["name"]: (fk["constrained_columns"], fk["referred_table"])
            for table in ("documents", "chunks", "ingestion_jobs", "crawl_pages")
            for fk in inspect(connection).get_foreign_keys(table)
        }
        assert fks["fk_chunks_document_same_workspace"] == (
            ["workspace_id", "document_id"],
            "documents",
        )
        assert fks["fk_documents_source_same_workspace"] == (
            ["workspace_id", "source_id"],
            "sources",
        )
        assert fks["fk_ingestion_jobs_source_same_workspace"] == (
            ["workspace_id", "source_id"],
            "sources",
        )
        assert fks["fk_crawl_pages_source_same_workspace"] == (
            ["workspace_id", "source_id"],
            "sources",
        )
        crawl_indexes = {i["name"] for i in inspect(connection).get_indexes("crawl_pages")}
        assert "ix_crawl_pages_workspace_source_status" in crawl_indexes


WS_A = "00000000-0000-0000-0000-00000000000a"
WS_B = "00000000-0000-0000-0000-00000000000b"
SOURCE_A = "00000000-0000-0000-0000-0000000000a1"
DOC_A = "00000000-0000-0000-0000-0000000000a2"
VECTOR = "[" + ",".join(["0.1"] * 768) + "]"


def _seed(connection: Connection) -> None:
    connection.execute(
        text(
            f"""
            INSERT INTO users (id, email, password_hash) VALUES ('{USER_ID}', 'a@example.com', 'x');
            INSERT INTO workspaces (id, name, owner_user_id) VALUES
                ('{WS_A}', 'A', '{USER_ID}'), ('{WS_B}', 'B', '{USER_ID}');
            INSERT INTO sources (id, workspace_id, kind, display_name) VALUES
                ('{SOURCE_A}', '{WS_A}', 'file', 'a.md');
            INSERT INTO documents (id, workspace_id, source_id, title, content_hash, text_content)
                VALUES ('{DOC_A}', '{WS_A}', '{SOURCE_A}', 'A', 'h', 't');
            INSERT INTO chunks (workspace_id, document_id, chunk_index, content, content_hash,
                                embedding, embedding_model)
                VALUES ('{WS_A}', '{DOC_A}', 0, 'c', 'h', '{VECTOR}', 'm');
            INSERT INTO ingestion_jobs (workspace_id, source_id, job_type) VALUES
                ('{WS_A}', '{SOURCE_A}', 'ingest');
            INSERT INTO crawl_pages (workspace_id, source_id, url, depth, discovered_via, status)
                VALUES ('{WS_A}', '{SOURCE_A}', 'https://example.com/', 0, 'start', 'indexed');
            """
        )
    )


@pytest.mark.parametrize(
    ("statement", "constraint"),
    [
        (
            # B's chunk pointing at A's document: the composite key refuses it.
            f"INSERT INTO chunks (workspace_id, document_id, chunk_index, content, content_hash, "
            f"embedding, embedding_model) "
            f"VALUES ('{WS_B}', '{DOC_A}', 1, 'c', 'h', '{VECTOR}', 'm')",
            "fk_chunks_document_same_workspace",
        ),
        (
            # canonical_url set, so only the cross-workspace foreign key can object.
            f"INSERT INTO documents (workspace_id, source_id, title, canonical_url, content_hash, "
            f"text_content) VALUES ('{WS_B}', '{SOURCE_A}', 'x', 'https://example.com/', 'h', 't')",
            "fk_documents_source_same_workspace",
        ),
        (
            f"INSERT INTO ingestion_jobs (workspace_id, source_id, job_type, status) "
            f"VALUES ('{WS_B}', '{SOURCE_A}', 'ingest', 'running')",
            "fk_ingestion_jobs_source_same_workspace",
        ),
        (
            f"INSERT INTO chunks (workspace_id, document_id, chunk_index, content, content_hash, "
            f"embedding, embedding_model) "
            f"VALUES ('{WS_A}', '{DOC_A}', 0, 'dup', 'h', '{VECTOR}', 'm')",
            "uq_chunks_document_chunk_index",
        ),
        (
            f"INSERT INTO documents (workspace_id, source_id, title, content_hash, text_content) "
            f"VALUES ('{WS_A}', '{SOURCE_A}', 'second', 'h', 't')",
            "uq_documents_one_per_file_source",
        ),
        (
            f"INSERT INTO ingestion_jobs (workspace_id, source_id, job_type) "
            f"VALUES ('{WS_A}', '{SOURCE_A}', 'reindex')",
            "uq_ingestion_jobs_one_queued_per_source",
        ),
        (
            f"INSERT INTO sources (workspace_id, kind, display_name, status) "
            f"VALUES ('{WS_A}', 'file', 'x', 'exploded')",
            "ck_sources_status",
        ),
        (
            f"INSERT INTO crawl_pages (workspace_id, source_id, url, depth, discovered_via, "
            f"status) VALUES ('{WS_B}', '{SOURCE_A}', 'https://example.com/b', 0, 'link', "
            f"'indexed')",
            "fk_crawl_pages_source_same_workspace",
        ),
        (
            f"INSERT INTO crawl_pages (workspace_id, source_id, url, depth, discovered_via, "
            f"status) VALUES ('{WS_A}', '{SOURCE_A}', 'https://example.com/', 1, 'link', "
            f"'indexed')",
            "uq_crawl_pages_source_url",
        ),
        (
            f"INSERT INTO crawl_pages (workspace_id, source_id, url, depth, discovered_via, "
            f"status) VALUES ('{WS_A}', '{SOURCE_A}', 'https://example.com/c', 0, 'link', "
            f"'teleported')",
            "ck_crawl_pages_status",
        ),
    ],
    ids=[
        "chunk-into-other-workspace",
        "document-into-other-workspace",
        "job-into-other-workspace",
        "duplicate-chunk-index",
        "second-file-document",
        "second-waiting-job",
        "unknown-status",
        "crawl-page-into-other-workspace",
        "duplicate-crawl-page-url",
        "unknown-crawl-page-status",
    ],
)
def test_database_enforces_ingestion_integrity(
    scratch_engine: Engine, alembic_config: Config, statement: str, constraint: str
) -> None:
    with scratch_engine.begin() as connection:
        migrate(connection, alembic_config, "head")
        _seed(connection)
    with scratch_engine.connect() as connection, pytest.raises(IntegrityError) as excinfo:
        connection.execute(text(statement))
    assert constraint in str(excinfo.value)


def test_vectors_must_have_768_dimensions(scratch_engine: Engine, alembic_config: Config) -> None:
    with scratch_engine.begin() as connection:
        migrate(connection, alembic_config, "head")
        _seed(connection)
    with (
        scratch_engine.connect() as connection,
        pytest.raises(DataError, match="expected 768 dimensions"),
    ):
        connection.execute(
            text(
                "INSERT INTO chunks (workspace_id, document_id, chunk_index, content, "
                "content_hash, embedding, embedding_model) "
                f"VALUES ('{WS_A}', '{DOC_A}', 5, 'c', 'h', '[1,2,3]', 'm')"
            )
        )
