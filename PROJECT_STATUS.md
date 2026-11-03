# Project status

| Field | Value |
|---|---|
| Current work | **File-only cleanup (C1)**: website crawling removed; Knowbase AI is file-upload-only ([ADR 0008](docs/decisions/0008-file-upload-only-scope.md)) |
| Status | **READY_FOR_REVIEW**: checks pass, dev database migrated with file data verified identical, pushed to `main`; awaiting owner review and approval. M6 not started |
| GitHub repository | https://github.com/luphone04/knowbase-ai. Visibility: **private** (confirmed by owner 2026-11-01) |
| Workflow | Work on `main`; one step at a time, then commit, push, report, and STOP ([ADR 0002](docs/decisions/0002-main-branch-workflow.md)) |
| Current branch | `main` |
| Last verified checkpoint | 2026-11-03 |

## Milestones

| Milestone | Scope | Status | Commit on `main` | Approval |
|---|---|---|---|---|
| M0 | Repository and compatibility preflight | APPROVED_AND_MERGED | `47b96a4` | `APPROVE MILESTONE 0`, 2026-11-01 |
| M1 | Database and service foundation | APPROVED_AND_MERGED | `e8d667a` | `APPROVE MILESTONE 1`, 2026-11-01 |
| M2 | Authentication and workspace isolation | APPROVED_AND_MERGED | `e488b7b` | `APPROVE MILESTONE 2`, 2026-11-02 |
| M3 | Gemini provider and embeddings | APPROVED_AND_MERGED | `bb36797` | `APPROVE MILESTONE 3`, 2026-11-02 |
| M4 | File ingestion and durable worker | APPROVED_AND_MERGED | `8af4b01` | `APPROVE MILESTONE 4`, 2026-11-02 |
| M5 | Website crawler and incremental indexing | **DESCOPED** (owner decision 2026-11-03) | built up to `2b8513e`, then removed | Not applicable: replaced by C1 |
| C1 | File-only cleanup (not a milestone; tracked separately) | READY_FOR_REVIEW | see Commits | Pending owner approval |
| M6 | Hybrid retrieval and RRF | NOT_STARTED | — | — |
| M7 | Grounded chat, citations, and abstention | NOT_STARTED | — | — |
| M8 | Frontend product experience | NOT_STARTED | — | — |
| M9 | Evaluation, hardening, and portfolio finish | NOT_STARTED | — | — |

## C1 — File-only cleanup (2026-11-03)

Owner decisions: M5 is descoped; the website endpoints are removed (404);
`ck_sources_kind` is restricted to `('file')` while `sources.kind` stays generic;
website-only response fields are removed but the nullable database columns
are kept; SPEC.md is updated, with ADR 0008 added and earlier ADRs kept; no
recovery tag.

### What changed

| Area | Change |
|---|---|
| Worker | Ingestion dispatches through a per-kind handler table; `file` is the only kind, and an unknown kind fails once with a clear message. `index_document` stays the shared indexing path. The crawl path, crawler hooks, crawl progress writes, and `TransientJobError` are removed |
| API | `POST /sources/websites` and `GET /sources/{id}/pages` removed (both 404: source ids are matched as UUIDs, so `websites` matches no route). Website-only response fields removed: `source_url`, `settings`, `page_counts`, and documents' `canonical_url` and `fetched_at`. `kind` stays |
| Code and dependencies | `app/crawler/` and `app/ingestion/website.py` removed; `beautifulsoup4` (and `soupsieve`) removed; `httpx` stays (the Gemini SDK) |
| Configuration | 8 `CRAWL_*` settings removed from Settings, `.env.example`, and the test environment. An old local `.env` that still sets them loads fine (tested) |
| Database | Migration `0006_retire_website_crawling`: refuses if website data exists unless explicitly purged; drops `crawl_pages`; restricts `ck_sources_kind` to `'file'`. Migrations 0001–0005 unchanged |
| Bug found and fixed | Stale-job recovery used the worker's clock while claims compare against the database clock, so recovered jobs could briefly be "not yet due". This made two M4 tests flaky. Now the database clock is used, with a deterministic test |
| Docs | SPEC.md (file-upload-only scope; §7.3 and §17.10 kept as descoped records), ADR 0008, ADR 0007 marked superseded, README, architecture overview, this file |

