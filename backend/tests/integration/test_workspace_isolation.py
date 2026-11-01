"""Two real tenants: A must not be able to read, change, or probe B's workspace."""

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.api.dependencies import AUTH_COOKIE
from app.config import get_settings
from app.core.security import create_access_token
from app.db.models import Workspace, WorkspaceMember
from app.db.tenancy import bind_workspace
from tests.integration.conftest import Tenant, new_client

pytestmark = pytest.mark.integration

NOT_FOUND = {"error": {"code": "not_found", "message": "Workspace not found."}}


def ws(workspace_id: object, suffix: str = "") -> str:
    return f"/api/v1/workspaces/{workspace_id}{suffix}"


# --- own workspace works -----------------------------------------------------


def test_member_can_read_own_workspace(tenant_a: Tenant) -> None:
    response = tenant_a.client.get(ws(tenant_a.workspace_id))
    assert response.status_code == 200
    assert response.json()["name"] == tenant_a.workspace_name
    assert response.json()["role"] == "owner"


def test_owner_can_rename_own_workspace(tenant_a: Tenant, db: Session) -> None:
    response = tenant_a.client.patch(ws(tenant_a.workspace_id), json={"name": "Renamed"})
    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"
    assert db.get_one(Workspace, tenant_a.workspace_id).name == "Renamed"


def test_members_list_shows_own_members_only(tenant_a: Tenant, tenant_b: Tenant) -> None:
    members = tenant_a.client.get(ws(tenant_a.workspace_id, "/members")).json()
    assert [(m["email"], m["role"]) for m in members] == [(tenant_a.email, "owner")]


# --- cross-workspace read / write -----------------------------------------------


def test_cannot_read_another_workspace(tenant_a: Tenant, tenant_b: Tenant) -> None:
    response = tenant_a.client.get(ws(tenant_b.workspace_id))
    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert tenant_b.workspace_name not in response.text


def test_cannot_list_another_workspaces_members(tenant_a: Tenant, tenant_b: Tenant) -> None:
    response = tenant_a.client.get(ws(tenant_b.workspace_id, "/members"))
    assert response.status_code == 404
    assert tenant_b.email not in response.text


def test_cannot_rename_another_workspace(tenant_a: Tenant, tenant_b: Tenant, db: Session) -> None:
    response = tenant_a.client.patch(ws(tenant_b.workspace_id), json={"name": "Hijacked"})
    assert response.status_code == 404
    assert db.get_one(Workspace, tenant_b.workspace_id).name == tenant_b.workspace_name
    assert tenant_b.client.get(ws(tenant_b.workspace_id)).json()["name"] == tenant_b.workspace_name


def test_isolation_holds_in_both_directions(tenant_a: Tenant, tenant_b: Tenant) -> None:
    assert tenant_b.client.get(ws(tenant_a.workspace_id)).status_code == 404
    assert tenant_b.client.patch(ws(tenant_a.workspace_id), json={"name": "X"}).status_code == 404


def test_unauthenticated_requests_get_nothing(tenant_b: Tenant) -> None:
    anonymous = new_client()
    assert anonymous.get(ws(tenant_b.workspace_id)).status_code == 401
    assert anonymous.patch(ws(tenant_b.workspace_id), json={"name": "X"}).status_code == 401


# --- UUID guessing -------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "suffix", "payload"),
    [("GET", "", None), ("PATCH", "", {"name": "X"}), ("GET", "/members", None)],
)
def test_real_foreign_id_and_random_guess_are_indistinguishable(
    tenant_a: Tenant, tenant_b: Tenant, method: str, suffix: str, payload: dict[str, str] | None
) -> None:
    def call(target: object) -> tuple[int, str]:
        response = tenant_a.client.request(method, ws(target, suffix), json=payload)
        return response.status_code, response.text

    real = call(tenant_b.workspace_id)
    guesses = {call(uuid.uuid4()) for _ in range(5)}
    assert guesses == {real}
    assert real[0] == 404


