# Knowbase AI

A locally runnable website-and-document knowledge assistant. Add a public
website or upload documents. The system indexes them into PostgreSQL +
pgvector, and answers questions with Gemini (free tier), grounded in your
own sources and with validated citations. When the sources don't support an
answer, it says so.

This is a well-engineered prototype for local demos, not production software.

> **Status:** Milestone 0 (repository and compatibility preflight). No
> application code exists yet. See [PROJECT_STATUS.md](PROJECT_STATUS.md)
> for progress.

## Documents

- [SPEC.md](SPEC.md): the implementation specification (source of truth)
- [PROJECT_STATUS.md](PROJECT_STATUS.md): milestone tracker, test results, blockers
- [docs/architecture/overview.md](docs/architecture/overview.md): service boundaries
- [docs/m00-compatibility-report.md](docs/m00-compatibility-report.md): environment and compatibility findings

## Planned stack

Next.js (App Router, TypeScript) · FastAPI (Python 3.12) · SQLAlchemy 2 +
Alembic · PostgreSQL 18 + pgvector 0.8.7 · Google `google-genai` SDK
(`gemini-3.8-flash`, `gemini-embedding-001` at 768 dims) · Docker Compose.
Requires no paid services.

## Prerequisites

- Docker Desktop (or Docker Engine) with Compose v2+
- A Gemini API key from [Google AI Studio](https://aistudio.google.com/). Do
  **not** enable billing. Free-tier content may be used by Google to improve
  its products, so use only synthetic or public data.
- Python 3.12 (only for the Milestone 0 preflight script)

## Milestone 0 checks you can run now

```bash
# Repository hygiene: fails if secrets or local data are tracked or staged
bash scripts/check_repo_hygiene.sh

# Live Gemini smoke test (needs GEMINI_API_KEY in .env; makes 5 API requests)
cp .env.example .env              # then set GEMINI_API_KEY in .env
python3.12 -m venv .venv-preflight
.venv-preflight/bin/pip install -r scripts/preflight/requirements.txt
.venv-preflight/bin/python scripts/preflight/gemini_smoke.py
```

## Running the app

Not available yet. The `docker compose up --build` workflow arrives in
Milestone 1 and will be documented here.

When it exists, note that `docker compose down -v` **deletes** the database
and uploaded-file volumes.

## Security notes

- `.env` is git-ignored. Only `.env.example` (no real values) is committed.
- The Gemini key is backend-only and never goes in a `NEXT_PUBLIC_*` variable.
