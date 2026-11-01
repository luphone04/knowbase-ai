# Knowbase AI

A locally runnable website-and-document knowledge assistant. Add a public
website or upload documents. The system indexes them into PostgreSQL +
pgvector, and answers questions with Gemini (free tier), grounded in your
own sources and with validated citations. When the sources don't support an
answer, it says so.

This is a well-engineered prototype for local demos, not production software.

> **Status:** Milestone 2 (authentication and workspace isolation). You can
> register, sign in, and manage your workspace through the API. Sources and
> chat come in later milestones. See [PROJECT_STATUS.md](PROJECT_STATUS.md)
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
