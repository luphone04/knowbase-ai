# Project status

| Field | Value |
|---|---|
| Current milestone | **M0 — Repository, GitHub, and compatibility preflight** |
| Status | **BLOCKED**: M0 work is done locally; waiting on user input (see Blockers) |
| GitHub repository | https://github.com/luphone04/knowbase-ai. Visibility: believed **private**, not yet confirmed by user |
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
- [x] Selected Gemini models are listed in the official docs (3.8 Flash free tier confirmed in docs; embedding free tier **pending**)
- [ ] Selected models are available **on the user's key**: **PENDING** (no API key)
- [x] Installed stable `google-genai` (2.34.0) supports the exact spec call shapes (offline introspection)
- [ ] `gemini-embedding-001` returns 768-dim vectors live: **PENDING** (no API key)
- [x] Pinned pgvector image pulled and extension enabled (`vector` 0.8.7, `pg_trgm` 1.6, PG 18.6)
- [x] Spec vs official docs mismatches identified (report §6, C1–C9). No architecture changes made
- [ ] User has a Gemini API key: **awaiting user**
- [ ] GitHub repository URL and visibility confirmed: **awaiting user**
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
| Live Gemini smoke test | `.venv-preflight/bin/python scripts/preflight/gemini_smoke.py` | **PENDING**: no API key. Without a key the script exits 2 and makes no requests (verified) |

## Manual review steps (user)

| Step | Result |
|---|---|
| Review README, `.gitignore`, `.env.example`, CI workflow, PR template | not yet checked |
| Read the compatibility report and decide D1–D4 (below) | not yet checked |
| Confirm GitHub remote URL and **private** visibility | not yet checked |
| Create an AI Studio key (no billing), put it in `.env`, run the smoke test (README) | not yet checked |

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
3. **Live Gemini smoke test:** needs a free-tier AI Studio key in `.env`.

## Decisions requested

- **D1 (needed before M3):** thinking tokens count toward `max_output_tokens=1200` (report C1).
  Recommendation: add a configurable `GEMINI_THINKING_LEVEL` (default `low`), and confirm with the live smoke test.
- **D2 (needed now):** GitHub push and PR route (Blockers 1–2).
- **D3 (needed before M1 frontend placeholder):** Node 22 (spec; maintenance LTS until 2027-04-30) or Node 24 (matches host; maintenance until 2028-04-30). Recommendation: keep Node 22 as specified.
- **D4 (needed before M1):** Python lock tool. Recommendation: `uv` with a committed `uv.lock` (`brew install uv`). Alternative: `pip-tools` with a compiled `requirements.lock`.

## Risks and deferred work

- Gemini free-tier quotas and model availability can change; RPM/RPD are only visible in AI Studio.
- `gemini-embedding-001` has an earliest shutdown of 2028-05-14 (replacement `gemini-embedding-2` would need a design change and a full re-index).
- Spec §2.1 states a 3.6 Flash retirement date that the Gemini API deprecations page doesn't show (report C2). Informational only.
- CI runs only repository hygiene for now; backend/frontend jobs come in M1/M8.
