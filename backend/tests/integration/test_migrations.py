"""Run the migration chain against a fresh, throwaway database."""

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, inspect, text
from sqlalchemy.exc import IntegrityError

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
