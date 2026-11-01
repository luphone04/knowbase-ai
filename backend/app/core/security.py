"""Password hashing and signed access tokens."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

# Argon2id, 64 MiB memory, 3 iterations, 4 lanes (pwdlib defaults; above OWASP minimums).
_password_hash = PasswordHash((Argon2Hasher(),))

# Verified against when an email is unknown, so a failed login costs one Argon2
# verification whether or not the account exists (no timing-based enumeration).
_DUMMY_HASH = _password_hash.hash("knowbase-timing-equalisation-only")


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str) -> tuple[bool, str | None]:
    """Return (valid, replacement_hash). A replacement is set when parameters changed."""
    return _password_hash.verify_and_update(password, password_hash)


def burn_password_check(password: str) -> None:
    _password_hash.verify(password, _DUMMY_HASH)


TOKEN_ALGORITHM = "HS256"
TOKEN_ISSUER = "knowbase-api"
TOKEN_AUDIENCE = "knowbase"
_REQUIRED_CLAIMS = ["sub", "wid", "jti", "iat", "nbf", "exp", "iss", "aud"]


class InvalidTokenError(Exception):
    """The token is missing, malformed, expired, or not signed by this server."""


@dataclass(frozen=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    workspace_id: uuid.UUID
    token_id: uuid.UUID
    issued_at: datetime
    expires_at: datetime


def create_access_token(
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    *,
    secret: str,
    lifetime: timedelta,
    now: datetime | None = None,
) -> tuple[str, AccessTokenClaims]:
    issued_at = (now or datetime.now(UTC)).replace(microsecond=0)
    claims = AccessTokenClaims(
        user_id=user_id,
        workspace_id=workspace_id,
        token_id=uuid.uuid4(),
        issued_at=issued_at,
        expires_at=issued_at + lifetime,
    )
    payload = {
        "sub": str(claims.user_id),
        "wid": str(claims.workspace_id),
        "jti": str(claims.token_id),
        "iat": claims.issued_at,
        "nbf": claims.issued_at,
        "exp": claims.expires_at,
        "iss": TOKEN_ISSUER,
        "aud": TOKEN_AUDIENCE,
    }
    return jwt.encode(payload, secret, algorithm=TOKEN_ALGORITHM), claims


def decode_access_token(token: str, *, secret: str) -> AccessTokenClaims:
    try:
        payload = jwt.decode(
            token,
            secret,
            # Pinned: rejects "none" and algorithm-substitution tokens.
            algorithms=[TOKEN_ALGORITHM],
            audience=TOKEN_AUDIENCE,
            issuer=TOKEN_ISSUER,
            options={"require": _REQUIRED_CLAIMS},
        )
        return AccessTokenClaims(
            user_id=uuid.UUID(payload["sub"]),
            workspace_id=uuid.UUID(payload["wid"]),
            token_id=uuid.UUID(payload["jti"]),
            issued_at=datetime.fromtimestamp(payload["iat"], UTC),
            expires_at=datetime.fromtimestamp(payload["exp"], UTC),
        )
    except (jwt.PyJWTError, ValueError, TypeError, KeyError) as exc:
        raise InvalidTokenError(type(exc).__name__) from exc
