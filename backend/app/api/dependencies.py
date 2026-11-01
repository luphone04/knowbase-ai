import logging
import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.rate_limit import RateLimiter
from app.auth.service import is_token_revoked
from app.config import Settings, get_settings
from app.core.errors import AppError
from app.core.security import AccessTokenClaims, InvalidTokenError, decode_access_token
from app.db.models import User, WorkspaceMember
from app.db.session import get_db, get_engine
from app.db.tenancy import bind_workspace

logger = logging.getLogger(__name__)

AUTH_COOKIE = "kb_session"

DbSession = Annotated[Session, Depends(get_db)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


class NotAuthenticated(AppError):
    def __init__(self) -> None:
        # Same response for a missing, invalid, expired, or revoked session.
        super().__init__(401, "not_authenticated", "Not authenticated.")


class Forbidden(AppError):
    def __init__(self) -> None:
        super().__init__(403, "forbidden", "You do not have permission to do this.")


@dataclass(frozen=True)
class AuthContext:
    user: User
    membership: WorkspaceMember
    claims: AccessTokenClaims

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id

    @property
    def workspace_id(self) -> uuid.UUID:
        return self.membership.workspace_id

    @property
    def role(self) -> str:
        return self.membership.role


def get_auth_context(request: Request, db: DbSession, settings: SettingsDep) -> AuthContext:
    """Authenticate the cookie and bind the request's session to its workspace.

    The workspace comes from the server-signed token and is re-checked against
    current membership on every request, never from client-supplied input.
    """
    token = request.cookies.get(AUTH_COOKIE)
    if not token:
        raise NotAuthenticated()
    try:
        claims = decode_access_token(token, secret=settings.jwt_secret.get_secret_value())
    except InvalidTokenError as exc:
        logger.info("Rejected access token (%s)", exc)
        raise NotAuthenticated() from exc
    if is_token_revoked(db, claims.token_id):
        raise NotAuthenticated()

    bind_workspace(db, claims.workspace_id)
    row = db.execute(
        select(User, WorkspaceMember)
        .join(WorkspaceMember, WorkspaceMember.user_id == User.id)
        .where(User.id == claims.user_id, User.is_active.is_(True))
    ).one_or_none()
    if row is None:  # user disabled or deleted, or membership removed
        raise NotAuthenticated()
    user, membership = row
    return AuthContext(user=user, membership=membership, claims=claims)


CurrentAuth = Annotated[AuthContext, Depends(get_auth_context)]


def get_rate_limiter(settings: SettingsDep) -> RateLimiter:
    return RateLimiter(get_engine(), settings.app_secret_key.get_secret_value())


RateLimiterDep = Annotated[RateLimiter, Depends(get_rate_limiter)]


def client_ip(request: Request) -> str:
    # The direct peer address. X-Forwarded-For is ignored: nothing in this stack
    # sets it, and a client could forge it to dodge per-IP limits.
    return request.client.host if request.client else "unknown"


def set_auth_cookie(response: Response, token: str, settings: Settings) -> None:
    response.set_cookie(
        AUTH_COOKIE,
        token,
        max_age=settings.access_token_expire_minutes * 60,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_auth_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        AUTH_COOKIE, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax"
    )
