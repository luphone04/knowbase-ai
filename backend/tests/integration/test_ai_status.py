"""GET /api/v1/ai/status: safe configuration info, never secrets, never a Gemini call."""

from collections.abc import Iterator

import pytest
from google import genai
from sqlalchemy import make_url

from app.config import get_settings
from app.providers.health import provider_health
from tests.integration.conftest import Tenant, new_client

pytestmark = pytest.mark.integration

STATUS = "/api/v1/ai/status"
FAKE_KEY = "test-only-fake-gemini-key-0123456789"


@pytest.fixture(autouse=True)
def clean_health() -> Iterator[None]:
    provider_health.reset()
    yield
    provider_health.reset()


@pytest.fixture
def gemini_key(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    monkeypatch.setenv("GEMINI_API_KEY", FAKE_KEY)
    get_settings.cache_clear()
    yield FAKE_KEY
    monkeypatch.setenv("GEMINI_API_KEY", "")
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def no_gemini_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """The status check must answer from configuration alone."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("the status endpoint must not create a Gemini client")

    monkeypatch.setattr(genai, "Client", forbidden)


def test_requires_authentication() -> None:
    response = new_client().get(STATUS)
    assert response.status_code == 401


def test_missing_key_is_reported_without_breaking_the_app(tenant_a: Tenant) -> None:
    body = tenant_a.client.get(STATUS).json()
    assert body["status"] == "missing_key"
    assert body["configured"] is False
    assert "GEMINI_API_KEY is not set" in body["message"]
    assert tenant_a.client.get("/api/v1/auth/me").status_code == 200  # non-AI features work


def test_configured_status_shows_safe_model_settings(tenant_a: Tenant, gemini_key: str) -> None:
    response = tenant_a.client.get(STATUS)
    assert response.status_code == 200
    body = response.json()
    settings = get_settings()
    assert body["status"] == "configured" and body["configured"] is True
    assert body["provider"] == "gemini"
    assert body["chat_model"] == settings.gemini_chat_model
    assert body["thinking_level"] == "low"
    assert body["max_output_tokens"] == 1200
    assert body["embedding_model"] == settings.gemini_embedding_model
    assert body["embedding_dimensions"] == 768
    assert body["document_task_type"] == "RETRIEVAL_DOCUMENT"
    assert body["query_task_type"] == "RETRIEVAL_QUERY"
    assert body["paid_fallback"] is False
    assert body["last_error"] is None
    assert "does not call Gemini" in body["message"]  # no claim of live verification


def test_quota_error_is_surfaced_and_cleared_by_success(tenant_a: Tenant, gemini_key: str) -> None:
    provider_health.record_failure("quota_exhausted")
    body = tenant_a.client.get(STATUS).json()
    assert body["status"] == "quota_error"
    assert body["last_error"]["category"] == "quota_exhausted"
    assert "No paid model" in body["message"]

    provider_health.record_success()
    body = tenant_a.client.get(STATUS).json()
    assert body["status"] == "configured" and body["last_error"] is None
    assert body["last_success_at"] is not None


def test_rate_limit_counts_as_a_quota_error(tenant_a: Tenant, gemini_key: str) -> None:
    provider_health.record_failure("rate_limited")
    assert tenant_a.client.get(STATUS).json()["status"] == "quota_error"


@pytest.mark.parametrize("category", ["unavailable", "timeout"])
def test_outage_does_not_masquerade_as_quota(
    tenant_a: Tenant, gemini_key: str, category: str
) -> None:
    provider_health.record_failure(category)
    body = tenant_a.client.get(STATUS).json()
    assert body["status"] == "configured"
    assert body["last_error"]["category"] == category


def test_no_response_ever_contains_a_secret(tenant_a: Tenant, gemini_key: str) -> None:
    settings = get_settings()
    secrets = {
        "gemini key": gemini_key,
        "jwt secret": settings.jwt_secret.get_secret_value(),
        "app secret": settings.app_secret_key.get_secret_value(),
        "db password": make_url(settings.database_url).password or "",
    }
    anonymous = new_client()
    responses = {
        "ai status": tenant_a.client.get(STATUS),
        "me": tenant_a.client.get("/api/v1/auth/me"),
        "workspace": tenant_a.client.get(f"/api/v1/workspaces/{tenant_a.workspace_id}"),
        "health": anonymous.get("/health"),
        "ready": anonymous.get("/ready"),
        "openapi": anonymous.get("/openapi.json"),
        "docs": anonymous.get("/docs"),
    }
    for name, response in responses.items():
        assert response.status_code == 200, name
        for label, secret in secrets.items():
            if secret:
                assert secret not in response.text, f"{label} leaked in {name}"
    # Not even a fragment of the key.
    assert gemini_key[:12] not in responses["ai status"].text
