# Knowbase AI

A locally runnable website-and-document knowledge assistant. Add a public
website or upload documents. The system indexes them into PostgreSQL +
pgvector, and answers questions with Gemini (free tier), grounded in your
own sources and with validated citations. When the sources don't support an
answer, it says so.

This is a well-engineered prototype for local demos, not production software.

> **Status:** Milestone 5 (website crawler and incremental indexing). You can
> sign in, upload PDF, DOCX, TXT, and Markdown files, and add a public website.
> The worker crawls the site politely, respecting robots.txt and limited to
> public addresses, and indexes files and pages into pgvector with Gemini
> embeddings. Pages that haven't changed aren't embedded again. Retrieval (M6)
> and chat (M7) are not built yet. See [PROJECT_STATUS.md](PROJECT_STATUS.md)
> for progress.

## Documents

- [SPEC.md](SPEC.md): the implementation specification (source of truth)
- [PROJECT_STATUS.md](PROJECT_STATUS.md): milestone tracker, test results, blockers
- [docs/architecture/overview.md](docs/architecture/overview.md): service boundaries
- [docs/m00-compatibility-report.md](docs/m00-compatibility-report.md): environment and compatibility findings
- [docs/decisions/](docs/decisions/): decision records

## Stack

Next.js (App Router, TypeScript, Node 24 LTS; from M8) · FastAPI (Python
3.12, dependencies locked with `uv`) · SQLAlchemy 2 + Alembic ·
PostgreSQL 18 + pgvector 0.8.7 · Google `google-genai` SDK
(`gemini-3.8-flash`, `gemini-embedding-001` at 768 dims) · Docker Compose.
Requires no paid services.

## Prerequisites

