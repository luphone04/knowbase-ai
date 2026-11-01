# Project status

| Field | Value |
|---|---|
| Current milestone | **M1 — Database and service foundation** |
| Status | **READY_FOR_REVIEW**: checks pass, committed and pushed to `main`; awaiting `APPROVE MILESTONE 1` |
| GitHub repository | https://github.com/luphone04/knowbase-ai. Visibility: **private** (confirmed by owner 2026-11-01) |
| Workflow | Work on `main`; one milestone, then commit, push, report, and STOP ([ADR 0002](docs/decisions/0002-main-branch-workflow.md)) |
| Current branch | `main` |
| Last verified checkpoint | 2026-11-01 |

## Milestones

| Milestone | Scope | Status | Commit on `main` | Approval |
|---|---|---|---|---|
| M0 | Repository and compatibility preflight | APPROVED_AND_MERGED | `47b96a4` | `APPROVE MILESTONE 0`, 2026-11-01 |
| M1 | Database and service foundation | READY_FOR_REVIEW | see Commits | Pending: `APPROVE MILESTONE 1` |
| M2 | Authentication and workspace isolation | NOT_STARTED | — | — |
| M3 | Gemini provider and embeddings | NOT_STARTED | — | — |
| M4 | File ingestion and durable worker | NOT_STARTED | — | — |
| M5 | Website crawler and incremental indexing | NOT_STARTED | — | — |
| M6 | Hybrid retrieval and RRF | NOT_STARTED | — | — |
| M7 | Grounded chat, citations, and abstention | NOT_STARTED | — | — |
| M8 | Frontend product experience | NOT_STARTED | — | — |
| M9 | Evaluation, hardening, and portfolio finish | NOT_STARTED | — | — |

## M1 checklist (SPEC.md §17.6)

- [x] Compose services: `db` (pgvector 0.8.7 / PG 18), `migrate`, `api`, `worker` placeholder. Frontend deferred to M8, since startup doesn't need it ([ADR 0003](docs/decisions/0003-m1-service-foundation.md))
- [x] Healthchecks (`pg_isready`; API `/ready`) and startup order (`service_healthy`, `service_completed_successfully`)
- [x] FastAPI `/health` and `/ready`
- [x] SQLAlchemy engine/session setup (sync, psycopg 3)
- [x] Alembic configuration; URL from `DATABASE_URL` via `app.config`
- [x] Migration `0001_enable_extensions` enables `vector` and `pg_trgm`
- [x] `SELECT extversion FROM pg_extension WHERE extname='vector'` → `0.8.7`
- [x] Database connection and migration tests
- [x] API reads the database (`/ready` reports live extension versions)
- [x] `docker compose down` preserves named volumes
- [x] Startup and shutdown commands documented in README

## M1 acceptance evidence (2026-11-01)

| Criterion | Command | Result |
|---|---|---|
| Compose file valid | `docker compose config --quiet` | PASS |
| Stack starts | `docker compose up --build -d`; `docker compose ps -a` | PASS: db healthy, migrate `Exited (0)`, api healthy, worker up |
| Liveness | `curl http://localhost:8000/health` | PASS: `{"status":"ok"}`, HTTP 200 |
| Readiness | `curl http://localhost:8000/ready` | PASS: HTTP 200, `{"vector":"0.8.7","pg_trgm":"1.6"}` |
| API docs | `GET /docs` | PASS: HTTP 200 |
| Migration ran | `migrate` logs; `SELECT version_num FROM alembic_version` | PASS: `0001_enable_extensions`; second `up` is a no-op |
| pgvector present | spec query (README step 5) | PASS: `0.8.7` |
| Tests in container | `docker compose exec api pytest -q` | PASS: 22 passed, 2 skipped (`.env.example` is not in the image; both run on host and CI), 0 warnings |
| CI job reproduced locally | throwaway pgvector container + `uv sync --locked`, ruff, format, mypy, `alembic upgrade head`, `uv run pytest` | PASS: 24 passed, 0 skipped; no leftover test databases |
| Lint / format / types | `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` (strict, app + tests) | PASS |
| Lockfile current | `uv lock --check` | PASS |
| Volumes survive `down` | compare `pg_control_system().system_identifier` and an uploads marker file across `down` / `up` | PASS: identifier unchanged; marker read back by worker; marker removed |
| Restart | `docker compose restart` | PASS: all services back; `/ready` 200 |
| Worker stops cleanly | `docker compose stop worker` | PASS: logs SIGTERM, exit code 0, under 1 s |
| Network isolation | TCP from db to `1.1.1.1:443`; from api/worker to `generativelanguage.googleapis.com:443` | PASS: db "Network is unreachable"; api and worker reachable |
| No secrets leaked | `docker compose logs` grep for the key; hygiene script | PASS: 0 hits |
| CI workflow valid | `actionlint` | PASS. CI run on GitHub: not checked from here (`gh` not installed) |

