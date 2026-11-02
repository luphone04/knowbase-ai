"""Ingestion worker entry point: `python -m app.worker.runner`.

One process handling one job at a time (SPEC.md §4.2). The loop:
1. Every lease period, recover jobs abandoned by a crashed worker.
2. Claim the next due job (FOR UPDATE SKIP LOCKED) and process it.
3. Sleep `WORKER_POLL_SECONDS` when the queue is empty.

SIGTERM/SIGINT stop the loop after the current job. If the process is killed
mid-job, the job's lease lapses and the next recovery pass requeues it.
"""

import logging
import os
import signal
import socket
import sys
import threading
import time
import uuid
from types import FrameType

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.session import get_engine, get_sessionmaker
from app.ingestion.pipeline import IngestionWorker
from app.ingestion.storage import FileStorage
from app.providers.factory import get_embedding_provider

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

    worker = build_worker(settings)
    if not settings.gemini_configured:
        logger.warning("GEMINI_API_KEY is not set: jobs that need new embeddings will fail clearly")
    logger.info("Worker %s ready", worker.worker_id)
    run_loop(worker, stop, settings)
    logger.info("Worker stopped")
    return 0


def build_worker(settings: Settings) -> IngestionWorker:
    return IngestionWorker(
        settings=settings,
        session_factory=get_sessionmaker(),
        engine=get_engine(),
        storage=FileStorage(settings.upload_dir),
        embedding_provider_factory=get_embedding_provider,
        worker_id=f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}",
    )


def run_loop(worker: IngestionWorker, stop: threading.Event, settings: Settings) -> None:
    next_recovery = 0.0
    while not stop.is_set():
        try:
            if time.monotonic() >= next_recovery:
                worker.recover()
                next_recovery = time.monotonic() + settings.job_lease_seconds / 2
            if worker.run_once():
                continue  # more work may be waiting
        except SQLAlchemyError as exc:
            logger.warning("Worker loop database error (%s); backing off", type(exc).__name__)
        stop.wait(settings.worker_poll_seconds)


if __name__ == "__main__":
    sys.exit(main())
