# 0006 — Milestone 4 file ingestion and durable worker

- Status: Accepted (within SPEC.md §4, §6, §7, §8, §17.9; no architecture change)
- Date: 2 November 2026

## Flow

```
POST /sources/files ── validate ── store file ── upsert source ── enqueue job ── 202
                                                                    │ (PostgreSQL row)
worker loop: recover stale ── claim (SKIP LOCKED, commit) ── read ── parse ── clean
   ── unchanged? ──yes── mark ready (0 embeddings)
          └─no── chunk ── embed (cache → budget → Gemini) ── publish in ONE transaction
```

The API never parses or embeds; an upload returns in about a second. Long work runs only in the worker.

## Database (migration `0003_ingestion`)

- SPEC.md §6.1 tables: `sources`, `documents`, `chunks`, `ingestion_jobs`, plus `embedding_cache`. All are `WorkspaceScoped`, so the M2 session guard filters them automatically.
- **Tenant-safe composite keys.** `documents(workspace_id, source_id) → sources(workspace_id, id)`, `chunks(workspace_id, document_id) → documents(workspace_id, id)`, and `ingestion_jobs(workspace_id, source_id) → sources(...)`. The database itself rejects a row pointing into another workspace (tested).
- `chunks.embedding vector(768)` with an HNSW `vector_cosine_ops` index. `search_tsv` is generated (`to_tsvector('simple', …)`) with a GIN index, for M6. `chunks.embedding_model` is stored per chunk, so M6 never compares vectors from different models.
- **Partial unique indexes enforce idempotency in the database:**
  - one source per file name (case-insensitive `dedupe_key`) per workspace;
  - one document per file source;
  - `(document_id, chunk_index)` unique;
  - one *queued* job per source.
- **Extensions to SPEC.md §6.1:**
  - `sources`: `file_type`, `file_size`, `file_sha256`, `dedupe_key`, and status `deleting`;
  - `ingestion_jobs`: `lock_token`, `locked_by`, `storage_key`, `result`;
  - `chunks.embedding_model`, and the `embedding_cache` table.
- The migration is reversible (tested upgrade, downgrade, and re-upgrade), and `alembic check` confirms the models match it.

## Upload safety (SPEC.md §7.2)

- **Size:** a body-size middleware refuses oversized requests from `Content-Length`, or while streaming, before multipart parsing writes anything. Uploads get `MAX_UPLOAD_MB` plus 64 KiB of multipart framing; JSON bodies get 1 MiB. The endpoint re-checks the exact file size.
- **Type:** an extension allow-list (pdf, docx, txt, md). The bytes must then match: a `%PDF-` header; a real Word ZIP (`word/document.xml`, the main content type, not macro-enabled, at most 5,000 entries and 100 MB unpacked); or strict UTF-8 text with no NUL bytes or binary signatures. The browser's MIME type is ignored.
- **Names and storage:** the display name drops any path the client sent and strips control and bidi characters. Storage keys are generated: `<workspace>/<source>/<random hex>.<type>`. Every key must match that pattern and resolve inside `UPLOAD_DIR` (symlink escapes are refused). Files are written atomically with mode `0640`, are never executed, and keys and paths never appear in API responses.
- **Ordering:** the file is written before the job is committed, so a committed job never references a missing file.

## Parsing and cleaning

- **PDF (`pypdf`):** page by page, with the page number on every block. Password-protected and unreadable files are rejected clearly. Pages without text are recorded. **No OCR**: if text is below `MIN_DOCUMENT_CHARS`, the error says scanned PDFs need OCR, which isn't supported. Headings are detected *conservatively*: a short capitalised line, no closing punctuation, not ending on a function word, followed by a capitalised line. A wrapped sentence must never become a heading.
- **DOCX (`python-docx`):** body elements in reading order. Heading styles become headings and tables become `cell | cell` rows. The title comes from core properties, then the Title style, then the first heading.
- **TXT/MD:** strict UTF-8 (BOM allowed). Markdown `#` headings are recognised, and fenced code blocks stay whole.
- **Cleaning:** NFKC, which folds PDF ligatures; control characters removed; whitespace collapsed; at most one blank line. Blocks are joined with blank lines, each with its exact offsets. The SHA-256 of this text is the document `content_hash`.

## Chunking (SPEC.md §7.5)

Deterministic and paragraph-aware: target 1,800, maximum 2,400, overlap 250, all configurable.
- Every chunk is an exact slice of the document text (`content == text[char_start:char_end]`), so offsets are exact.
- **Paragraphs:** a paragraph that fits within `maximum − overlap` is never split. Longer ones are split into pieces of about `target − overlap` at sentence ends (closing quotes stay with their sentence), then at spaces, and only then by hard cut.
- **Headings:** a heading starts a new chunk once the current one is 40% full. A chunk never ends on a heading, and a heading is never a chunk on its own. There is no overlap across a section break.
- **Metadata:** each chunk records title, heading, PDF page range, offsets, and content hash. A chunker fingerprint (`paragraph-v1:t1800:m2400:o250`) is stored with the document.

