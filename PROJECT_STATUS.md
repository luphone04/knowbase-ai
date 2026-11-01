# Project status

| Field | Value |
|---|---|
| Current milestone | **M0 — Repository, GitHub, and compatibility preflight** |
| Status | **READY_FOR_REVIEW**: checks pass; fast-forwarded into `main` and pushed; awaiting `APPROVE MILESTONE 0` |
| GitHub repository | https://github.com/luphone04/knowbase-ai. Visibility: **private** (confirmed by owner 2026-11-01) |
| Workflow | Work on `main`; one milestone, then commit, push, report, and STOP ([ADR 0002](docs/decisions/0002-main-branch-workflow.md)) |
| Current branch | `main` |
| Pull requests | Not used from M1 on. M0's PR #1 (opened by owner) has its head `bd61934` contained in `main`. `chore/m00-project-setup` is kept on the remote |
| Last verified checkpoint | 2026-11-01 |

## Milestones

| Milestone | Scope | Status | Commit on `main` | Approval |
|---|---|---|---|---|
| M0 | Repository and compatibility preflight | READY_FOR_REVIEW | head of `main` (see Commits) | Pending: `APPROVE MILESTONE 0` |
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
| Git status clean apart from intended setup files | `git status --short` | PASS: clean before push |
| No `.env` or key anywhere in history | `git log --all --name-only`; `git grep` for the key literal and `AIza…`/`AQ.…` patterns over `git rev-list --all` | PASS: 0 hits; only the 13 intended files were ever committed |
| Setup committed and pushed | `git push -u origin main`; `git push -u origin chore/m00-project-setup`; `git ls-remote origin` | PASS: both refs on origin, remote HEAD → `main` |
| CI on GitHub | runs on push to `main` and on PR #1 | Result not checked from here (`gh` not installed); see the repo's Actions tab |
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
| Review README, `.gitignore`, `.env.example`, CI workflow | not yet checked |
| Read the compatibility report and decide D1–D4 | Done by owner 2026-11-01 (see Decisions) |
| Confirm GitHub remote URL and **private** visibility | Confirmed by owner 2026-11-01 |
| Confirm in AI Studio that billing is **not** enabled for the key's project | not yet checked |
| Optionally re-run the smoke test yourself (README) | not yet checked |
| Review the SPEC.md changes since v1.1: `git diff e2b2a9f main -- SPEC.md` | not yet checked |

## Commits

| Branch | SHA | Message | Pushed |
|---|---|---|---|
| `main` | `e2b2a9f28edb655b8507281a8b352069e0b12f86` | docs: add Knowbase AI implementation specification | yes |
| `chore/m00-project-setup` | `76d46ab` | chore: add repository hygiene, env template, and CI skeleton | yes |
| `chore/m00-project-setup` | `3b1d37e` | docs: add README, architecture overview, compatibility report, and status tracker | yes |
| `chore/m00-project-setup` | `4466b34` | fix(hygiene): detect AQ.-format Google API keys | yes |
| `chore/m00-project-setup` | `c07acd3` | docs: record live Gemini preflight results | yes |
| `chore/m00-project-setup` | `c20092c` | docs: apply Milestone 0 decisions to spec and project docs | yes |
| `chore/m00-project-setup` | `bd61934` | docs: record Milestone 0 push in project status | yes |

Merge: `main` fast-forwarded from `e2b2a9f` to `bd61934` (no merge commit), then the workflow commit below.

| Branch | SHA | Message | Pushed |
|---|---|---|---|
| `main` | branch head | docs: switch to main-branch workflow (ADR 0002) | yes |

## Blockers

None for M0. Next step is the owner's review, then `APPROVE MILESTONE 0`.

## Decisions (owner, 2026-11-01)

Recorded in [docs/decisions/0001-m0-preflight-decisions.md](docs/decisions/0001-m0-preflight-decisions.md) and applied in SPEC.md v1.2.

- **D1:** configurable `GEMINI_THINKING_LEVEL`, default `low` (implemented in M3; added to `.env.example`).
- **D2:** remote `luphone04/knowbase-ai` (private). Superseded on 2026-11-01 by ADR 0002: work on `main`, push after each milestone, no PRs.
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
