"""BodySizeLimitMiddleware: oversized bodies are refused before the app reads them."""

from collections.abc import Iterator

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.body_limit import BodySizeLimitMiddleware


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    seen: dict[str, int] = {}

    @app.post("/upload")
    async def upload(request: Request) -> dict[str, int]:
        body = await request.body()
        seen["bytes"] = len(body)
        return {"bytes": len(body)}

    @app.post("/json")
    async def small(request: Request) -> dict[str, int]:
        return {"bytes": len(await request.body())}

    app.add_middleware(BodySizeLimitMiddleware, default_limit=100, path_limits={"/upload": 1000})
    return TestClient(app)


def test_body_within_limit_passes(client: TestClient) -> None:
    assert client.post("/upload", content=b"x" * 1000).json() == {"bytes": 1000}


def test_declared_oversized_body_is_refused_immediately(client: TestClient) -> None:
    response = client.post("/upload", content=b"x" * 1001)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


def test_streamed_body_without_length_is_counted(client: TestClient) -> None:
    def chunks() -> Iterator[bytes]:
        for _ in range(20):
            yield b"x" * 100

    response = client.post("/upload", content=chunks())  # chunked: no Content-Length
    assert response.status_code == 413


def test_other_paths_use_the_default_limit(client: TestClient) -> None:
    assert client.post("/json", content=b"x" * 100).status_code == 200
    assert client.post("/json", content=b"x" * 101).status_code == 413
