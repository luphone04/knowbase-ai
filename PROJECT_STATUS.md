# Project status

| Field | Value |
|---|---|
| Current milestone | **M4 — File ingestion and durable worker** |
| Status | **READY_FOR_REVIEW**: checks pass, live Gemini ingestion PASS, pushed to `main`; awaiting `APPROVE MILESTONE 4` |
| GitHub repository | https://github.com/luphone04/knowbase-ai. Visibility: **private** (confirmed by owner 2026-11-01) |
| Workflow | Work on `main`; one milestone, then commit, push, report, and STOP ([ADR 0002](docs/decisions/0002-main-branch-workflow.md)) |
| Current branch | `main` |
| Last verified checkpoint | 2026-11-02 |

## Milestones

| Milestone | Scope | Status | Commit on `main` | Approval |
|---|---|---|---|---|
| M0 | Repository and compatibility preflight | APPROVED_AND_MERGED | `47b96a4` | `APPROVE MILESTONE 0`, 2026-11-01 |
| M1 | Database and service foundation | APPROVED_AND_MERGED | `e8d667a` | `APPROVE MILESTONE 1`, 2026-11-01 |
| M2 | Authentication and workspace isolation | APPROVED_AND_MERGED | `e488b7b` | `APPROVE MILESTONE 2`, 2026-11-02 |
| M3 | Gemini provider and embeddings | APPROVED_AND_MERGED | `bb36797` | `APPROVE MILESTONE 3`, 2026-11-02 |
| M4 | File ingestion and durable worker | READY_FOR_REVIEW | see Commits | Pending: `APPROVE MILESTONE 4` |
| M5 | Website crawler and incremental indexing | NOT_STARTED | — | — |
| M6 | Hybrid retrieval and RRF | NOT_STARTED | — | — |
| M7 | Grounded chat, citations, and abstention | NOT_STARTED | — | — |
| M8 | Frontend product experience | NOT_STARTED | — | — |
| M9 | Evaluation, hardening, and portfolio finish | NOT_STARTED | — | — |

## M4 checklist (SPEC.md §17.9)

Design and rationale: [ADR 0006](docs/decisions/0006-m4-file-ingestion.md).

- [x] Source, document, chunk, job (and embedding cache) schema; migration `0003_ingestion`. It is reversible, and `alembic check` shows it matches the models
- [x] File type, signature, size, UTF-8, and safe storage name validated; uploads are size-limited before parsing
- [x] PDF (`pypdf`), DOCX (`python-docx`), TXT, and Markdown parsers
- [x] PDF page numbers and document metadata preserved (title, page count, tables, chunker fingerprint, embedding model)
- [x] Text cleaning and deterministic paragraph-aware chunking (1800 / 2400 / 250)
- [x] Durable PostgreSQL job queue
- [x] Atomic claiming with `FOR UPDATE SKIP LOCKED`, committed before slow work; fencing token
- [x] Retry count with backoff, terminal failure, deferral, and stale-lock recovery with heartbeat
- [x] Chunks embedded through the M3 Gemini provider (`RETRIEVAL_DOCUMENT`, 768, batches of 8)
- [x] 768-dimensional normalised vectors persisted in pgvector (HNSW cosine index)
- [x] Idempotent re-ingestion: no duplicate documents or chunks, no Gemini calls for unchanged content
- [x] A failed replacement never deletes the last good index (tested for both embedding failure and a crash inside the publish transaction)
- [x] Daily embedding guardrail (300 texts per UTC day, installation-wide, cached texts excluded), deferred from M3
- [x] Unit and integration tests

## M4 acceptance evidence (2026-11-02)

