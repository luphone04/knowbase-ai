"""Run the migration chain against a fresh, throwaway database."""

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, Engine, text

from app.db.migrations_support import installed_extensions

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
        migrate(connection, alembic_config, "head")
        assert set(installed_extensions(connection)) == {"vector", "pg_trgm"}
