# Milestone 0 — Environment and compatibility report

Checked: 1 November 2026, on the developer machine (macOS 26.6.2, Apple
Silicon arm64). Everything here comes from commands actually run or from
official pages actually read. Items not verified are marked **PENDING**.

## 1. Local environment

| Tool | Found | Spec expects | Result |
|---|---|---|---|
| Python | `python3` → 3.14.0; `python3.12` → 3.12.10 | 3.12 | OK. Use `python3.12` on the host; the backend image will pin 3.12 |
| uv / pyenv | not installed | `uv` (decision D4) | Install `uv` before M1 |
| Node | v24.21.0 | Node 22 LTS in spec v1.1; Node 24 LTS from v1.2 (D3) | OK under the new decision |
| npm | 11.19.0 | — | OK |
| Docker Engine | 29.8.1 (Docker Desktop, aarch64) | — | OK. Daemon was stopped; started Docker Desktop during the check |
| Docker Compose | v5.5.1 | `service_healthy`, `service_completed_successfully` | OK (supported) |
| Git | 2.50.1 | — | OK |
| GitHub CLI `gh` | **not installed** | Used to open PRs | Blocker for automated PR creation (see D2) |

## 2. Database image

`pgvector/pgvector:0.8.7-pg18-trixie` was pulled and tested in a throwaway
container (`--rm`, no volume):

- Digest `sha256:5895949dfcdf308eb0cd31c950c93c14895c77f33fbedfecf1fa511ab1d4bcd2`; platforms `linux/amd64`, `linux/arm64`.
- `PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2)`.
- `CREATE EXTENSION vector` → `extversion = 0.8.7`; `CREATE EXTENSION pg_trgm` → `1.6`.
- Ran the spec's exact `chunks.search_tsv` definition
  (`GENERATED ALWAYS AS (to_tsvector('simple', coalesce(content, ''))) STORED`)
  plus `vector(768)`, an HNSW `vector_cosine_ops` index, a GIN index,
  `websearch_to_tsquery('simple', …)`, and `ts_rank_cd`. All succeeded.

**Result: compatible.**

## 3. Python SDK and dependencies

Resolved latest stable releases in a throwaway Python 3.12.10 venv (not committed):

| Package | Version | Package | Version |
|---|---|---|---|
| google-genai | 2.34.0 (requires Python ≥3.11) | fastapi | 0.162.0 |
| pydantic | 2.14.2 | pydantic-settings | 2.16.2 |
| SQLAlchemy | 2.1.6 | alembic | 1.20.1 |
| psycopg[binary] | 3.3.6 | pgvector (Python) | 0.5.1 |
| pypdf | 6.21.4 | python-docx | 1.2.0 |
| httpx | 0.28.1 | beautifulsoup4 | 4.15.0 |
| pwdlib[argon2] | 0.3.1 (argon2-cffi 25.1.0) | PyJWT | 2.15.1 |
| tenacity | 9.1.4 | pytest | 9.2.0 |
| numpy | 2.5.4 | uvicorn | 0.54.0 |

Offline SDK checks (`google-genai` 2.34.0, introspection only, no network):

- `client.models.embed_content(model=, contents=list[str], config=)` exists; `EmbedContentConfig` has `task_type` and `output_dimensionality`.
- `client.models.generate_content(model=, contents=, config=)` exists; `GenerateContentConfig` has `system_instruction`, `max_output_tokens`, `thinking_config`.
- The spec's config objects (§2.2, §2.3) build without errors.
- `ThinkingConfig` has `thinking_level` (enum: `MINIMAL, LOW, MEDIUM, HIGH`) and `thinking_budget`.
- Error classes: `errors.APIError`, `ClientError` (4xx incl. 429), `ServerError` (5xx).

**Result: the call shapes in the spec match the installed stable SDK.** Live behaviour is **PENDING** (§5).

## 4. Gemini models: official documentation

