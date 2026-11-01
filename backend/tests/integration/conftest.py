from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Engine, create_engine, make_url, pool, text

from app.config import get_settings

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    """The application database, already migrated by `alembic upgrade head`."""
    engine = create_engine(get_settings().database_url, poolclass=pool.NullPool)
    yield engine
    engine.dispose()


@pytest.fixture
def alembic_config() -> Config:
    return Config(str(ALEMBIC_INI))


@pytest.fixture
def scratch_engine() -> Iterator[Engine]:
    """An empty, throwaway database, dropped after the test."""
    url = make_url(get_settings().database_url)
    name = f"knowbase_test_{uuid4().hex[:12]}"
    admin = create_engine(url, isolation_level="AUTOCOMMIT", poolclass=pool.NullPool)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    scratch = create_engine(url.set(database=name), poolclass=pool.NullPool)
    try:
        yield scratch
    finally:
        scratch.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()
