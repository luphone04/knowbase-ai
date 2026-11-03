"""Run the migration chain against a fresh, throwaway database."""

import argparse

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
            for table in ("documents", "chunks", "ingestion_jobs")
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
            f"INSERT INTO sources (workspace_id, kind, display_name) "
            f"VALUES ('{WS_A}', 'website', 'https://example.com/')",
            "ck_sources_kind",
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
        "website-kind-retired",
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


def test_0005_moves_duplicate_links_out_of_document_url(
    scratch_engine: Engine, alembic_config: Config
) -> None:
    """Before 0005 a duplicate's document_url pointed at another page's document."""
    with scratch_engine.begin() as connection:
        migrate(connection, alembic_config, "0004_crawler")
        _seed(connection)
        connection.execute(
            text(
                "INSERT INTO crawl_pages (workspace_id, source_id, url, document_url, depth, "
                f"discovered_via, status) VALUES ('{WS_A}', '{SOURCE_A}', "
                "'https://example.com/alias', 'https://example.com/', 1, 'link', 'duplicate')"
            )
        )
        migrate(connection, alembic_config, "0005_crawl_page_duplicates")
        moved = connection.execute(
            text("SELECT url, document_url, duplicate_of FROM crawl_pages ORDER BY url")
        ).all()
        assert [tuple(row) for row in moved] == [
            ("https://example.com/alias", None, "https://example.com/"),
        ]
        migrate(connection, alembic_config, "0004_crawler", down=True)
        restored = connection.execute(
            text("SELECT document_url FROM crawl_pages WHERE url = 'https://example.com/alias'")
        ).scalar_one()
        assert restored == "https://example.com/"


# --- 0006: retiring website crawling (file data must survive untouched) ------------------

WEB_SOURCE = "00000000-0000-0000-0000-0000000000b1"
WEB_DOC = "00000000-0000-0000-0000-0000000000b2"
VECTOR_B = "[" + ",".join(["0.2"] * 768) + "]"


def _seed_files(connection: Connection) -> None:
    """File data at revision 0005: source, document, chunk, cached vector, job."""
    _seed(connection)
    connection.execute(
        text(
            "INSERT INTO embedding_cache (workspace_id, content_hash, model, dimensions, "
            f"embedding) VALUES ('{WS_A}', 'h', 'm', 768, '{VECTOR}')"
        )
    )


def _seed_website(connection: Connection) -> None:
    """A website source at 0005: page document, chunks, crawl record, job, cache rows.

    One chunk shares content hash 'h' with the file's chunk (its cached vector
    must survive a purge); the other ('web-only') is used by the website alone.
    """
    connection.execute(
        text(
            f"""
            INSERT INTO sources (id, workspace_id, kind, display_name, source_url) VALUES
                ('{WEB_SOURCE}', '{WS_A}', 'website', 'https://example.com/',
                 'https://example.com/');
            INSERT INTO documents (id, workspace_id, source_id, title, canonical_url,
                                   content_hash, text_content)
                VALUES ('{WEB_DOC}', '{WS_A}', '{WEB_SOURCE}', 'Home', 'https://example.com/',
                        'p', 'page text');
            INSERT INTO chunks (workspace_id, document_id, chunk_index, content, content_hash,
                                embedding, embedding_model) VALUES
                ('{WS_A}', '{WEB_DOC}', 0, 'shared', 'h', '{VECTOR}', 'm'),
                ('{WS_A}', '{WEB_DOC}', 1, 'web', 'web-only', '{VECTOR_B}', 'm');
            INSERT INTO crawl_pages (workspace_id, source_id, url, document_url, depth,
                                     discovered_via, status)
                VALUES ('{WS_A}', '{WEB_SOURCE}', 'https://example.com/', 'https://example.com/',
                        0, 'start', 'indexed');
            INSERT INTO ingestion_jobs (workspace_id, source_id, job_type, status)
                VALUES ('{WS_A}', '{WEB_SOURCE}', 'ingest', 'succeeded');
            INSERT INTO embedding_cache (workspace_id, content_hash, model, dimensions, embedding)
                VALUES ('{WS_A}', 'web-only', 'm', 768, '{VECTOR_B}');
            """
        )
    )


