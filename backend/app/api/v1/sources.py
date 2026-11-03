"""Sources: upload files, add websites, inspect, re-index, delete (SPEC.md §11).

The API only validates, stores the file (or the URL), and enqueues a job; it
never parses, fetches pages, or embeds, so every request returns quickly. The
worker does the slow part. A submitted website URL is checked here (http(s)
only, no credentials, public host name, every DNS address public) for a
clear, early error. The worker checks again on every request and connection.
All access goes through the session bound to the caller's workspace: another
workspace's source id gets the same 404 as a random one.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile
from sqlalchemy import distinct, func, select
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import (
    CurrentAuth,
    DbSession,
    Forbidden,
    RateLimiterDep,
    SettingsDep,
)
from app.api.v1.schemas import (
    CrawlPageOut,
    DocumentOut,
    JobOut,
    JobSummaryOut,
    SourceDetailOut,
    SourceOut,
    UploadOut,
    WebsiteIn,
    WebsiteOut,
)
from app.auth.rate_limit import RateLimitRule
from app.config import Settings
from app.core.errors import AppError
from app.crawler.network import (
    BlockedDestination,
    DnsFailure,
    Resolver,
    resolve_public,
    system_resolver,
)
from app.crawler.urls import InvalidUrl, normalize_url, origin_of, skip_reason
from app.db.models import CRAWL_PAGE_STATUSES, Chunk, CrawlPage, Document, IngestionJob, Source
from app.ingestion.files import validate_upload
from app.ingestion.storage import FileStorage
from app.worker.queue import enqueue_job

router = APIRouter(prefix="/sources", tags=["sources"])

MANAGER_ROLES = ("owner", "admin")
RECENT_JOBS = 10
ACTIVE_JOB_STATUSES = ("queued", "running")
CRAWL_WINDOW_SECONDS = 24 * 3600


class SourceNotFound(AppError):
    def __init__(self) -> None:
        super().__init__(404, "not_found", "Source not found.")


class SourceBeingDeleted(AppError):
    def __init__(self) -> None:
        super().__init__(409, "source_deleting", "This source is being deleted.")


def get_storage(settings: SettingsDep) -> FileStorage:
    return FileStorage(settings.upload_dir)


StorageDep = Annotated[FileStorage, Depends(get_storage)]


def get_resolver() -> Resolver:
    """DNS for URL checks; tests override it so they never touch the network."""
    return system_resolver


ResolverDep = Annotated[Resolver, Depends(get_resolver)]


def crawl_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule(
        "crawl-requests-daily", settings.daily_crawl_request_limit, CRAWL_WINDOW_SECONDS
    )


def max_upload_bytes(settings: Settings) -> int:
    return settings.max_upload_mb * 1024 * 1024


# --- read helpers ------------------------------------------------------------------------


def job_out(job: IngestionJob) -> JobOut:
    return JobOut(
        id=job.id,
        source_id=job.source_id,
        job_type=job.job_type,
        status=job.status,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        next_run_at=job.next_run_at,
        last_error=job.last_error,
        result=job.result,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _source_outs(db: DbSession, sources: list[Source]) -> list[SourceOut]:
    ids = [s.id for s in sources]
    counts = {
        source_id: (documents, chunks)
        for source_id, documents, chunks in db.execute(
            select(Document.source_id, func.count(distinct(Document.id)), func.count(Chunk.id))
            .outerjoin(Chunk, Chunk.document_id == Document.id)
            .where(Document.source_id.in_(ids))
            .group_by(Document.source_id)
        )
    }
    latest = {
        job.source_id: job
        for job in db.scalars(
            select(IngestionJob)
            .where(IngestionJob.source_id.in_(ids))
            .order_by(IngestionJob.source_id, IngestionJob.created_at.desc())
            .ext(distinct_on(IngestionJob.source_id))  # newest job per source
        )
    }
    outs = []
    for source in sources:
        documents, chunks = counts.get(source.id, (0, 0))
        job = latest.get(source.id)
        outs.append(
            SourceOut(
                id=source.id,
                kind=source.kind,
                display_name=source.display_name,
                source_url=source.source_url,
                settings=source.settings,
                original_filename=source.original_filename,
                file_type=source.file_type,
                file_size=source.file_size,
                status=source.status,
                error_message=source.error_message,
                last_indexed_at=source.last_indexed_at,
                created_at=source.created_at,
                updated_at=source.updated_at,
                document_count=documents,
                chunk_count=chunks,
                latest_job=(
                    JobSummaryOut(
                        id=job.id,
                        job_type=job.job_type,
                        status=job.status,
                        last_error=job.last_error,
                    )
                    if job
                    else None
                ),
            )
        )
    return outs


def _get_source(db: DbSession, source_id: uuid.UUID) -> Source:
    source = db.get(Source, source_id)  # scoped to the caller's workspace by the session guard
    if source is None:
        raise SourceNotFound()
    return source


# --- endpoints --------------------------------------------------------------------------


@router.get("")
def list_sources(_: CurrentAuth, db: DbSession) -> list[SourceOut]:
    sources = list(db.scalars(select(Source).order_by(Source.created_at.desc())))
    return _source_outs(db, sources)


@router.post("/files", status_code=202, responses={200: {"model": UploadOut}})
def upload_file(
    auth: CurrentAuth,
    db: DbSession,
    settings: SettingsDep,
    storage: StorageDep,
    response: Response,
    file: Annotated[
        UploadFile, File(description="PDF, DOCX, TXT, or Markdown; up to MAX_UPLOAD_MB")
    ],
) -> UploadOut:
    """Store the file and queue it for indexing (202). Re-uploading a file with the
    same name updates that source; identical bytes on a ready source change nothing (200)."""
    limit = max_upload_bytes(settings)
    upload = validate_upload(file.filename, file.file.read(limit + 1), max_bytes=limit)
    dedupe_key = upload.original_filename.casefold()

    source = db.scalars(select(Source).where(Source.dedupe_key == dedupe_key)).one_or_none()
    if source is None:
        source = Source(
            workspace_id=auth.workspace_id,
            kind="file",
            display_name=upload.original_filename,
            original_filename=upload.original_filename,
            dedupe_key=dedupe_key,
            file_type=upload.file_type,
            status="pending",
        )
        try:
            with db.begin_nested():
                db.add(source)
        except IntegrityError:  # the same name was just uploaded concurrently
            source = db.scalars(select(Source).where(Source.dedupe_key == dedupe_key)).one()
    elif source.status == "deleting":
        raise SourceBeingDeleted()
    elif source.status == "ready" and source.file_sha256 == upload.sha256:
        response.status_code = 200
        return UploadOut(source=_source_outs(db, [source])[0], job=None, unchanged=True)

    # The file goes to disk before the job is committed, so a committed job never
    # points at a missing file. A failed commit only leaves an orphan, which the
    # worker removes the next time this source is indexed.
    key = storage.new_key(auth.workspace_id, source.id, upload.file_type)
    storage.save(key, upload.data)
    source.display_name = upload.original_filename
    source.original_filename = upload.original_filename
    source.file_type = upload.file_type
    source.storage_key = key
    source.file_size = upload.size
    source.file_sha256 = upload.sha256
    source.status = "pending"
    source.error_message = None
    job = enqueue_job(
        db,
        workspace_id=auth.workspace_id,
        source_id=source.id,
        job_type="ingest",
        storage_key=key,
        max_attempts=settings.job_max_attempts,
    )
    db.commit()
    return UploadOut(source=_source_outs(db, [source])[0], job=job_out(job), unchanged=False)


@router.get("/{source_id}")
def get_source(source_id: uuid.UUID, _: CurrentAuth, db: DbSession) -> SourceDetailOut:
    source = _get_source(db, source_id)
    summary = _source_outs(db, [source])[0]
    documents = [
        DocumentOut(
            id=document.id,
            title=document.title,
            canonical_url=document.canonical_url,
            fetched_at=document.fetched_at,
            content_hash=document.content_hash,
            chunk_count=chunk_count,
            metadata=document.meta,
            created_at=document.created_at,
            updated_at=document.updated_at,
        )
        for document, chunk_count in db.execute(
            select(Document, func.count(Chunk.id))
            .outerjoin(Chunk, Chunk.document_id == Document.id)
            .where(Document.source_id == source.id)
            .group_by(Document.id)
            .order_by(Document.created_at, Document.canonical_url)
        )
    ]
    jobs = [
        job_out(job)
        for job in db.scalars(
            select(IngestionJob)
            .where(IngestionJob.source_id == source.id)
            .order_by(IngestionJob.created_at.desc())
            .limit(RECENT_JOBS)
        )
    ]
    page_counts = {
        status: count
        for status, count in db.execute(
            select(CrawlPage.status, func.count())
            .where(CrawlPage.source_id == source.id)
            .group_by(CrawlPage.status)
        )
    }
    return SourceDetailOut(
        **summary.model_dump(), documents=documents, jobs=jobs, page_counts=page_counts
    )


@router.post("/websites", status_code=202, responses={200: {"model": WebsiteOut}})
def add_website(
    body: WebsiteIn,
    auth: CurrentAuth,
    db: DbSession,
    settings: SettingsDep,
    limiter: RateLimiterDep,
    resolver: ResolverDep,
    response: Response,
) -> WebsiteOut:
    """Add a public website and queue its first crawl (202).

    The same URL again returns the existing source (200, no new crawl); use
    POST /sources/{id}/reindex to crawl it again.
    """
    url = validate_website_url(body.url, resolver)
    max_pages = min(body.max_pages or settings.max_crawl_pages, settings.max_crawl_pages)
    max_depth = min(
        body.max_depth if body.max_depth is not None else settings.max_crawl_depth,
        settings.max_crawl_depth,
    )
    dedupe_key = f"url:{url}"  # can't collide with a file name: those never contain "/"

    source = db.scalars(select(Source).where(Source.dedupe_key == dedupe_key)).one_or_none()
    if source is not None:
        if source.status == "deleting":
            raise SourceBeingDeleted()
        response.status_code = 200
        return WebsiteOut(source=_source_outs(db, [source])[0], job=None, created=False)

    limiter.reserve(crawl_rule(settings), str(auth.workspace_id))
    source = Source(
        workspace_id=auth.workspace_id,
        kind="website",
        display_name=url,
        source_url=url,
        dedupe_key=dedupe_key,
        status="pending",
        settings={"max_pages": max_pages, "max_depth": max_depth},
    )
    try:
        with db.begin_nested():
            db.add(source)
    except IntegrityError:  # the same URL was just added concurrently
        source = db.scalars(select(Source).where(Source.dedupe_key == dedupe_key)).one()
        response.status_code = 200
        return WebsiteOut(source=_source_outs(db, [source])[0], job=None, created=False)
    job = enqueue_job(
        db,
        workspace_id=auth.workspace_id,
        source_id=source.id,
        job_type="ingest",
        storage_key=None,
        max_attempts=settings.job_max_attempts,
    )
    db.commit()
    return WebsiteOut(source=_source_outs(db, [source])[0], job=job_out(job), created=True)


def validate_website_url(raw: str, resolver: Resolver) -> str:
    try:
        url = normalize_url(raw)
    except InvalidUrl as exc:
        raise AppError(422, "invalid_url", exc.message) from exc
    origin = origin_of(url)
    try:
        resolve_public(origin.host, origin.port, resolver=resolver)
    except BlockedDestination as exc:
        raise AppError(422, "blocked_url", exc.message) from exc
    except DnsFailure as exc:
        raise AppError(422, "unresolvable_host", exc.message) from exc
    skip = skip_reason(url)
    if skip is not None:
        raise AppError(422, "invalid_url", skip[1])
    return url


@router.get("/{source_id}/pages")
def list_pages(
    source_id: uuid.UUID,
    _: CurrentAuth,
    db: DbSession,
    status: Annotated[str | None, Query(description="e.g. failed, skipped, indexed")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[CrawlPageOut]:
    """Per-page crawl results for a website source, in crawl order (depth, then URL)."""
    source = _get_source(db, source_id)
    if source.kind != "website":
        raise AppError(409, "not_a_website", "Only website sources have crawl pages.")
    if status is not None and status not in CRAWL_PAGE_STATUSES:
        raise AppError(
            422, "validation_error", f"status must be one of: {', '.join(CRAWL_PAGE_STATUSES)}."
        )
    latest = db.scalar(
        select(IngestionJob.id)
        .where(IngestionJob.source_id == source.id, IngestionJob.job_type != "delete")
        .where(IngestionJob.status.in_(("succeeded", "failed", "running")))
        .order_by(IngestionJob.created_at.desc())
        .limit(1)
    )
    query = select(CrawlPage).where(CrawlPage.source_id == source.id)
    if status is not None:
        query = query.where(CrawlPage.status == status)
    pages = db.scalars(query.order_by(CrawlPage.depth, CrawlPage.url).limit(limit).offset(offset))
    return [
        CrawlPageOut(
            id=page.id,
            url=page.url,
            final_url=page.final_url,
            document_url=page.document_url,
            depth=page.depth,
            discovered_via=page.discovered_via,
            status=page.status,
            reason=page.reason,
            message=page.message,
            http_status=page.http_status,
            content_type=page.content_type,
            title=page.title,
            content_hash=page.content_hash,
            in_latest_crawl=latest is not None and page.last_job_id == latest,
            first_seen_at=page.first_seen_at,
            fetched_at=page.fetched_at,
            indexed_at=page.indexed_at,
            changed_at=page.changed_at,
        )
        for page in pages
    ]


@router.post("/{source_id}/reindex", status_code=202)
def reindex_source(
    source_id: uuid.UUID,
    auth: CurrentAuth,
    db: DbSession,
    settings: SettingsDep,
    limiter: RateLimiterDep,
) -> JobOut:
    """Re-index the stored file, or crawl the website again.

    Unchanged content (files or pages) costs no embeddings. A website with a
    crawl already queued or running gets 409 rather than a second crawl.
    """
    source = _get_source(db, source_id)
    if source.status == "deleting":
        raise SourceBeingDeleted()
    if source.kind == "website":
        active = db.scalar(
            select(IngestionJob.id).where(
                IngestionJob.source_id == source.id,
                IngestionJob.status.in_(ACTIVE_JOB_STATUSES),
            )
        )
        if active is not None:
            raise AppError(
                409, "crawl_in_progress", f"A crawl of this website is already queued ({active})."
            )
        limiter.reserve(crawl_rule(settings), str(auth.workspace_id))
        source.status = "pending"
        job = enqueue_job(
            db,
            workspace_id=auth.workspace_id,
            source_id=source.id,
            job_type="reindex",
            storage_key=None,
            max_attempts=settings.job_max_attempts,
        )
        db.commit()
        return job_out(job)
    if source.storage_key is None:
        raise AppError(409, "no_file", "This source has no stored file to re-index.")
    source.status = "pending"
    job = enqueue_job(
        db,
        workspace_id=auth.workspace_id,
        source_id=source.id,
        job_type="reindex",
        storage_key=source.storage_key,
        max_attempts=settings.job_max_attempts,
    )
    db.commit()
    return job_out(job)


@router.delete("/{source_id}", status_code=202)
def delete_source(
    source_id: uuid.UUID, auth: CurrentAuth, db: DbSession, settings: SettingsDep
) -> JobOut:
    """Queue deletion of the source and everything derived from it.

    That is its documents (all pages, for a website), chunks, crawl results,
    cached embeddings no longer used elsewhere, and the stored file.

    When the job completes the source (and its job history) are gone:
    GET /sources/{id} then returns 404.
    """
    source = _get_source(db, source_id)  # 404 before 403: no existence leak
    if auth.role not in MANAGER_ROLES:
        raise Forbidden()
    source.status = "deleting"
    job = enqueue_job(
        db,
        workspace_id=auth.workspace_id,
        source_id=source.id,
        job_type="delete",
        storage_key=None,
        max_attempts=settings.job_max_attempts,
    )
    db.commit()
    return job_out(job)
