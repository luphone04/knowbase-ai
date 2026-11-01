"""Registration, login, sessions, and logout through the real API and database."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.dependencies import AUTH_COOKIE
from app.config import get_settings
from app.core.security import create_access_token, decode_access_token
from app.db.models import RevokedToken, User, Workspace, WorkspaceMember
from app.db.tenancy import SKIP_WORKSPACE_SCOPE
from tests.integration.conftest import TEST_PASSWORD, Tenant, new_client

pytestmark = pytest.mark.integration

REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/auth/me"


def mint_token(user_id: uuid.UUID, workspace_id: uuid.UUID, **kwargs: object) -> str:
    """A token signed exactly as the server signs them (not a mock)."""
    options: dict[str, object] = {
        "secret": get_settings().jwt_secret.get_secret_value(),
        "lifetime": timedelta(hours=1),
    }
    options.update(kwargs)
    token, _ = create_access_token(user_id, workspace_id, **options)  # type: ignore[arg-type]
    return token


def client_with_token(token: str) -> TestClient:
    client = new_client("198.51.100.50")
    client.cookies.set(AUTH_COOKIE, token)
    return client


def set_cookie_attributes(header: str) -> dict[str, str]:
    parts = [part.strip() for part in header.split(";")]
    attributes = {"__value__": parts[0]}
    for part in parts[1:]:
        key, _, value = part.partition("=")
        attributes[key.lower()] = value
    return attributes


# --- registration ------------------------------------------------------------


def test_register_creates_user_workspace_and_owner_membership(
    tenant_a: Tenant, db: Session
) -> None:
    user = db.scalars(select(User).where(User.id == tenant_a.user_id)).one()
    workspace = db.get_one(Workspace, tenant_a.workspace_id)
    memberships = db.scalars(
        select(WorkspaceMember)
        .where(WorkspaceMember.user_id == tenant_a.user_id)
        .execution_options(**SKIP_WORKSPACE_SCOPE)
    ).all()

    assert user.email == tenant_a.email
    assert user.is_active is True
    assert workspace.name == tenant_a.workspace_name
    assert workspace.owner_user_id == tenant_a.user_id
    assert [(m.workspace_id, m.role) for m in memberships] == [(tenant_a.workspace_id, "owner")]


def test_password_is_stored_only_as_an_argon2id_hash(tenant_a: Tenant, db: Session) -> None:
    stored = db.scalars(select(User.password_hash).where(User.id == tenant_a.user_id)).one()
    assert stored != TEST_PASSWORD
    assert TEST_PASSWORD not in stored
    assert stored.startswith("$argon2id$v=19$m=65536,t=3,p=4$")


def test_register_sets_a_hardened_session_cookie() -> None:
    client = new_client()
    response = client.post(
        REGISTER,
        json={"email": "cookie@example.com", "password": TEST_PASSWORD, "workspace_name": "W"},
    )
    assert response.status_code == 201
    cookie = set_cookie_attributes(response.headers["set-cookie"])
    assert cookie["__value__"].startswith(f"{AUTH_COOKIE}=")
    assert "httponly" in cookie  # unreadable from JavaScript
    assert cookie["samesite"].lower() == "lax"
    assert cookie["path"] == "/"
    assert cookie["max-age"] == str(get_settings().access_token_expire_minutes * 60)
    assert "secure" not in cookie  # local HTTP; COOKIE_SECURE=true sets it
    assert "domain" not in cookie  # host-only
    # The token travels only in the cookie, never in the JSON body.
    token = cookie["__value__"].split("=", 1)[1]
    assert token not in response.text


def test_secure_flag_is_set_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COOKIE_SECURE", "true")
    get_settings.cache_clear()
    try:
        response = new_client().post(
            REGISTER,
            json={"email": "secure@example.com", "password": TEST_PASSWORD, "workspace_name": "W"},
        )
        assert "secure" in set_cookie_attributes(response.headers["set-cookie"])
    finally:
        monkeypatch.setenv("COOKIE_SECURE", "false")
        get_settings.cache_clear()


def test_registered_user_is_signed_in(tenant_a: Tenant) -> None:
    response = tenant_a.client.get(ME)
    assert response.status_code == 200
    body = response.json()
    assert body["user"]["id"] == str(tenant_a.user_id)
    assert body["workspace"] == {
        "id": str(tenant_a.workspace_id),
        "name": tenant_a.workspace_name,
        "role": "owner",
        "created_at": body["workspace"]["created_at"],
    }
    assert "password" not in response.text and "argon2" not in response.text


def test_duplicate_email_is_rejected_case_insensitively(tenant_a: Tenant, db: Session) -> None:
    response = new_client().post(
        REGISTER,
        json={
            "email": tenant_a.email.upper(),
            "password": "another-password-123",
            "workspace_name": "Copy",
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "email_taken"
    assert db.scalar(select(func.count()).select_from(User)) == 1
    assert db.scalar(select(func.count()).select_from(Workspace)) == 1


def test_email_is_stored_lower_case(db: Session) -> None:
    response = new_client().post(
        REGISTER,
        json={"email": "Mixed.Case@Example.COM", "password": TEST_PASSWORD, "workspace_name": "W"},
    )
    assert response.status_code == 201
    assert response.json()["user"]["email"] == "mixed.case@example.com"
    assert db.scalars(select(User.email)).one() == "mixed.case@example.com"


def test_workspace_name_is_trimmed() -> None:
    response = new_client().post(
        REGISTER,
        json={"email": "trim@example.com", "password": TEST_PASSWORD, "workspace_name": "  Acme  "},
    )
    assert response.json()["workspace"]["name"] == "Acme"


# --- login ---------------------------------------------------------------------


def test_login_with_correct_credentials_starts_a_session(tenant_a: Tenant) -> None:
    client = new_client()
    response = client.post(LOGIN, json={"email": tenant_a.email, "password": TEST_PASSWORD})
    assert response.status_code == 200
    assert response.json()["workspace"]["id"] == str(tenant_a.workspace_id)
    assert client.get(ME).json()["user"]["id"] == str(tenant_a.user_id)


def test_login_email_is_case_insensitive(tenant_a: Tenant) -> None:
    response = new_client().post(
        LOGIN, json={"email": tenant_a.email.upper(), "password": TEST_PASSWORD}
    )
    assert response.status_code == 200


def test_wrong_password_is_rejected_without_a_cookie(tenant_a: Tenant) -> None:
    client = new_client()
    response = client.post(LOGIN, json={"email": tenant_a.email, "password": "wrong-password-1"})
    assert response.status_code == 401
    assert response.json() == {
        "error": {"code": "invalid_credentials", "message": "Invalid email or password."}
    }
    assert "set-cookie" not in response.headers
    assert client.get(ME).status_code == 401


def test_unknown_email_and_wrong_password_look_identical(tenant_a: Tenant) -> None:
    wrong_password = new_client().post(
        LOGIN, json={"email": tenant_a.email, "password": "wrong-password-1"}
    )
    unknown_email = new_client().post(
        LOGIN, json={"email": "nobody@example.com", "password": "wrong-password-1"}
    )
    assert (wrong_password.status_code, wrong_password.json()) == (
        unknown_email.status_code,
        unknown_email.json(),
    )


def test_disabled_account_cannot_log_in_or_keep_its_session(tenant_a: Tenant, db: Session) -> None:
    db.get_one(User, tenant_a.user_id).is_active = False
    db.commit()

    login = new_client().post(LOGIN, json={"email": tenant_a.email, "password": TEST_PASSWORD})
    assert login.status_code == 401
    assert login.json()["error"]["code"] == "invalid_credentials"  # no "disabled" hint
    assert tenant_a.client.get(ME).status_code == 401  # existing session ends too


def test_outdated_password_hash_is_upgraded_at_login(tenant_a: Tenant, db: Session) -> None:
    legacy = PasswordHash((Argon2Hasher(time_cost=1, memory_cost=8192, parallelism=1),))
    db.get_one(User, tenant_a.user_id).password_hash = legacy.hash(TEST_PASSWORD)
    db.commit()

    response = new_client().post(LOGIN, json={"email": tenant_a.email, "password": TEST_PASSWORD})
    assert response.status_code == 200
    db.expire_all()
    assert db.get_one(User, tenant_a.user_id).password_hash.startswith("$argon2id$v=19$m=65536")


# --- sessions and tokens -----------------------------------------------------


def test_expired_token_is_rejected(tenant_a: Tenant) -> None:
    past = datetime.now(UTC) - timedelta(hours=2)
    token = mint_token(tenant_a.user_id, tenant_a.workspace_id, now=past)
    assert client_with_token(token).get(ME).status_code == 401


def test_token_signed_with_another_secret_is_rejected(tenant_a: Tenant) -> None:
    token = mint_token(
        tenant_a.user_id, tenant_a.workspace_id, secret="attacker-secret-0123456789abcdef0123"
    )
    assert client_with_token(token).get(ME).status_code == 401


def test_tampered_token_is_rejected(tenant_a: Tenant) -> None:
    token = tenant_a.client.cookies[AUTH_COOKIE]
    tampered = token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB")
    assert client_with_token(tampered).get(ME).status_code == 401


def test_token_for_a_workspace_the_user_is_not_in_is_rejected(
    tenant_a: Tenant, tenant_b: Tenant
) -> None:
    # Correctly signed, but A isn't a member of B's workspace: membership is re-checked.
    token = mint_token(tenant_a.user_id, tenant_b.workspace_id)
    client = client_with_token(token)
    assert client.get(ME).status_code == 401
    assert client.get(f"/api/v1/workspaces/{tenant_b.workspace_id}").status_code == 401


def test_token_for_a_user_that_does_not_exist_is_rejected(tenant_a: Tenant) -> None:
    token = mint_token(uuid.uuid4(), tenant_a.workspace_id)
    assert client_with_token(token).get(ME).status_code == 401


def test_all_session_failures_share_one_response(tenant_a: Tenant) -> None:
    expired = mint_token(
        tenant_a.user_id, tenant_a.workspace_id, now=datetime.now(UTC) - timedelta(hours=2)
    )
    bodies = {
        new_client().get(ME).text,  # no cookie
        client_with_token("garbage").get(ME).text,
        client_with_token(expired).get(ME).text,
    }
    assert len(bodies) == 1


# --- logout ------------------------------------------------------------------


def test_logout_clears_the_cookie(tenant_a: Tenant) -> None:
    response = tenant_a.client.post(LOGOUT)
    assert response.status_code == 204
    cookie = set_cookie_attributes(response.headers["set-cookie"])
    assert cookie["__value__"] in (f'{AUTH_COOKIE}=""', f"{AUTH_COOKIE}=")
    assert cookie["max-age"] == "0"
    assert "httponly" in cookie
    assert tenant_a.client.get(ME).status_code == 401


def test_logged_out_token_cannot_be_replayed(tenant_a: Tenant, db: Session) -> None:
    token = tenant_a.client.cookies[AUTH_COOKIE]
    assert tenant_a.client.post(LOGOUT).status_code == 204

    # An attacker who copied the cookie before logout gets nothing, though the token
    # has not expired yet: logout revokes it server-side.
    assert client_with_token(token).get(ME).status_code == 401
    claims = decode_access_token(token, secret=get_settings().jwt_secret.get_secret_value())
    assert db.get(RevokedToken, claims.token_id) is not None


def test_logout_only_ends_that_session(tenant_a: Tenant) -> None:
    second = new_client()
    second.post(LOGIN, json={"email": tenant_a.email, "password": TEST_PASSWORD})
    assert tenant_a.client.post(LOGOUT).status_code == 204
    assert second.get(ME).status_code == 200


def test_logout_is_idempotent(tenant_a: Tenant) -> None:
    assert tenant_a.client.post(LOGOUT).status_code == 204
    assert tenant_a.client.post(LOGOUT).status_code == 204
    assert client_with_token("garbage").post(LOGOUT).status_code == 204


def test_expired_revocations_are_pruned(tenant_a: Tenant, db: Session) -> None:
    db.add(
        RevokedToken(
            jti=uuid.uuid4(),
            user_id=tenant_a.user_id,
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
    )
    db.commit()
    tenant_a.client.post(LOGOUT)
    remaining = db.scalars(select(RevokedToken.expires_at)).all()
    assert len(remaining) == 1 and remaining[0] > datetime.now(UTC)


def test_database_errors_do_not_expose_bound_values(db: Session) -> None:
    """An unexpected failure must not carry the password hash into logs or messages."""
    secret_hash = "$argon2id$v=19$m=65536,t=3,p=4$do-not-log-me"
    db.add(User(email="dup@example.com", password_hash="x"))
    db.commit()
    db.add(User(email="dup@example.com", password_hash=secret_hash))
    with pytest.raises(IntegrityError) as excinfo:
        db.commit()
    db.rollback()
    # Logged tracebacks render the exception as text; that text must not hold bound values.
    # (PostgreSQL's own DETAIL line may still name the conflicting key, here the email.)
    rendered = str(excinfo.value) + repr(excinfo.value)
    assert secret_hash not in rendered
    assert "hidden due to hide_parameters" in rendered
