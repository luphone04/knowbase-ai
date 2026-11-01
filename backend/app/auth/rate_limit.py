"""Database-backed fixed-window rate limits.

Counters live in PostgreSQL rather than Redis (SPEC.md §3.1), so they survive
API restarts and hold across processes. Each operation runs in its own short
transaction, independent of the request's session, so a failed login stays
counted even though the request then fails.

Bucket keys are HMAC-SHA256 digests of the identifier, keyed with
APP_SECRET_KEY, so the table never stores raw emails or IP addresses.

Fixed windows allow up to twice the limit across a window boundary. That's
acceptable for slowing password guessing in a local prototype.
"""

import hashlib
import hmac
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, delete, select
from sqlalchemy.dialects.postgresql import insert

from app.config import Settings
from app.core.errors import AppError
from app.db.models import RateLimitCounter

_RETENTION = timedelta(days=1)


@dataclass(frozen=True)
class RateLimitRule:
    name: str
    limit: int
    window_seconds: int


@dataclass(frozen=True)
class AuthRateLimits:
    login_per_ip: RateLimitRule
    login_failures_per_email: RateLimitRule
    register_per_ip: RateLimitRule

    @classmethod
    def from_settings(cls, settings: Settings) -> "AuthRateLimits":
        window = settings.auth_rate_limit_window_seconds
        return cls(
            login_per_ip=RateLimitRule("login-ip", settings.login_rate_limit_per_ip, window),
            login_failures_per_email=RateLimitRule(
                "login-failures-email", settings.login_failure_limit_per_email, window
            ),
            register_per_ip=RateLimitRule(
                "register-ip", settings.register_rate_limit_per_ip, window
            ),
        )


class RateLimitExceeded(AppError):
    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__(
            429,
            "rate_limited",
            "Too many attempts. Try again later.",
            headers={"Retry-After": str(retry_after_seconds)},
        )


class RateLimiter:
    def __init__(self, engine: Engine, key_secret: str) -> None:
        self._engine = engine
        self._key_secret = key_secret.encode()

    def hit(self, rule: RateLimitRule, identifier: str, *, now: datetime | None = None) -> None:
        """Count this attempt, and reject it if it goes over the limit."""
        now = now or datetime.now(UTC)
        if self._increment(rule, identifier, now) > rule.limit:
            raise RateLimitExceeded(self._retry_after(rule, now))

    def check(self, rule: RateLimitRule, identifier: str, *, now: datetime | None = None) -> None:
        """Reject if the limit is already reached, without counting this attempt."""
        now = now or datetime.now(UTC)
        with self._engine.connect() as connection:
            count = connection.execute(
                select(RateLimitCounter.count).where(
                    RateLimitCounter.bucket_key == self._key(rule, identifier),
                    RateLimitCounter.window_start == self._window_start(rule, now),
                )
            ).scalar_one_or_none()
        if count is not None and count >= rule.limit:
            raise RateLimitExceeded(self._retry_after(rule, now))

    def record(self, rule: RateLimitRule, identifier: str, *, now: datetime | None = None) -> None:
        self._increment(rule, identifier, now or datetime.now(UTC))

    def reset(self, rule: RateLimitRule, identifier: str) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                delete(RateLimitCounter).where(
                    RateLimitCounter.bucket_key == self._key(rule, identifier)
                )
            )

    def _increment(self, rule: RateLimitRule, identifier: str, now: datetime) -> int:
        statement = (
            insert(RateLimitCounter)
            .values(
                bucket_key=self._key(rule, identifier),
                window_start=self._window_start(rule, now),
                count=1,
            )
            .on_conflict_do_update(
                index_elements=["bucket_key", "window_start"],
                set_={"count": RateLimitCounter.count + 1},
            )
            .returning(RateLimitCounter.count)
        )
        with self._engine.begin() as connection:
            count: int = connection.execute(statement).scalar_one()
            connection.execute(
                delete(RateLimitCounter).where(RateLimitCounter.window_start < now - _RETENTION)
            )
        return count

    def _key(self, rule: RateLimitRule, identifier: str) -> str:
        message = f"{rule.name}:{identifier}".encode()
        return f"{rule.name}:{hmac.new(self._key_secret, message, hashlib.sha256).hexdigest()}"

    @staticmethod
    def _window_start(rule: RateLimitRule, now: datetime) -> datetime:
        epoch = int(now.timestamp())
        return datetime.fromtimestamp(epoch - epoch % rule.window_seconds, UTC)

    def _retry_after(self, rule: RateLimitRule, now: datetime) -> int:
        window_end = self._window_start(rule, now) + timedelta(seconds=rule.window_seconds)
        return max(1, math.ceil((window_end - now).total_seconds()))
