# Project status

| Field | Value |
|---|---|
| Current milestone | **M5 — Website crawler and incremental indexing** |
| Status | **READY_FOR_REVIEW**: checks pass, live crawl PASS, pushed to `main`; awaiting `APPROVE MILESTONE 5` |
| GitHub repository | https://github.com/luphone04/knowbase-ai. Visibility: **private** (confirmed by owner 2026-11-01) |
| Workflow | Work on `main`; one milestone, then commit, push, report, and STOP ([ADR 0002](docs/decisions/0002-main-branch-workflow.md)) |
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
| M5 | Website crawler and incremental indexing | READY_FOR_REVIEW | see Commits | Pending: `APPROVE MILESTONE 5` |
| M6 | Hybrid retrieval and RRF | NOT_STARTED | — | — |
| M7 | Grounded chat, citations, and abstention | NOT_STARTED | — | — |
| M8 | Frontend product experience | NOT_STARTED | — | — |
| M9 | Evaluation, hardening, and portfolio finish | NOT_STARTED | — | — |

## M5 checklist (SPEC.md §17.10)

Design and rationale: [ADR 0007](docs/decisions/0007-m5-website-crawler.md).

- [x] Validate HTTP/HTTPS URL input (no other schemes, no credentials, valid public host names)
- [x] Resolve and reject private, reserved, loopback, link-local, multicast, and other non-public IPs, at submission, per request, and **on the connection itself** (DNS rebinding)
- [x] Revalidate redirects and block cross-origin redirects (manual, at most 5, each hop re-checked)
- [x] Parse robots.txt (RFC 9309) and respect disallowed paths; 5xx/unreachable means complete disallow
- [x] Parse sitemap.xml and sitemap indexes when available (DTDs refused)
- [x] Crawl same-origin pages breadth-first with depth, page, and time limits
- [x] Rate-limit requests per host (≥1 s apart, or a longer Crawl-delay)
- [x] Extract main/article content and preserve headings (BeautifulSoup, no LLM)
- [x] Normalise URLs and remove tracking parameters
- [x] Hash extracted page content
- [x] Skip unchanged pages and re-index changed pages (M4 indexing path, atomic per page)
- [x] SSRF, redirect, timeout, and duplicate-page tests
- [x] Page-level crawl status and failures (`GET /sources/{id}/pages?status=failed`)

## M5 acceptance evidence (2026-11-03)

