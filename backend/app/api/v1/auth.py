import uuid
from datetime import timedelta

from fastapi import APIRouter, Request, Response

from app.api.dependencies import (
    AUTH_COOKIE,
    CurrentAuth,
    DbSession,
    RateLimiterDep,
    SettingsDep,
    clear_auth_cookie,
    client_ip,
    set_auth_cookie,
)
from app.api.v1.schemas import LoginRequest, RegisterRequest, SessionOut, UserOut, WorkspaceOut
from app.auth.rate_limit import AuthRateLimits
from app.auth.service import (
    InvalidCredentials,
    authenticate,
    normalize_email,
    primary_membership,
    register_user,
    revoke_token,
)
from app.config import Settings
from app.core.errors import AppError
from app.core.security import InvalidTokenError, create_access_token, decode_access_token
from app.db.models import User, Workspace, WorkspaceMember
from app.db.tenancy import bind_workspace

router = APIRouter(prefix="/auth", tags=["auth"])


def _session_out(user: User, workspace: Workspace, membership: WorkspaceMember) -> SessionOut:
    return SessionOut(
        user=UserOut(id=user.id, email=user.email, created_at=user.created_at),
        workspace=WorkspaceOut(
            id=workspace.id,
            name=workspace.name,
            role=membership.role,
            created_at=workspace.created_at,
        ),
    )


def _start_session(
    response: Response, user_id: uuid.UUID, workspace_id: uuid.UUID, settings: Settings
) -> None:
    token, _ = create_access_token(
        user_id,
        workspace_id,
        secret=settings.jwt_secret.get_secret_value(),
        lifetime=timedelta(minutes=settings.access_token_expire_minutes),
    )
    set_auth_cookie(response, token, settings)


@router.post("/register", status_code=201)
def register(
    body: RegisterRequest,
    request: Request,
    response: Response,
    db: DbSession,
    settings: SettingsDep,
    limiter: RateLimiterDep,
) -> SessionOut:
    """Create an account and its workspace, and sign the new user in."""
    limiter.hit(AuthRateLimits.from_settings(settings).register_per_ip, client_ip(request))
    user, workspace, membership = register_user(
        db, email=body.email, password=body.password, workspace_name=body.workspace_name
    )
    _start_session(response, user.id, workspace.id, settings)
    return _session_out(user, workspace, membership)


@router.post("/login")
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: DbSession,
    settings: SettingsDep,
    limiter: RateLimiterDep,
) -> SessionOut:
    limits = AuthRateLimits.from_settings(settings)
    email = normalize_email(body.email)
    limiter.hit(limits.login_per_ip, client_ip(request))
    limiter.check(limits.login_failures_per_email, email)
    try:
        user = authenticate(db, email=email, password=body.password)
    except InvalidCredentials:
        limiter.record(limits.login_failures_per_email, email)
        raise
    limiter.reset(limits.login_failures_per_email, email)

    membership = primary_membership(db, user.id)
    if membership is None:
        raise AppError(403, "no_workspace", "This account has no workspace.")
    bind_workspace(db, membership.workspace_id)
    workspace = db.get_one(Workspace, membership.workspace_id)
    _start_session(response, user.id, workspace.id, settings)
    return _session_out(user, workspace, membership)


@router.post("/logout", status_code=204)
def logout(request: Request, db: DbSession, settings: SettingsDep) -> Response:
    """End the session: revoke the token server-side and clear the cookie. Idempotent."""
    token = request.cookies.get(AUTH_COOKIE)
    if token:
        try:
            claims = decode_access_token(token, secret=settings.jwt_secret.get_secret_value())
        except InvalidTokenError:
            claims = None  # nothing to revoke; still clear the cookie
        if claims is not None:
            revoke_token(db, claims)
    response = Response(status_code=204)
    clear_auth_cookie(response, settings)
    return response


@router.get("/me")
def me(auth: CurrentAuth, db: DbSession) -> SessionOut:
    workspace = db.get_one(Workspace, auth.workspace_id)
    return _session_out(auth.user, workspace, auth.membership)
