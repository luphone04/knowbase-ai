# 0004 — Milestone 2 authentication and tenancy design

- Status: Accepted (within SPEC.md §6, §11, §14, §17.7; no architecture change)
- Date: 1 November 2026

## Authentication

| Choice | Why |
|---|---|
| **Argon2id** via `pwdlib` (64 MiB, t=3, p=4), rehashed on login when parameters change | Memory-hard and above OWASP minimums. `verify_and_update` lets parameters be raised later without a migration |
| **Dummy Argon2 verification for unknown emails** | Failed logins cost the same time whether or not the account exists, so timing doesn't reveal which emails are registered |
| **One error for unknown email, wrong password, and disabled account** (`401 invalid_credentials`) | No account-state leakage through login |
| **HS256 JWT** with `sub`, `wid`, `jti`, `iat`, `nbf`, `exp`, `iss`, `aud`, all required; algorithm pinned on decode | Rejects `alg: none`, algorithm substitution, and tokens from other issuers or audiences. `JWT_SECRET` must be at least 32 characters (RFC 7518 for HS256), and the `.env.example` placeholder is refused at startup |
| **HttpOnly cookie `kb_session`**: `SameSite=Lax`, `Path=/`, no `Domain`, `Max-Age` = token lifetime; `Secure` from `COOKIE_SECURE` (required in production) | SPEC.md §11. JavaScript can't read the token, and the response body never contains it |
| **Server-side revocation on logout** (`revoked_tokens` by `jti`, pruned after expiry) | A stateless JWT stays valid until `exp`. Logout without revocation would only clear the browser's copy. Each authenticated request does one primary-key lookup |
| **Membership re-checked on every request** | The token's workspace is a claim, not proof. Removing a member, or disabling a user, ends access at once, without waiting for expiry |
| **Register signs the user in** | Saves a round trip. Protected by the same Origin and rate-limit checks as login |
| **Token lifetime stays 480 minutes** (spec value), no refresh tokens | A shorter lifetime would need a refresh flow; that is out of M2 scope. Revocation covers logout |

## CSRF, CORS, and Host checks

- **Origin check middleware** on POST/PUT/PATCH/DELETE. `SameSite=Lax` stops cross-*site* requests, but every `localhost` port is the same site, so a page on another local port could still send the cookie. The Origin must equal `FRONTEND_ORIGIN` exactly, or the API's own origin (for `/docs`). Without an Origin header, `Sec-Fetch-Site` must be `same-origin` or `none`. Clients sending neither header (curl) carry no browser cookies. `Origin: null` is rejected. This also covers login and register, against login CSRF.
- **CORS**: exactly `FRONTEND_ORIGIN`, credentials allowed, explicit methods and headers, never `*`.
- **TrustedHost** (`ALLOWED_HOSTS`, default `localhost,127.0.0.1`) against DNS rebinding and Host spoofing.

## Rate limiting

- PostgreSQL fixed-window counters (no Redis, per SPEC.md §3.1). An atomic `INSERT … ON CONFLICT DO UPDATE … RETURNING` makes concurrent attempts count exactly. Each operation runs in its own transaction, so a failed login stays counted.
- Login: 20 attempts per IP per 15 minutes; 5 **failed** attempts per email per 15 minutes (reset on success). The email limit blocks even the correct password while active, so guessing can't continue. Register: 5 per IP per 15 minutes. `429` with `Retry-After`.
- **The per-email limit takes a slot before verifying the password (fix after M2 review).** The first version used a read-only `check()` before verification and `record()` after a failure. Concurrent requests all read the same stale count and all got through: 12 of 12 reached Argon2 in the regression test. Now `reserve()` is one statement:
  `INSERT … ON CONFLICT DO UPDATE SET count = count + 1 WHERE count < :limit RETURNING count`.
  PostgreSQL locks the counter row and re-checks the `WHERE` against the latest committed value, so concurrent callers can take at most `limit` slots. No row back means no slot: `429`, and Argon2 never runs. A failure keeps its slot. A success clears the counter. A rejected attempt takes no slot, so the counter equals the number of attempts that reached verification.
  - **Conservative while a success is in flight:** the successful attempt holds a slot until it clears the counter. If 4 failures are recorded and a correct login is mid-verification, a fifth concurrent wrong attempt is refused. That fails closed.
  - **In-flight failures can go uncounted:** failures already past the gate when a success clears the counter aren't counted. Only the real user's own concurrent successful login can cause that.
