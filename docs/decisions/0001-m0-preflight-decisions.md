# 0001 — Milestone 0 preflight decisions

- Status: Accepted by the project owner, 1 November 2026
- Context: [Milestone 0 compatibility report](../m00-compatibility-report.md)

| ID | Finding | Decision | Applied in |
|---|---|---|---|
| D1 / C1 | `max_output_tokens` includes thinking tokens. `gemini-3.8-flash` defaults to `medium` thinking. Measured live: 191 thinking tokens on a one-word reply at the default level, none at `low` | Add a configurable `GEMINI_THINKING_LEVEL`, default `low`, passed through `ThinkingConfig(thinking_level=...)` | SPEC.md §2.1, §2.3, §15.2, M3 checklist; `.env.example`. Implemented in M3 |
| D2 | `origin` = `luphone04/knowbase-ai` | Confirmed as the owner's **private** repository. Push `main` and `chore/m00-project-setup`. Owner opens the PR manually; nothing is merged by Claude Code | PROJECT_STATUS.md |
| D3 / C7 | Spec said Node 22 (maintenance LTS, EOL 2027-04-30); host has Node 24 | Use **Node 24 LTS** (maintenance LTS since 2026-10-20, EOL 2028-04-30) | SPEC.md §3.1; frontend image and CI from M1/M8 |
| D4 | Python dependency pinning tool was open | Use **`uv`** with a committed `uv.lock` | SPEC.md §3.1; backend from M1 |
| C2 | Spec v1.1 said `gemini-3.6-flash` retires 19 Nov 2026. The Gemini API deprecations page shows no shutdown date | Correct the statement. Keep `gemini-3.8-flash` | SPEC.md §2.1, §20 ref 4 |
| C3 | `gemini-embedding-001` earliest shutdown 14 May 2028; replacement `gemini-embedding-2` lacks `task_type` and aggregates batch inputs | Keep `gemini-embedding-001` (768 dims, normalized) for the MVP | SPEC.md §2.1 |

## Consequences

- The settings class (M1/M3) must expose `gemini_thinking_level`, validated
  against `low | medium | high`.
- `uv` is not installed on the development machine yet; M1 needs it
  (`brew install uv`).
- Changing the embedding model later still requires a full re-index
  (spec §2.2).
