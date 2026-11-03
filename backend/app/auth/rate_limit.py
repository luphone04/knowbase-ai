"""Database-backed fixed-window rate limits.

Counters live in PostgreSQL rather than Redis (SPEC.md §3.1), so they survive
API restarts and hold across processes. Each operation runs in its own short
transaction, independent of the request's session, so a failed login stays
counted even though the request then fails.

Bucket keys are HMAC-SHA256 digests of the identifier, keyed with
APP_SECRET_KEY, so the table never stores raw emails or IP addresses.

Every decision is a single atomic upsert, never a read followed by a separate
write. A read-then-write lets concurrent requests all see the same count and
all get through. `reserve()` takes a slot before the protected work starts, so
`limit` is a hard ceiling even under concurrency.

Fixed windows allow up to twice the limit across a window boundary. That's
acceptable for slowing password guessing in a local prototype.
"""

import hashlib
import hmac
import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import Engine, delete, func, select, update
from sqlalchemy.dialects.postgresql import insert

from app.config import Settings
from app.core.errors import AppError
from app.db.models import RateLimitCounter


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
        """Count this attempt, and reject it if it goes over the limit.

        Rejected attempts are counted too.
        """
        now = now or datetime.now(UTC)
        count = self._upsert(rule, identifier, now, capped=False)
        if count is None or count > rule.limit:
            raise RateLimitExceeded(self._retry_after(rule, now))

    def reserve(
        self, rule: RateLimitRule, identifier: str, *, now: datetime | None = None
    ) -> datetime:
        """Atomically take one of the window's `limit` slots, or reject.

        Concurrency-safe: during INSERT ... ON CONFLICT DO UPDATE, PostgreSQL
        locks the counter row and evaluates `WHERE count < limit` against the
        latest committed value, so concurrent callers can never take more than
        `limit` slots between them. A rejected attempt takes no slot, so the
        counter is exactly the number of attempts let through.
        Returns the window the slot was taken from (for `release_many`).
        """
        return self.reserve_many(rule, identifier, 1, now=now)

    def reserve_many(
        self, rule: RateLimitRule, identifier: str, amount: int, *, now: datetime | None = None
    ) -> datetime:
        """Atomically take `amount` slots at once, or none of them (same guarantees as reserve)."""
        if amount < 1:
            raise ValueError("amount must be at least 1")
        now = now or datetime.now(UTC)
        if (
            amount > rule.limit
            or self._upsert(rule, identifier, now, amount=amount, capped=True) is None
        ):
            raise RateLimitExceeded(self._retry_after(rule, now))
        return self._window_start(rule, now)

    def release_many(
        self, rule: RateLimitRule, identifier: str, amount: int, *, window_start: datetime
    ) -> None:
        """Give back `amount` slots taken in `window_start`, never going below zero.

        A window that has already ended is left alone: its slots no longer matter.
        """
        with self._engine.begin() as connection:
            connection.execute(
                update(RateLimitCounter)
                .where(
                    RateLimitCounter.bucket_key == self._key(rule, identifier),
                    RateLimitCounter.window_start == window_start,
                )
                .values(count=func.greatest(RateLimitCounter.count - amount, 0))
            )

    def remaining(
        self, rule: RateLimitRule, identifier: str, *, now: datetime | None = None
    ) -> int:
        now = now or datetime.now(UTC)
        with self._engine.connect() as connection:
            used = connection.execute(
                select(RateLimitCounter.count).where(
                    RateLimitCounter.bucket_key == self._key(rule, identifier),
                    RateLimitCounter.window_start == self._window_start(rule, now),
                )
            ).scalar_one_or_none()
        return max(0, rule.limit - (used or 0))

    def reset(self, rule: RateLimitRule, identifier: str) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                delete(RateLimitCounter).where(
                    RateLimitCounter.bucket_key == self._key(rule, identifier)
                )
            )

    def _upsert(
        self,
        rule: RateLimitRule,
        identifier: str,
        now: datetime,
        *,
        capped: bool,
        amount: int = 1,
    ) -> int | None:
        """Add `amount` to the window's counter in one statement; return the new count.

        With `capped`, the addition only happens while count + amount <= limit;
        otherwise no row comes back and None is returned. (Callers make sure
        amount <= limit, so the first insert is always allowed.)
        """
        window_start = self._window_start(rule, now)
        statement = (
            insert(RateLimitCounter)
            .values(bucket_key=self._key(rule, identifier), window_start=window_start, count=amount)
            .on_conflict_do_update(
                index_elements=["bucket_key", "window_start"],
                set_={"count": RateLimitCounter.count + amount},
                where=(RateLimitCounter.count + amount <= rule.limit) if capped else None,
            )
            .returning(RateLimitCounter.count)
        )
        with self._engine.begin() as connection:
            count: int | None = connection.execute(statement).scalar_one_or_none()
            # Drop this rule's finished windows. Pruning is per rule and relative to
            # the rule's own window, so it can't delete a live window of any length.
            connection.execute(
                delete(RateLimitCounter).where(
                    RateLimitCounter.bucket_key.startswith(f"{rule.name}:", autoescape=True),
                    RateLimitCounter.window_start < window_start,
                )
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
