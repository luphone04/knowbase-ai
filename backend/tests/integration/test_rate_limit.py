"""Login and registration rate limits, through the API and against the limiter directly."""

import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import service
from app.auth.rate_limit import RateLimiter, RateLimitExceeded, RateLimitRule
from app.config import get_settings
from app.core import security
from app.db.models import RateLimitCounter
from app.db.session import get_engine
from tests.integration.conftest import TEST_PASSWORD, Tenant, new_client

pytestmark = pytest.mark.integration

LOGIN = "/api/v1/auth/login"
REGISTER = "/api/v1/auth/register"


@pytest.fixture(autouse=True)
def no_window_rollover(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the API-level tests inside a single rate-limit window.

    Windows are clock-aligned, so a test that happened to run across a
    rollover (every 15 minutes) would get a fresh allowance mid-test and fail
    by chance. A 10-year window removes that dependency on the wall clock.
    The rollover behaviour itself is pinned down, with an injected clock, in
    test_fixed_window_rollover_grants_a_fresh_allowance.
    """
    monkeypatch.setenv("AUTH_RATE_LIMIT_WINDOW_SECONDS", str(10 * 365 * 24 * 3600))
    get_settings.cache_clear()
    yield
    monkeypatch.setenv("AUTH_RATE_LIMIT_WINDOW_SECONDS", "900")
    get_settings.cache_clear()


def failed_login(email: str, ip: str) -> int:
    response = new_client(ip).post(LOGIN, json={"email": email, "password": "wrong-password-1"})
    return response.status_code


def test_repeated_failures_lock_the_email_even_for_the_right_password(tenant_a: Tenant) -> None:
    limit = get_settings().login_failure_limit_per_email
    # Each attempt from a different address: the limit follows the account, not the IP.
    statuses = [failed_login(tenant_a.email, f"203.0.113.{i}") for i in range(limit)]
    assert statuses == [401] * limit

    blocked = new_client("203.0.113.250").post(
        LOGIN, json={"email": tenant_a.email, "password": TEST_PASSWORD}
    )
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    retry_after = int(blocked.headers["Retry-After"])
    assert 0 < retry_after <= get_settings().auth_rate_limit_window_seconds


def test_email_lock_does_not_affect_other_accounts(tenant_a: Tenant, tenant_b: Tenant) -> None:
    for i in range(get_settings().login_failure_limit_per_email):
        failed_login(tenant_a.email, f"203.0.113.{i}")
    response = new_client().post(LOGIN, json={"email": tenant_b.email, "password": TEST_PASSWORD})
    assert response.status_code == 200


def email_counter(db: Session) -> int | None:
    """The per-email failure counter (the only login-failures bucket in these tests)."""
    db.expire_all()
    return db.scalar(
        select(RateLimitCounter.count).where(
            RateLimitCounter.bucket_key.startswith("login-failures-email:")
        )
    )


def test_successful_login_resets_the_failure_count(tenant_a: Tenant, db: Session) -> None:
    limit = get_settings().login_failure_limit_per_email
    for i in range(limit - 1):
        failed_login(tenant_a.email, f"203.0.113.{i}")
    assert email_counter(db) == limit - 1

    ok = new_client().post(LOGIN, json={"email": tenant_a.email, "password": TEST_PASSWORD})
    assert ok.status_code == 200
    assert email_counter(db) is None  # cleared, not merely decremented

    # A full allowance again: `limit` more failures are verified, then the lock.
    statuses = [failed_login(tenant_a.email, f"203.0.113.{100 + i}") for i in range(limit + 1)]
    assert statuses == [401] * limit + [429]


def test_rejected_attempts_do_not_extend_the_counter(tenant_a: Tenant, db: Session) -> None:
    limit = get_settings().login_failure_limit_per_email
    for i in range(limit + 4):
        failed_login(tenant_a.email, f"203.0.113.{i}")
    assert email_counter(db) == limit  # only attempts that reached verification


# --- concurrency -------------------------------------------------------------


@pytest.fixture
def verification_counter(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count calls that reach Argon2 (real or dummy), slowed to widen any race window.

    The real functions still run: this only observes and delays them.
    """
    calls = [0]
    lock = threading.Lock()

    def observed(real: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any) -> Any:
            with lock:
                calls[0] += 1
            time.sleep(0.3)  # the gap between "allowed?" and "failure recorded"
            return real(*args)

        return wrapper

    # Patched where authenticate() looks them up; the originals come from app.core.security.
    monkeypatch.setattr(service, "verify_password", observed(security.verify_password))
    monkeypatch.setattr(service, "burn_password_check", observed(security.burn_password_check))
    return calls


@pytest.mark.parametrize("account", ["registered", "unregistered"])
def test_concurrent_failures_cannot_exceed_the_email_limit(
    tenant_a: Tenant, verification_counter: list[int], account: str
) -> None:
    limit = get_settings().login_failure_limit_per_email
    attempts = 12
    email = tenant_a.email if account == "registered" else "nobody-here@example.com"
    # Different address per request: only the per-email rule can stop them.
    clients = [new_client(f"203.0.113.{i}") for i in range(attempts)]
    start = threading.Barrier(attempts)

    def attempt(client: TestClient) -> int:
        start.wait()  # release every request at the same moment
        response = client.post(LOGIN, json={"email": email, "password": "wrong-password-1"})
        return response.status_code

    with ThreadPoolExecutor(max_workers=attempts) as pool:
        statuses = list(pool.map(attempt, clients))

    assert verification_counter[0] == limit
    assert sorted(statuses) == [401] * limit + [429] * (attempts - limit)

    # The account stays locked, even for the right password, and nothing more is verified.
    retry = new_client("203.0.113.250").post(
        LOGIN, json={"email": email, "password": TEST_PASSWORD}
    )
    assert retry.status_code == 429
    assert verification_counter[0] == limit


def test_login_attempts_per_ip_are_limited() -> None:
    limit = get_settings().login_rate_limit_per_ip
    # Different (unknown) emails, so only the per-IP rule can trigger.
    statuses = [failed_login(f"user{i}@example.com", "203.0.113.77") for i in range(limit + 1)]
    assert statuses[:limit] == [401] * limit
    assert statuses[limit] == 429
    assert failed_login("other@example.com", "203.0.113.78") == 401  # another IP is unaffected


def test_registrations_per_ip_are_limited() -> None:
    limit = get_settings().register_rate_limit_per_ip
    client = new_client("203.0.113.80")
    statuses = [
        client.post(
            REGISTER,
            json={"email": f"r{i}@example.com", "password": TEST_PASSWORD, "workspace_name": "W"},
        ).status_code
        for i in range(limit + 1)
    ]
    assert statuses[:limit] == [201] * limit
    assert statuses[limit] == 429


def test_counters_store_no_raw_emails_or_addresses(tenant_a: Tenant, db: Session) -> None:
    failed_login(tenant_a.email, "203.0.113.5")
    keys = db.scalars(select(RateLimitCounter.bucket_key)).all()
    assert keys, "attempts should have been counted"
    for key in keys:
        assert tenant_a.email not in key and "203.0.113.5" not in key
        name, digest = key.split(":")
        assert len(digest) == 64  # hex HMAC-SHA256


# --- limiter behaviour, with an injected clock ------------------------------

RULE = RateLimitRule("test-rule", limit=3, window_seconds=60)


@pytest.fixture
def limiter() -> RateLimiter:
    return RateLimiter(get_engine(), get_settings().app_secret_key.get_secret_value())


def test_limit_resets_in_the_next_window(limiter: RateLimiter) -> None:
    start = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
    for _ in range(RULE.limit):
        limiter.hit(RULE, "id", now=start)
    with pytest.raises(RateLimitExceeded) as excinfo:
        limiter.hit(RULE, "id", now=start + timedelta(seconds=15))
    assert excinfo.value.headers["Retry-After"] == "45"
    limiter.hit(RULE, "id", now=start + timedelta(seconds=60))  # new window


def counter_values() -> list[int]:
    with get_engine().connect() as connection:
        return list(connection.execute(select(RateLimitCounter.count)).scalars())


def test_reserve_grants_exactly_limit_slots_per_window(limiter: RateLimiter) -> None:
    start = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
    for _ in range(RULE.limit):
        limiter.reserve(RULE, "id", now=start)
    for _ in range(5):
        with pytest.raises(RateLimitExceeded) as excinfo:
            limiter.reserve(RULE, "id", now=start + timedelta(seconds=15))
    assert excinfo.value.headers["Retry-After"] == "45"
    assert counter_values() == [RULE.limit]  # rejections took no slot
    limiter.reserve(RULE, "id", now=start + timedelta(seconds=60))  # new window


def test_fixed_window_rollover_grants_a_fresh_allowance(limiter: RateLimiter) -> None:
    """Known trade-off of clock-aligned fixed windows, kept visible (ADR 0004).

    Each window allows at most `limit`, but attempts just before and just after
    a rollover land in different windows: up to 2 x limit within moments.
    """
    rule = RateLimitRule("rollover", limit=5, window_seconds=900)
    rollover = datetime(2026, 11, 1, 15, 30, 0, tzinfo=UTC)
    granted = 0
    for moment in [rollover - timedelta(milliseconds=50)] * 7 + [rollover] * 7:
        try:
            limiter.reserve(rule, "id", now=moment)
            granted += 1
        except RateLimitExceeded:
            pass
    assert granted == 2 * rule.limit


def test_reset_restores_the_full_allowance(limiter: RateLimiter) -> None:
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
    for _ in range(RULE.limit):
        limiter.reserve(RULE, "id", now=now)
    limiter.reset(RULE, "id")
    for _ in range(RULE.limit):
        limiter.reserve(RULE, "id", now=now)
    with pytest.raises(RateLimitExceeded):
        limiter.reserve(RULE, "id", now=now)


def test_concurrent_reservations_never_exceed_the_limit(limiter: RateLimiter) -> None:
    rule = RateLimitRule("concurrent-reserve", limit=5, window_seconds=3600)
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
    callers = 40
    start = threading.Barrier(callers)

    def try_reserve(_: int) -> bool:
        start.wait()
        try:
            limiter.reserve(rule, "id", now=now)
        except RateLimitExceeded:
            return False
        return True

    with ThreadPoolExecutor(max_workers=callers) as pool:
        granted = list(pool.map(try_reserve, range(callers)))
    assert granted.count(True) == rule.limit
    assert counter_values() == [rule.limit]


def test_concurrent_hits_are_all_counted(limiter: RateLimiter) -> None:
    rule = RateLimitRule("concurrent-hit", limit=1000, window_seconds=3600)
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: limiter.hit(rule, "id", now=now), range(40)))
    assert counter_values() == [40]  # atomic upsert: no lost updates


