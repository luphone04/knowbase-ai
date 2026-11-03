"""Integration tests run against a dedicated, freshly migrated database.

For each test session, a new database is created next to DATABASE_URL and
brought to head with `alembic upgrade head`, so every run also proves the
migrations work on a clean database. The test points the app at it and drops
it afterwards. The application's own database is never written to. Tables
are emptied after every test.
"""

import os
import shutil
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import URL, Engine, create_engine, make_url, pool, text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.crawler.fetcher import SafeFetcher
from app.crawler.network import Resolver
from app.crawler.urls import Origin
from app.db.session import get_engine, get_sessionmaker
from app.ingestion.pipeline import IngestionWorker
from app.ingestion.storage import FileStorage
from app.main import app
from tests.fakes.providers import MockEmbeddingProvider
from tests.fakes.websites import FakeClock, FakeSite, make_fetcher, public_resolver

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"
APP_TABLES = (
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
)
TEST_PASSWORD = "integration-test-password-1"


def _clear_caches() -> None:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_sessionmaker.cache_clear()


def _create_database(base: URL, name: str) -> None:
    admin = create_engine(base, isolation_level="AUTOCOMMIT", poolclass=pool.NullPool)
    with admin.connect() as connection:
        connection.execute(text(f'CREATE DATABASE "{name}"'))
    admin.dispose()


def _drop_database(base: URL, name: str) -> None:
    admin = create_engine(base, isolation_level="AUTOCOMMIT", poolclass=pool.NullPool)
    with admin.connect() as connection:
        connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
    admin.dispose()


def upgrade_to_head(engine: Engine) -> None:
    with engine.begin() as connection:
        config = Config(str(ALEMBIC_INI))
        config.attributes["connection"] = connection
        command.upgrade(config, "head")


@pytest.fixture(scope="session", autouse=True)
def test_database_url() -> Iterator[URL]:
    original = os.environ["DATABASE_URL"]
    base = make_url(original)
    name = f"knowbase_test_{uuid.uuid4().hex[:12]}"
    _create_database(base, name)
    url = base.set(database=name)
    engine = create_engine(url, poolclass=pool.NullPool)
    upgrade_to_head(engine)
    engine.dispose()

    os.environ["DATABASE_URL"] = url.render_as_string(hide_password=False)
    _clear_caches()
    try:
        yield url
    finally:
        get_engine().dispose()
        os.environ["DATABASE_URL"] = original
        _clear_caches()
        _drop_database(base, name)


@pytest.fixture(autouse=True)
def _restore_environment() -> Iterator[None]:
    """make_worker(env=...) changes os.environ; no test may leak settings into the next."""
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _empty_tables(test_database_url: URL) -> Iterator[None]:
    yield
    with get_engine().begin() as connection:
        connection.execute(text(f"TRUNCATE {', '.join(APP_TABLES)} CASCADE"))
    upload_root = Path(get_settings().upload_dir)
    for entry in upload_root.iterdir() if upload_root.is_dir() else []:
        shutil.rmtree(entry, ignore_errors=True)


@pytest.fixture(scope="session")
def engine(test_database_url: URL) -> Iterator[Engine]:
    """The migrated test database."""
    engine = create_engine(test_database_url, poolclass=pool.NullPool)
    yield engine
    engine.dispose()


@pytest.fixture
def db() -> Iterator[Session]:
    """An unbound session for arranging and inspecting test data."""
    with get_sessionmaker()() as session:
        yield session


@pytest.fixture
def alembic_config() -> Config:
    return Config(str(ALEMBIC_INI))


@pytest.fixture
def scratch_engine(test_database_url: URL) -> Iterator[Engine]:
    """An empty, throwaway database, dropped after the test."""
    name = f"knowbase_scratch_{uuid.uuid4().hex[:12]}"
    _create_database(test_database_url, name)
    scratch = create_engine(test_database_url.set(database=name), poolclass=pool.NullPool)
    try:
        yield scratch
    finally:
        scratch.dispose()
        _drop_database(test_database_url, name)


# --- tenants -----------------------------------------------------------------


def new_client(ip: str = "198.51.100.200") -> TestClient:
    """A browser stand-in: its own cookie jar and client address."""
    return TestClient(app, base_url="http://localhost", client=(ip, 50000))


@dataclass
class Tenant:
    """A registered user with their own workspace and a signed-in client."""

    client: TestClient
    email: str
    password: str
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    workspace_name: str


def register_tenant(label: str, ip: str) -> Tenant:
    client = new_client(ip)
    email = f"{label}-{uuid.uuid4().hex[:8]}@example.com"
    workspace_name = f"{label.title()} Workspace"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": TEST_PASSWORD, "workspace_name": workspace_name},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return Tenant(
        client=client,
        email=email,
        password=TEST_PASSWORD,
        user_id=uuid.UUID(body["user"]["id"]),
        workspace_id=uuid.UUID(body["workspace"]["id"]),
        workspace_name=workspace_name,
    )


@pytest.fixture
def tenant_a() -> Tenant:
    return register_tenant("alice", "198.51.100.1")


@pytest.fixture
def tenant_b() -> Tenant:
    return register_tenant("bob", "198.51.100.2")


# --- ingestion worker ----------------------------------------------------------------


@dataclass
class WorkerHarness:
    worker: IngestionWorker
    provider: MockEmbeddingProvider

    def run_all(self, limit: int = 20) -> int:
        """Process due jobs until the queue is empty; returns how many ran."""
        ran = 0
        while ran < limit and self.worker.run_once():
            ran += 1
        return ran


def _no_network(origin: Origin) -> SafeFetcher:
    raise AssertionError(f"test tried to crawl {origin} without a FakeSite")


def make_worker(
    provider: MockEmbeddingProvider | None = None,
    env: dict[str, str] | None = None,
    site: FakeSite | None = None,
    resolver: Resolver = public_resolver,
) -> WorkerHarness:
    """A real IngestionWorker on the test database, with a deterministic embedding
    provider and, for website sources, a fake site and clock (never the network)."""
    for name, value in (env or {}).items():
        os.environ[name] = value
    get_settings.cache_clear()
    settings = get_settings()
    mock = provider or MockEmbeddingProvider(model=settings.gemini_embedding_model)
    clock = FakeClock()
    if site is not None:
        site.clock = clock
    worker = IngestionWorker(
        settings=settings,
        session_factory=get_sessionmaker(),
        engine=get_engine(),
        storage=FileStorage(settings.upload_dir),
        embedding_provider_factory=lambda: mock,
        worker_id="test-worker",
        fetcher_factory=(
            (lambda origin: make_fetcher(site, origin, clock=clock, resolver=resolver))
            if site is not None
            else _no_network
        ),
        crawl_clock=clock,
        crawl_sleep=clock.sleep,
    )
    return WorkerHarness(worker, mock)


@pytest.fixture
def worker(monkeypatch: pytest.MonkeyPatch) -> Iterator[WorkerHarness]:
    yield make_worker()
    get_settings.cache_clear()
