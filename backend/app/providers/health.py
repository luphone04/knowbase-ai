"""The most recent provider outcome seen by this process, for the status endpoint.

It is process-local by design: the status check must never call Gemini itself
(SPEC.md §11), so it reports what real calls in this process last saw.
Categories and timestamps only, never messages.
"""

import threading
from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True)
class HealthSnapshot:
    last_error_category: str | None
    last_error_at: datetime | None
    last_success_at: datetime | None


class ProviderHealth:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._snapshot = HealthSnapshot(None, None, None)

    def record_success(self) -> None:
        with self._lock:
            self._snapshot = HealthSnapshot(None, None, datetime.now(UTC))

    def record_failure(self, category: str) -> None:
        with self._lock:
            self._snapshot = HealthSnapshot(
                category, datetime.now(UTC), self._snapshot.last_success_at
            )

    def snapshot(self) -> HealthSnapshot:
        with self._lock:
            return self._snapshot

    def reset(self) -> None:
        with self._lock:
            self._snapshot = HealthSnapshot(None, None, None)


provider_health = ProviderHealth()
