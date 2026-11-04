# 0010 — M0–M6 audit remediation

- Status: Accepted (fixes within SPEC.md; no new product features; SPEC.md not changed, see "Spec")
- Date: 4 November 2026
- Context: the M0–M6 engineering audit (0 critical, 3 high, about 20 medium findings). Each fix
  below has a regression test that failed on the previous code, unless marked as a coverage test.

## Untrusted documents (H1, H2, H3, M12, M13)

- **Linear parsing.** The Markdown heading pattern was cubic on long whitespace runs (one 20 KB
  line could hold the worker for hours). It is now a linear pattern, and lines over 1,000
  characters are never headings. Chunking is linear in the number of blocks.
- **Limits checked while parsing, not after:** `MAX_PDF_PAGES` (500, checked before any text is
  extracted), `MAX_DOCUMENT_CHARS` (2,000,000, accumulated per page or block, so a tiny PDF
  whose pages share one compressed stream stops early), and `MAX_DOCUMENT_CHUNKS` (1,500).
  DOCX archives are capped at 50 MB unpacked and 10 MB for the main part. Encrypted or damaged
  archives are a clear 422. Over a limit, the job fails once with a message naming the setting.
- **Parsing runs in a child process** (`forkserver`), killed after `PARSE_TIMEOUT_SECONDS` (120)
  and limited to `PARSE_MEMORY_LIMIT_MB` (1,536, Linux only; macOS has no `RLIMIT_AS`).
- **Every job has a deadline,** `JOB_MAX_RUNTIME_SECONDS` (900). The heartbeat stops renewing at
  the deadline, so a stuck job's lease lapses and recovery takes over. The job checks the deadline
  and its lease between steps and between embedding batches, and stops before any Gemini call
  once either is gone.

## One actor per source at a time (M1, M2, M3, M4, queue edge cases)

Decision: **per-source serialization with row locks, not one enforced worker and not distributed
locking.** Any number of workers stays safe, and the locks are PostgreSQL's own.

- Uploads, reindexes, and deletes lock the source row (`FOR UPDATE`) before checking its status.
  An upload racing a delete waits, then gets 409. A waiting delete job is never turned back into
  an ingest (`enqueue_job` returns None).
- `claim_next_job` skips sources that already have a running job.
- A job whose file was replaced stops before parsing, or before publishing if the new upload
  arrives mid-job ("superseded"). Older content never overwrites newer content.
- **Lock order is always source, then job** (API, worker, recovery), so writers can't deadlock.
  Recovery runs one short transaction per stale job and skips rows a request holds.
- Statuses: recovery and failures never overwrite `deleting`. A source returns to `pending`
  while a newer request waits. A failed or abandoned delete marks the source `failed` with
  "Delete it again" instead of leaving it stuck in `deleting`.
- **File cleanup** deletes only files of *finished* jobs that nothing references. An upload still
  being written, or saved but not yet committed, is never touched. An orphan left by a failed
  commit stays until the source is deleted.
- **Cache provenance** (migration 0008): `embedding_cache.source_id` (nullable, no foreign key)
  records which source first embedded a text. Deleting the source removes every cache row it
  created that no chunk still uses, including rows from older versions and unpublished batches.
  Rows from before 0008 are still removed by hash, as before.

## Gemini cost and reliability (M6 of the audit, M7, M8, M9)

- **One `generate()` is one request.** google-genai 2.x follows continuation tokens
  automatically by default; that is now disabled. A cut-off answer is `IncompleteGeneration`.
- **Classification:** 408 and 499 are temporary and retried. A non-JSON or unknown response shape
  is `InvalidProviderResponse`, not an unhandled 500.
- **Overall deadline:** `GEMINI_CALL_DEADLINE_SECONDS` (120). No retry starts after it, so a call
  lasts at most the deadline plus one request timeout. Before, the worst case was about 6 minutes.
- **Budget refunds:** a reservation (documents: `DAILY_EMBEDDING_TEXT_LIMIT`; searches: both
  query limits) is given back when Gemini refused the call outright (429, bad key, bad request).
  After a 5xx or a timeout it is kept, because Gemini may have counted the request.
