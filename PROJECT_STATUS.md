# Project status

| Field | Value |
|---|---|
| Current milestone | **M3 — Gemini provider and embeddings** |
| Status | **READY_FOR_REVIEW**: checks pass, live smoke test PASS, pushed to `main`; awaiting `APPROVE MILESTONE 3` |
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
| M3 | Gemini provider and embeddings | READY_FOR_REVIEW | see Commits | Pending: `APPROVE MILESTONE 3` |
| M4 | File ingestion and durable worker | NOT_STARTED | — | — |
| M5 | Website crawler and incremental indexing | NOT_STARTED | — | — |
| M6 | Hybrid retrieval and RRF | NOT_STARTED | — | — |
| M7 | Grounded chat, citations, and abstention | NOT_STARTED | — | — |
| M8 | Frontend product experience | NOT_STARTED | — | — |
| M9 | Evaluation, hardening, and portfolio finish | NOT_STARTED | — | — |

## M3 checklist (SPEC.md §17.8)

Design and rationale: [ADR 0005](docs/decisions/0005-m3-gemini-provider.md).

- [x] Provider interfaces (`GenerationProvider`, `EmbeddingProvider`) and Gemini implementations (`app/providers/`)
- [x] API key loaded only from backend settings (`GEMINI_API_KEY`); the SDK's own env lookup and Vertex AI are bypassed explicitly
- [x] Model IDs re-verified in Google's docs on 2026-11-02 (`gemini-3.8-flash` stable, no shutdown date; `gemini-embedding-001` listed, earliest shutdown 2028-05-14) and by live requests
- [x] One real generation request (live PASS)
- [x] One real document-embedding and one real query-embedding request (live PASS)
- [x] Task types, 768 dimensions, finite values, and normalised vectors verified (unit tests and live)
- [x] Bounded retry for 429/5xx, with clear quota errors (an exhausted daily quota is not retried; deadline exceeded is not retried, see below)
- [x] `GEMINI_THINKING_LEVEL` passed; a finish reason other than `STOP`, or an empty answer, is reported as incomplete
- [x] Usage and latency logging without keys or prompts, plus log redaction of secrets
- [x] Deterministic mocks for automated tests (`tests/fakes/`)
- [x] Tests proving no paid fallback is attempted
- [x] Configuration status endpoint that reveals no secrets (`GET /api/v1/ai/status`)

Deferred, with reasons (ADR 0005): the SPEC.md §2.5 daily caps (300 embedded texts per day, 30 chats per user) go into M4 and M7, where real usage is counted.

## M3 acceptance evidence (2026-11-02)

