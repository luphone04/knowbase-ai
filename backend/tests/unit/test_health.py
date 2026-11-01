from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from app.api import health
from app.db.session import get_db
from app.main import app


class UnreachableSession:
    def connection(self) -> None:
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))


class StubSession:
    def connection(self) -> object:
        return object()


@pytest.fixture
def client() -> Iterator[TestClient]:
    # No `with` block: the lifespan (which loads settings) is not needed here.
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_health_needs_no_database(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_returns_503_when_database_unreachable(client: TestClient) -> None:
    app.dependency_overrides[get_db] = lambda: UnreachableSession()
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["database"] == "unreachable"
    assert "connection refused" not in response.text


def test_ready_returns_503_when_vector_extension_missing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app.dependency_overrides[get_db] = lambda: StubSession()
    monkeypatch.setattr(health, "installed_extensions", lambda _conn: {"pg_trgm": "1.6"})
    response = client.get("/ready")
    assert response.status_code == 503
    assert response.json()["missing_extensions"] == ["vector"]


def test_ready_reports_installed_extensions(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    app.dependency_overrides[get_db] = lambda: StubSession()
    monkeypatch.setattr(
        health, "installed_extensions", lambda _conn: {"vector": "0.8.7", "pg_trgm": "1.6"}
    )
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "database": "ok",
        "extensions": {"vector": "0.8.7", "pg_trgm": "1.6"},
        "missing_extensions": [],
    }
