# Project status

| Field | Value |
|---|---|
| Current milestone | **M0 — Repository, GitHub, and compatibility preflight** |
| Status | **BLOCKED**: M0 work and live Gemini check are done locally; waiting on push confirmation and PR route (see Blockers) |
| GitHub repository | https://github.com/luphone04/knowbase-ai. Visibility: **private** (confirmed by owner 2026-11-01) |
| Current branch | `chore/m00-project-setup` (local only, not pushed) |
| Pull request | not created |
| Last verified checkpoint | 2026-11-01 |

## Milestones

| Milestone | Scope | Status | Branch / PR | Approval |
|---|---|---|---|---|
| M0 | Repository and compatibility preflight | BLOCKED | `chore/m00-project-setup` / not created | — |
| M1 | Database and service foundation | NOT_STARTED | — | — |
| M2 | Authentication and workspace isolation | NOT_STARTED | — | — |
| M3 | Gemini provider and embeddings | NOT_STARTED | — | — |
| M4 | File ingestion and durable worker | NOT_STARTED | — | — |
| M5 | Website crawler and incremental indexing | NOT_STARTED | — | — |
| M6 | Hybrid retrieval and RRF | NOT_STARTED | — | — |
| M7 | Grounded chat, citations, and abstention | NOT_STARTED | — | — |
| M8 | Frontend product experience | NOT_STARTED | — | — |
| M9 | Evaluation, hardening, and portfolio finish | NOT_STARTED | — | — |

## M0 checklist

- [x] Inspect folder, files, Git status, branches, remotes, and commits
- [x] Inspect Python, Node, npm, Docker, and Docker Compose versions
- [x] Docker is running and the pinned pgvector image can be pulled
- [x] Selected Gemini models are listed in the official docs (3.8 Flash free tier confirmed in docs)
- [x] Selected models are available on the user's key (live `models.get`, 2026-11-01)
- [x] Installed stable `google-genai` (2.34.0) supports the exact spec call shapes (offline introspection)
- [x] `gemini-embedding-001` returns 768-dim vectors live (document batch of 2 + query); raw norms ~0.59, so normalization is required
- [x] Pinned pgvector image pulled and extension enabled (`vector` 0.8.7, `pg_trgm` 1.6, PG 18.6)
- [x] Spec vs official docs mismatches identified (report §6, C1–C9) and owner decisions applied to SPEC.md v1.2 ([ADR 0001](docs/decisions/0001-m0-preflight-decisions.md)). No architecture changes
- [x] User has a Gemini API key (stored only in git-ignored `.env`; user reports free tier)
- [x] GitHub repository URL and visibility confirmed (private; owner, 2026-11-01)
- [x] `.gitignore`, `README.md`, `PROJECT_STATUS.md`, `.github/PULL_REQUEST_TEMPLATE.md`, CI skeleton created
- [x] `docs/architecture/overview.md` added
- [x] No secrets or local data staged (`scripts/check_repo_hygiene.sh`)

## M0 acceptance criteria and test commands

| Criterion | Command | Result |
|---|---|---|
| Written environment and compatibility report | see [docs/m00-compatibility-report.md](docs/m00-compatibility-report.md) | Done |
| `docker compose config` | No `compose.yaml` yet; deferred to M1 per spec §17.5 | Deferred |
| Git status clean apart from intended setup files | `git status --short` | Clean after commits |
| No secrets or local data tracked/staged | `bash scripts/check_repo_hygiene.sh` | PASS (`OK: no secrets or local data tracked or staged.`) |
| Hygiene check negative tests | stage, in turn, a dummy `.env`, a file with a fake `AIza…` key, and a file declaring a `NEXT_PUBLIC_*` variable for the Gemini key; run check; unstage | PASS (each exits 1 with a FAIL message). The first run exposed a bug: the content checks never set a failing status because `xargs` exits 123. Fixed and re-tested |
| CI workflow is valid | `actionlint` (Docker image `rhysd/actionlint`) | PASS (no findings) |
| pgvector image works | throwaway `docker run --rm` + `psql` checks | PASS |
| Live Gemini smoke test | `.venv-preflight/bin/python scripts/preflight/gemini_smoke.py` | PASS: all checks; `finish_reason=STOP`; thoughts 191 of 207 total tokens |
| Live smoke test, low thinking | `... gemini_smoke.py --thinking-level low` | First attempt **FAILED** with `503 UNAVAILABLE` ("high demand"); immediate retry PASS, thoughts none, 16 total tokens |
| Smoke test without a key | `env -u GEMINI_API_KEY ... gemini_smoke.py` (no `.env`) | PASS: exits 2, makes no requests |

## Manual review steps (user)

| Step | Result |
|---|---|
| Review README, `.gitignore`, `.env.example`, CI workflow, PR template | not yet checked |
| Read the compatibility report and decide D1–D4 | Done by owner 2026-11-01 (see Decisions) |
| Confirm GitHub remote URL and **private** visibility | Confirmed by owner 2026-11-01 |
| Confirm in AI Studio that billing is **not** enabled for the key's project | not yet checked |
| Optionally re-run the smoke test yourself (README) | not yet checked |
| Review the SPEC.md v1.1 → v1.2 diff in the PR | not yet checked |

## Commits

| Branch | SHA | Message |
|---|---|---|
| `main` (local) | `e2b2a9f` | docs: add Knowbase AI implementation specification |
| `chore/m00-project-setup` | see `git log`; recorded in the PR | chore: add repository hygiene, env template, and CI skeleton |
| `chore/m00-project-setup` | see `git log`; recorded in the PR | docs: add README, architecture overview, compatibility report, and status tracker |

Merge commit: none.

## Blockers

1. **Push confirmation:** confirm the remote and visibility. Plan: push
   `main` (contains only SPEC.md), then push `chore/m00-project-setup`.
2. **PR creation:** `gh` is not installed. Either `brew install gh && gh auth login`,
   or open the PR in the GitHub web UI after the push.

## Decisions (owner, 2026-11-01)

Recorded in [docs/decisions/0001-m0-preflight-decisions.md](docs/decisions/0001-m0-preflight-decisions.md) and applied in SPEC.md v1.2.

- **D1:** configurable `GEMINI_THINKING_LEVEL`, default `low` (implemented in M3; added to `.env.example`).
- **D2:** remote `luphone04/knowbase-ai` (private). Push `main` and `chore/m00-project-setup`; owner opens the PR manually; no merge by Claude Code.
- **D3:** Node 24 LTS.
- **D4:** `uv` with a committed `uv.lock` (`uv` must be installed before M1).
- **C2:** Gemini 3.6 Flash retirement statement corrected in SPEC.md §2.1 and §20.
- **C3:** keep `gemini-embedding-001` for the MVP.

## Risks and deferred work

- Gemini free-tier quotas and model availability can change; RPM/RPD are only visible in AI Studio.
- Gemini returned a transient `503 UNAVAILABLE` during M0, so the provider must retry 5xx with bounded backoff (spec §2.4).
- `gemini-embedding-001` has an earliest shutdown of 2028-05-14 (replacement `gemini-embedding-2` would need a design change and a full re-index).
- CI runs only repository hygiene for now; backend/frontend jobs come in M1/M8.
- `uv` is not installed on the development machine yet; needed at the start of M1.
