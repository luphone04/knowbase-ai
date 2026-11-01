"""Registration, credential checks, and session revocation."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import case, delete, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.core.security import (
    AccessTokenClaims,
    burn_password_check,
    hash_password,
    verify_password,
)
from app.db.models import RevokedToken, User, Workspace, WorkspaceMember
from app.db.tenancy import SKIP_WORKSPACE_SCOPE, bind_workspace


class EmailAlreadyRegistered(AppError):
    def __init__(self) -> None:
        super().__init__(409, "email_taken", "An account with this email already exists.")


class InvalidCredentials(AppError):
    def __init__(self) -> None:
        # One message for unknown email, wrong password, and disabled account.
        super().__init__(401, "invalid_credentials", "Invalid email or password.")


def normalize_email(email: str) -> str:
    return email.strip().lower()


def register_user(
    db: Session, *, email: str, password: str, workspace_name: str
) -> tuple[User, Workspace, WorkspaceMember]:
    """Create a user, their workspace, and the owner membership in one transaction."""
    email = normalize_email(email)
    if db.scalar(select(User.id).where(User.email == email)) is not None:
        raise EmailAlreadyRegistered()

    user = User(email=email, password_hash=hash_password(password))
    db.add(user)
    try:
        db.flush()
    except IntegrityError as exc:  # a concurrent registration won the race
        db.rollback()
        raise EmailAlreadyRegistered() from exc

    workspace = Workspace(name=workspace_name, owner_user_id=user.id)
    db.add(workspace)
    db.flush()
    bind_workspace(db, workspace.id)
    membership = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role="owner")
    db.add(membership)
    db.commit()
    return user, workspace, membership


def authenticate(db: Session, *, email: str, password: str) -> User:
    user = db.scalar(select(User).where(User.email == normalize_email(email)))
    if user is None:
        burn_password_check(password)
        raise InvalidCredentials()

    valid, replacement_hash = verify_password(password, user.password_hash)
    if not valid or not user.is_active:
        raise InvalidCredentials()
    if replacement_hash is not None:
        user.password_hash = replacement_hash
        db.commit()
    return user


def primary_membership(db: Session, user_id: uuid.UUID) -> WorkspaceMember | None:
    """The workspace a new session opens in: the one the user owns, else the oldest."""
    statement = (
        select(WorkspaceMember)
        .where(WorkspaceMember.user_id == user_id)
        .order_by(case((WorkspaceMember.role == "owner", 0), else_=1), WorkspaceMember.created_at)
        .limit(1)
        # Cross-workspace by design: no workspace is chosen yet. Filtered by user instead.
        .execution_options(**SKIP_WORKSPACE_SCOPE)
    )
    return db.scalar(statement)


def revoke_token(db: Session, claims: AccessTokenClaims) -> None:
    db.execute(
        insert(RevokedToken)
        .values(jti=claims.token_id, user_id=claims.user_id, expires_at=claims.expires_at)
        .on_conflict_do_nothing(index_elements=["jti"])
    )
    db.execute(delete(RevokedToken).where(RevokedToken.expires_at < datetime.now(UTC)))
    db.commit()


def is_token_revoked(db: Session, token_id: uuid.UUID) -> bool:
    return db.get(RevokedToken, token_id) is not None
