# Project status

| Field | Value |
|---|---|
| Current milestone | **M2 — Authentication and workspace isolation** |
| Status | **READY_FOR_REVIEW**: checks pass, committed and pushed to `main`; awaiting `APPROVE MILESTONE 2` |
| GitHub repository | https://github.com/luphone04/knowbase-ai. Visibility: **private** (confirmed by owner 2026-11-01) |
| Workflow | Work on `main`; one milestone, then commit, push, report, and STOP ([ADR 0002](docs/decisions/0002-main-branch-workflow.md)) |
| Current branch | `main` |
| Last verified checkpoint | 2026-11-01 |

## Milestones

| Milestone | Scope | Status | Commit on `main` | Approval |
|---|---|---|---|---|
| M0 | Repository and compatibility preflight | APPROVED_AND_MERGED | `47b96a4` | `APPROVE MILESTONE 0`, 2026-11-01 |
| M1 | Database and service foundation | APPROVED_AND_MERGED | `e8d667a` | `APPROVE MILESTONE 1`, 2026-11-01 |
| M2 | Authentication and workspace isolation | READY_FOR_REVIEW | see Commits | Pending: `APPROVE MILESTONE 2` |
| M3 | Gemini provider and embeddings | NOT_STARTED | — | — |
| M4 | File ingestion and durable worker | NOT_STARTED | — | — |
| M5 | Website crawler and incremental indexing | NOT_STARTED | — | — |
| M6 | Hybrid retrieval and RRF | NOT_STARTED | — | — |
| M7 | Grounded chat, citations, and abstention | NOT_STARTED | — | — |
| M8 | Frontend product experience | NOT_STARTED | — | — |
| M9 | Evaluation, hardening, and portfolio finish | NOT_STARTED | — | — |

## M2 checklist (SPEC.md §17.7)

Design and rationale: [ADR 0004](docs/decisions/0004-m2-auth-and-tenancy.md).

- [x] Registration with email validation (`email-validator`, lower-cased and checked by a DB constraint)
- [x] Argon2id password hashing (64 MiB, t=3, p=4), with rehash on login
- [x] Login, logout, and a JWT in an HttpOnly cookie (HS256; required claims; algorithm pinned); logout revokes the token server-side
- [x] Workspace and owner membership created in the same transaction as the user
- [x] Authenticated-user and workspace dependency (`get_auth_context`), re-checking membership per request
- [x] Workspace membership and role checks (owner/admin can rename; member read-only)
- [x] Tenant-safe query layer: session guard that auto-filters `WorkspaceScoped` models and fails closed (`app/db/tenancy.py`)
- [x] Cross-workspace tests for read, write, and delete, plus UUID guessing, with two real tenants. Source, conversation, and message isolation tests are added in M4 and M7, when those tables exist; the fixtures and mechanism are ready.
- [x] CSRF Origin check and exact CORS; Host allowlist
- [x] Login and registration rate limiting (PostgreSQL counters)
- [x] Tests that hashes are never plaintext and that cookies carry the expected flags

## M2 acceptance evidence (2026-11-01)