### Dev database migration (0005 → 0006)

Before: verified counts and checksums. Backup:
`/Users/richard/knowbase-backups/knowbase-before-0006-20261103T080306Z.dump`
(`pg_dump -Fc`, mode 600, outside the repository; verified with `pg_restore --list`: 12 tables).
The worker and api were stopped and no job was active; the real `migrate` service then applied 0006.

| Item | Before | After |
|---|---|---|
| Alembic revision | `0005_crawl_page_duplicates` | `0006_retire_website_crawling` |
| File sources / website sources | 1 / 0 | 1 / 0 |
| Documents / chunks | 1 / 8 | 1 / 8 |
| Embedding cache rows | 8 | 8 |
| Users / workspaces / jobs | 3 / 3 / 1 | 3 / 3 / 1 |
| Uploaded files | 1 | 1 |
| Checksum of source, document, chunk, and user ids | `caf266e5…` | `caf266e5…` (identical) |
| Checksum of chunk contents and vectors | `33bb3b71…` | `33bb3b71…` (identical) |
| Checksum of cache keys | `c8663dbe…` | `c8663dbe…` (identical) |
| Checksum of uploaded file bytes | `cc900538…` | `cc900538…` (identical) |
| `crawl_pages` | present (0 rows) | dropped |
| `ck_sources_kind` | `website`, `file` | `CHECK (kind = 'file')` |

No purge was needed (there was no website data). Both Docker volumes are intact; `docker compose down -v` was never used.

### Verification