Pages read 1 Nov 2026: models, gemini-3.8-flash, gemini-embedding-001,
pricing, embeddings, rate-limits, thinking, deprecations, whats-new-gemini-3.8
(all under `ai.google.dev/gemini-api/docs/`).

| Check | Finding | Result |
|---|---|---|
| `gemini-3.8-flash` listed | "Gemini 3 > Stable", model code `gemini-3.8-flash`, updated Sept 2026. Deprecations page: "No shutdown date announced" | OK |
| 3.8 Flash free tier | Pricing, Free Tier: input and output "Free of charge" | OK |
| `gemini-embedding-001` listed | Stable; input limit 2,048 tokens; output "128 - 3072, Recommended: 768, 1536, 3072" | OK |
| 768 dims + normalization | "you must manually normalize non-3072 dimensions" for `gemini-embedding-001` | Matches spec §2.2 |
| Task types | `RETRIEVAL_DOCUMENT`, `RETRIEVAL_QUERY` listed | OK |
| Batch of texts | 001 "lets you generate individual embeddings for a list of strings" | OK |
| Embedding free tier | Only in the truncated part of the pricing page; not confirmed verbatim | Calls succeed on the user's free-tier key (§5); user to confirm billing is off in AI Studio |
| Free-tier rate limits | Not published per model in the docs; shown in AI Studio only | **PENDING**: user to check AI Studio |
| Free-tier data use | Free: "Content used to improve our products" | Matches spec §2.4 |

## 5. Live Gemini checks: PASS

Ran [scripts/preflight/gemini_smoke.py](../scripts/preflight/gemini_smoke.py)
on 1 Nov 2026 with the user's AI Studio key, stored in the git-ignored
`.env`. The user says it's a free-tier key; billing status wasn't checked
by the script. Interpreter: Python 3.12.10, `google-genai` 2.34.0.

| Check | Result |
|---|---|
| `models.get("gemini-3.8-flash")` | PASS: "Gemini 3.8 Flash", actions include `generateContent` |
| `models.get("gemini-embedding-001")` | PASS: "Gemini Embedding 001", actions include `embedContent` |
| `RETRIEVAL_DOCUMENT`, list of 2 texts, `output_dimensionality=768` | PASS: 2 embeddings, each 768-dim, finite |
| `RETRIEVAL_QUERY`, 768 | PASS: 1 embedding, 768-dim, finite |
| Normalization needed | Confirmed: raw norms 0.592, 0.589, 0.587 (not unit length); unit length after dividing by the L2 norm |
| `generate_content` with spec §2.3 config | PASS: text returned, `finish_reason=STOP` |

Generation token usage on a trivial prompt ("Reply with the word: ready"):

| Run | Thinking level | Prompt | Output | Thoughts | Total |
|---|---|---|---|---|---|
| 1 | model default (medium) | 14 | 2 | **191** | 207 |
| 2 | `low` via `ThinkingConfig` | 14 | 2 | none | 16 |

The first attempt of run 2 failed with
`code=503 status=UNAVAILABLE message=This model is currently experiencing high demand...`.
An immediate retry succeeded. This is the retryable 5xx case that spec
§2.4 requires bounded backoff for.

Total live requests: 15 (3 runs × 5). Run 2's first attempt reached the
generation call before the 503.

## 6. Spec compatibility findings

The owner's decisions on these findings are recorded in
[docs/decisions/0001-m0-preflight-decisions.md](decisions/0001-m0-preflight-decisions.md)
and applied in SPEC.md v1.2. The architecture is unchanged.