## Durable queue and worker (SPEC.md §4.2)

- **Claim:** `UPDATE … WHERE id = (SELECT … WHERE status='queued' AND next_run_at <= now() ORDER BY next_run_at, created_at LIMIT 1 FOR UPDATE SKIP LOCKED)`. It is committed at once; no lock is held during parsing or Gemini calls. This is an explicit, auditable `SKIP_WORKSPACE_SCOPE` statement. Each job is then processed in a session bound to its workspace.
- **Fencing token:** every claim writes a fresh `lock_token`. Every later write (publish, retry, fail, heartbeat) requires it. A worker that stalls past its lease and loses the job to recovery can't publish, and its heartbeat is refused (tested).
- **Heartbeat and recovery:** a background thread renews `locked_at` every `JOB_HEARTBEAT_SECONDS` (30). A running job older than `JOB_LEASE_SECONDS` (120) belongs to a crashed worker: recovery requeues it, or fails it and its source if no attempts are left. Recovery runs at startup and every half lease. Verified live by stopping the worker with a job marked running: it was recovered on restart and finished on attempt 2.
- **Failure classes:**
  - *Permanent*, failed immediately with a clear message: bad or unsupported file, no text, password, missing file, missing or rejected key, `DAILY_EMBEDDING_TEXT_LIMIT=0`.
  - *Transient*, retried after 30 s × 2^(attempt−1) (capped at 1 h) up to `JOB_MAX_ATTEMPTS` (3): Gemini overload or timeout, an invalid provider response, a database error, unexpected bugs (generic message; details only in the log).
  - *Deferred*, requeued without using an attempt: the daily budget, or Gemini's daily quota.
- **One waiting job per source:** a newer upload updates the waiting job. A retry that finds a newer waiting job is marked superseded instead.
- **Delete** runs as a job. It removes the source, which cascades to documents, chunks, and jobs, then any cached embeddings no longer used in the workspace, then the files.

## Idempotency and replacement safety

- **Unchanged content:** same `content_hash`, chunker fingerprint, embedding model, and dimensions, with chunks present. The job finishes as `unchanged` with 0 Gemini calls and the chunks untouched (same ids). An identical re-upload of a ready source is short-circuited in the API (200, no job).
- **Changed content:** chunks are rebuilt. Unchanged chunk texts reuse their vectors from the cache, so only changed chunks go to Gemini (tested).
- **The last good index is never destroyed.** Parsing and embedding happen first, with the old chunks untouched. The swap (update document, delete old chunks, insert new ones, mark source ready, finish job) is one transaction, fenced by the lock token. Tests prove it both ways: when embedding fails on every attempt, the old chunks remain (same ids and content) and the source says "The previously indexed version is still available"; and a crash *inside* the publish transaction rolls back completely.

## Embedding cache and daily budget (SPEC.md §2.5, §8)

- **Cache key:** (workspace, chunk content hash, model, dimensions). Each batch is written as soon as it is embedded, so a later failure or a deferral never wastes finished work. There is no cross-workspace sharing (tested).
- **Budget scope:** **installation-wide**, `DAILY_EMBEDDING_TEXT_LIMIT` (300) texts per **UTC day**. Gemini's free-tier quota belongs to one Google project shared by every workspace on the deployment, so a per-workspace limit would let N workspaces spend N × 300.
- **Accounting:** only texts actually sent to Gemini count. Cached and unchanged texts never do. Each batch reserves its size **atomically before the call** (`INSERT … ON CONFLICT DO UPDATE SET count = count + n WHERE count + n <= limit`), so concurrent workers can't overspend (tested with 20 threads). Reservations **aren't refunded** if the call fails, because Gemini may already have counted it. That fails closed for the free tier. A batch that doesn't fit is refused whole.
- **When the limit is reached:** the job is deferred to 00:00 UTC without using an attempt, the source shows the reason, and finished batches stay cached. Batches are capped at the limit, so a small limit still makes progress. M4 counts document embeddings only; whether query embeddings (M7) share this budget is decided in M7.

## Known limitations

- **No OCR:** scanned PDFs are rejected, never faked. Legacy `.doc` and password-protected files are rejected too.
- **PDF headings are heuristic,** and only short capitalised lines qualify. PDF reading order is whatever `pypdf` extracts, so multi-column layouts may interleave.
- **One worker process, one job at a time** (by design for the MVP). Gemini latency dominates: about 0.5–0.9 s per embedding batch live, and much more during free-tier slowdowns.
- **A stopped worker is recovered by lease expiry,** so up to about 2 minutes plus the next recovery pass. Docker's default 10 s stop grace usually interrupts a running job, which is then recovered on restart.
- **The daily budget uses UTC days,** while Gemini's own daily quota resets at midnight Pacific. Gemini quota deferrals retry hourly.
- **Deleting a source removes its job history,** so `GET /jobs/{id}` for a finished delete job returns 404.
- The API trusts the PDF signature check plus worker-side parsing. A PDF that crashes `pypdf` fails its job cleanly, but `pypdf` itself runs in the worker process. There is no separate sandbox.
