"""Login and registration rate limits, through the API and against the limiter directly."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.rate_limit import RateLimiter, RateLimitExceeded, RateLimitRule
from app.config import get_settings
from app.db.models import RateLimitCounter
from app.db.session import get_engine
from tests.integration.conftest import TEST_PASSWORD, Tenant, new_client

pytestmark = pytest.mark.integration

LOGIN = "/api/v1/auth/login"
REGISTER = "/api/v1/auth/register"


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


def test_successful_login_resets_the_failure_count(tenant_a: Tenant) -> None:
    limit = get_settings().login_failure_limit_per_email
    for i in range(limit - 1):
        failed_login(tenant_a.email, f"203.0.113.{i}")
    ok = new_client().post(LOGIN, json={"email": tenant_a.email, "password": TEST_PASSWORD})
    assert ok.status_code == 200
    assert [failed_login(tenant_a.email, "203.0.113.99") for _ in range(limit - 1)] == [401] * (
        limit - 1
    )


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


def test_check_does_not_count(limiter: RateLimiter) -> None:
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
    for _ in range(10):
        limiter.check(RULE, "id", now=now)
    limiter.hit(RULE, "id", now=now)


def test_concurrent_attempts_are_all_counted(limiter: RateLimiter) -> None:
    rule = RateLimitRule("concurrency", limit=1000, window_seconds=3600)
    now = datetime(2026, 11, 1, 12, 0, 0, tzinfo=UTC)
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: limiter.record(rule, "id", now=now), range(40)))
    with get_engine().connect() as connection:
        counts = connection.execute(select(RateLimitCounter.count)).scalars().all()
    assert counts == [40]  # atomic upsert: no lost updates


def test_old_windows_are_pruned(limiter: RateLimiter) -> None:
    long_ago = datetime.now(UTC) - timedelta(days=3)
    limiter.record(RULE, "old", now=long_ago)
    limiter.record(RULE, "new")
    with get_engine().connect() as connection:
        windows = connection.execute(select(RateLimitCounter.window_start)).scalars().all()
    assert len(windows) == 1 and windows[0] > long_ago