def test_windows_longer_than_a_day_still_limit(limiter: RateLimiter) -> None:
    """Regression: pruning once deleted any window older than a day, the live one included."""
    rule = RateLimitRule("week", limit=3, window_seconds=7 * 24 * 3600)
    for _ in range(rule.limit):
        limiter.reserve(rule, "id")
    with pytest.raises(RateLimitExceeded):
        limiter.reserve(rule, "id")
    with pytest.raises(RateLimitExceeded):
        for _ in range(rule.limit + 1):
            limiter.hit(rule, "hit-id")


def test_pruning_never_touches_other_rules(limiter: RateLimiter) -> None:
    long_rule = RateLimitRule("long", limit=10, window_seconds=30 * 24 * 3600)
    short_rule = RateLimitRule("short", limit=10, window_seconds=60)
    limiter.reserve(long_rule, "id")
    limiter.reserve(short_rule, "id", now=datetime.now(UTC) + timedelta(days=2))
    assert sorted(counter_values()) == [1, 1]


def test_old_windows_are_pruned(limiter: RateLimiter) -> None:
    long_ago = datetime.now(UTC) - timedelta(days=3)
    limiter.hit(RULE, "old", now=long_ago)
    limiter.hit(RULE, "new")
    with get_engine().connect() as connection:
        windows = connection.execute(select(RateLimitCounter.window_start)).scalars().all()
    assert len(windows) == 1 and windows[0] > long_ago