**C1 — `max_output_tokens=1200` includes thinking tokens. Decided (D1): configurable `GEMINI_THINKING_LEVEL`, default `low`.**
The thinking docs say `max_output_tokens` "applies to the combined total of
thinking tokens and output tokens, setting a low limit can truncate
responses" and can return "truncated or empty output". `gemini-3.8-flash`
thinks at `medium` by default. With spec §2.3 as written, grounded answers
could be cut off or come back empty. **Measured (§5):** a one-word reply used
191 thinking tokens at the default level and none at `low`. A grounded
answer over 5 excerpts will likely think more than that.
Options, all free-tier and without architecture change:
(a) add a `GEMINI_THINKING_LEVEL` setting (for example `low`) passed through `ThinkingConfig`;
(b) raise `max_output_tokens`;
(c) keep the spec as written and decide after the live test.
Note: `minimal` is in the SDK enum but "is not supported for Gemini 3.8 Flash and will return an error".

**C2 — Gemini 3.6 Flash retirement date differs by source. Decided: statement corrected in SPEC.md v1.2.**
Spec §2.1 says 19 Nov 2026, citing Google Cloud model docs. The Gemini API
deprecations page lists `gemini-3.6-flash` with "No shutdown date announced".
This doesn't change anything because the spec selects 3.8 Flash. The
wording in §2.1 could be corrected.

**C3 — `gemini-embedding-001` has a scheduled shutdown. Decided: keep `gemini-embedding-001` for the MVP.**
Deprecations page: earliest shutdown 14 May 2028, replacement
`gemini-embedding-2`. Spec selection stays valid. `gemini-embedding-2` would
not fit the current design: it does not accept `task_type`, and it returns
one aggregated embedding for multiple inputs. Spec §2.2 already requires a
full re-index on a model change.

**C4 — Docs now lead with the Interactions API (info; verify live).**
The 3.8 and thinking pages use `client.interactions.create(...)` in examples.
The spec uses `client.models.generate_content`, which still exists in SDK
2.34.0. Nothing read says `generate_content` is deprecated. **Live test:
`generate_content` works with `gemini-3.8-flash` (§5).**

**C5 — Sampling parameters (consistent).** The 3.8 migration checklist says
"Remove deprecated sampling parameters (`temperature`, `top_p`, `top_k`),
`candidate_count`". This matches spec §2.3. Use `thinking_level`, not
`thinking_budget`.

**C6 — Chunk size vs embedding input limit (consistent).** The max chunk is
2,400 characters (roughly 600 tokens), well under the 2,048-token input
limit. The docs give no per-call batch limit, so the spec's batch of 8 is
conservative.

**C7 — Node version. Decided (D3): Node 24 LTS.** Node 22 has been in *maintenance* LTS
since 2025-10-21, with EOL 2027-04-30. Node 24 entered maintenance
2026-10-20. Node 26 has been Active LTS since 2026-10-28. Latest Next.js is
16.4.3 (`engines.node >=20.9.0`), so Node 22 works. The host has Node 24.21.0.
`node:22-bookworm-slim` is available.

**C8 — `NEXT_PUBLIC_API_BASE_URL` is build-time (note for M1/M8).** Next.js
inlines `NEXT_PUBLIC_*` at build time, so the frontend image has to receive
it as a build arg. It is not a secret.

**C9 — Cookie auth across ports (consistent).** `localhost:3000` →
`localhost:8000` is same-site (port is ignored for site), so a
`SameSite=Lax` cookie is sent with `credentials: "include"` under exact-origin
CORS, as spec §11 intends.

## 7. Repository and GitHub

- Before M0: Git repo initialised on `main` with **no commits**; only `SPEC.md` (untracked); clean otherwise.
- Remote: `origin` → `https://github.com/luphone04/knowbase-ai`. `git ls-remote origin` succeeded (exit 0) with **no refs**, so the remote is empty and reachable with the keychain credentials.
- Visibility: the unauthenticated GitHub API returns 404 for the repo, which suggests it is **private**. Can't confirm without `gh` or the web UI; user to confirm.
- Done locally (nothing pushed): root commit on `main` containing only `SPEC.md`, so the PR has a base; branch `chore/m00-project-setup` for the setup files.