| Check | Result |
|---|---|
| Full suite on host (PostgreSQL + pgvector) | **PASS: 489 passed, 0 skipped** (307 unit, 182 integration); 5 consecutive full runs green after the recovery-clock fix |
| Full suite in the api container | PASS: 486 passed, 3 skipped (`.env.example` isn't in the image) |
| M0–M4 regression | PASS: every M0–M4 test kept and passing (auth, isolation, Gemini provider, file ingestion, queue, retries, fencing, recovery, budget) |
| New tests | Kind dispatch; removed endpoints return 404; no website fields in responses; old `.env` with crawler variables still loads; recovery uses the database clock; migration 0006 (file data snapshot preserved across upgrade, downgrade, and re-upgrade; refusal rolls back completely; the purge removes only website data and keeps a vector shared with a file) |
| Mutation check of 0006 | 3 of 3 caught: guard disabled; shared cache rows purged; file sources purged |
| Lint / format / strict types / lock | `ruff check`, `ruff format --check`, `mypy` (strict, 85 files), `uv lock --check` |
| Migrations | `alembic check` clean; full round trip from base to head and back to head |
| Compose / CI / hygiene | `docker compose config`; `actionlint`; `scripts/check_repo_hygiene.sh` |
| Live acceptance (rebuilt stack) | Removed endpoints 404. A TXT upload indexed through the real worker and Gemini (1 chunk, 768 dims, norm 1.000000; 1 text embedded). Re-index unchanged with 0 embeddings. Delete → 404. No key, password, or document text in logs. Your data unchanged afterwards; the test user removed |

### Manual review steps (owner)

| Step | Expected | Result |
|---|---|---|
| `docker compose up --build -d` (`API_HOST_PORT=8001` while 8000 is busy) | migrate reports `0006` already applied; api healthy; worker "ready" | not yet checked |
| `docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT version_num FROM alembic_version"'` | `0006_retire_website_crawling` | not yet checked |
| `curl -s -b alice.jar $API/sources` | your file source is still listed, with no website fields | not yet checked |
| `curl -s -o /dev/null -w '%{http_code}' -b alice.jar -H "$O" -X POST $API/sources/websites` | `404` | not yet checked |
| README → "Upload and index documents (curl)": upload, poll, reindex, delete | unchanged M4 behaviour | not yet checked |
| `docker compose exec api pytest -q` | 486 passed, 3 skipped | not yet checked |

Restore the backup only if needed:
`docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --clean --if-exists' < /Users/richard/knowbase-backups/knowbase-before-0006-20261103T080306Z.dump`
(this returns the database to 0005; running it would need the pre-cleanup code).

## M5 (descoped) — record

M5 built a crawler with SSRF defences checked on the connection itself, RFC 9309 robots.txt handling,
sitemaps, same-origin BFS, and incremental indexing, then had two rounds of review fixes. The live
crawl passed (see [ADR 0007](docs/decisions/0007-m5-website-crawler.md), now superseded). It was never
approved: the owner descoped it on 2026-11-03 before approval. All of its code is in git history,
up to `2b8513e`.

## Commits

| Item | SHA | Message |
|---|---|---|
| M0 | `e2b2a9f` … `47b96a4` | 8 commits; see [M0 summary](#m0-summary) |
| M1 | `2412552` … `e8d667a` | 6 commits: backend skeleton, migrations, Compose, tests, CI, docs |
| M2 | `33199da` … `e488b7b` | 7 commits: auth schema, tenancy guard, auth, API, tests, docs, rate-limit race fix |
| M3 | `b9f32a2` … `bb36797` | 6 commits: providers, status endpoint and redaction, smoke command, tests, timeout fix, docs |
| M4 | `3c2095d` … `8af4b01` | 7 commits: schema, ingestion, worker and queue, API, tests, mutation gaps, docs |
| M5 (descoped) | `0b8fa04` … `2b8513e` | 11 commits: crawler, worker, API, tests, two review-fix rounds, docs |
| C1 | `4dcccb8` | refactor(worker): dispatch ingestion by source kind; remove the website crawl path |
| C1 | `6b9b471` | feat(api)!: remove website source and crawl page endpoints |
| C1 | `f8530e3` | chore: remove the crawler package, beautifulsoup4, crawler settings, and crawler tests |
| C1 | `a4e7c67` | fix(queue): use the database clock when recovering stale jobs |
| C1 | `217fb4b` | feat(db): migration 0006 retires website crawling; file is the only source kind |
| C1 | see `git log` | docs: file-upload-only scope (ADR 0008); update SPEC, README, architecture, status |

## Blockers

None.

## Decisions

- Owner: [ADR 0001](docs/decisions/0001-m0-preflight-decisions.md) (thinking level `low`, Node 24, `uv`, 3.6 Flash correction, keep `gemini-embedding-001`) and [ADR 0002](docs/decisions/0002-main-branch-workflow.md) (main-branch workflow).
- Implementation:
  - [ADR 0003](docs/decisions/0003-m1-service-foundation.md) (M1);
  - [ADR 0004](docs/decisions/0004-m2-auth-and-tenancy.md) (M2);
  - [ADR 0005](docs/decisions/0005-m3-gemini-provider.md) (M3);
  - [ADR 0006](docs/decisions/0006-m4-file-ingestion.md) (M4): same-name upload as a new version of one source; one waiting job per source; an installation-wide daily budget in UTC days; deletion as a job; schema extensions.
  - [ADR 0007](docs/decisions/0007-m5-website-crawler.md) (M5): superseded by ADR 0008.
- Owner: [ADR 0008](docs/decisions/0008-file-upload-only-scope.md) (2026-11-03):
  - the product is file-upload-only and M5 is descoped;
  - the website endpoints are removed (404);
  - `kind` is restricted to `'file'`, with the generic source abstraction kept;
  - the nullable columns are kept, and website response fields removed;
  - no recovery tag.

## Risks, limitations, and deferred work

- **File uploads only** (ADR 0008). Bringing back any URL-based ingestion would be a new milestone, with SSRF protection required (see ADR 0007 and git history for the earlier design).
- **The pre-0006 backup restores only to revision 0005,** with the old code. After 0006, a downgrade recreates an empty `crawl_pages` table but cannot bring back purged website data (there was none on the dev database).
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
- **M4:** uploads validated and stored safely; PDF/DOCX/TXT/MD parsing; deterministic chunking; durable PostgreSQL queue with fencing, heartbeat, and recovery; embedding cache and daily budget; atomic replacement; live Gemini ingestion PASS. See [ADR 0006](docs/decisions/0006-m4-file-ingestion.md).
- **M5:** descoped (see "M5 (descoped) — record" above).

## M0 summary

Environment, Gemini, SDK, and pgvector compatibility were verified (live Gemini smoke test PASS), along with repository hygiene and the CI skeleton.
Full details: [docs/m00-compatibility-report.md](docs/m00-compatibility-report.md).
