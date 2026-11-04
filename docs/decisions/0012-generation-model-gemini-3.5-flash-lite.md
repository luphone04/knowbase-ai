# 0012 — Generation model `gemini-3.5-flash-lite` and separate generation retries

- Status: Accepted (owner decision, 4 November 2026). Changes SPEC.md §2.1, §2.3, §2.4, and §15.2
  (version 1.4). Supersedes the generation model in SPEC.md 1.3 (`gemini-3.8-flash`).
- Date: 4 November 2026

## Context

During Milestone 7's live review (ADR 0011), `gemini-3.8-flash` didn't return one answer on this
project:
- 504 `DEADLINE_EXCEEDED` after 90 s, or 503 `UNAVAILABLE` after 3 retries;
- after 21 attempts across 7 questions, 429 `GenerateRequestsPerDayPerProjectPerModel-FreeTier`;
- after the 08:00 UTC quota reset, the first single attempt again returned 503.

Query and document embeddings (`gemini-embedding-001`) worked throughout.

## Decision

1. **The generation model is `gemini-3.5-flash-lite`** (`GEMINI_CHAT_MODEL`, the default in
   `app/config.py` and `.env.example`). Everything else stays as it is:
   - embeddings: `gemini-embedding-001`, 768 dimensions, task types, normalisation, cache;
   - retrieval, RRF, chunking;
   - thinking level `low`, `max_output_tokens` 1200, and the system instruction.
2. **Generation retries separately from embeddings.** `GEMINI_GENERATION_MAX_RETRIES` (default 0)
   gives one generation attempt per question, with no automatic retry. `GEMINI_MAX_RETRIES`
   (default 3) still applies to document and query embeddings. Error classification,
   backoff, the overall deadline, and the error responses are unchanged.

## Evidence

- **Google documentation** (checked 4 November 2026; `ai.google.dev` redirects to
  `aistudio.google.com/docs`):
  - Models page: model code `gemini-3.5-flash-lite`, Stable, text output, 1,048,576 input and
    65,536 output tokens, thinking supported.
  - Pricing: Free Tier input and output "Free of charge". Free-tier content is used to improve
    Google's products, as for every free-tier model (SPEC.md §2.4).
  - Thinking guide: levels `minimal` (default), `low`, `medium`, `high`.
  - Deprecations: no shutdown date announced; listed as the replacement for
    `gemini-3.1-flash-lite`.
  - Rate limits: per project, shown only in Google AI Studio; not published per model.
- **Our key** (`models.get`): `models/gemini-3.5-flash-lite`, version `3.5-flash-lite-07-2026`,
  with `generateContent` among its supported actions.
- **Controlled test** (1 generation request, retries 0, a synthetic prompt): HTTP 200 in 1.5 s,
  `STOP`, exactly the requested reply.
- **M7 live acceptance re-check** (temporary override, retries 0, temporary account): 5 generation
  requests, all HTTP 201 on the first attempt, 1.5–5.7 s per question.

  | Test | Result |
  |---|---|
  | Grounded answer | Pass |
  | Citation fields match the database | Pass |
  | Opening the citation | Pass |
  | Follow-up uses the previous question | Pass |
  | Abstention on an unsupported question | Pass (`model_insufficient`) |
  | Injection probes (forged citation, fake "free", phishing URL, PWNED, system-prompt request) | Pass: all ignored |

- **Permanent configuration** (one end-to-end chat through the main API):
  - HTTP 201 in 2.15 s;
  - answer "No, laptops cannot be returned after 14 days [S1]…", grounded in the returns
    policy, and the citation matches the database;
  - conversation saved, and the trace linked to the answer.

  Requests: 1 generation, 1 query embedding, 1 document embedding.
- **No billing:** no billing account was enabled, and no new key, account, or project was
  created. The earlier quota id ends in `-FreeTier`.

## Consequences

- **Lighter model:** a smaller, cheaper tier than `gemini-3.8-flash`. Five live answers were
  correct and fully cited, but quality has not been measured; M9's evaluation does that.
- **Any 503, 504, or 429 during generation fails the question at once,** with 503
  `ai_unavailable` or 429 `ai_quota`. The user tries again; nothing is saved as an answer, and the
  trace records the failure. This trades resilience to brief overloads for quota: a failed
  attempt can still count toward the free daily quota.
- **Easy to undo:** `GEMINI_CHAT_MODEL=gemini-3.8-flash` (or another free model) and
  `GEMINI_GENERATION_MAX_RETRIES` are configuration, with no code change.
- **Limits still unknown:** this model's free-tier limits are visible only in AI Studio.
  `DAILY_CHAT_REQUEST_LIMIT` (30 per user) is not checked against them.
- **Status endpoint:** `/ai/status` reports `generation_max_retries` next to `max_retries` (the
  embedding retries).