- Docker Desktop (or Docker Engine) with Compose v2+
- A Gemini API key from [Google AI Studio](https://aistudio.google.com/). Do
  **not** enable billing. Free-tier content may be used by Google to improve
  its products, so use only synthetic or public data. The stack starts
  without a key.

## Run the local stack

```bash
# 1. Create your local configuration (git-ignored)
cp .env.example .env
#    Replace APP_SECRET_KEY and JWT_SECRET with random values (required:
#    the backend refuses the placeholder and anything under 32 characters):
#    python3 -c "import secrets; print(secrets.token_urlsafe(48))"
#    Optionally set GEMINI_API_KEY (backend only).

# 2. Build and start db, migrate, api, and worker
docker compose up --build -d

# 3. Check the services: api and db "healthy", migrate "Exited (0)", worker "Up"
docker compose ps -a

# 4. Health endpoints and API docs
curl http://localhost:8000/health   # {"status":"ok"}
curl http://localhost:8000/ready    # database + pgvector extension status
open http://localhost:8000/docs

# 5. Inspect the pgvector extension
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
  -c "SELECT extversion FROM pg_extension WHERE extname = '\''vector'\''"'

# 6. Follow worker logs
docker compose logs -f worker

# 7. Run backend tests inside the api container
docker compose exec api pytest -q

# 8. Stop containers but keep data
docker compose down
```

> **Warning:** `docker compose down -v` **deletes** the database and
> uploaded-file volumes. Only use it when you want a clean slate.

If port 8000 is already in use on your machine, set `API_HOST_PORT` in `.env`
(for example `8001`) and use that port in the URLs above. The database
publishes no host port. Use `docker compose exec db psql ...` to reach it.

The web UI (`http://localhost:3000`) arrives in Milestone 8.

## Try authentication and workspace isolation (curl)

The session is an HttpOnly cookie, so each user needs their own cookie jar.
Requests that change data must send the frontend's `Origin` (the CSRF check).

```bash
API=http://localhost:8000/api/v1                 # or your API_HOST_PORT
O='Origin: http://localhost:3000'; J='Content-Type: application/json'

# Two users, each with their own workspace
curl -s -c alice.jar -H "$O" -H "$J" -X POST $API/auth/register \
  -d '{"email":"alice@example.com","password":"alice-password-123","workspace_name":"Alice Co"}'
curl -s -c bob.jar -H "$O" -H "$J" -X POST $API/auth/register \
  -d '{"email":"bob@example.com","password":"bob-password-1234","workspace_name":"Bob Ltd"}'

curl -s -b alice.jar $API/auth/me                # Alice's user + workspace
BOB_WS=$(curl -s -b bob.jar $API/auth/me | python3 -c 'import json,sys;print(json.load(sys.stdin)["workspace"]["id"])')
curl -s -b alice.jar $API/workspaces/$BOB_WS     # 404: Alice can't see Bob's workspace
curl -s -b alice.jar -H "$O" -H "$J" -X PATCH $API/workspaces/$BOB_WS -d '{"name":"x"}'  # 404
curl -s -b alice.jar -H 'Origin: http://localhost:5173' -H "$J" -X POST $API/auth/logout  # 403 CSRF
curl -s -b alice.jar -c alice.jar -H "$O" -X POST $API/auth/logout -o /dev/null -w '%{http_code}\n'  # 204
```

Interactive docs at `/docs` work too: log in with `POST /api/v1/auth/login`,
and the browser keeps the cookie for later calls.

## Upload and index documents (curl)

Sign in first (the curl block above writes `alice.jar`). Uploads return at
once (`202`); the worker then parses, chunks, embeds, and stores them.

```bash
API=http://localhost:8000/api/v1                 # or your API_HOST_PORT
O='Origin: http://localhost:3000'

# 1. Upload (PDF, DOCX, TXT, or .md; up to MAX_UPLOAD_MB)
curl -s -b alice.jar -H "$O" -F "file=@returns-policy.pdf" $API/sources/files
#    -> {"source": {"id": ..., "status": "pending"}, "job": {"id": ..., "status": "queued"}, "unchanged": false}

# 2. Watch the job: queued -> running -> succeeded (or failed, with last_error)
curl -s -b alice.jar $API/jobs/<job-id>
#    result: {"outcome": "indexed", "chunks": N, "embedded_texts": N, "embedding_requests": ...}

# 3. Source detail: document title, metadata (page_count, chunker, embedding model), chunk count
curl -s -b alice.jar $API/sources/<source-id>
curl -s -b alice.jar $API/sources                # all sources in your workspace

# 4. A stored vector: 768 dimensions, unit length
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT d.title, c.chunk_index, vector_dims(c.embedding) AS dims,
          round(vector_norm(c.embedding)::numeric, 6) AS norm, c.metadata->>'\''page_start'\'' AS page
   FROM chunks c JOIN documents d ON d.id = c.document_id ORDER BY 1, 2"'

# 5. Same file again: 200, "unchanged": true, no job, no Gemini calls
curl -s -b alice.jar -H "$O" -F "file=@returns-policy.pdf" $API/sources/files

# 6. Re-index: runs the pipeline, finds the content unchanged, embeds nothing
curl -s -b alice.jar -H "$O" -X POST $API/sources/<source-id>/reindex

# 7. Delete (owner/admin): removes chunks, cached vectors, and the file; then 404
curl -s -b alice.jar -H "$O" -X DELETE $API/sources/<source-id>
```

What goes wrong, and how it shows:
- **Unsupported type or fake content:** `415` / `422` immediately. Nothing is stored.
- **Damaged, scanned (no text), or password-protected PDF:** the job fails once
  with a clear `last_error`. It isn't retried, and no OCR is attempted.
- **Gemini busy:** the job retries with backoff (shown in `last_error`).
- **Daily limit reached** (`DAILY_EMBEDDING_TEXT_LIMIT`, 300 texts per UTC day,
  all workspaces): the job waits until 00:00 UTC. Finished batches are kept,
  and cached or unchanged text never counts.
- **A failed new version never removes the old one:** the source shows
  `failed` with "The previously indexed version is still available."

Crash recovery, by hand:

```bash
docker compose stop worker      # upload a file now: the job stays queued
docker compose start worker     # it is picked up; jobs left 'running' by a dead
                                # worker are recovered once their 120 s lease lapses
docker compose logs -f worker   # ids, counts, and timings only, never document text
```

## Crawl a website (curl)

Signed in as above. Only crawl sites you may crawl. These two are public
sandboxes built for practising scrapers:
- `www.scrapethissite.com` has a robots.txt that disallows `/lessons/` and `/faq/`.
- `quotes.toscrape.com/random` serves a different quote on every request,
  so it shows a "changed page".

```bash
API=http://localhost:8000/api/v1                 # or your API_HOST_PORT
O='Origin: http://localhost:3000'; J='Content-Type: application/json'

# 1. Add a site (202 at once; the worker crawls). Limits can only be lowered.
curl -s -b alice.jar -H "$O" -H "$J" -X POST $API/sources/websites \
  -d '{"url": "https://www.scrapethissite.com/pages/", "max_pages": 3, "max_depth": 1}'
#    -> {"source": {"id": ..., "kind": "website"}, "job": {"id": ..., "status": "queued"}, "created": true}

# 2. Watch the crawl: progress while running, then totals
curl -s -b alice.jar $API/jobs/<job-id>
#    result: pages_requested 3, stopped_reason "page_limit", robots "parsed",
#            pages_indexed, pages_skipped, embedded_texts, ...

# 3. Page by page: status, reason, HTTP status, title
curl -s -b alice.jar "$API/sources/<source-id>/pages"
#    /faq/ and /lessons/: "skipped", reason "robots" (and never requested; see the worker log)
#    /login/: "skipped", reason "auth_page"; a noindex page: reason "noindex"
curl -s -b alice.jar "$API/sources/<source-id>/pages?status=failed"   # failures only

# 4. Stored pages and vectors
docker compose exec db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "SELECT d.canonical_url, d.title, left(d.text_content, 120) AS text, count(c.id) AS chunks,
          min(vector_dims(c.embedding)) AS dims
   FROM documents d LEFT JOIN chunks c ON c.document_id = d.id
   WHERE d.canonical_url IS NOT NULL GROUP BY d.id ORDER BY 1"'

# 5. Crawl again: unchanged pages keep their chunks and cost no Gemini calls
curl -s -b alice.jar -H "$O" -X POST $API/sources/<source-id>/reindex
#    -> job result: pages_unchanged N, embedded_texts 0, embedding_requests 0
#    (a second reindex while one is queued: 409 crawl_in_progress)

# 6. A changed page gets re-indexed
curl -s -b alice.jar -H "$O" -H "$J" -X POST $API/sources/websites \
  -d '{"url": "https://quotes.toscrape.com/random", "max_depth": 0}'
curl -s -b alice.jar -H "$O" -X POST $API/sources/<that-source-id>/reindex
#    -> pages_indexed 1, embedded_texts 1 (a new quote: new text hash, chunks replaced)

# 7. Blocked targets: 422, nothing is created, nothing is fetched
for u in http://localhost:8000 http://127.0.0.1:8000/ http://10.0.0.1/ \
         http://169.254.169.254/latest/meta-data/ http://db:5432/ ftp://example.com/; do
  curl -s -b alice.jar -H "$O" -H "$J" -X POST $API/sources/websites -d "{\"url\": \"$u\"}"; echo
done
#    -> {"error": {"code": "blocked_url" | "invalid_url", "message": ...}}

# 8. Workspace isolation: Bob can't see or touch Alice's site (404 everywhere)
curl -s -b bob.jar $API/sources/<source-id>/pages

# 9. Delete: pages, chunks, crawl records, and unused cached vectors
curl -s -b alice.jar -H "$O" -X DELETE $API/sources/<source-id>
```

How the crawler behaves (details in
[ADR 0007](docs/decisions/0007-m5-website-crawler.md)):
- **Same site only:** scheme, host, and port must match. Pages are fetched
  breadth-first, at most `MAX_CRAWL_PAGES` (20) and `MAX_CRAWL_DEPTH` (2), and
  at least `CRAWL_DELAY_SECONDS` (1) apart.
- **robots.txt:**
  - read first;
  - if it's missing (4xx), there are no restrictions;
  - if it's unreachable (5xx, 429, network), nothing is crawled and the job
    retries;
  - sitemap pages obey it too.
- **Never fetched:** private, loopback, link-local, and metadata addresses,
  including hosts that *resolve* there now. The check is made on the actual
  connection, so DNS rebinding fails.
- **Some pages failing** (timeouts, 500s, too little text) doesn't fail the
  crawl. Each failure is listed under `pages?status=failed`, and that page's
  previous version stays indexed.
- **No JavaScript** is run, so pages that render in the browser come out
  nearly empty.

## Gemini (AI provider)

The backend talks to Gemini only through `app/providers`:

- **Generation** uses `GEMINI_CHAT_MODEL` with `GEMINI_THINKING_LEVEL` (default
  `low`) and `GEMINI_MAX_OUTPUT_TOKENS`, which includes thinking tokens. An
  answer counts only if Gemini finishes with `STOP` and returns text. Anything
  else (`MAX_TOKENS`, `SAFETY`, an empty reply) is reported as incomplete,
  never shown as a partial answer.
- **Embeddings** use `gemini-embedding-001` at 768 dimensions. Documents use
  task type `RETRIEVAL_DOCUMENT` and questions use `RETRIEVAL_QUERY`: the
  model embeds them asymmetrically, so a question lands near the passage
  that answers it.
  - Each input gets its own vector. Every vector is checked (exactly 768
    finite, non-zero values) and **normalised to length 1**. At 768
    dimensions Gemini's raw vectors are not unit length (about 0.59), and
    unit vectors make cosine similarity a simple dot product.
- **Retries** apply only to temporary problems: per-minute rate limits (429),
  500/502/503, and connection failures. There are up to 3 retries, with
  exponential backoff and jitter.
  - An exhausted daily quota fails at once with a clear message; it resets
    at midnight Pacific.
  - A request that misses its deadline (504) is not retried; raise
    `GEMINI_REQUEST_TIMEOUT_SECONDS` (default 90) instead.
  - Invalid requests are never retried, and **nothing ever falls back to
    another model, a paid tier, or Vertex AI.**
- The key is read only from the backend `.env`. It never appears in
  responses or logs; log output masks it even inside tracebacks.

Check the configuration (signed in; this does not call Gemini):

```bash
curl -s -b alice.jar http://localhost:8000/api/v1/ai/status
# {"status":"configured" | "missing_key" | "quota_error", "chat_model": ..., "paid_fallback": false, ...}
```

Run the **minimal live smoke test**. It makes 3 requests (one generation, one
document embedding of two texts, one query embedding) and prints measurements
only, never the key, the vectors, or the generated text:

```bash
docker compose exec api python -m app.providers.smoke
# Exit codes: 0 pass, 1 response/implementation problem, 2 no key,
#             3 rate limit or daily quota, 4 Gemini slow or unavailable,
#             5 key, model, or project rejected
```

Temporary 503s can add a few bounded retries; the attempt count is printed.

**Confirm no billing is involved:**
1. In [Google AI Studio](https://aistudio.google.com/), the key's project
   should show the **Free** tier. Google requires you to set up billing
   explicitly before a project can move to a paid tier.
2. Don't link a billing account.
3. The app only calls the Gemini Developer API with your AI Studio key
   (`vertexai=False`). It uses no paid features: no Vertex AI, no batch API,
   no grounding, and no File Search.

Run the **mocked provider tests** (no network, no key needed):

```bash
docker compose exec api pytest -q tests/unit -k "gemini or provider or mock or redaction"
```

## Backend development without Docker (optional)

Requires [`uv`](https://docs.astral.sh/uv/) (`brew install uv`).

```bash
cd backend
uv sync --locked
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest -m "not integration"   # unit tests; no database needed
```

Integration tests need a reachable database in `DATABASE_URL`. The simplest
way to run them is in the container (step 7). CI runs the full suite
against a pgvector service container.

## Milestone 0 preflight tools

```bash
# Repository hygiene: fails if secrets or local data are tracked or staged
bash scripts/check_repo_hygiene.sh

# Live Gemini smoke test (needs GEMINI_API_KEY in .env; makes 5 API requests)
python3.12 -m venv .venv-preflight
.venv-preflight/bin/pip install -r scripts/preflight/requirements.txt
.venv-preflight/bin/python scripts/preflight/gemini_smoke.py
```

## Security notes

- `.env` is git-ignored. Only `.env.example` (no real values) is committed.
- The Gemini key is backend-only and never goes in a `NEXT_PUBLIC_*` variable.
- The database has no host port and no internet route. The API binds to
  `127.0.0.1` only.
