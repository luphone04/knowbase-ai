# 0008 — File-upload-only scope (website crawling removed)

- Status: Accepted (owner decision)
- Date: 3 November 2026
- Supersedes: [ADR 0007](0007-m5-website-crawler.md) (website crawler)

## Decision

Knowbase AI ingests **uploaded files only**: PDF, DOCX, TXT, and Markdown.
Website crawling, built in Milestone 5, is removed from the product, and
Milestone 5 is marked **DESCOPED**. The file-only cleanup is tracked
separately from the milestones and needs its own approval.

## What was removed

- **API:** `POST /sources/websites` and `GET /sources/{id}/pages`. Both
  return 404. Source ids are matched as UUIDs in the path, so a segment such
  as `websites` matches no route at all (404, not 405).
- **API response fields:** the website-only `source_url`, `settings`, and
  `page_counts` on sources, and `canonical_url` and `fetched_at` on
  documents.
- **Worker:** the crawl path (`app/ingestion/website.py`), the crawler
  hooks, the fenced progress writes used only by crawls, and
  `TransientJobError`.
- **Code:** the `app/crawler` package (SSRF-guarded transport, fetcher,
  robots.txt, sitemaps, HTML extraction, BFS), and `beautifulsoup4`.
- **Settings:** `MAX_CRAWL_PAGES`, `MAX_CRAWL_DEPTH`, `CRAWL_DELAY_SECONDS`,
  `CRAWL_TIMEOUT_SECONDS`, `CRAWL_MAX_DURATION_SECONDS`, `CRAWL_MAX_HTML_MB`,
  `CRAWL_USER_AGENT`, and `DAILY_CRAWL_REQUEST_LIMIT`. A local `.env` that
  still sets them loads fine, because unknown variables are ignored.
- **Tests:** about 310 crawler tests, removed with the code.

Git history keeps everything (the last commit with the crawler is
`2b8513e`). No recovery tag was created, by owner decision.

## What stays

- File ingestion: upload checks and safe storage, the parsers, cleaning,
  paragraph-aware chunking, and the embedding cache and daily budget.
- The PostgreSQL job queue: `FOR UPDATE SKIP LOCKED` claims, fencing
  tokens, heartbeat and lease recovery, retries and deferrals, re-indexing,
  and deletion.
- Authentication and workspace isolation; the Gemini integration, with
  embeddings in pgvector.
- **The generic source abstraction:**
  - `sources.kind` (now only `'file'`);
  - the worker's per-kind handler table (`IngestionWorker._handlers`);
  - `index_document`, the shared indexing path (unchanged detection, cache,
    budget, and the atomic fenced swap);
  - the nullable `sources.source_url`, `documents.canonical_url`
    (a document key within a source), and `documents.fetched_at` columns.

  **To add a source type later:**
  1. add its kind to `SOURCE_KINDS`;
  2. write a migration widening `ck_sources_kind`;
  3. register a handler that produces clean documents and calls
     `index_document`;
  4. add the API endpoint.

## Database: migration `0006_retire_website_crawling`

Migrations 0001–0005 are unchanged. 0006:
1. **Refuses** to run if any website source or crawl record exists, with
   instructions. Nothing is deleted silently.
2. With the explicit `alembic -x purge_website_sources=true upgrade head`,
   removes website sources only (their documents, chunks, crawl records, and
   jobs cascade), plus cached vectors no remaining chunk uses. Vectors
   shared with a file stay.
3. Drops `crawl_pages`, which takes `duplicate_of` with it.
4. Restricts `ck_sources_kind` to `('file')`.

**Downgrade** restores the website kind and an empty `crawl_pages`. Purged
website data is not restored, so back up first.

On the development database there was no website data, so 0006 applied
without purging anything. Every file-data count and checksum was identical
before and after (see `PROJECT_STATUS.md`).

## Consequences

- **Smaller attack surface:** the server never fetches a user-supplied URL,
  so there is no SSRF exposure. The worker's only outbound traffic is
  Gemini. The Compose `egress` network stays for that.
- **The spec is updated:** §1 product definition, §2.5, §3, §4, §6.1,
  §7.1/§7.3/§7.4 (§7.3 left as a descoped note), §9.6, §10.2, §11, §12,
  §14, §15.2, §16, §17.10 (DESCOPED, original brief kept for the record),
  §17.13, §18, and §19.
- **M8 (frontend)** builds upload-only source management, and citations
  show the filename and PDF page.
- **Found during the cleanup:** an M4 recovery timing issue. The worker's
  clock was used where the database clock was compared, so recovered jobs
  could briefly be "not yet due". It's fixed in its own commit with a
  deterministic test.
