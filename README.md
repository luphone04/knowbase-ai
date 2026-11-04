# Knowbase AI

A locally runnable document knowledge assistant. Upload PDF, DOCX, TXT, or
Markdown files. The system indexes them into PostgreSQL + pgvector, and
answers questions with Gemini (free tier), grounded in your own documents
and with validated citations. When the documents don't support an answer,
it says so.

This is a well-engineered prototype for local demos, not production software.

> **Status:** File-upload-only (owner decision, 3 November 2026; see
> [ADR 0008](docs/decisions/0008-file-upload-only-scope.md)). You can sign in
> and upload PDF, DOCX, TXT, and Markdown files. The worker indexes them into
> pgvector with Gemini embeddings, and content that hasn't changed isn't
> embedded again. Website crawling (Milestone 5) was built and then removed;
> it is descoped. **Milestone 6 (hybrid retrieval)** is approved: you can search in
> vector-only or hybrid mode and inspect ranks, scores, and traces.
> **Milestone 7 (grounded chat)** is approved: conversations with answers that
> cite your documents, abstention when they don't support an answer, and clear
> errors when Gemini is unavailable. **Milestone 8 (a minimal web interface over
> this API)** is in progress.
> See [PROJECT_STATUS.md](PROJECT_STATUS.md) for progress.

## Documents

- [SPEC.md](SPEC.md): the implementation specification (source of truth)
- [PROJECT_STATUS.md](PROJECT_STATUS.md): milestone tracker, test results, blockers
- [docs/architecture/overview.md](docs/architecture/overview.md): service boundaries
- [docs/m00-compatibility-report.md](docs/m00-compatibility-report.md): environment and compatibility findings
- [docs/decisions/](docs/decisions/): decision records

## Stack

Next.js 16 (App Router, TypeScript, Node 24 LTS, Tailwind CSS, shadcn/ui) · FastAPI (Python
3.12, dependencies locked with `uv`) · SQLAlchemy 2 + Alembic ·
PostgreSQL 18 + pgvector 0.8.7 · Google `google-genai` SDK
(`gemini-3.5-flash-lite` for answers, `gemini-embedding-001` at 768 dims) · Docker Compose.
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

# 2. Build and start db, migrate, api, worker, and frontend
docker compose up --build -d

# 3. Check the services: api and db "healthy", migrate "Exited (0)", worker "Up"
docker compose ps -a

# 4. Open the app (use localhost, not 127.0.0.1: it must match FRONTEND_ORIGIN)
open http://localhost:3000

#    Health endpoints and API docs
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
(for example `8001`) and use that port in the URLs above. Then set
`NEXT_PUBLIC_API_BASE_URL` to the same port (`http://localhost:8001/api/v1`)
and rebuild the frontend (`docker compose up --build -d frontend`): the URL is
built into the browser bundle. The database publishes no host port. Use
`docker compose exec db psql ...` to reach it.

## Web interface

The frontend (`frontend/`, [ADR 0013](docs/decisions/0013-m8-frontend-scope.md))
is a small Next.js app that demonstrates the backend. The browser calls the API
directly with the HttpOnly session cookie; the frontend container receives no
`.env` and holds no secrets.

To work on it outside Docker (with the backend running in Compose):

```bash
cd frontend
cp .env.example .env.local   # NEXT_PUBLIC_* settings only; never a secret
npm ci
npm run dev                  # http://localhost:3000
npm run lint && npm run typecheck && npm test && npm run build && npm run check:bundle
```

Stop the Compose `frontend` container first (`docker compose stop frontend`),
since both use port 3000.

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
- **Too big to index:** more than `MAX_PDF_PAGES` (500) pages, `MAX_DOCUMENT_CHARS`
  (2,000,000) characters of text, or `MAX_DOCUMENT_CHUNKS` (1,500) chunks. These
  are checked while parsing, so a small file that expands hugely stops early.
  The job fails once, naming the setting.
- **A document that hangs or exhausts the parser:** parsing runs in a separate
  process. It is stopped after `PARSE_TIMEOUT_SECONDS` (120) or at
  `PARSE_MEMORY_LIMIT_MB` (1536, Linux), and no job runs longer than
  `JOB_MAX_RUNTIME_SECONDS` (900).
- **Gemini busy:** the job retries with backoff (shown in `last_error`). A
  per-minute rate limit waits without using an attempt. Gemini's daily quota
  waits until 08:05 UTC, after the midnight-Pacific reset.
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

## Search your documents (curl)

Retrieval can be inspected on its own. Upload the fictional demo
documents in [`sample_data/demo_store/`](sample_data/README.md), then search.
Each search makes one Gemini request, to embed the query. Searches are limited
per workspace per day (`DAILY_RETRIEVAL_REQUEST_LIMIT`, 100), and in total for
the installation (`DAILY_QUERY_EMBEDDING_LIMIT`, 500). A search spends nothing
without a key, in an empty workspace, or when Gemini refuses the request.

