"""Website sources: crawl, then index every page through the M4 path (SPEC.md §7.3, §7.6).

1. **Crawl** (app.crawler.crawl): robots.txt, sitemaps, bounded BFS. Network
   only; nothing is written except progress.
2. **Boilerplate:** a block that appears on at least half of the fetched pages
   (and at least 3) is site chrome that survived tag removal: a repeated
   banner, a newsletter blurb. It's dropped from every page.
3. **Index each page** with `IngestionWorker.index_document`, the same code
   that indexes files: clean, hash, chunk, embed (cache, then budget, then
   Gemini), and an atomic, fenced swap. Each page is one document keyed by
   its URL (`documents.canonical_url`), so a page whose text hash hasn't
   changed keeps its chunks and costs no embeddings. Each page commits on its
   own, so a crash or a budget deferral keeps the pages already done; the
   retry finds them unchanged.
4. **Finish** in one fenced transaction: record skipped and failed pages,
   remove documents for pages that are gone (404/410), now disallowed by
   robots.txt, or marked noindex, and mark the source ready with a summary.

The document key is the page's canonical URL when it's on the same site,
otherwise the URL it was fetched from. A canonical URL can't merge different
content: if two pages claim the same canonical URL with different text, the
second is keyed by its own URL. Identical text found at two URLs is indexed
once; the other page is recorded as a duplicate.

Failures stay page-level. A page that times out, errors, or has too little
text is reported and its previously indexed version is kept. The job fails
only if no page could be indexed, and retries only if a retry could help
(timeouts, 5xx, rate limiting).
"""

import math
from collections import Counter
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from sqlalchemy import and_, case, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.crawler.crawl import CrawlBlocked, Crawler, CrawlLimits, PageResult, RobotsUnreachable
from app.crawler.urls import origin_of
from app.db.models import CrawlPage, Document, IngestionJob, Source
from app.ingestion.cleaner import build_clean_document
from app.ingestion.parsers import Block, ParsedDocument, UnsupportedDocument
from app.worker.queue import ClaimedJob

if TYPE_CHECKING:
    from app.ingestion.pipeline import IndexedDocument, IngestionWorker

BOILERPLATE_MIN_PAGES = 3
# Page failures worth retrying the whole crawl for, when nothing could be indexed.
TRANSIENT_REASONS = frozenset(
    {"timeout", "network_error", "server_error", "rate_limited", "dns_failure"}
)
# Outcomes that mean the page's previous document should go.
GONE_REASONS = frozenset({"not_found", "robots", "noindex"})
MAX_SITEMAPS_REPORTED = 10


def repeated_blocks(pages: list[list[Block]]) -> set[str]:
    """Texts that occur on at least half of the pages (and at least 3 of them)."""
    if len(pages) < BOILERPLATE_MIN_PAGES:
        return set()
    threshold = max(BOILERPLATE_MIN_PAGES, math.ceil(len(pages) / 2))
    counts = Counter(text for blocks in pages for text in {block.text for block in blocks})
    return {text for text, count in counts.items() if count >= threshold}


def _page_values(job: ClaimedJob, result: PageResult, **fields: Any) -> dict[str, Any]:
    status = fields.pop("status", None) or result.status
    if result.reason == "duplicate" and status == "failed":
        status = "duplicate"
    values: dict[str, Any] = {
        "workspace_id": job.workspace_id,
        "source_id": job.source_id,
        "url": result.url,
        "final_url": result.final_url,
        "depth": result.depth,
        "discovered_via": result.discovered_via,
        "status": status,
        "reason": result.reason,
        "message": result.message,
        "http_status": result.http_status,
        "content_type": result.content_type,
        "title": result.page.title if result.page else None,
        "content_hash": None,
        "last_job_id": job.id,
        "fetched_at": result.fetched_at,
    }
    values.update(fields)
    return values


def _upsert_pages(db: Session, rows: list[dict[str, Any]], *, keep_document: bool) -> None:
    """Insert or update crawl rows. `keep_document`: a failed fetch keeps its old link."""
    if not rows:
        return
    statement = insert(CrawlPage).values(rows)
    excluded = statement.excluded
    updates: dict[str, Any] = {
        name: excluded[name]
        for name in (
            "final_url",
            "depth",
            "discovered_via",
            "status",
            "reason",
            "message",
            "http_status",
            "content_type",
            "last_job_id",
            "fetched_at",
        )
    }
    updates["updated_at"] = func.now()
    if keep_document:
        updates["title"] = func.coalesce(excluded.title, CrawlPage.title)
    else:
        updates["document_url"] = excluded.document_url
        updates["title"] = excluded.title
        updates["content_hash"] = excluded.content_hash
        updates["indexed_at"] = func.coalesce(excluded.indexed_at, CrawlPage.indexed_at)
        updates["changed_at"] = case(
            (
                and_(
                    excluded.content_hash.is_not(None),
                    CrawlPage.content_hash.is_distinct_from(excluded.content_hash),
                ),
                excluded.changed_at,
            ),
            else_=CrawlPage.changed_at,
        )
    db.execute(
        statement.on_conflict_do_update(
            index_elements=["source_id", "url"], set_=updates
        ).execution_options(synchronize_session=False)
    )