- **Pruning is per rule (fix after M2 review).** Each upsert used to delete *every* counter older than one day. With a window over a day, that deleted the live window too, which silently disabled the limit. It could also delete other rules' windows. Now each call deletes only its own rule's finished windows.
- Bucket keys are HMAC-SHA256 digests (keyed with `APP_SECRET_KEY`), so the table stores no raw emails or IPs.
- The client address is the direct peer. `X-Forwarded-For` is ignored, because nothing in this stack sets it and clients could forge it.

## Workspace isolation

Two independent layers:

1. **Session guard** (`app/db/tenancy.py`). `get_auth_context` binds the request's session to the token's workspace. A `do_orm_execute` hook then adds `workspace_id = :bound` to every ORM SELECT, UPDATE, and DELETE on `WorkspaceScoped` models. That includes `Session.get()`, joins, and bulk writes, even when the query has no workspace filter. It fails closed: an unbound session raises `TenantScopeError` on direct queries, and matches nothing through indirect joins. Cross-workspace code must opt out per statement with `SKIP_WORKSPACE_SCOPE`. Today that happens in exactly two places: membership lookup at login, and tests. Rebinding a session to another workspace is refused.
2. **Explicit filters in each endpoint** (for example, the caller's `user_id`). Mutation testing showed the guard alone keeps tenants apart. The explicit filter is still required for correct per-user roles inside a workspace.

Plus database constraints: the `workspace_members` composite primary key, cascading foreign keys, a role check, a normalised-email check, and a workspace-name length check.

**Not covered by the guard:** raw `text()` SQL and Core statements on `Table` objects. Retrieval SQL in M6 must filter by `workspace_id` explicitly and carry its own isolation tests. PostgreSQL row-level security would cover that too, but needs a separate non-owner database role for the app. It's recorded as possible future hardening.

**Responses never confirm existence:** another workspace's id, a random id, and a deleted id all return the same 404 body. 404 is decided before 403, so role errors appear only inside your own workspace.

## API additions and scope

- New: `GET/PATCH /api/v1/workspaces/{id}` and `GET /api/v1/workspaces/{id}/members` (SPEC.md §11 updated). They give M2 real cross-workspace read and write targets.
- SPEC.md's M2 checklist also lists isolation tests for sources, conversations, and messages. Those tables arrive in M4 and M7. M2 delivers the mechanism and the two-tenant fixtures; each later milestone adds isolation tests for its own tables. Delete-isolation is proven in M2 at the session layer (bulk DELETE across workspaces affects 0 rows). HTTP-level DELETE arrives with `DELETE /sources/{id}` in M4.
- Errors share one shape, `{"error": {"code", "message", "details"?}}`. Validation details carry only `loc`, `msg`, and `type`; FastAPI's default would echo submitted values, passwords included. Request bodies reject unknown fields, which blocks mass assignment of fields like `role`.
- The engine uses `hide_parameters=True`, so bound values (password hashes) never appear in exception text or logs.

## Known limitations

- Registration returns `409` for a taken email, which reveals that the account exists. Avoiding that needs email verification, which is out of scope. Mitigated by the per-IP registration limit.
- PostgreSQL's own unique-violation DETAIL can name the conflicting email in an unexpected error's log line. Bound parameters are hidden.
- Behind Docker Desktop's port forwarding, all browser traffic shares one peer address, so the per-IP limits act globally on a local machine.
- No password reset, email verification, refresh tokens, MFA, or invitations (memberships beyond the owner are only created in tests).
- **Fixed windows are clock-aligned** (every 15 minutes). Attempts just before and just after a rollover land in different windows, so up to 2 × limit (10) can be verified within moments. `test_fixed_window_rollover_grants_a_fresh_allowance` records this behaviour. The sustained rate is still 5 per 15 minutes. A strict "at most 5 in any rolling 15 minutes" would need a sliding-log limiter (one row per attempt, with a per-key lock), which means a schema change.

## Amendment (4 November 2026, ADR 0010)

"Exactly two places" no longer holds: the job queue's cross-workspace claim and recovery
statements (`app/worker/queue.py`) also opt out with `SKIP_WORKSPACE_SCOPE`, each filtering by
the job's own workspace explicitly. The bypass is still per statement and greppable.