| Criterion | Command | Result |
|---|---|---|
| Mocked tests pass | `uv run pytest` (host, throwaway pgvector; same as CI) | **PASS: 303 passed, 0 skipped** (202 unit, 101 integration; 127 of them new for M3) |
| Same suite in the api container | `docker compose exec api pytest -q` | PASS: 300 passed, 3 skipped (`.env.example` isn't in the image; those 3 run on host and CI) |
| Live generation + embeddings | `docker compose exec api python -m app.providers.smoke` | **PASS** on the final run (details below) |
| No API response contains the key | `test_no_response_ever_contains_a_secret` (status, me, workspace, health, ready, openapi, docs); live: status and register responses compared against the real key inside the container | PASS: key and its 10-character prefix absent |
| No logs contain the key | `RedactingFormatter` tests; live: 140 lines of Compose logs checked against the real key inside the container | PASS: absent |
| Status endpoint never calls Gemini | integration tests replace `genai.Client` with a function that fails if called | PASS |
| M2 auth and isolation still work | full suite; live: anonymous `/ai/status` → 401, A reading B's workspace → 404 | PASS |
| Lint / format / strict types / lock | `ruff check`, `ruff format --check`, `mypy` (strict, app + tests), `uv lock --check` | PASS |
| Compose config | `docker compose config --quiet` | PASS |
| Tests catch regressions (mutation check) | 20 controls broken one at a time, then restored with `git checkout`: Vertex guard, SDK-retry guard, normalisation, finish-reason and empty checks, retry bound, daily-quota rule, long-wait rule, 400 handling, dimension/NaN/count checks, query task type, thinking level, prompt logging, status key leak, status quota state, log redaction, timeout retry, automatic function calling | PASS: every mutation caught (1–10 failing tests each); working tree clean |
| CI workflow valid | `actionlint` | PASS. CI run on GitHub: not checked from here (`gh` not installed) |

### Live Gemini results (real key, free tier, 2026-11-02 UTC)

| Run | Generation (`gemini-3.8-flash`) | Document embedding | Query embedding | Outcome |
|---|---|---|---|---|
| 1 (02:15) | 4 × `504 DEADLINE_EXCEEDED` at about 30 s | not run (the command stopped at step 1) | not run | exit 4: provider slow |
| 2 (02:19) | 4 × `504 DEADLINE_EXCEEDED` | **PASS**: 2 texts, 1 request, 2 × 768, norm 1.000000000, 803 ms | **PASS**: 768, norm 1.000000000, 504 ms | exit 4; the key, project, and embeddings are fine |
| Diagnostic (between runs 2 and 3) | **1** raw-SDK request with no deadline: **OK in 50.7 s**, `STOP`, 1 output token, 0 thinking tokens | — | — | the provider was slow; the request was correct |
| 3, final (02:25) | 3 × `503` (overload) retried with backoff 0.6/1.5/2.8 s, then **200**: `STOP`, 5 chars, prompt 20 / output 1 / thinking 0 tokens, 41.6 s total | **PASS** (847 ms) | **PASS** (523 ms); query is closer to the returns text than the shipping text (cosine 0.715 vs 0.578) | **exit 0, RESULT: PASS** |

Total live requests today: 17 (13 generation, 4 embedding). No quota error was hit, and no paid feature was used.

### Problems found and fixed during M3

1. **The 30 s timeout turned provider slowness into failure.** The SDK sends the client timeout to Gemini as `X-Server-Timeout`, so slow requests came back as `504 DEADLINE_EXCEEDED`, and each retry restarted the same 50 s wait (8 of 8 failed). Google's API-errors reference says for 504: "Remove or increase client deadline setting to use the server default." **Fix:** the default timeout is now 90 s, and deadline-exceeded (504, read or write timeout) is a `ProviderTimeout` that isn't retried. 500/502/503, 429 rate limits, and connection failures still back off.
2. **The smoke command stopped at the first failing step**, which hid the embedding results. Each step now runs and reports independently.
3. **Automatic function calling was on by default in the SDK.** It was inert, because no tools are attached, but it's now disabled explicitly so each call is exactly one request.
4. **A dependency side effect:** `google-genai` pulls in `cryptography`, which tightens PyJWT's key types. One M2 `alg: none` test was updated to pass an empty key, which PyJWT expects; the behaviour tested is the same.
5. **A test fake looked like a real key.** It matched the `AQ.` format and was caught by the hygiene check. Renamed rather than weakening the check.
6. **My first mutation run damaged the working tree.** zsh didn't split the file list, so the backups failed silently and mutations accumulated in files not yet committed. Each mutation was reversed exactly, verified by the full suite (296 passed) and a read-through. The work was then committed, and the mutation tests were rerun as a bash script that restores with `git checkout` and checks the tree is clean.

## Manual review steps (owner)

| Step | Expected | Result |
|---|---|---|
| `docker compose up --build -d` (keep `API_HOST_PORT=8001` if 8000 is busy) | all services healthy | not yet checked |
| `docker compose exec api python -m app.providers.smoke` | `RESULT: PASS`, exit 0; or exit 3/4 with a clear message if Gemini is busy or out of quota | not yet checked |
| Sign in (README curl block), then `curl -b alice.jar …/api/v1/ai/status` | `"status": "configured"`, models listed, `"paid_fallback": false`, no key anywhere | not yet checked |
| Temporarily set `GEMINI_API_KEY=` in `.env`, restart api, check status and smoke | status `missing_key`; smoke exit 2 "PENDING"; the rest of the app still works | not yet checked |
| AI Studio: the key's project is on the **Free** tier with no billing account linked | Free tier | not yet checked |
| `docker compose exec api pytest -q` | 300 passed, 3 skipped | not yet checked |

## Commits

| Milestone | SHA | Message |
|---|---|---|
| M0 | `e2b2a9f` … `47b96a4` | 8 commits; see [M0 summary](#m0-summary) |
| M1 | `2412552` … `e8d667a` | 6 commits: backend skeleton, migrations, Compose, tests, CI, docs |
| M2 | `33199da` … `8541c30` | 6 commits: auth schema, tenancy guard, auth, API, tests, docs |
| M2 review fix | `e488b7b` | fix(auth): reserve per-email login slots atomically before verifying |
| M3 | `b9f32a2` | feat(providers): add provider interfaces with Gemini generation and embeddings |
| M3 | `1a86982` | feat(api): add AI configuration status endpoint and secret redaction in logs |
| M3 | `45defa1` | feat(providers): add a minimal live Gemini smoke test command |
| M3 | `2f6450d` | test: cover Gemini provider, retries, quota handling, and AI status |
| M3 | `f6741de` | fix(providers): stop retrying deadline-exceeded requests and raise the timeout |
| M3 | see `git log` | docs: document the Gemini provider and record Milestone 3 results |

## Blockers

None.

## Decisions

- Owner: [ADR 0001](docs/decisions/0001-m0-preflight-decisions.md) (thinking level `low`, Node 24, `uv`, 3.6 Flash correction, keep `gemini-embedding-001`) and [ADR 0002](docs/decisions/0002-main-branch-workflow.md) (main-branch workflow).
- Implementation: [ADR 0003](docs/decisions/0003-m1-service-foundation.md) (M1), [ADR 0004](docs/decisions/0004-m2-auth-and-tenancy.md) (M2), and [ADR 0005](docs/decisions/0005-m3-gemini-provider.md) (M3: interfaces, normalisation, retry policy, deadline-exceeded handling, the status endpoint, and §2.5 caps moved to M4/M7).

## Risks, limitations, and deferred work

- **Gemini latency and availability vary a lot on the free tier.** Today one generation took 41–51 s, with 503s and 504s along the way. Chat (M7) must show a clear loading state and a clean error. Worst case per call is about 90 s for a timeout, or up to 4 attempts for overload errors.
- **The status endpoint is process-local:** `last_error` reflects only what this API process saw (not the worker), and resets on restart. By design it never calls Gemini.
- **Daily caps (§2.5) not yet enforced:** 300 embedded texts and 30 chats per day arrive with their callers in M4 and M7.
- **Free-tier data use:** Google may use free-tier content to improve its products. Use only synthetic or public data.
- Registration enumeration (409); no refresh tokens, password reset, or MFA; per-IP limits are global behind Docker Desktop; fixed windows can allow 2 × limit across a rollover (M2; see ADR 0004).
- The session guard covers ORM statements only. M6's raw pgvector SQL must filter `workspace_id` explicitly.
- `gemini-embedding-001` earliest shutdown 2028-05-14.
- Frontend and the CI frontend job: M8.
- `uv` isn't installed system-wide on the development machine. Docker and CI use pinned `uv` 0.13.6.
- Port 8000 is used by another local project on the development machine; use `API_HOST_PORT`.

## Earlier milestones

- **M0:** environment, Gemini, SDK, and pgvector compatibility verified; repo hygiene and CI skeleton. See [docs/m00-compatibility-report.md](docs/m00-compatibility-report.md).
- **M1:** Compose stack (db, migrate, api, worker placeholder), `/health` and `/ready`, Alembic, an internal data network, and persistent volumes. See [ADR 0003](docs/decisions/0003-m1-service-foundation.md).
- **M2:** Argon2id, HS256 cookie sessions with server-side revocation, Origin/CORS/Host checks, PostgreSQL rate limits (with the concurrency fix), and a fail-closed workspace session guard. Proven by two-tenant tests and mutation checks. See [ADR 0004](docs/decisions/0004-m2-auth-and-tenancy.md) and the M2 sections in git history (`e488b7b:PROJECT_STATUS.md`).

## M0 summary

Environment, Gemini, SDK, and pgvector compatibility were verified (live Gemini smoke test PASS), along with repository hygiene and the CI skeleton.
Full details: [docs/m00-compatibility-report.md](docs/m00-compatibility-report.md).
