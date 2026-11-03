"""Processing one claimed job: read -> parse -> clean -> chunk -> embed -> publish.

Ingestion is dispatched by source kind (`IngestionWorker._handlers`). Today the
only kind is "file". A future ingestion method adds a handler that produces
clean documents and indexes each with `index_document`, the shared path that
owns unchanged detection, embedding, and the atomic swap.

The previous good index is never at risk. Everything slow (parsing,
Gemini) happens first, with the old chunks untouched. The swap from old chunks
to new is a single transaction, guarded by the job's fencing token. If
anything fails before or during that transaction, PostgreSQL rolls back and
the last good version stays fully indexed and searchable.

Failures are sorted by what should happen next:
- permanent (bad file, no text, key missing or rejected): fail now, clear message;
- transient (Gemini overload or timeout, database hiccup, unexpected bug): retry
  with backoff, up to `max_attempts`;
- deferred (daily budget or Gemini daily quota): requeue for later without using
  an attempt, keeping finished embedding batches in the cache.

Logs carry ids, counts, and timings, never document text, vectors, or keys.
"""

import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from types import TracebackType
from typing import Any

from sqlalchemy import ColumnElement, Engine, delete, exists, func, insert, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.auth.rate_limit import RateLimiter
from app.config import Settings
from app.db.models import Chunk, Document, EmbeddingCacheEntry, IngestionJob, Source
from app.db.tenancy import bind_workspace
from app.ingestion.chunker import ChunkDraft, ChunkerConfig, chunk_document
from app.ingestion.cleaner import CleanDocument, build_clean_document
from app.ingestion.embedding import (
    EmbeddingBudget,
    EmbeddingDeferred,
    EmbeddingDisabled,
    EmbeddingOutcome,
    embed_document_texts,
)
from app.ingestion.parsers import UnsupportedDocument, parse_document
from app.ingestion.storage import FileStorage, StoredFileMissing
from app.providers.errors import (
    InvalidProviderResponse,
    ProviderConfigurationError,
    ProviderError,
    ProviderNotConfigured,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderRequestError,
    ProviderUnavailable,
)
from app.providers.interfaces import EmbeddingProvider
from app.worker.queue import (
    ClaimedJob,
    claim_next_job,
    engine_heartbeat,
    lock_owned_job,
    recover_stale_jobs,
    requeue_or_supersede,
)

logger = logging.getLogger(__name__)

MAX_RETRY_DELAY = timedelta(hours=1)
PROVIDER_QUOTA_RETRY = timedelta(hours=1)  # Gemini's daily quota resets at midnight Pacific
RETAINED_INDEX_NOTE = " The previously indexed version is still available."
UNEXPECTED_MESSAGE = (
    "An unexpected error occurred while processing this source (see the worker log)."
)
DATABASE_MESSAGE = "The database was temporarily unavailable."
MISSING_FILE_MESSAGE = "The uploaded file is missing from storage. Upload it again."


class JobLost(Exception):
    """This worker no longer owns the job (recovered by another worker, or deleted)."""


class PermanentJobError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class _Outcome:
    result: dict[str, Any]


@dataclass(frozen=True)
class IndexedDocument:
    document_id: uuid.UUID
    unchanged: bool
    chunks: int
    embedded: int  # texts sent to Gemini
    reused: int  # texts served from the embedding cache
    requests: int


# Runs inside the publishing transaction, with the locked job row and source.
PublishHook = Callable[[Session, IngestionJob, Source, IndexedDocument], None]


def _document_key(canonical_url: str | None) -> ColumnElement[bool]:
    if canonical_url is None:
        return Document.canonical_url.is_(None)
    return Document.canonical_url == canonical_url


class _Heartbeat:
    """Renews the job's lease in the background while it is processed."""

    def __init__(self, engine: Engine, job: ClaimedJob, interval: float) -> None:
        self._engine = engine
        self._job = job
        self._interval = interval
        self._stop = threading.Event()
        self.lost = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"heartbeat-{job.id}", daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                if not engine_heartbeat(self._engine, self._job):
                    self.lost.set()
                    return
            except DBAPIError:
                logger.warning("Heartbeat failed for job %s; will retry", self._job.id)

    def __enter__(self) -> "_Heartbeat":
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


class IngestionWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: sessionmaker[Session],
        engine: Engine,
        storage: FileStorage,
        embedding_provider_factory: Callable[[], EmbeddingProvider],
        worker_id: str,
    ) -> None:
        self.settings = settings
        self._session_factory = session_factory
        self._engine = engine
        self._storage = storage
        self._provider_factory = embedding_provider_factory
        self.worker_id = worker_id
        # One handler per source kind (app.db.models.SOURCE_KINDS).
        self._handlers: dict[str, Callable[[ClaimedJob], _Outcome]] = {
            "file": self._ingest_file,
        }
        self.budget = EmbeddingBudget(
            RateLimiter(engine, settings.app_secret_key.get_secret_value()),
            settings.daily_embedding_text_limit,
        )
        self.chunker = ChunkerConfig(
            target=settings.chunk_target_chars,
            maximum=settings.chunk_max_chars,
            overlap=settings.chunk_overlap_chars,
        )

    # --- loop entry points ------------------------------------------------------------

    def run_once(self) -> bool:
        """Claim and process one due job. False if there was nothing to do."""
        job = claim_next_job(self._session_factory, self.worker_id)
        if job is None:
            return False
        with _Heartbeat(self._engine, job, self.settings.job_heartbeat_seconds):
            self.process(job)
        return True

    def recover(self) -> int:
        return recover_stale_jobs(self._session_factory, self.settings.job_lease_seconds)

    # --- one job ------------------------------------------------------------------------

    @contextmanager
    def session(self, job: ClaimedJob) -> Iterator[Session]:
        """A session bound to the job's workspace: every tenant row is scoped to it."""
        with self._session_factory() as session:
            bind_workspace(session, job.workspace_id)
            yield session

    def process(self, job: ClaimedJob) -> None:
        started = time.perf_counter()
        outcome = "failed"
        try:
            if job.job_type == "delete":
                self._delete(job)
                outcome = "deleted"
            else:
                result = self._ingest(job)
                outcome = result.result["outcome"]
        except JobLost:
            outcome = "lost"
            logger.warning("Job %s is no longer owned by this worker; dropping it", job.id)
        except EmbeddingDeferred as exc:
            outcome = "deferred"
            self._defer(job, exc.run_at, exc.reason)
        except ProviderQuotaExhausted as exc:
            outcome = "deferred"
            self._defer(job, datetime.now(UTC) + PROVIDER_QUOTA_RETRY, exc.message)
        except (UnsupportedDocument, PermanentJobError) as exc:
            self._fail(job, exc.message)
        except EmbeddingDisabled as exc:
            self._fail(job, exc.message)
        except StoredFileMissing:
            self._fail(job, MISSING_FILE_MESSAGE)
        except (ProviderNotConfigured, ProviderConfigurationError, ProviderRequestError) as exc:
            self._fail(job, exc.message)
        except (ProviderUnavailable, ProviderRateLimited, InvalidProviderResponse) as exc:
            self._retry_or_fail(job, exc.message)
        except ProviderError as exc:
            self._retry_or_fail(job, exc.message)
        except DBAPIError:
            logger.exception("Database error processing job %s", job.id)
            self._retry_or_fail(job, DATABASE_MESSAGE)
        except Exception:
            logger.exception("Unexpected error processing job %s", job.id)
            self._retry_or_fail(job, UNEXPECTED_MESSAGE)
        logger.info(
            "Job %s type=%s workspace=%s source=%s attempt=%d/%d outcome=%s duration_ms=%d",
            job.id,
            job.job_type,
            job.workspace_id,
            job.source_id,
            job.attempts,
            job.max_attempts,
            outcome,
            round((time.perf_counter() - started) * 1000),
        )

    # --- ingest ---------------------------------------------------------------------------

    def _ingest(self, job: ClaimedJob) -> _Outcome:
        with self.session(job) as db:
            source = db.get(Source, job.source_id)
            if source is None:
                raise JobLost()
            kind = source.kind
        handler = self._handlers.get(kind)
        if handler is None:
            raise PermanentJobError(
                f'Sources of type "{kind}" are not supported. Delete this source.'
            )
        return handler(job)

    def _ingest_file(self, job: ClaimedJob) -> _Outcome:
        with self.session(job) as db:
            source = db.get(Source, job.source_id)
            if source is None:
                raise JobLost()
            if source.status == "deleting":
                raise PermanentJobError("This source is being deleted.")
            key = job.storage_key or source.storage_key
            if key is None or source.file_type is None:
                raise PermanentJobError("This source has no uploaded file to index.")
            file_type = source.file_type
            fallback_title = PurePosixPath(source.original_filename or source.display_name).stem
            source.status = "processing"
            db.commit()

        data = self._storage.read(key)
        parsed = parse_document(file_type, data)
        clean = build_clean_document(
            parsed,
            file_type=file_type,
            fallback_title=fallback_title or "Untitled",
            min_chars=self.settings.min_document_chars,
        )
        result: dict[str, Any] = {}

        def finish(
            db: Session, row: IngestionJob, source: Source, indexed: IndexedDocument
        ) -> None:
            if indexed.unchanged:
                result.update(outcome="unchanged")
            else:
                result.update(outcome="indexed", characters=len(clean.text))
                if "page_count" in clean.metadata:
                    result["pages"] = clean.metadata["page_count"]
            result.update(
                chunks=indexed.chunks,
                embedded_texts=indexed.embedded,
                reused_embeddings=indexed.reused,
                embedding_requests=indexed.requests,
                document_id=str(indexed.document_id),
            )
            self._mark_ready(source)
            self._finish(row, result)

        self.index_document(
            job,
            clean,
            canonical_url=None,
            extra_meta={"file_type": file_type},
            on_publish=finish,
        )
        self._remove_superseded_files(job)
        return _Outcome(result)

    def index_document(
        self,
        job: ClaimedJob,
        clean: CleanDocument,
        *,
        canonical_url: str | None,
        extra_meta: dict[str, Any],
        on_publish: PublishHook,
    ) -> "IndexedDocument":
        """Make `clean` the indexed version of one document of the job's source.

        `canonical_url` is the document's key within its source. A file source
        has exactly one document, keyed None. Unchanged content (same text hash, title, chunker,
        and model) keeps its chunks and costs no embeddings. Otherwise the
        chunks are rebuilt, embedded (cache, then budget, then Gemini), and
        swapped in by `_publish`. `on_publish` runs inside the publishing
        transaction, so whatever it records commits or rolls back with the index.
        """
        model = self.settings.gemini_embedding_model
        dimensions = self.settings.embedding_dimensions

        with self.session(job) as db:
            current = db.scalars(
                select(Document).where(
                    Document.source_id == job.source_id, _document_key(canonical_url)
                )
            ).one_or_none()
            current_chunks = (
                db.scalar(select(func.count()).where(Chunk.document_id == current.id))
                if current is not None
                else 0
            )
            unchanged = (
                current is not None
                and current_chunks
                and current.content_hash == clean.content_hash
                and current.title == clean.title
                and current.meta.get("chunker") == self.chunker.fingerprint
                and current.meta.get("embedding_model") == model
                and current.meta.get("embedding_dimensions") == dimensions
            )
        if unchanged and current is not None:
            indexed = IndexedDocument(current.id, True, int(current_chunks or 0), 0, 0, 0)
            with self.session(job) as db:
                row, source = self.lock_for_publish(db, job)
                on_publish(db, row, source, indexed)
                db.commit()
            return indexed

        drafts = chunk_document(clean, self.chunker)
        texts_by_hash = {draft.content_hash: draft.content for draft in drafts}
        embeddings = embed_document_texts(
            lambda: self.session(job),
            workspace_id=job.workspace_id,
            texts_by_hash=texts_by_hash,
            model=model,
            dimensions=dimensions,
            batch_size=self.settings.gemini_embedding_batch_size,
            budget=self.budget,
            provider_factory=self._provider_factory,
        )
        return self._publish(
            job,
            clean,
            drafts,
            embeddings,
            canonical_url=canonical_url,
            extra_meta=extra_meta,
            on_publish=on_publish,
        )

    def lock_for_publish(self, db: Session, job: ClaimedJob) -> tuple[IngestionJob, Source]:
        row = lock_owned_job(db, job)  # fencing: only the current owner may publish
        if row is None:
            raise JobLost()
        source = db.get(Source, job.source_id, with_for_update=True)
        if source is None:
            raise JobLost()
        if source.status == "deleting":
            raise PermanentJobError("This source is being deleted.")
        return row, source

    def _publish(
        self,
        job: ClaimedJob,
        clean: CleanDocument,
        drafts: list[ChunkDraft],
        embeddings: EmbeddingOutcome,
        *,
        canonical_url: str | None,
        extra_meta: dict[str, Any],
        on_publish: PublishHook,
    ) -> "IndexedDocument":
        """Swap in the new document and chunks atomically (all or nothing)."""
        model = self.settings.gemini_embedding_model
        vectors = embeddings.vectors
        with self.session(job) as db:
            row, source = self.lock_for_publish(db, job)
            document = db.scalars(
                select(Document)
                .where(Document.source_id == source.id, _document_key(canonical_url))
                .with_for_update()
            ).one_or_none()
            if document is None:
                document = Document(
                    workspace_id=job.workspace_id, source_id=source.id, canonical_url=canonical_url
                )
                db.add(document)
            document.title = clean.title
            document.content_hash = clean.content_hash
            document.text_content = clean.text
            document.meta = {
                **clean.metadata,
                **extra_meta,
                "chunker": self.chunker.fingerprint,
                "embedding_model": model,
                "embedding_dimensions": self.settings.embedding_dimensions,
                "chunk_count": len(drafts),
                "character_count": len(clean.text),
            }
            db.flush()

            db.execute(delete(Chunk).where(Chunk.document_id == document.id))
            if drafts:
                db.execute(
                    insert(Chunk),
                    [
                        {
                            "workspace_id": job.workspace_id,
                            "document_id": document.id,
                            "chunk_index": draft.index,
                            "content": draft.content,
                            "content_hash": draft.content_hash,
                            "embedding": vectors[draft.content_hash],
                            "embedding_model": model,
                            "meta": {
                                "title": clean.title,
                                **({"url": canonical_url} if canonical_url else {}),
                                "heading": draft.heading,
                                "page_start": draft.page_start,
                                "page_end": draft.page_end,
                                "char_start": draft.char_start,
                                "char_end": draft.char_end,
                            },
                        }
                        for draft in drafts
                    ],
                )
            indexed = IndexedDocument(
                document.id,
                False,
                len(drafts),
                embeddings.embedded,
                embeddings.reused,
                embeddings.requests,
            )
            on_publish(db, row, source, indexed)
            db.commit()
            return indexed

    @staticmethod
    def _mark_ready(source: Source) -> None:
        source.status = "ready"
        source.error_message = None
        source.last_indexed_at = datetime.now(UTC)

    @staticmethod
    def _finish(row: IngestionJob, result: dict[str, Any]) -> None:
        row.status = "succeeded"
        row.result = result
        row.last_error = None
        row.finished_at = datetime.now(UTC)
        row.locked_at = row.lock_token = row.locked_by = None

    def _remove_superseded_files(self, job: ClaimedJob) -> None:
        """Keep the current upload and any still waiting to be processed; drop the rest."""
        with self.session(job) as db:
            source = db.get(Source, job.source_id)
            if source is None:
                return
            keep = set(
                db.scalars(
                    select(IngestionJob.storage_key).where(
                        IngestionJob.source_id == source.id,
                        IngestionJob.status.in_(("queued", "running")),
                        IngestionJob.storage_key.is_not(None),
                    )
                )
            )
            if source.storage_key:
                keep.add(source.storage_key)
        removed = self._storage.delete_others(
            job.workspace_id, job.source_id, {k for k in keep if k}
        )
        if removed:
            logger.info("Removed %d superseded upload(s) for source %s", removed, job.source_id)

    # --- delete -----------------------------------------------------------------------------

    def _delete(self, job: ClaimedJob) -> None:
        with self.session(job) as db:
            if lock_owned_job(db, job) is None:
                raise JobLost()
            source = db.get(Source, job.source_id, with_for_update=True)
            if source is None:
                return
            hashes = set(
                db.scalars(
                    select(Chunk.content_hash)
                    .join(Document, Document.id == Chunk.document_id)
                    .where(Document.source_id == source.id)
                )
            )
            # Cascades to documents, chunks, and this source's jobs (this one included).
            db.execute(delete(Source).where(Source.id == source.id))
            if hashes:
                still_used = exists().where(
                    Chunk.content_hash == EmbeddingCacheEntry.content_hash,
                    Chunk.workspace_id == EmbeddingCacheEntry.workspace_id,
                )
                db.execute(
                    delete(EmbeddingCacheEntry).where(
                        EmbeddingCacheEntry.content_hash.in_(hashes), ~still_used
                    )
                )
            db.commit()
        self._storage.delete_source(job.workspace_id, job.source_id)

    # --- failure handling --------------------------------------------------------------------

    def _has_index(self, db: Session, source_id: Any) -> bool:
        return (
            db.scalar(
                select(Chunk.id)
                .join(Document, Document.id == Chunk.document_id)
                .where(Document.source_id == source_id)
                .limit(1)
            )
            is not None
        )

    def _fail(self, job: ClaimedJob, message: str) -> None:
        with self.session(job) as db:
            row = lock_owned_job(db, job)
            if row is None:
                return
            row.status = "failed"
            row.last_error = message
            row.finished_at = datetime.now(UTC)
            row.locked_at = row.lock_token = row.locked_by = None
            source = db.get(Source, job.source_id, with_for_update=True)
            if source is not None and source.status != "deleting":
                note = RETAINED_INDEX_NOTE if self._has_index(db, source.id) else ""
                source.status = "failed"
                source.error_message = message + note
            db.commit()

    def _retry_or_fail(self, job: ClaimedJob, message: str) -> None:
        with self.session(job) as db:
            row = lock_owned_job(db, job)
            if row is None:
                return
            if row.attempts >= row.max_attempts:
                db.rollback()
                self._fail(job, f"{message} Gave up after {row.attempts} attempts.")
                return
            delay = min(
                MAX_RETRY_DELAY,
                timedelta(seconds=self.settings.job_retry_base_seconds * 2 ** (row.attempts - 1)),
            )
            note = (
                f"{message} Retrying in {int(delay.total_seconds())} s "
                f"(attempt {row.attempts} of {row.max_attempts})."
            )
            requeued = requeue_or_supersede(
                db, row, run_at=datetime.now(UTC) + delay, message=note, count_attempt=True
            )
            source = db.get(Source, job.source_id, with_for_update=True)
            if requeued and source is not None and source.status != "deleting":
                source.status = "pending"
                source.error_message = note
            db.commit()

    def _defer(self, job: ClaimedJob, run_at: datetime, reason: str) -> None:
        with self.session(job) as db:
            row = lock_owned_job(db, job)
            if row is None:
                return
            requeued = requeue_or_supersede(
                db, row, run_at=run_at, message=reason, count_attempt=False
            )
            source = db.get(Source, job.source_id, with_for_update=True)
            if requeued and source is not None and source.status != "deleting":
                source.status = "pending"
                source.error_message = reason
            db.commit()