def _fallback_title(url: str) -> str:
    parts = urlsplit(url)
    path = parts.path.rstrip("/").rsplit("/", 1)[-1]
    return path or parts.hostname or "Untitled page"


def crawl_website(worker: "IngestionWorker", job: ClaimedJob) -> dict[str, Any]:
    from app.ingestion.pipeline import JobLost, PermanentJobError, TransientJobError

    settings = worker.settings
    with worker.session(job) as db:
        source = db.get(Source, job.source_id)
        if source is None:
            raise JobLost()
        if source.status == "deleting":
            raise PermanentJobError("This source is being deleted.")
        if not source.source_url:
            raise PermanentJobError("This website source has no URL.")
        start_url = source.source_url
        max_pages = min(
            int(source.settings.get("max_pages", settings.max_crawl_pages)),
            settings.max_crawl_pages,
        )
        max_depth = min(
            int(source.settings.get("max_depth", settings.max_crawl_depth)),
            settings.max_crawl_depth,
        )
        previous = {
            row.url: row.document_url
            for row in db.scalars(select(CrawlPage).where(CrawlPage.source_id == source.id))
        }
        source.status = "processing"
        db.commit()

    limits = CrawlLimits(
        max_pages=max_pages,
        max_depth=max_depth,
        delay_seconds=settings.crawl_delay_seconds,
        timeout_seconds=settings.crawl_timeout_seconds,
        max_duration_seconds=settings.crawl_max_duration_seconds,
        max_bytes=settings.crawl_max_html_mb * 1024 * 1024,
    )
    fetcher = worker.fetcher_factory(origin_of(start_url))
    crawler = Crawler(
        fetcher=fetcher,
        limits=limits,
        product_token=settings.crawler_product_token,
        clock=worker.crawl_clock,
        sleep=worker.crawl_sleep,
        on_progress=lambda progress: worker.report_progress(job, progress),
    )
    try:
        report = crawler.crawl(start_url)
    except CrawlBlocked as exc:
        raise PermanentJobError(exc.message) from exc
    except RobotsUnreachable as exc:
        raise TransientJobError(exc.message) from exc
    finally:
        fetcher.close()

    fetched = [r for r in report.pages if r.status == "fetched" and r.page is not None]
    boilerplate = repeated_blocks([r.page.blocks for r in fetched if r.page is not None])
    counts = Counter[str]()
    totals = Counter[str]()
    other_rows: list[dict[str, Any]] = []
    claimed: dict[str, str] = {}  # document key -> content hash, in this crawl
    by_hash: dict[str, str] = {}  # content hash -> document key, in this crawl
    live: set[str] = set()
    removable: set[str] = set()
    removed_blocks = 0

    for result in report.pages:
        if result.status != "fetched" or result.page is None:
            counts[result.status if result.reason != "duplicate" else "duplicate"] += 1
            if result.reason in GONE_REASONS and previous.get(result.url):
                removable.add(str(previous[result.url]))
                other_rows.append(_page_values(job, result, document_url=None))
            else:
                other_rows.append(_page_values(job, result))
            continue

        page = result.page
        blocks = [block for block in page.blocks if block.text not in boilerplate]
        removed_blocks += len(page.blocks) - len(blocks)
        url = result.final_url or result.url
        try:
            clean = build_clean_document(
                ParsedDocument(title=page.title, blocks=blocks, metadata={}),
                file_type="html",
                fallback_title=_fallback_title(url),
                min_chars=settings.min_document_chars,
            )
        except UnsupportedDocument as exc:
            counts["failed"] += 1
            result.status, result.reason, result.message = "failed", "too_little_text", exc.message
            other_rows.append(_page_values(job, result))
            continue

        key = result.canonical_url or url
        if key in claimed and claimed[key] != clean.content_hash:
            key = url  # the canonical URL is already taken by different content
        if clean.content_hash in by_hash or key in claimed:
            counts["duplicate"] += 1
            same_as = by_hash.get(clean.content_hash, key)
            result.reason, result.message = "duplicate", f"Same content as {same_as}."
            other_rows.append(
                _page_values(
                    job,
                    result,
                    status="duplicate",
                    document_url=same_as,
                    title=clean.title,
                    content_hash=clean.content_hash,
                )
            )
            continue
        claimed[key] = clean.content_hash
        by_hash[clean.content_hash] = key
        if previous.get(result.url) and previous[result.url] != key:
            removable.add(str(previous[result.url]))

        extra_meta: dict[str, Any] = {
            "source_kind": "website",
            "url": key,
            "fetched_url": url,
            "http_status": result.http_status,
            "depth": result.depth,
            "discovered_via": result.discovered_via,
            **page.metadata,
        }

        def record(
            db: Session,
            _row: IngestionJob,
            _source: Source,
            indexed: "IndexedDocument",
            result: PageResult = result,
            key: str = key,
            content_hash: str = clean.content_hash,
            title: str = clean.title,
        ) -> None:
            now = datetime.now(UTC)
            _upsert_pages(
                db,
                [
                    _page_values(
                        job,
                        result,
                        status="unchanged" if indexed.unchanged else "indexed",
                        document_url=key,
                        title=title,
                        content_hash=content_hash,
                        indexed_at=None if indexed.unchanged else now,
                        changed_at=now,
                    )
                ],
                keep_document=False,
            )

        indexed = worker.index_document(
            job,
            clean,
            canonical_url=key,
            extra_meta=extra_meta,
            fetched_at=result.fetched_at,
            on_publish=record,
        )
        live.add(key)
        counts["unchanged" if indexed.unchanged else "indexed"] += 1
        totals["chunks"] += indexed.chunks
        totals["embedded_texts"] += indexed.embedded
        totals["reused_embeddings"] += indexed.reused
        totals["embedding_requests"] += indexed.requests
        worker.report_progress(
            job, {"phase": "indexing", "pages_total": len(fetched), **counts, **totals}
        )

    usable = counts["indexed"] + counts["unchanged"]
    result_summary: dict[str, Any] = {
        "outcome": "crawled",
        "start_url": start_url,
        "robots": report.robots,
        "crawl_delay_seconds": report.crawl_delay,
        "sitemaps": report.sitemaps[:MAX_SITEMAPS_REPORTED],
        "stopped_reason": report.stopped_reason,
        "max_pages": max_pages,
        "max_depth": max_depth,
        "pages_discovered": report.counts.get("discovered", 0),
        "pages_requested": report.counts.get("requested", 0),
        "pages_indexed": counts["indexed"],
        "pages_unchanged": counts["unchanged"],
        "pages_duplicate": counts["duplicate"],
        "pages_skipped": report.counts.get("skipped", 0),
        "pages_failed": counts["failed"],
        "pages_removed": 0,
        "external_links_ignored": report.counts.get("external_links", 0),
        "boilerplate_blocks_removed": removed_blocks,
        "fetch_duration_ms": report.duration_ms,
        "chunks": totals["chunks"],
        "embedded_texts": totals["embedded_texts"],
        "reused_embeddings": totals["reused_embeddings"],
        "embedding_requests": totals["embedding_requests"],
    }

    with worker.session(job) as db:
        row, source = worker.lock_for_publish(db, job)
        _upsert_pages(db, [r for r in other_rows if "document_url" not in r], keep_document=True)
        _upsert_pages(db, [r for r in other_rows if "document_url" in r], keep_document=False)
        if usable == 0:
            db.commit()
            failures = [r for r in report.pages if r.status == "failed"]
            reason = failures[0].message if failures else "no indexable pages were found"
            message = f"No pages could be indexed: {reason}"
            if any(r.reason in TRANSIENT_REASONS for r in failures):
                raise TransientJobError(message)
            raise PermanentJobError(message)

        gone = removable - live
        if gone:
            removed = db.scalars(
                delete(Document)
                .where(Document.source_id == source.id, Document.canonical_url.in_(gone))
                .returning(Document.id)
                .execution_options(synchronize_session=False)
            ).all()
            result_summary["pages_removed"] = len(removed)
        worker._mark_ready(source)
        if counts["failed"]:
            source.error_message = (
                f"{counts['failed']} of {report.counts.get('requested', 0)} pages could not "
                "be indexed; see the page results."
            )
        worker._finish(row, result_summary)
        db.commit()
    return result_summary
