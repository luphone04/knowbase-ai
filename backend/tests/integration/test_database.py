"""Checks against the application database after `alembic upgrade head`."""

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from app.main import app

pytestmark = pytest.mark.integration


def test_database_connection(engine: Engine) -> None:
    with engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1


def test_server_is_postgresql_18(engine: Engine) -> None:
    with engine.connect() as connection:
        version_num = int(connection.execute(text("SHOW server_version_num")).scalar_one())
    assert 180000 <= version_num < 190000


def test_vector_extension_is_pinned_version(engine: Engine) -> None:
    with engine.connect() as connection:
        version = connection.execute(
            text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        ).scalar_one()
    assert version == "0.8.7"


def test_database_is_at_migration_head(engine: Engine, alembic_config: Config) -> None:
    head = ScriptDirectory.from_config(alembic_config).get_current_head()
    with engine.connect() as connection:
        current = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
    assert current == head


def test_vector_768_operations(engine: Engine) -> None:
    unit = "[" + ",".join(["1"] + ["0"] * 767) + "]"
    with engine.connect() as connection:
        dims, self_distance = connection.execute(
            text(
                "SELECT vector_dims(CAST(:v AS vector(768))),"
                " CAST(:v AS vector) <=> CAST(:v AS vector)"
            ),
            {"v": unit},
        ).one()
    assert dims == 768
    assert self_distance == 0


def test_ready_endpoint_reads_real_database() -> None:
    with TestClient(app) as client:
        response = client.get("/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["extensions"]["vector"] == "0.8.7"
    assert "pg_trgm" in body["extensions"]
