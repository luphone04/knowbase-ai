"""Worker leases: a deadline for one unit of work and a heartbeat that renews its lock.

Shared by ingestion jobs and evaluation runs, which use the same pattern
(ADR 0006, 0010, 0014): a claimed row carries `locked_at` and a fencing token,
the heartbeat refreshes `locked_at` while the work runs, and a row whose lease
lapsed belongs to a crashed worker and can be taken over.
"""

import logging
import threading
import time
from collections.abc import Callable
from types import TracebackType

from sqlalchemy.exc import SQLAlchemyError

logger = logging.getLogger(__name__)


class Deadline:
    """When the work must stop, and whether its lease was lost to another worker."""

    def __init__(self, max_runtime: float, clock: Callable[[], float] = time.monotonic) -> None:
        self.max_runtime = max_runtime
        self._clock = clock
        self.deadline = clock() + max_runtime
        self.lost = threading.Event()

    def remaining(self) -> float:
        return max(0.0, self.deadline - self._clock())

    def expired(self) -> bool:
        return self._clock() >= self.deadline


class LeaseHeartbeat:
    """Calls `renew` every `interval` seconds while the work runs.

    `renew` returns False when the row is no longer ours: `deadline.lost` is set
    and renewal stops. Renewal also stops at the deadline, so work wedged where
    no checkpoint reaches loses its lease and is recovered instead of holding
    its row forever.
    """

    def __init__(
        self, renew: Callable[[], bool], *, interval: float, deadline: Deadline, name: str
    ) -> None:
        self._renew = renew
        self._interval = interval
        self._deadline = deadline
        self._name = name
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"heartbeat-{name}", daemon=True)

    @property
    def lost(self) -> threading.Event:
        return self._deadline.lost

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            if self._deadline.expired():
                logger.warning("%s passed its deadline; no longer renewing its lease", self._name)
                return
            try:
                if not self._renew():
                    self._deadline.lost.set()
                    return
            except SQLAlchemyError:  # includes pool timeouts, not only DBAPI errors
                logger.warning("Heartbeat failed for %s; will retry", self._name)

    def __enter__(self) -> "LeaseHeartbeat":
        self._thread.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join(timeout=self._interval)