def _file_snapshot(connection: Connection) -> list[object]:
    """Everything that belongs to the file source, rendered for exact comparison."""
    queries = [
        f"SELECT id, kind, display_name, status FROM sources WHERE id = '{SOURCE_A}'",
        f"SELECT id, title, content_hash, text_content FROM documents WHERE id = '{DOC_A}'",
        f"SELECT id, chunk_index, content, embedding::text FROM chunks "
        f"WHERE document_id = '{DOC_A}' ORDER BY id",
        f"SELECT id, job_type, status FROM ingestion_jobs WHERE source_id = '{SOURCE_A}'",
        "SELECT workspace_id, content_hash, embedding::text FROM embedding_cache "
        "WHERE content_hash = 'h'",
        "SELECT id, email FROM users ORDER BY id",
        "SELECT id, name FROM workspaces ORDER BY id",
    ]
    return [[tuple(row) for row in connection.execute(text(q))] for q in queries]


def test_0006_retires_crawling_and_keeps_file_data(
    scratch_engine: Engine, alembic_config: Config
) -> None:
    with scratch_engine.begin() as connection:
        migrate(connection, alembic_config, "0005_crawl_page_duplicates")
        _seed_files(connection)
        before = _file_snapshot(connection)

        migrate(connection, alembic_config, "0006_retire_website_crawling")
        assert _file_snapshot(connection) == before
        assert "crawl_pages" not in inspect(connection).get_table_names()
        with pytest.raises(IntegrityError, match="ck_sources_kind"), connection.begin_nested():
            connection.execute(
                text(
                    f"INSERT INTO sources (workspace_id, kind, display_name) "
                    f"VALUES ('{WS_A}', 'website', 'x')"
                )
            )

        migrate(connection, alembic_config, "0005_crawl_page_duplicates", down=True)
        assert _file_snapshot(connection) == before
        assert connection.execute(text("SELECT count(*) FROM crawl_pages")).scalar_one() == 0
        columns = {c["name"] for c in inspect(connection).get_columns("crawl_pages")}
        assert {"document_url", "duplicate_of"} <= columns

        migrate(connection, alembic_config, "head")
        assert _file_snapshot(connection) == before


def test_0006_refuses_while_website_data_exists(
    scratch_engine: Engine, alembic_config: Config
) -> None:
    with scratch_engine.begin() as connection:
        migrate(connection, alembic_config, "0005_crawl_page_duplicates")
        _seed_files(connection)
        _seed_website(connection)
    with (
        scratch_engine.begin() as connection,
        pytest.raises(RuntimeError, match="Refusing to retire website crawling"),
    ):
        migrate(connection, alembic_config, "head")
    with scratch_engine.connect() as connection:  # rolled back: nothing changed
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert version == "0005_crawl_page_duplicates"
        assert connection.execute(text("SELECT count(*) FROM crawl_pages")).scalar_one() == 1
        assert connection.execute(text("SELECT count(*) FROM sources")).scalar_one() == 2
        assert connection.execute(text("SELECT count(*) FROM chunks")).scalar_one() == 3


def test_0006_purge_flag_removes_only_website_data(
    scratch_engine: Engine, alembic_config: Config
) -> None:
    with scratch_engine.begin() as connection:
        migrate(connection, alembic_config, "0005_crawl_page_duplicates")
        _seed_files(connection)
        before = _file_snapshot(connection)
        _seed_website(connection)

    alembic_config.cmd_opts = argparse.Namespace(x=["purge_website_sources=true"])
    with scratch_engine.begin() as connection:
        migrate(connection, alembic_config, "head")
        assert _file_snapshot(connection) == before  # incl. the cached vector shared with it

        def count(sql: str) -> int:
            return int(connection.execute(text(sql)).scalar_one())

        assert count("SELECT count(*) FROM sources") == 1
        assert count(f"SELECT count(*) FROM documents WHERE id = '{WEB_DOC}'") == 0
        assert count(f"SELECT count(*) FROM chunks WHERE document_id = '{WEB_DOC}'") == 0
        assert count(f"SELECT count(*) FROM ingestion_jobs WHERE source_id = '{WEB_SOURCE}'") == 0
        assert count("SELECT count(*) FROM embedding_cache WHERE content_hash = 'web-only'") == 0
        assert count("SELECT count(*) FROM embedding_cache WHERE content_hash = 'h'") == 1
        assert "crawl_pages" not in inspect(connection).get_table_names()
