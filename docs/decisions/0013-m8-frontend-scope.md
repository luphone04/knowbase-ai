# 0013 — Milestone 8: a minimal frontend that demonstrates the backend

- Status: Accepted (owner decisions, 4 November 2026). Changes SPEC.md §11, §12, §16, and §17.13
  (version 1.5).
- Date: 4 November 2026

## Context

Knowbase AI is an AI engineering project: the backend (ingestion, hybrid retrieval, grounded
chat with validated citations and abstention) is the product. M0–M7 built and tested it with no
browser interface. SPEC.md §12 describes a fuller SaaS dashboard, including views that need
endpoints no milestone built (`GET /analytics/overview`) or that belong to M9 (evaluations).

The owner set M8's scope: a simple, clean, working interface whose job is to demonstrate the
backend. The acceptance flow is:

Register → upload → index → ask → grounded answer → open citation → follow-up → abstention.

## Decision

### Scope

1. **Built:** register, login, logout; a minimal dashboard (workspace, AI status, document
   counts); documents (upload, list, live status, detail with jobs and errors, re-index,
   delete); chat (conversations, questions, answers, follow-ups); a citation panel; clear
   states for failed uploads, failed jobs, AI errors, and abstentions.
2. **No backend changes.** The frontend uses only the existing, tested API. A requirement the
   API can't serve is reported to the owner before any backend work.
3. **Moved to M9:** the evaluation page, with no placeholder page or navigation item.
4. **Deferred** (recorded in PROJECT_STATUS.md, not scheduled): `GET /analytics/overview` and the
   dashboard metrics that need it: job totals across history, a recent-jobs list, chat request
   counts, and token usage.
5. **Not built:** streaming, renaming or deleting conversations, workspace settings and members,
   a retrieval-mode switch (chat always uses `hybrid`), dark mode, multi-file upload, a file
   viewer, mobile-specific polish beyond a usable narrow layout.

### Data shown must be complete

- `GET /sources` returns every source in the workspace (no limit or paging), each with its
  document and chunk counts, so the dashboard's totals are sums over that list.
- `GET /conversations` returns at most 100 conversations, so the UI shows the list, never a
  conversation total.
- The PDF page count comes from `documents[].metadata.page_count` on the detail page only;
  the list response has no page count.

### Architecture

- **Browser → API directly**, with `credentials: "include"`. The HttpOnly `kb_session` cookie is
  never read by JavaScript; the session comes from `GET /auth/me`. This is the setup the M2
  defences were built for: exact-origin CORS, the Origin check, `SameSite=Lax`
  (`localhost:3000` and `localhost:8001` are the same site).
- **No API proxy in Next.js.** It would change the Host and Origin headers that
  `TrustedHostMiddleware` and `OriginCheckMiddleware` check.
- **No server-side API calls.** Pages are client components; the Next.js server serves the app
  and holds no secrets.
- `lib/api.ts` holds every request and turns the API's `{"error": {code, message, details}}`
  into one typed error; `lib/types.ts` mirrors `backend/app/api/v1/schemas.py`.
- **TanStack Query** for fetching. Polling that stops when a job finishes, refreshing lists
  after an upload, re-index, or delete, and loading and error states each come from one hook
  instead of hand-written intervals per page.

### Behaviour

- **Job status is by stage** (`queued`, `running`, `succeeded`, `failed`, with attempts); the API
  has no percentage. The documents list polls every 3 s while any source is `pending`,
  `processing`, or `deleting`, and stops otherwise. A source shows Ready only when the worker
  has marked it ready. A deleted source leaves the list when its delete job removes it.
- **Chat** posts one question and waits (up to about 90 s on a slow Gemini). A conversation is
  created when its first question is sent. Provider errors (`ai_quota`, `rate_limited`,
  `ai_unavailable`, `ai_not_configured`, `ai_incomplete`, `ai_error`) show as an error and keep the
  question for a retry, because the server did not save the turn. An abstention is a saved
  reply and shows as a distinct card.
- **Answers** render as Markdown with `react-markdown` (raw HTML ignored). An `[Sn]` marker that
  matches a returned citation becomes a button that opens the citation panel (source title,
  file name, page range, heading, full excerpt); any other marker stays text. Other links
  render only for `http(s)`, with `rel="noopener noreferrer nofollow"`.
- **Oversized uploads:** the body-size middleware answers 413 outside CORS, so a browser sees
  only a network error (ADR 0010 deferred CORS headers on middleware errors to M8). Instead of
  changing the backend, the frontend checks the size first against `MAX_UPLOAD_MB` (passed at
  build time) and explains a failed upload. The backend item stays open.

### Running it

- Compose adds a `frontend` service on `127.0.0.1:3000`. It **does not load `.env`** (which holds
  the Gemini key): it gets only `NEXT_PUBLIC_API_BASE_URL` (default
  `http://localhost:${API_HOST_PORT}/api/v1`) and `NEXT_PUBLIC_MAX_UPLOAD_MB` as build
  arguments, which are not secrets. It runs as a non-root user, on its own network.
- Open the app at `http://localhost:3000`. `127.0.0.1:3000` fails the Origin check, because
  `FRONTEND_ORIGIN` is an exact match.

### Tests

- CI: ESLint, `tsc --noEmit`, Vitest for pure logic (citation markers, error messages, job
  status labels, when polling stops), `next build`, and a scan of the build output for the
  Gemini key variable and key-shaped strings.
- One Playwright test of the acceptance flow, run locally against the live stack with live
  Gemini (CI has no key). Its temporary account is removed afterwards.

## Consequences

- The backend stays exactly as approved in M7; M8 adds no endpoints, fields, or migrations.
- The dashboard is smaller than SPEC.md §12.1 describes, and says only what the API returns.
- An oversized upload is explained by the frontend's own check, not by the server's 413, until
  the deferred CORS item is done.