def test_brute_force_guessing_finds_nothing(tenant_a: Tenant, tenant_b: Tenant) -> None:
    candidates = [uuid.uuid4() for _ in range(200)]
    candidates += [uuid.UUID(int=0), uuid.UUID(int=2**128 - 1), tenant_b.workspace_id]
    statuses = {tenant_a.client.get(ws(c)).status_code for c in candidates}
    assert statuses == {404}


@pytest.mark.parametrize(
    "bad_id", ["not-a-uuid", "1 OR 1=1", "%27%20OR%20%271%27%3D%271", "..%2F..%2Fetc", "0" * 33]
)
def test_malformed_ids_are_rejected_cleanly(tenant_a: Tenant, bad_id: str) -> None:
    response = tenant_a.client.get(ws(bad_id))
    assert response.status_code in (404, 422)  # 404 when routing itself doesn't match
    assert "Traceback" not in response.text and "sqlalchemy" not in response.text.lower()


# --- client-supplied workspace hints are ignored -------------------------------


def test_workspace_hints_from_the_client_are_ignored(tenant_a: Tenant, tenant_b: Tenant) -> None:
    response = tenant_a.client.get(
        f"/api/v1/auth/me?workspace_id={tenant_b.workspace_id}",
        headers={"X-Workspace-Id": str(tenant_b.workspace_id)},
    )
    assert response.status_code == 200
    assert response.json()["workspace"]["id"] == str(tenant_a.workspace_id)


def test_swapping_another_users_cookie_is_the_only_way_in(
    tenant_a: Tenant, tenant_b: Tenant
) -> None:
    """Possession of B's session is what grants B's data, and nothing else does."""
    stolen = new_client()
    stolen.cookies.set(AUTH_COOKIE, tenant_b.client.cookies[AUTH_COOKIE])
    assert stolen.get(ws(tenant_b.workspace_id)).status_code == 200
    assert tenant_a.client.get(ws(tenant_b.workspace_id)).status_code == 404


# --- roles and membership changes ---------------------------------------------


def session_for(user_id: uuid.UUID, workspace_id: uuid.UUID) -> TestClient:
    token, _ = create_access_token(
        user_id,
        workspace_id,
        secret=get_settings().jwt_secret.get_secret_value(),
        lifetime=timedelta(hours=1),
    )
    client = new_client("198.51.100.60")
    client.cookies.set(AUTH_COOKIE, token)
    return client


def add_member(db: Session, workspace_id: uuid.UUID, user_id: uuid.UUID, role: str) -> None:
    bind_workspace(db, workspace_id)
    db.add(WorkspaceMember(workspace_id=workspace_id, user_id=user_id, role=role))
    db.commit()


def test_plain_member_can_read_but_not_rename(
    tenant_a: Tenant, tenant_b: Tenant, db: Session
) -> None:
    add_member(db, tenant_a.workspace_id, tenant_b.user_id, "member")
    client = session_for(tenant_b.user_id, tenant_a.workspace_id)
    assert client.get(ws(tenant_a.workspace_id)).json()["role"] == "member"
    response = client.patch(ws(tenant_a.workspace_id), json={"name": "Nope"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"
    assert db.get_one(Workspace, tenant_a.workspace_id).name == tenant_a.workspace_name


def test_admin_can_rename(tenant_a: Tenant, tenant_b: Tenant, db: Session) -> None:
    add_member(db, tenant_a.workspace_id, tenant_b.user_id, "admin")
    client = session_for(tenant_b.user_id, tenant_a.workspace_id)
    response = client.patch(ws(tenant_a.workspace_id), json={"name": "Admin rename"})
    assert response.status_code == 200


def test_removed_member_loses_access_immediately(
    tenant_a: Tenant, tenant_b: Tenant, db: Session
) -> None:
    add_member(db, tenant_a.workspace_id, tenant_b.user_id, "member")
    client = session_for(tenant_b.user_id, tenant_a.workspace_id)
    assert client.get(ws(tenant_a.workspace_id)).status_code == 200

    db.execute(delete(WorkspaceMember).where(WorkspaceMember.user_id == tenant_b.user_id))
    db.commit()
    # Same unexpired token: membership is checked per request, not trusted from the token.
    assert client.get(ws(tenant_a.workspace_id)).status_code == 401