Verification ran with `API_HOST_PORT=8001` (see problem 4). The URLs above use the default port.

Problems found and fixed during M1:
1. **`EMBEDDING_DIMENSIONS=768` from the environment was rejected.** `Literal[768]` doesn't coerce the string `"768"`, so `migrate` exited with code 1 on the first real startup. Unit tests had only passed integers. Fixed with an `int` and a validator. Added a test that loads `.env.example` exactly as committed.
2. **SQLAlchemy 2.1 deprecation:** `Result.tuples()` produced warnings in 4 tests. Fixed. pytest now treats warnings as errors.
3. **Starlette 1.10 deprecates `httpx` for its TestClient.** Dev dependency switched to `httpx2`.
4. **Port 8000 is taken on the development machine** by another local project, so verification used `API_HOST_PORT=8001`. The port is now configurable; the default stays 8000.

## Manual review steps (owner)

| Step | Expected | Result |
|---|---|---|
| `cp .env.example .env` if needed. Set random `APP_SECRET_KEY`/`JWT_SECRET`. If 8000 is busy, set `API_HOST_PORT=8001` | — | not yet checked |
| `docker compose up --build -d` then `docker compose ps -a` | db/api healthy, migrate Exited (0), worker Up | not yet checked |
| Open `http://localhost:<port>/docs`; call `/health` and `/ready` | 200 responses; `/ready` lists vector 0.8.7 | not yet checked |
| Run README step 5 (extension query) | `0.8.7` | not yet checked |
| `docker compose exec api pytest -q` | 22 passed, 2 skipped | not yet checked |
| `docker compose restart`, then `/ready` again | 200 | not yet checked |
| `docker compose down` then `up -d` | data kept (volumes listed by `docker volume ls`) | not yet checked |
| Confirm in AI Studio that billing is **not** enabled (from M0) | — | not yet checked |

## Commits

| Milestone | SHA | Message |
|---|---|---|
| M0 | `e2b2a9f` | docs: add Knowbase AI implementation specification |
| M0 | `76d46ab` | chore: add repository hygiene, env template, and CI skeleton |
| M0 | `3b1d37e` | docs: add README, architecture overview, compatibility report, and status tracker |
| M0 | `4466b34` | fix(hygiene): detect AQ.-format Google API keys |
| M0 | `c07acd3` | docs: record live Gemini preflight results |
| M0 | `c20092c` | docs: apply Milestone 0 decisions to spec and project docs |
| M0 | `bd61934` | docs: record Milestone 0 push in project status |
| M0 | `47b96a4` | docs: switch to main-branch workflow (ADR 0002) |
| M1 | see `git log` | M1 commits; SHAs in the M1 report, recorded here at the start of M2 |

## Blockers

None.

## Decisions (owner, 2026-11-01)

Recorded in [ADR 0001](docs/decisions/0001-m0-preflight-decisions.md) and [ADR 0002](docs/decisions/0002-main-branch-workflow.md):
`GEMINI_THINKING_LEVEL` default `low` (D1) · Node 24 LTS (D3) · `uv` + `uv.lock` (D4) ·
3.6 Flash retirement statement corrected (C2) · keep `gemini-embedding-001` (C3) · main-branch workflow, no PRs.

## Risks and deferred work

- Gemini free-tier quotas and model availability can change; RPM/RPD are only visible in AI Studio.
- Gemini returned a transient `503 UNAVAILABLE` during M0, so the M3 provider must retry 5xx with bounded backoff.
- `gemini-embedding-001` earliest shutdown is 2028-05-14.
- Frontend service and CI frontend job: M8.
- `uv` is not installed system-wide on the development machine. Docker and CI use pinned `uv` 0.13.6. For host development, `brew install uv`.
- The backend image includes dev dependencies, which is fine for local use. A slimmer production target is out of scope.
- A host-native PostgreSQL listens on `127.0.0.1:5432` on the development machine. The stack doesn't publish the db port, so nothing conflicts.

## M0 summary

Environment, Gemini, SDK, and pgvector compatibility were verified (live Gemini smoke test PASS), along with repository hygiene and the CI skeleton.
Full details: [docs/m00-compatibility-report.md](docs/m00-compatibility-report.md).