| Criterion | Command | Result |
|---|---|---|
| A known public test site is crawled within limits | live: `www.scrapethissite.com/pages/` with `max_pages` 3 | PASS: 3 requests, `stopped_reason: page_limit` |
| Disallowed and private targets are blocked | live: robots `/lessons/`, `/faq/`; 9 unsafe URLs via the API; tests | PASS: skipped and never requested; 422 for every unsafe URL |
| Unchanged re-crawl doesn't duplicate chunks | live re-crawl; tests | PASS: 2 pages unchanged, identical chunk ids, 0 Gemini calls |
| A changed page gets re-indexed | live: `quotes.toscrape.com/random` re-crawled; tests | PASS: 1 page re-indexed, 1 text embedded, chunk replaced |
| Full suite on host (same as CI) | `uv run pytest` against throwaway pgvector | **PASS: 773 passed, 0 skipped** (540 unit, 233 integration; **293 new in M5**) |
| Full suite in the api container | `docker compose exec api pytest -q` | PASS: 770 passed, 3 skipped (`.env.example` isn't in the image; those 3 run on host and CI) |
| M0–M4 regression | same runs (the M4 pipeline now shares `index_document` with websites) | PASS |
| Lint / format / strict types / lock / migrations | `ruff check`, `ruff format --check`, `mypy` (strict, 104 files), `uv lock --check`, `alembic check` | PASS |
| Compose and CI workflow | `docker compose config`; migrate applied `0004_crawler`; `actionlint` | PASS |
| Tests catch regressions (mutation check) | 26 protections broken one at a time, restored with `git checkout` (list below) | PASS: 25 caught (after two gaps were fixed) + 1 equivalent mutant; tree clean |
| No secrets or content in logs | 108 lines of api/worker logs against the Gemini key, JWT secret, app secret, DB password, test password, cookies, and crawled text | PASS: none present |
| Hygiene | `scripts/check_repo_hygiene.sh` | PASS |

**Mutations caught** (25):
1. Address policy allowing everything.
2. The connection skipping its own DNS check.
3. One public DNS answer accepted among private ones.
4. Cross-origin redirects followed.
5. No destination check per redirect hop.
6. Decompressed size unchecked (memory bound).
7. Slow-drip deadline ignored.
8. robots.txt first match instead of longest.
9. robots.txt 5xx treated as allow-all.
10. robots.txt ignored for discovered links.
11. External links queued.
12. Depth limit ignored.
13. Page limit ignored.
14. No per-host delay.
15. Sitemap DTD accepted.
16. Boilerplate tags kept.
17. Credentials allowed in URLs.
18. Tracking parameters kept.
19. Unchanged pages re-embedded.
20. Gone pages kept in the index.
21. Repeated blocks not removed.
22. Identical content indexed twice.
23. Progress write not fenced (a stale worker kept crawling).
24. The API skipping its DNS check.
25. Duplicate crawl allowed.

**Equivalent mutant:** `trust_env=True` on the client. httpx never reads environment proxies when a transport is supplied (`allow_env_proxies = trust_env and transport is None`), so the setting is a second layer, not the protection that does the work. The proxy test passes either way, because the real protection holds.

### Live crawl (real sites, real Gemini free tier, dev stack on port 8001)

Both sites are public sandboxes built for practising scrapers.

| Step | Result |
|---|---|
| `POST /sources/websites` `https://www.scrapethissite.com/` (`max_pages` 4, depth 1) | 202 in 0.03 s; job succeeded on attempt 1 in 5.4 s |
| robots.txt (`Disallow: /lessons/`, `/faq/`) | `robots: parsed`. `/lessons/` and `/faq/` skipped with reason `robots`; the worker log shows they were never requested. `/login/` skipped as `auth_page`. Only robots.txt, sitemap.xml (404 → `missing`), `/`, and `/pages/` were requested |
| Stored | 2 documents with titles and canonical URLs; 3 chunks with URL and heading metadata; 768 dims, norm 1.000000 |
| Re-crawl, unchanged | 2 pages `unchanged`, `embedded_texts` 0, `embedding_requests` 0; chunk ids identical; second re-crawl request while queued → 409 |
| `https://quotes.toscrape.com/random` (a different quote per request), crawled twice | `robots: missing` (404); 2nd crawl: `pages_indexed` 1, `embedded_texts` 1, chunk replaced |
| `https://www.scrapethissite.com/pages/` (`max_pages` 3) | 3 requests, `stopped_reason: page_limit`; `/pages/simple/` has `meta robots noindex` and was skipped as `noindex`; 2 pages indexed from the workspace embedding cache (0 Gemini calls) |
| `http://localhost:8000`, `http://127.0.0.1:8001/...`, `10.0.0.1`, `169.254.169.254`, `db:5432`, `api:8000`, `host.docker.internal`, `ftp://`, `user:pw@` | 422 each (`blocked_url` / `invalid_url`) with a clear message; nothing created |
| Workspace isolation (second live user) | 404 on A's source, pages, and job; reindex and delete 404; B's source list empty |
| Delete all three sources via the API | 0 website sources, documents, chunks, and crawl pages |
| Gemini used by the whole run | 5 texts embedded, 5 requests (daily budget 5 of 300) |

The two test users and their workspaces were removed afterwards. Your three accounts and their data are untouched.

### Problems found and fixed during M5

1. **A redirect to an already-seen page was fetched again.** A unit test caught it; redirect targets are now checked against the visited set before the hop is requested.
2. **Settings leaked between integration tests.** `make_worker(env=...)` wrote to `os.environ` permanently, so a 2-text budget from one test broke the next. An autouse fixture now restores the environment after every test.
3. **The test fake returned pre-read bodies,** which the streaming fetcher can't read. It now returns real byte streams, like a server.
4. **A SQLAlchemy 2.1 deprecation** (`Result.tuples()`) was caught by warnings-as-errors and removed.
5. **Two gaps found by mutation testing:**
   - the gzip-bomb test didn't bound memory (a final size check alone still passed);
   - the stale-worker test didn't check that the worker stops crawling.
   Both are asserted now.
6. **Check order at submission:** `http://192.168.0.10/admin` was reported as an admin page instead of a private address. The destination check now runs first.
7. **Re-adding a website that is being deleted** returned the doomed source; it now gets 409 like uploads.

## Manual review steps (owner)

Full commands are in README → "Crawl a website (curl)".

| Step | Expected | Result |
|---|---|---|
| `docker compose up --build -d` (keep `API_HOST_PORT=8001` while 8000 is busy) | migrate applies `0004`; worker logs "ready" | not yet checked |
| Add `https://www.scrapethissite.com/pages/` with `max_pages` 3 | 202; job `queued`, then `succeeded` | not yet checked |
| `GET /jobs/<id>` | `pages_requested` 3, `stopped_reason` `page_limit`, `robots` `parsed` | not yet checked |
| `GET /sources/<id>/pages` | `/faq/` and `/lessons/` skipped (`robots`); `/login/` skipped (`auth_page`) | not yet checked |
| README step 4 (`psql`) | titles, text previews, 768-dim chunks | not yet checked |
| Re-crawl (`POST /sources/<id>/reindex`) | `pages_unchanged`, `embedded_texts` 0 | not yet checked |
| Add `quotes.toscrape.com/random` (depth 0), then re-crawl | 2nd job: `pages_indexed` 1, `embedded_texts` 1 | not yet checked |
| Submit `http://localhost:8000` and `http://10.0.0.1/` | 422 `blocked_url` | not yet checked |
| Bob's cookie on Alice's source | 404 | not yet checked |
| `docker compose exec api pytest -q` | 770 passed, 3 skipped | not yet checked |
| AI Studio: project still on the **Free** tier, no billing linked | Free tier | not yet checked |

## Commits

| Milestone | SHA | Message |
|---|---|---|
| M0 | `e2b2a9f` … `47b96a4` | 8 commits; see [M0 summary](#m0-summary) |
| M1 | `2412552` … `e8d667a` | 6 commits: backend skeleton, migrations, Compose, tests, CI, docs |
| M2 | `33199da` … `e488b7b` | 7 commits: auth schema, tenancy guard, auth, API, tests, docs, rate-limit race fix |
| M3 | `b9f32a2` … `bb36797` | 6 commits: providers, status endpoint and redaction, smoke command, tests, timeout fix, docs |
| M4 | `3c2095d` … `8af4b01` | 7 commits: schema, ingestion, worker and queue, API, tests, mutation gaps, docs |
| M5 | `0b8fa04` | feat(db): add crawl_pages and crawler settings |
| M5 | `d45fd09` | feat(crawler): add SSRF-safe fetching, robots.txt, sitemaps, extraction, and BFS |
| M5 | `5ec866d` | feat(worker): index crawled pages through the M4 pipeline |
| M5 | `8d90436` | feat(api): add website sources, page results, and re-crawl |
| M5 | `8d48404` | test: cover the crawler, SSRF defences, incremental crawling, and website API |
| M5 | `57d1cc0` | test: close two gaps found by mutation testing |
| M5 | see `git log` | docs: document the website crawler and record Milestone 5 results |

## Blockers

None.

## Decisions

- Owner: [ADR 0001](docs/decisions/0001-m0-preflight-decisions.md) (thinking level `low`, Node 24, `uv`, 3.6 Flash correction, keep `gemini-embedding-001`) and [ADR 0002](docs/decisions/0002-main-branch-workflow.md) (main-branch workflow).
- Implementation:
  - [ADR 0003](docs/decisions/0003-m1-service-foundation.md) (M1);
  - [ADR 0004](docs/decisions/0004-m2-auth-and-tenancy.md) (M2);
  - [ADR 0005](docs/decisions/0005-m3-gemini-provider.md) (M3);
  - [ADR 0006](docs/decisions/0006-m4-file-ingestion.md) (M4): same-name upload as a new version of one source; one waiting job per source; an installation-wide daily budget in UTC days; deletion as a job; schema extensions.
  - [ADR 0007](docs/decisions/0007-m5-website-crawler.md) (M5):
    - one page per document, keyed by canonical URL;
    - robots.txt 4xx allows all, while 5xx/unreachable means disallow;
    - sitemap pages at depth 1;
    - repeated blocks across at least half the pages are boilerplate;
    - 404/robots/noindex pages are removed from the index;
    - re-crawling while a crawl is active gives 409;
    - 20 crawl starts per workspace per day.

## Risks, limitations, and deferred work

- **Crawler:** no JavaScript rendering (pages built in the browser come out nearly empty); boilerplate removal is heuristic, and sites without semantic tags keep some chrome; one origin per source (www vs bare domain, and http vs https, are different sites); no gzipped sitemaps or linked PDFs; no conditional requests, so every crawl re-fetches (unchanged pages still cost no embeddings); the per-host delay is per worker process; the time limit covers fetching only.
- **Crawling in a public deployment** would need a domain allowlist and network egress controls (SPEC.md §7.3). This is a local prototype.
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

## M0 summary

Environment, Gemini, SDK, and pgvector compatibility were verified (live Gemini smoke test PASS), along with repository hygiene and the CI skeleton.
Full details: [docs/m00-compatibility-report.md](docs/m00-compatibility-report.md).