| Criterion | Command | Result |
|---|---|---|
| Register, login, `/auth/me`, logout work | README curl walkthrough; `curl` end-to-end against the Compose stack | PASS (16/16 checks, below) |
| A cannot reach B's workspace, even by guessing UUIDs | `tests/integration/test_workspace_isolation.py`; curl steps 5–7 | PASS: 404, identical to a random id |
| Full suite on host against throwaway pgvector (what CI runs) | `uv run pytest` | **PASS: 168 passed, 0 skipped** (83 unit, 85 integration) |
| Full suite in the api container | `docker compose exec api pytest -q` | PASS: 165 passed, 3 skipped (`.env.example` isn't in the image; these run on host and CI) |
| M1 regression | same runs (health, readiness, extensions, migrations, volumes) | PASS |
| Lint / format / strict types | `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy` | PASS |
| Lockfile current | `uv lock --check` | PASS |
| Migrations from a clean database | every test session creates a new database and runs `alembic upgrade head`; scratch DBs test upgrade, idempotency, downgrade to base, and re-upgrade | PASS |
| Models match migrations | `alembic check`; `test_models_match_migrations` (`compare_metadata` is empty) | PASS |
| Compose stack | `docker compose up --build -d`; migrate applied `0002_users_workspaces` to the dev DB | PASS: all services healthy |
| Tests can actually fail (mutation check) | Disable each control and re-run its tests: tenancy guard, revocation, origin check, failed-login counting, input echo, `is_active`, `hide_parameters` | PASS: every mutation caught (1–14 failing tests each); files restored byte-for-byte |
| No secrets leaked | grep Compose logs for passwords, JWTs, Argon2 hashes, Gemini key; hygiene script | PASS: 0 hits |
| CI workflow valid | `actionlint` | PASS. CI run on GitHub: not checked from here (`gh` not installed) |

End-to-end curl checks against the Compose stack (port 8001):
- Register (201, cookie `HttpOnly; Max-Age=28800; Path=/; SameSite=lax`).
- `/me` (200).
- Own workspace (200).
- B's workspace: GET and PATCH both 404, and B's name unchanged.
- Random UUID (404, same body).
- Wrong password and unknown email return identical 401s.
- Origin `localhost:5173` gets 403.
- Bad Host gets 400.
- CORS preflight grants exactly `http://localhost:3000` with credentials.
- Logout 204, then replaying the old token gets 401.
- `/ready` 200.
- A 422 response does not echo the password.

The test accounts were deleted afterwards; the dev DB has 0 users.

Problems found and fixed during M2:
1. **Bound SQL parameters could reach logs.** SQLAlchemy errors include bound values, so an unexpected failure could have put a password hash in the log. Fixed with `hide_parameters=True`; a test proves the hash no longer appears, and that it did before the fix. PostgreSQL's own unique-violation DETAIL can still name an email (recorded as a limitation).
2. **A first draft of that test was wrong.** It asserted on the exception's in-memory `.params` rather than its rendered text, the part logs actually contain. Corrected.
3. **CI secrets were too short.** CI's dummy secrets were 20 characters, and the new 32-character minimum would have failed the migration step. Lengthened.
4. **Deprecated SQLAlchemy 2.1 APIs** (`Row.tuple()`, `Result.tuples()`) were replaced before they could trip warnings-as-errors.
5. **Mutation testing refined the design.** The session guard alone keeps tenants apart, but explicit per-user filters are still needed for correct roles inside a shared workspace. Both layers are kept and tested.

## Manual review steps (owner)

| Step | Expected | Result |
|---|---|---|
| `docker compose up --build -d` (set `API_HOST_PORT=8001` in `.env` if 8000 is busy) | migrate applies `0002`; api healthy | not yet checked |
| Run the README "Try authentication and workspace isolation" block | 201, 201, 200, 404, 404, 403, 204 | not yet checked |
| Open `/docs`, `POST /api/v1/auth/login`, then `GET /api/v1/auth/me` | 200 with your user and workspace | not yet checked |
| Register two accounts in two browser profiles; try the other's workspace id at `/api/v1/workspaces/{id}` | 404 | not yet checked |
| Six wrong passwords for one email, then the right one | 401 ×5, then 429 with `Retry-After` | not yet checked |
| `docker compose exec api pytest -q` | 165 passed, 3 skipped | not yet checked |
| Confirm in AI Studio that billing is **not** enabled (from M0) | — | not yet checked |

## Commits

| Milestone | SHA | Message |
|---|---|---|
| M0 | `e2b2a9f` … `47b96a4` | 8 commits; see [M0 summary](#m0-summary) |
| M1 | `2412552` | feat(backend): add FastAPI service with settings, database session, and health probes |
| M1 | `b1520cc` | feat(db): add Alembic migrations enabling vector and pg_trgm |
| M1 | `55b4748` | feat(infra): add Docker Compose stack with healthchecks and worker placeholder |
| M1 | `fa98e47` | test: cover settings, health probes, database, and migrations |
| M1 | `8cfd07f` | ci: run backend lint, type checks, and tests against pgvector |
| M1 | `e8d667a` | docs: document the local stack and record Milestone 1 results |
| M2 | see `git log` | M2 commits; SHAs in the M2 report, recorded here at the start of M3 |

## Blockers

None.

## Decisions

- Owner (2026-11-01): [ADR 0001](docs/decisions/0001-m0-preflight-decisions.md) (thinking level `low`, Node 24, `uv`, 3.6 Flash correction, keep `gemini-embedding-001`) and [ADR 0002](docs/decisions/0002-main-branch-workflow.md) (main-branch workflow).
- Implementation: [ADR 0003](docs/decisions/0003-m1-service-foundation.md) (M1) and [ADR 0004](docs/decisions/0004-m2-auth-and-tenancy.md) (M2: workspace endpoints added to SPEC.md §11; source/conversation/message isolation tests move to M4/M7 with their tables).

## Risks, limitations, and deferred work

- **Registration enumeration:** `409` reveals a registered email (no email-verification flow). Rate-limited per IP.
- **No full auth lifecycle yet:** no refresh tokens, password reset, email verification, MFA, or invitations. Tokens last 480 minutes (spec value); logout revokes server-side.
- **Per-IP limits are effectively global locally:** behind Docker Desktop's port forwarding all browser traffic shares one peer address.
- **Guard coverage:** the session guard covers ORM statements only. Raw SQL in later milestones (pgvector retrieval in M6) must filter `workspace_id` explicitly and be tested for it. PostgreSQL RLS is possible future hardening.
- Gemini free-tier quotas can change; transient `503`s seen in M0, so M3 needs bounded retries.
- `gemini-embedding-001` earliest shutdown 2028-05-14.
- Frontend service and CI frontend job: M8.
- `uv` isn't installed system-wide on the development machine. Docker and CI use pinned `uv` 0.13.6.
- Port 8000 is used by another local project on the development machine; use `API_HOST_PORT`.

## M0 summary

Environment, Gemini, SDK, and pgvector compatibility were verified (live Gemini smoke test PASS), along with repository hygiene and the CI skeleton.
Full details: [docs/m00-compatibility-report.md](docs/m00-compatibility-report.md).
