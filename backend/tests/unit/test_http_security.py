"""Origin/CSRF check, CORS, Host allowlist, and error-body hygiene on the real app.

These requests stop before any database access: either a middleware rejects
them, request validation fails, or logout runs without a cookie.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

FRONTEND = "http://localhost:3000"
LOGOUT = "/api/v1/auth/logout"


@pytest.fixture
def client() -> TestClient:
    return TestClient(app, base_url="http://localhost")


# --- origin check (CSRF) -----------------------------------------------------


def test_unsafe_request_from_frontend_origin_is_allowed(client: TestClient) -> None:
    assert client.post(LOGOUT, headers={"Origin": FRONTEND}).status_code == 204


@pytest.mark.parametrize(
    "origin",
    [
        "http://localhost:5173",  # another local port: same site, so SameSite=Lax won't block it
        "http://evil.example",
        "https://localhost:3000",  # scheme mismatch
        "http://localhost:3000.evil.example",
        "null",  # sandboxed iframe / file://
    ],
)
def test_unsafe_request_from_other_origin_is_blocked(client: TestClient, origin: str) -> None:
    response = client.post(LOGOUT, headers={"Origin": origin})
    assert response.status_code == 403
    assert response.json() == {
        "error": {"code": "origin_not_allowed", "message": "Cross-origin request blocked."}
    }


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE", "PUT"])
def test_every_unsafe_method_is_checked(client: TestClient, method: str) -> None:
    response = client.request(method, LOGOUT, headers={"Origin": "http://evil.example"})
    assert response.status_code == 403


def test_safe_methods_are_not_blocked_by_origin(client: TestClient) -> None:
    assert client.get("/health", headers={"Origin": "http://evil.example"}).status_code == 200


def test_api_own_origin_is_allowed_for_docs(client: TestClient) -> None:
    assert client.post(LOGOUT, headers={"Origin": "http://localhost"}).status_code == 204


@pytest.mark.parametrize("site", ["cross-site", "same-site"])
def test_cross_site_fetch_without_origin_is_blocked(client: TestClient, site: str) -> None:
    assert client.post(LOGOUT, headers={"Sec-Fetch-Site": site}).status_code == 403


@pytest.mark.parametrize("site", ["same-origin", "none"])
def test_same_origin_fetch_without_origin_is_allowed(client: TestClient, site: str) -> None:
    assert client.post(LOGOUT, headers={"Sec-Fetch-Site": site}).status_code == 204


def test_non_browser_client_without_origin_is_allowed(client: TestClient) -> None:
    assert client.post(LOGOUT).status_code == 204


# --- CORS --------------------------------------------------------------------


def test_preflight_from_frontend_allows_credentials(client: TestClient) -> None:
    response = client.options(
        "/api/v1/auth/login",
        headers={"Origin": FRONTEND, "Access-Control-Request-Method": "POST"},
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == FRONTEND  # exact, never "*"
    assert response.headers["access-control-allow-credentials"] == "true"


def test_preflight_from_other_origin_gets_no_cors_grant(client: TestClient) -> None:
    response = client.options(
        "/api/v1/auth/login",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in response.headers


def test_simple_request_from_other_origin_gets_no_cors_grant(client: TestClient) -> None:
    response = client.get("/health", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in response.headers


# --- Host allowlist (DNS rebinding) -----------------------------------------


def test_unknown_host_header_is_rejected() -> None:
    rebinding = TestClient(app, base_url="http://attacker.example")
    assert rebinding.get("/health").status_code == 400


# --- error bodies ------------------------------------------------------------


def test_validation_errors_never_echo_submitted_values(client: TestClient) -> None:
    secret_password = "my-very-secret-password-123"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "not-an-email", "password": secret_password, "workspace_name": ""},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["code"] == "validation_error"
    assert {tuple(d["loc"]) for d in body["error"]["details"]} >= {
        ("body", "email"),
        ("body", "workspace_name"),
    }
    assert secret_password not in response.text
    assert "not-an-email" not in response.text
    assert all(set(d) == {"loc", "msg", "type"} for d in body["error"]["details"])


def test_unknown_fields_are_rejected(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": "a@example.com",
            "password": "long-enough-password",
            "workspace_name": "W",
            "role": "admin",
        },
    )
    assert response.status_code == 422
    assert ["body", "role"] in [d["loc"] for d in response.json()["error"]["details"]]


@pytest.mark.parametrize(
    ("password", "reason"),
    [("short-pass1", "too short"), ("x" * 129, "too long"), (" " * 12, "blank")],
)
def test_registration_password_policy(client: TestClient, password: str, reason: str) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "a@example.com", "password": password, "workspace_name": "W"},
    )
    assert response.status_code == 422, reason


def test_unknown_route_uses_the_standard_error_shape(client: TestClient) -> None:
    response = client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_protected_endpoint_without_cookie_is_401(client: TestClient) -> None:
    response = client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "not_authenticated", "message": "Not authenticated."}
    }


# --- errors a browser can read (ADR 0010 deferred item, fixed in M9) ----------------------


def test_an_oversized_body_gets_a_413_the_frontend_can_read(client: TestClient) -> None:
    """The body limit sits inside CORS, so the browser sees the 413, not a network error."""
    response = client.post(LOGOUT, content=b"x" * (1024 * 1024 + 1), headers={"Origin": FRONTEND})
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"
    assert response.headers["access-control-allow-origin"] == FRONTEND
    assert response.headers["access-control-allow-credentials"] == "true"


def test_a_413_for_another_origin_gets_no_cors_headers(client: TestClient) -> None:
    response = client.post(
        LOGOUT, content=b"x" * (1024 * 1024 + 1), headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 413
    assert "access-control-allow-origin" not in response.headers


def test_an_unhandled_error_is_a_json_500_the_frontend_can_read() -> None:
    from app.main import create_app

    broken = create_app()

    @broken.get("/api/v1/broken-for-test")
    def explode() -> None:
        raise RuntimeError("secret detail that must not leak")

    response = TestClient(broken, base_url="http://localhost").get(
        "/api/v1/broken-for-test", headers={"Origin": FRONTEND}
    )
    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "internal_error", "message": "Internal server error."}
    }
    assert response.headers["access-control-allow-origin"] == FRONTEND
    assert "secret detail" not in response.text