```bash
API=http://localhost:8000/api/v1                 # or your API_HOST_PORT
O='Origin: http://localhost:3000'; J='Content-Type: application/json'

# 1. Index the demo corpus (wait for each job to succeed)
for f in sample_data/demo_store/*.md; do curl -s -b alice.jar -H "$O" -F "file=@$f" $API/sources/files; echo; done

# 2. Hybrid (vector + full-text, fused with RRF) or vector-only
curl -s -b alice.jar -H "$O" -H "$J" -X POST $API/retrieval/search \
  -d '{"query": "Can I return a laptop after 20 days?", "mode": "hybrid"}'
#    -> results[]: rank, score (RRF), vector_rank/vector_distance, lexical_rank/lexical_score,
#       filename, document_title, heading, page_start, content; plus trace_id and latency_ms
curl -s -b alice.jar -H "$O" -H "$J" -X POST $API/retrieval/search \
  -d '{"query": "What is NorthCare Plus?", "mode": "vector"}'

# 3. A follow-up borrows the previous question (no extra Gemini call)
curl -s -b alice.jar -H "$O" -H "$J" -X POST $API/retrieval/search \
  -d '{"query": "And the warranty?", "previous_question": "How long do I have to return a Trailblazer 14 laptop?"}'
#    -> "contextualized": true, "retrieval_query": "How long ... laptop? And the warranty?"

# 4. The stored trace: every candidate's rank and raw score, the fused scores, timing
curl -s -b alice.jar $API/retrieval/traces/<trace-id>
```

Lexical scores come from PostgreSQL `ts_rank_cd`, which is not BM25. Fusion uses
ranks only (`1/(60 + rank)` per list), never the raw scores. Details:
[ADR 0009](docs/decisions/0009-m6-hybrid-retrieval.md).

## Chat with your documents (curl)

Each question retrieves fresh evidence (as in search), then asks Gemini for an
answer that may only use the numbered excerpts `[S1]`, `[S2]`, ... It makes two
Gemini requests: one query embedding and one answer. Only answers that cite
at least one excerpt they were given are shown. Otherwise the reply is
"I couldn't find enough information in this knowledge base to answer that.",
with a reason. Questions are limited per user per day (`DAILY_CHAT_REQUEST_LIMIT`, 30).

```bash
# 1. A conversation (the first question becomes its title)
curl -s -b alice.jar -H "$O" -H "$J" -X POST $API/conversations -d '{}'
#    -> {"id": "<conversation-id>", "title": "New conversation", ...}

# 2. A policy question: an answer with validated citations
curl -s -b alice.jar -H "$O" -H "$J" -X POST $API/conversations/<conversation-id>/messages \
  -d '{"content": "Can I return a laptop after 20 days?"}'
#    -> 201: assistant_message.outcome "answered", content with [S1], and
#       citations[]: id, filename, page_start, excerpt, source_id, chunk_id

# 3. Open a citation's source
curl -s -b alice.jar $API/sources/<source-id>

# 4. A follow-up (retrieves with the previous question too)
curl -s -b alice.jar -H "$O" -H "$J" -X POST $API/conversations/<conversation-id>/messages \
  -d '{"content": "And the warranty?"}'

# 5. An unsupported question: outcome "abstained", abstain_reason "model_insufficient"
#    ("no_evidence" when nothing is indexed; "no_valid_citation" when the model cited nothing valid)
curl -s -b alice.jar -H "$O" -H "$J" -X POST $API/conversations/<conversation-id>/messages \
  -d '{"content": "Who is the CEO of Northstar Outfitters?"}'

# 6. The persisted conversation, and the trace behind an answer
curl -s -b alice.jar $API/conversations/<conversation-id>
curl -s -b alice.jar $API/retrieval/traces/<assistant_message.metadata.trace_id>
```

When Gemini is unavailable or out of quota, a question gets an error response,
never an answer or an abstention: 429 `ai_quota`, 503 `ai_unavailable`, or 502
`ai_incomplete`. Nothing is saved. To check that uploaded documents are treated
as data, upload the prompt-injection documents in
[`backend/tests/fixtures/prompt_injection/`](backend/tests/fixtures/prompt_injection/README.md)
and ask "How much does gift wrapping cost?". Details:
[ADR 0011](docs/decisions/0011-m7-grounded-chat.md).

## Gemini (AI provider)

The backend talks to Gemini only through `app/providers`:

- **Generation** uses `GEMINI_CHAT_MODEL` (default `gemini-3.5-flash-lite`,
  [ADR 0012](docs/decisions/0012-generation-model-gemini-3.5-flash-lite.md)) with
  `GEMINI_THINKING_LEVEL` (default `low`) and `GEMINI_MAX_OUTPUT_TOKENS`, which
  includes thinking tokens. An
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
  500/502/503, and connection failures, with exponential backoff and jitter.
  - Embeddings retry up to `GEMINI_MAX_RETRIES` times (default 3).
  - Answer generation retries up to `GEMINI_GENERATION_MAX_RETRIES` times
    (default 0: one attempt per question). A failed attempt is reported as an
    error and the question can be asked again; on the free tier, a failed
    attempt can still count toward the daily quota.
  - An exhausted daily quota fails at once with a clear message; it resets
    at midnight Pacific.
  - A request that misses its deadline (504) is not retried; raise
    `GEMINI_REQUEST_TIMEOUT_SECONDS` (default 90) instead.
  - No retry starts after `GEMINI_CALL_DEADLINE_SECONDS` (120), so one call
    lasts at most that plus one request timeout.
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
# Repository hygiene: fails if secrets, session tokens, cookie jars (*.jar), or
# local data are tracked or staged (it reads the git index, archives included)
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
