# 0003 — Milestone 1 service foundation choices

- Status: Accepted (implementation choices within SPEC.md; no architecture change)
- Date: 1 November 2026

| Choice | Why |
|---|---|
| **Synchronous SQLAlchemy 2 + psycopg 3** sessions; FastAPI runs sync endpoints in its threadpool | Simpler code and tests for a local, low-concurrency prototype. The worker is sync too. Async can be revisited if profiling shows a need |
| **`/health` and `/ready` at the root**, not under `/api/v1` | They are infrastructure probes, not versioned product API. `/ready` checks the database and the `vector` extension and never calls Gemini (SPEC.md §11) |
| **Two Compose networks:** `data` (`internal: true`: db, migrate, api, worker) and `egress` (api, worker) | The database has no route to the internet. `migrate` sits on `data` only. api and worker keep outbound access for Gemini, and the worker for crawling. A single `internal: true` network would have blocked Gemini calls and the published API port |
| **PG18 volume mounted at `/var/lib/postgresql`** | The pgvector PG18 image sets `PGDATA=/var/lib/postgresql/18/docker` and declares the volume at `/var/lib/postgresql`. The usual `/var/lib/postgresql/data` mount would miss the data directory |
| **API published on `127.0.0.1:${API_HOST_PORT:-8000}`**; the db is not published | Localhost-only binding (SPEC.md §15.1). The port is configurable because 8000 was already taken on the development machine |
| **Frontend service deferred to M8** | SPEC.md M1 asks for a frontend placeholder "only where needed for startup". The API stack doesn't need it, and a Next.js scaffold now would front-load M8 |
| **Worker placeholder** confirms DB connectivity, then idles; handles SIGTERM/SIGINT | A real long-running process that compose can manage. Job claiming arrives in M4. It doesn't pretend to process anything |
| **Dev dependencies included in the backend image** | `docker compose exec api pytest -q` (SPEC.md §15.3) must work. Acceptable for a local stack |
| **Non-root `app` user (uid 1000)** in the image; `/app/data/uploads` owned by it | Named volumes inherit that ownership on first mount, so api and worker can both write uploads |
| **`httpx2` for FastAPI's TestClient** (dev only) | Starlette 1.10 deprecates `httpx` for its test client in favour of `httpx2`. The runtime HTTP client for the crawler is decided in M5 |
| **Strict mypy on `app` and `tests`; ruff lint + format; pytest `filterwarnings = error`** | Catch type errors and deprecations early. A SQLAlchemy 2.1 deprecation was caught this way during M1 |
| **Migration tests run against a throwaway database** created and dropped per test | Upgrade, idempotency, and downgrade round-trips are tested without touching the application database |
