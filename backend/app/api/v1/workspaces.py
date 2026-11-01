"""The authenticated workspace as a resource.

The path id only selects a resource. Access comes from the session's bound
workspace and the caller's membership. Another workspace's id, a deleted one,
and a random guess all get the same 404, so responses never confirm that an
id exists.
"""

import uuid

from fastapi import APIRouter
from sqlalchemy import select

from app.api.dependencies import CurrentAuth, DbSession, Forbidden
from app.api.v1.schemas import MemberOut, WorkspaceOut, WorkspaceUpdateRequest
from app.core.errors import AppError
from app.db.models import User, Workspace, WorkspaceMember

router = APIRouter(prefix="/workspaces", tags=["workspaces"])

MANAGER_ROLES = ("owner", "admin")


class WorkspaceNotFound(AppError):
    def __init__(self) -> None:
        super().__init__(404, "not_found", "Workspace not found.")


def _load_for_member(db: DbSession, auth: CurrentAuth, workspace_id: uuid.UUID) -> WorkspaceOut:
    # WorkspaceMember is workspace-scoped, so this join only matches inside the
    # bound workspace even without an explicit workspace filter.
    row = db.execute(
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(Workspace.id == workspace_id, WorkspaceMember.user_id == auth.user_id)
    ).one_or_none()
    if row is None:
        raise WorkspaceNotFound()
    workspace, role = row
    return WorkspaceOut(
        id=workspace.id, name=workspace.name, role=role, created_at=workspace.created_at
    )


@router.get("/{workspace_id}")
def get_workspace(workspace_id: uuid.UUID, auth: CurrentAuth, db: DbSession) -> WorkspaceOut:
    return _load_for_member(db, auth, workspace_id)


@router.patch("/{workspace_id}")
def rename_workspace(
    workspace_id: uuid.UUID, body: WorkspaceUpdateRequest, auth: CurrentAuth, db: DbSession
) -> WorkspaceOut:
    current = _load_for_member(db, auth, workspace_id)  # 404 before 403: no existence leak
    if current.role not in MANAGER_ROLES:
        raise Forbidden()
    workspace = db.get_one(Workspace, current.id)
    workspace.name = body.name
    db.commit()
    return current.model_copy(update={"name": workspace.name})


@router.get("/{workspace_id}/members")
def list_members(workspace_id: uuid.UUID, auth: CurrentAuth, db: DbSession) -> list[MemberOut]:
    _load_for_member(db, auth, workspace_id)
    rows = db.execute(
        select(WorkspaceMember, User.email)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == workspace_id)
        .order_by(WorkspaceMember.created_at)
    )
    return [
        MemberOut(
            user_id=member.user_id, email=email, role=member.role, joined_at=member.created_at
        )
        for member, email in rows
    ]
