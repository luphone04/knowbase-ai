"""Ingestion worker entry point: `python -m app.worker.runner`.

Milestone 1 placeholder. It confirms database connectivity and then idles until
stopped. Claiming and processing ingestion jobs arrives in Milestone 4.
"""

import logging
import signal
import sys
import threading
from types import FrameType

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.core.logging import configure_logging
from app.db.session import get_engine

logger = logging.getLogger("app.worker")

DB_CONNECT_ATTEMPTS = 10
DB_CONNECT_DELAY_SECONDS = 2.0


def wait_for_database(stop: threading.Event) -> bool:
    for attempt in range(1, DB_CONNECT_ATTEMPTS + 1):
        try:
            with get_engine().connect() as connection:
                connection.execute(text("SELECT 1"))
            return True
        except SQLAlchemyError as exc:
            logger.warning(
                "Database not reachable (attempt %d/%d, %s)",
                attempt,
                DB_CONNECT_ATTEMPTS,
                type(exc).__name__,
            )
            if stop.wait(DB_CONNECT_DELAY_SECONDS):
                return False
    return False


def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.secret_values)

    stop = threading.Event()

    def request_stop(signum: int, _frame: FrameType | None) -> None:
        logger.info("Received %s, stopping", signal.Signals(signum).name)
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    if not wait_for_database(stop):
        if stop.is_set():
            return 0
        logger.error("Database unreachable after %d attempts; exiting", DB_CONNECT_ATTEMPTS)
        return 1

    logger.info("Worker ready; job processing is not implemented yet (Milestone 4)")
    while not stop.wait(settings.worker_poll_seconds):
        pass
    logger.info("Worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
