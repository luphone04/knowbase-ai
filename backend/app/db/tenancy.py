"""Workspace isolation enforced in the ORM session.

A request session is bound to the authenticated workspace. From then on, every
ORM SELECT, UPDATE, and DELETE that touches a WorkspaceScoped model is filtered
to that workspace, including Session.get() and joins, even when the query
itself forgets to filter.

The guard fails closed. A session with no workspace bound raises
TenantScopeError when it queries a WorkspaceScoped model directly, and matches
no scoped rows reached indirectly (for example through a join). Code that must
work across workspaces, such as finding a user's memberships at login, opts
out per statement with `.execution_options(**SKIP_WORKSPACE_SCOPE)`, which
keeps every bypass visible and greppable.

Raw SQL (`text()`) and Core statements on Table objects are not covered. Code
that uses them must filter by workspace_id itself and needs its own isolation tests.
"""

import uuid
from typing import Any

from sqlalchemy import event, false
from sqlalchemy.orm import ORMExecuteState, Session, with_loader_criteria

from app.db.models import WorkspaceScoped

_SESSION_KEY = "workspace_id"
_SKIP_OPTION = "skip_workspace_scope"
SKIP_WORKSPACE_SCOPE: dict[str, Any] = {_SKIP_OPTION: True}


class TenantScopeError(RuntimeError):
    """A query on workspace-owned data without a workspace scope."""


def bind_workspace(session: Session, workspace_id: uuid.UUID) -> None:
    current = session.info.get(_SESSION_KEY)
    if current is not None and current != workspace_id:
        # Rebinding could return objects already loaded for the other workspace.
        raise TenantScopeError("session is already bound to a different workspace")
    session.info[_SESSION_KEY] = workspace_id


def bound_workspace(session: Session) -> uuid.UUID | None:
    workspace_id: uuid.UUID | None = session.info.get(_SESSION_KEY)
    return workspace_id


@event.listens_for(Session, "do_orm_execute")
def _apply_workspace_scope(state: ORMExecuteState) -> None:
    if not (state.is_select or state.is_update or state.is_delete):
        return
    if state.execution_options.get(_SKIP_OPTION):
        return

    workspace_id = bound_workspace(state.session)
    if workspace_id is None:
        scoped = [
            m.class_.__name__ for m in state.all_mappers if issubclass(m.class_, WorkspaceScoped)
        ]
        if scoped:
            raise TenantScopeError(
                f"query on workspace-owned {', '.join(scoped)} without a bound workspace"
            )
        state.statement = state.statement.options(
            with_loader_criteria(WorkspaceScoped, lambda cls: false(), include_aliases=True)
        )
        return

    state.statement = state.statement.options(
        with_loader_criteria(
            WorkspaceScoped,
            lambda cls: cls.workspace_id == workspace_id,
            include_aliases=True,
        )
    )