| Criterion | Command | Result |
|---|---|---|
| Upload creates a source and a queued job | `test_sources_api.py`; live `curl` | PASS: 202 in about 1 s; the API never parses or embeds (a test fails if it calls the provider) |
| Worker processes a real supported file | live PDF and DOCX through the real worker and Gemini | PASS (details below) |
| Chunks and embeddings in PostgreSQL | `vector_dims`, `vector_norm` via `psql` | PASS: 768 dims, norm 1.000000 |
| Unchanged re-upload: no duplicate chunks, no re-embedding | live: identical upload → 200 `unchanged`; reindex → `unchanged`, 0 requests; tests | PASS |
| Malformed and unsupported files fail clearly | live: damaged PDF → job failed (1 attempt, "could not be read"); `.exe` → 415; fake PDF → 422; tests for scanned and encrypted PDFs | PASS |
| Full suite on host (same as CI) | `uv run pytest` against throwaway pgvector | **PASS: 480 passed, 0 skipped** (307 unit, 173 integration; **177 new in M4**) |
| Full suite in the api container | `docker compose exec api pytest -q` | PASS: 477 passed, 3 skipped (`.env.example` isn't in the image; those 3 run on host and CI) |
| M0–M3 regression | same runs | PASS |
| Lint / format / strict types / lock | `ruff check`, `ruff format --check`, `mypy` (strict, 85 files), `uv lock --check` | PASS |
| Compose | `docker compose config`; migrate applied `0003_ingestion` to the dev DB | PASS |
| Tests catch regressions (mutation check) | 20 protections broken one at a time, then restored with `git checkout` (list below) | PASS: all 20 caught after two gaps were fixed; tree clean |
| No secrets or content in logs | 324 lines of Compose logs checked against the Gemini key, JWT secret, test password, and document text | PASS: none present |
| Hygiene | `scripts/check_repo_hygiene.sh` | PASS |

**Mutations caught:**
1. Committing between the old-chunk delete and the new insert.
2. Unchanged detection off.
3. Claiming without a row lock.
4. No fencing check.
5. Recovery disabled.
6. Budget not reserved.
7. Cache ignored.
8. Worker session not workspace-bound (the M2 guard fails closed).
9. PDF signature unchecked.
10. File names unsanitised.
11. Storage-key pattern unchecked.
12. Bad files retried.
13. Deferral using an attempt.
14. Delete keeping cache orphans.
15. Body limit off.
16. Source list unscoped.
17. Identical re-upload re-queued.
18. Chunk overlap off.
19. Budget reservation uncapped.
20. Vector-count check off.

### Live ingestion (real Gemini, free tier, dev stack on port 8001)

| Step | Result |
|---|---|
| Upload `northstar-returns.pdf` (2 pages) and `northstar-warranty.docx` (heading, paragraphs, table) | 202 each; jobs succeeded on attempt 1 |
| PDF | title "Northstar Returns Policy (fictional demo)" (from PDF metadata); `page_count` 2; 1 chunk, pages 1–2, heading "Returns Policy"; 768 dims, norm 0.999999990; Gemini 862 ms |
| DOCX | title from core properties; `table_count` 1; 1 chunk; 768 dims, norm 0.999999999; Gemini 524 ms |
| Identical re-upload | 200, `unchanged: true`, no job; chunk count unchanged; budget unchanged |
| Reindex | `outcome: unchanged`, `embedded_texts: 0`, `embedding_requests: 0` |
| Damaged PDF (valid header) | job failed once: "The PDF could not be read; it may be damaged." |
| Worker crash and restart | worker stopped; a job left `running` by a "crashed" worker (lock 10 min old); worker started → "Recovered stale job" → succeeded on attempt 2 |
| Delete all four test sources via the API | 0 sources, documents, chunks, cache rows, and jobs; 0 stored files |
| Daily budget used by the whole run | 3 texts (of 300) |

The test account and its workspace were removed afterwards. Your two accounts, created before M3, are untouched.

### Problems found and fixed during M4

1. **Chunker defects found by a smoke run before any tests were written:**
   - a heading could become a chunk on its own;
   - a chunk could start with the blank-line separator;
   - long paragraphs produced chunks near the maximum instead of the target;
   - closing quotes could fall between chunks.
   All four are fixed and covered by unit tests.
2. **The PDF heading heuristic misread wrapped lines as headings,** which would split sentences across blocks and attach wrong section labels. It now accepts only clearly heading-like lines; ambiguous lines stay text.
3. **A SQLAlchemy 2.1 deprecation** (`.distinct(col)` for `DISTINCT ON`) was caught by warnings-as-errors and replaced with `postgresql.distinct_on`.
4. **A test-helper bug:** the PDF builder lost its `trailer` keyword. The real parser correctly rejected the file as damaged, and the helper was fixed.
5. **Two gaps found by mutation testing:**
   - the body-limit test couldn't tell whether a request was refused up front or while streaming;
   - the pipeline's own provider-contract check was untested.
   Both now have tests: the handler never starts, and a misbehaving provider gets nothing published or cached.
6. **macOS `/var` → `/private/var` symlink:** a path assertion in a test was fixed to compare resolved paths. The application already resolved paths correctly.

## Manual review steps (owner)

Full commands are in README → "Upload and index documents (curl)".

| Step | Expected | Result |
|---|---|---|
| `docker compose up --build -d` (keep `API_HOST_PORT=8001` while 8000 is busy) | migrate applies `0003`; worker logs "ready" | not yet checked |
| Sign in, then upload one real PDF and one real DOCX | 202 each, jobs `queued` | not yet checked |
| Poll `GET /jobs/<id>` | `succeeded`; `result` shows chunks and embedded texts | not yet checked |
| `GET /sources/<id>`: document metadata and chunk count | title, `page_count` (PDF), `table_count` (DOCX), embedding model 768 | not yet checked |
| README step 4 (`psql`): one stored vector | 768 dims, norm 1.000000 | not yet checked |
| Upload the same PDF again | 200, `unchanged: true`, no new job | not yet checked |
| Upload a damaged PDF (e.g. `printf '%%PDF-1.7 broken' > bad.pdf`) and an `.exe` | job fails once with a clear message; 415 | not yet checked |
| `docker compose stop worker`, upload, `docker compose start worker` | the job is picked up and succeeds | not yet checked |
| `docker compose exec api pytest -q` | 477 passed, 3 skipped | not yet checked |
| AI Studio: project still on the **Free** tier, no billing linked | Free tier | not yet checked |

## Commits

| Milestone | SHA | Message |
|---|---|---|
| M0 | `e2b2a9f` … `47b96a4` | 8 commits; see [M0 summary](#m0-summary) |
| M1 | `2412552` … `e8d667a` | 6 commits: backend skeleton, migrations, Compose, tests, CI, docs |
| M2 | `33199da` … `e488b7b` | 7 commits: auth schema, tenancy guard, auth, API, tests, docs, rate-limit race fix |
| M3 | `b9f32a2` … `bb36797` | 6 commits: providers, status endpoint and redaction, smoke command, tests, timeout fix, docs |
| M4 | `3c2095d` | feat(db): add ingestion schema for sources, documents, chunks, jobs, and embedding cache |
| M4 | `e46b5aa` | feat(ingestion): validate, store, parse, clean, and chunk uploaded files |
| M4 | `cf99993` | feat(worker): add the durable PostgreSQL job queue and ingestion pipeline |
| M4 | `98db45b` | feat(api): add file upload, source inspection, reindex, delete, and job status |
| M4 | `2f71b8a` | test: cover ingestion validation, parsing, chunking, worker, queue, and budget |
| M4 | `bae1a64` | test: close two gaps found by mutation testing |
| M4 | see `git log` | docs: document file ingestion and record Milestone 4 results |

## Blockers

None.

## Decisions

- Owner: [ADR 0001](docs/decisions/0001-m0-preflight-decisions.md) (thinking level `low`, Node 24, `uv`, 3.6 Flash correction, keep `gemini-embedding-001`) and [ADR 0002](docs/decisions/0002-main-branch-workflow.md) (main-branch workflow).
- Implementation:
  - [ADR 0003](docs/decisions/0003-m1-service-foundation.md) (M1);
  - [ADR 0004](docs/decisions/0004-m2-auth-and-tenancy.md) (M2);
  - [ADR 0005](docs/decisions/0005-m3-gemini-provider.md) (M3);
  - [ADR 0006](docs/decisions/0006-m4-file-ingestion.md) (M4): same-name upload as a new version of one source; one waiting job per source; an installation-wide daily budget in UTC days; deletion as a job; schema extensions.

## Risks, limitations, and deferred work

- **No OCR:** scanned PDFs, legacy `.doc`, and password-protected files are rejected. PDF headings are heuristic, and multi-column PDFs may interleave.
- **One worker, one job at a time** (MVP by design). Free-tier Gemini latency varies; jobs retry and back off.
- **A crashed worker's job is recovered after its lease** (120 s) plus the next recovery pass. Docker's 10 s stop grace usually interrupts a running job, which is then recovered.
- **The daily embedding budget is shared by all workspaces** (one Gemini project) and resets at 00:00 UTC. Gemini's own daily quota resets at midnight Pacific; those deferrals retry hourly. Whether queries share the budget is decided in M7. The 30-chats-per-day cap arrives with chat in M7.
- **Deleting a source also removes its job history.**
- **`pypdf` runs inside the worker process,** with no separate sandbox.
- **The status endpoint (M3) is process-local:** it doesn't show the worker's provider errors.
- The M2 session guard covers ORM statements only; M6's raw pgvector SQL must filter `workspace_id` explicitly.
- Registration enumeration (409); no refresh tokens, password reset, or MFA; fixed-window rate limits (M2).
- Frontend and the CI frontend job: M8.
- `uv` isn't installed system-wide on the development machine. Docker and CI use pinned `uv` 0.13.6.
- Port 8000 is used by another local project; use `API_HOST_PORT`.

## Earlier milestones

- **M0:** environment, Gemini, SDK, and pgvector compatibility verified; repo hygiene and CI skeleton. See [docs/m00-compatibility-report.md](docs/m00-compatibility-report.md).
- **M1:** Compose stack, `/health` and `/ready`, Alembic, an internal data network, and persistent volumes. See [ADR 0003](docs/decisions/0003-m1-service-foundation.md).
- **M2:** Argon2id, cookie sessions with revocation, Origin/CORS/Host checks, PostgreSQL rate limits, and a fail-closed workspace session guard. See [ADR 0004](docs/decisions/0004-m2-auth-and-tenancy.md).
- **M3:** Gemini provider interfaces; generation and embeddings with validation and normalisation; bounded retries; no paid fallback; the AI status endpoint; log redaction; a live smoke test PASS. See [ADR 0005](docs/decisions/0005-m3-gemini-provider.md).

## M0 summary

Environment, Gemini, SDK, and pgvector compatibility were verified (live Gemini smoke test PASS), along with repository hygiene and the CI skeleton.
Full details: [docs/m00-compatibility-report.md](docs/m00-compatibility-report.md).
