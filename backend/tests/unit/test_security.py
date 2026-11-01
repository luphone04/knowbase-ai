import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from app.core.security import (
    TOKEN_AUDIENCE,
    TOKEN_ISSUER,
    InvalidTokenError,
    burn_password_check,
    create_access_token,
    decode_access_token,
    hash_password,
    verify_password,
)

SECRET = "unit-jwt-secret-0123456789abcdef0123"
PASSWORD = "correct horse battery staple"


# --- passwords -------------------------------------------------------------


def test_hash_is_argon2id_and_never_contains_the_password() -> None:
    hashed = hash_password(PASSWORD)
    assert hashed.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert PASSWORD not in hashed


def test_same_password_hashes_differently_each_time() -> None:
    assert hash_password(PASSWORD) != hash_password(PASSWORD)  # random salt


def test_verify_accepts_correct_and_rejects_wrong_password() -> None:
    hashed = hash_password(PASSWORD)
    assert verify_password(PASSWORD, hashed) == (True, None)
    assert verify_password("correct horse battery stapler", hashed)[0] is False
    assert verify_password("", hashed)[0] is False


def test_weaker_legacy_hash_is_upgraded_on_verify() -> None:
    legacy = PasswordHash((Argon2Hasher(time_cost=1, memory_cost=8192, parallelism=1),))
    old_hash = legacy.hash(PASSWORD)
    valid, replacement = verify_password(PASSWORD, old_hash)
    assert valid is True
    assert replacement is not None and replacement.startswith("$argon2id$v=19$m=65536,t=3,p=4$")


def test_dummy_check_for_unknown_users_runs_without_error() -> None:
    burn_password_check(PASSWORD)


# --- access tokens ---------------------------------------------------------


def make_token(now: datetime | None = None, lifetime: timedelta = timedelta(hours=1)) -> str:
    token, _ = create_access_token(
        uuid.uuid4(), uuid.uuid4(), secret=SECRET, lifetime=lifetime, now=now
    )
    return token


def payload(**overrides: object) -> dict[str, object]:
    now = datetime.now(UTC)
    base: dict[str, object] = {
        "sub": str(uuid.uuid4()),
        "wid": str(uuid.uuid4()),
        "jti": str(uuid.uuid4()),
        "iat": now,
        "nbf": now,
        "exp": now + timedelta(hours=1),
        "iss": TOKEN_ISSUER,
        "aud": TOKEN_AUDIENCE,
    }
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not None}


def test_round_trip_preserves_identity_and_workspace() -> None:
    user_id, workspace_id = uuid.uuid4(), uuid.uuid4()
    token, issued = create_access_token(
        user_id, workspace_id, secret=SECRET, lifetime=timedelta(minutes=30)
    )
    claims = decode_access_token(token, secret=SECRET)
    assert claims == issued
    assert (claims.user_id, claims.workspace_id) == (user_id, workspace_id)
    assert claims.expires_at - claims.issued_at == timedelta(minutes=30)


def test_each_token_has_a_unique_id() -> None:
    a = decode_access_token(make_token(), secret=SECRET)
    b = decode_access_token(make_token(), secret=SECRET)
    assert a.token_id != b.token_id


def test_expired_token_is_rejected() -> None:
    token = make_token(now=datetime.now(UTC) - timedelta(hours=2), lifetime=timedelta(hours=1))
    with pytest.raises(InvalidTokenError, match="ExpiredSignatureError"):
        decode_access_token(token, secret=SECRET)


def test_token_expiring_one_second_ago_is_rejected() -> None:
    now = datetime.now(UTC)
    token = make_token(now=now - timedelta(seconds=61), lifetime=timedelta(seconds=60))
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, secret=SECRET)


def test_not_yet_valid_token_is_rejected() -> None:
    token = make_token(now=datetime.now(UTC) + timedelta(minutes=10))
    with pytest.raises(InvalidTokenError, match="ImmatureSignatureError"):
        decode_access_token(token, secret=SECRET)


def test_token_signed_with_another_secret_is_rejected() -> None:
    forged = jwt.encode(payload(), "attacker-secret-0123456789abcdef01234", algorithm="HS256")
    with pytest.raises(InvalidTokenError, match="InvalidSignatureError"):
        decode_access_token(forged, secret=SECRET)


def test_tampered_payload_is_rejected() -> None:
    header, _, signature = make_token().split(".")
    other = jwt.encode(payload(), SECRET, algorithm="HS256").split(".")[1]
    with pytest.raises(InvalidTokenError):
        decode_access_token(f"{header}.{other}.{signature}", secret=SECRET)


def test_unsigned_alg_none_token_is_rejected() -> None:
    unsigned = jwt.encode(payload(), None, algorithm="none")
    with pytest.raises(InvalidTokenError):
        decode_access_token(unsigned, secret=SECRET)


# The warning comes from forging the attacker's HS512 token, not from our code.
@pytest.mark.filterwarnings("ignore::jwt.warnings.InsecureKeyLengthWarning")
def test_other_algorithm_with_the_right_secret_is_rejected() -> None:
    hs512 = jwt.encode(payload(), SECRET, algorithm="HS512")
    with pytest.raises(InvalidTokenError):
        decode_access_token(hs512, secret=SECRET)


@pytest.mark.parametrize("missing", ["sub", "wid", "jti", "iat", "nbf", "exp", "iss", "aud"])
def test_token_missing_a_required_claim_is_rejected(missing: str) -> None:
    token = jwt.encode(payload(**{missing: None}), SECRET, algorithm="HS256")
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, secret=SECRET)


@pytest.mark.parametrize(
    "overrides",
    [
        {"iss": "someone-else"},
        {"aud": "another-app"},
        {"sub": "not-a-uuid"},
        {"wid": "1 OR 1=1"},
    ],
)
def test_token_with_wrong_claim_values_is_rejected(overrides: dict[str, object]) -> None:
    token = jwt.encode(payload(**overrides), SECRET, algorithm="HS256")
    with pytest.raises(InvalidTokenError):
        decode_access_token(token, secret=SECRET)


@pytest.mark.parametrize("garbage", ["", "abc", "a.b.c", "Bearer x.y.z"])
def test_malformed_token_is_rejected(garbage: str) -> None:
    with pytest.raises(InvalidTokenError):
        decode_access_token(garbage, secret=SECRET)
