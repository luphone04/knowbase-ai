"""The session-level workspace guard (app.db.tenancy), tested against real rows.

These queries deliberately leave out workspace filters. They stand in for a
future endpoint that forgets one, and show the guard still keeps tenants apart.
"""

import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import CursorResult, Executable, Result, delete, select, update
from sqlalchemy.orm import Session

from app.db.models import Workspace, WorkspaceMember
from app.db.session import get_sessionmaker
from app.db.tenancy import SKIP_WORKSPACE_SCOPE, TenantScopeError, bind_workspace
from tests.integration.conftest import Tenant, register_tenant

pytestmark = pytest.mark.integration

SessionFactory = Callable[..., Session]


@pytest.fixture
def make_session() -> Iterator[SessionFactory]:
    sessions: list[Session] = []

    def factory(workspace_id: uuid.UUID | None = None) -> Session:
        session = get_sessionmaker()()
        if workspace_id is not None:
            bind_workspace(session, workspace_id)
        sessions.append(session)
        return session

    yield factory
    for session in sessions:
        session.close()


def rowcount(result: Result[Any]) -> int:
    assert isinstance(result, CursorResult)
    return result.rowcount


def memberships(session: Session) -> list[tuple[uuid.UUID, uuid.UUID]]:
    return [(m.workspace_id, m.user_id) for m in session.scalars(select(WorkspaceMember))]


# --- unbound sessions fail closed ---------------------------------------------


def test_unbound_session_refuses_to_query_workspace_data(
    tenant_a: Tenant, make_session: SessionFactory
) -> None:
    session = make_session()
    with pytest.raises(TenantScopeError, match="WorkspaceMember"):
        session.scalars(select(WorkspaceMember)).all()


@pytest.mark.parametrize(
    "statement",
    [
        update(WorkspaceMember).values(role="admin"),
        delete(WorkspaceMember),
    ],
    ids=["bulk-update", "bulk-delete"],
)
def test_unbound_session_refuses_bulk_writes(
    tenant_a: Tenant, make_session: SessionFactory, statement: Executable
) -> None:
    session = make_session()
    with pytest.raises(TenantScopeError):
        session.execute(statement)
    session.rollback()
    assert memberships(make_session(tenant_a.workspace_id)) == [
        (tenant_a.workspace_id, tenant_a.user_id)
    ]


def test_unbound_join_into_workspace_data_matches_nothing(
    tenant_a: Tenant, make_session: SessionFactory
) -> None:
    session = make_session()
    statement = (
        select(Workspace.id)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == tenant_a.user_id)
    )
    try:
        rows = session.scalars(statement).all()
    except TenantScopeError:
        return  # refusing outright is also fail-closed
    assert rows == []


# --- bound sessions see and change only their own workspace -----------------


def test_unfiltered_query_returns_only_the_bound_workspace(
    tenant_a: Tenant, tenant_b: Tenant, make_session: SessionFactory
) -> None:
    assert memberships(make_session(tenant_a.workspace_id)) == [
        (tenant_a.workspace_id, tenant_a.user_id)
    ]
    assert memberships(make_session(tenant_b.workspace_id)) == [
        (tenant_b.workspace_id, tenant_b.user_id)
    ]


def test_explicit_filter_for_another_workspace_still_returns_nothing(
    tenant_a: Tenant, tenant_b: Tenant, make_session: SessionFactory
) -> None:
    session = make_session(tenant_a.workspace_id)
    rows = session.scalars(
        select(WorkspaceMember).where(WorkspaceMember.workspace_id == tenant_b.workspace_id)
    ).all()
    assert rows == []


def test_get_by_primary_key_of_another_workspace_returns_none(
    tenant_a: Tenant, tenant_b: Tenant, make_session: SessionFactory
) -> None:
    session = make_session(tenant_a.workspace_id)
    assert session.get(WorkspaceMember, (tenant_b.workspace_id, tenant_b.user_id)) is None
    assert session.get(WorkspaceMember, (tenant_a.workspace_id, tenant_a.user_id)) is not None


def test_bulk_update_cannot_reach_another_workspace(
    tenant_a: Tenant, tenant_b: Tenant, make_session: SessionFactory
) -> None:
    session = make_session(tenant_a.workspace_id)
    result = session.execute(
        update(WorkspaceMember)
        .where(WorkspaceMember.user_id == tenant_b.user_id)
        .values(role="member")
    )
    session.commit()
    assert rowcount(result) == 0
    b_view = make_session(tenant_b.workspace_id).scalars(select(WorkspaceMember.role)).all()
    assert b_view == ["owner"]


def test_bulk_delete_cannot_reach_another_workspace(
    tenant_a: Tenant, tenant_b: Tenant, make_session: SessionFactory
) -> None:
    session = make_session(tenant_a.workspace_id)
    result = session.execute(
        delete(WorkspaceMember).where(WorkspaceMember.workspace_id == tenant_b.workspace_id)
    )
    session.commit()
    assert rowcount(result) == 0
    assert memberships(make_session(tenant_b.workspace_id)) == [
        (tenant_b.workspace_id, tenant_b.user_id)
    ]
    assert tenant_b.client.get("/api/v1/auth/me").status_code == 200


def test_bulk_delete_inside_own_workspace_still_works(
    tenant_a: Tenant, tenant_b: Tenant, make_session: SessionFactory
) -> None:
    session = make_session(tenant_a.workspace_id)
    session.add(
        WorkspaceMember(workspace_id=tenant_a.workspace_id, user_id=tenant_b.user_id, role="member")
    )
    session.commit()
    # Unfiltered by workspace: the guard limits it to A, so B's own ownership survives.
    result = session.execute(
        delete(WorkspaceMember).where(WorkspaceMember.user_id == tenant_b.user_id)
    )
    session.commit()
    assert rowcount(result) == 1
    assert memberships(make_session(tenant_b.workspace_id)) == [
        (tenant_b.workspace_id, tenant_b.user_id)
    ]


# --- binding rules -------------------------------------------------------------


def test_session_cannot_be_rebound_to_another_workspace(
    tenant_a: Tenant, tenant_b: Tenant, make_session: SessionFactory
) -> None:
    session = make_session(tenant_a.workspace_id)
    bind_workspace(session, tenant_a.workspace_id)  # same workspace: fine
    with pytest.raises(TenantScopeError, match="already bound"):
        bind_workspace(session, tenant_b.workspace_id)


def test_concurrent_sessions_keep_separate_scopes(make_session: SessionFactory) -> None:
    tenants = [register_tenant(f"t{i}", f"198.51.100.{100 + i}") for i in range(3)]
    sessions = [make_session(t.workspace_id) for t in tenants]
    for tenant, session in zip(tenants, sessions, strict=True):
        assert memberships(session) == [(tenant.workspace_id, tenant.user_id)]


def test_cross_workspace_access_needs_an_explicit_bypass(
    tenant_a: Tenant, tenant_b: Tenant, make_session: SessionFactory
) -> None:
    session = make_session()
    rows = session.scalars(select(WorkspaceMember).execution_options(**SKIP_WORKSPACE_SCOPE)).all()
    assert {m.workspace_id for m in rows} == {tenant_a.workspace_id, tenant_b.workspace_id}