- **Quota waits don't fail jobs.** A daily-quota stop defers the job to 08:05 UTC, which is after
  midnight Pacific in both winter and summer; it no longer retries hourly. A per-minute rate limit
  defers by Gemini's `RetryInfo` delay (30 s to 10 min) without using an attempt. Deferrals are
  not capped: they cost no budget, and Gemini doesn't count refused requests.
- **Query embeddings, decided:** a new installation-wide `DAILY_QUERY_EMBEDDING_LIMIT` (500) sits
  next to the per-workspace `DAILY_RETRIEVAL_REQUEST_LIMIT`. Searches therefore can't drain the
  shared free-tier project, and they stay separate from the document budget. A search spends no
  slot without a key, for an empty workspace, or when Gemini refused it.
- The base URL is pinned to `https://generativelanguage.googleapis.com/`, so
  `GOOGLE_GEMINI_BASE_URL` can't redirect the key.

## Database connections (M5 of the audit)

- A search ends its read transaction before the query embedding, so no connection is held while
  Gemini works. The Retriever adds and flushes its trace but doesn't commit: the caller owns the
  transaction, which M7 needs to save a chat turn atomically.

## Retrieval (M14, M16, determinism)

- A dash that starts a word is no longer NOT: "-tent" used to match every chunk *without* "tent".
  Quoted phrases and hyphenated words are unchanged.
- **The all-words AND is unchanged.** Whether to OR the words, or use the `english`
  configuration, is still an owner decision (ADR 0009, "Known limitation").
- NUL and other control characters in a query are rejected with 422 before any slot or Gemini call.
- When an exact tie crosses the top-k cut-off, the tied chunks are taken by id (SPEC.md §17.11).
- Coverage tests now run vector search through the HNSW index (iterative scan fills the limit) and
  test the explicit workspace filters with the session guard switched off.

## Security and hygiene (M10, M11, low findings)

- Log redaction now covers handlers that loggers own (uvicorn's tracebacks) and the database
  password.
- `.gitignore` covers `*.jar` and `cookies*.txt`. The hygiene check
  (`scripts/check_repo_hygiene.py`, run through the `.sh`):
  - reads the git index;
  - takes names NUL-separated;
  - scans binaries and zip members;
  - catches session tokens and cookie jars;
  - never prints a matched value.
- `ACCESS_TOKEN_EXPIRE_MINUTES` is at most 10,080, and the two secrets must differ.
- The API runs with `--no-proxy-headers`.
- An oversized chunked upload gets 413 (it used to get 400).
- Every setting is pinned in the test environment, with a guard test.

## Deferred (with reasons)

- **CORS headers on middleware errors (413/400/500):** matters once a browser frontend exists.
  Do it with M8. A 500 needs an extra ASGI layer inside CORS.
- **Missing indexes:** jobs by (workspace_id, source_id), documents by source_id, and chunks by
  (workspace_id, content_hash). They need a migration; table sizes are small until M9.
- **Migration 0006 breaks offline `alembic upgrade --sql`.** It is applied everywhere, and
  migrations 0001–0006 are frozen.
- **Container and CI hardening:**
  - digest pinning;
  - a CI image build;
  - `migrate` receiving the whole `.env`;
  - `no-new-privileges`.

  This is deployment work (M9).
- **Smaller items:**
  - the tenancy guard ignores INSERT…SELECT (no current caller);
  - the login lockout is shared by every client behind Docker Desktop's single IP;
  - headings over about 280 characters can become a chunk of their own;
  - the storage folder isn't fsynced after a rename;
  - truncated long file names can merge two files into one source;
  - failed searches leave no trace;
  - the follow-up length cap can cut mid-word.

## Spec

SPEC.md is unchanged. The behaviour changes are tightened limits and guardrails within §2.5,
§7.2, §11, §14, and §17.11, plus the dash fix within §9.3. Three are new settings the owner may
want recorded in the spec: the query-embedding limit, the document limits, and the job and parse
deadlines.
