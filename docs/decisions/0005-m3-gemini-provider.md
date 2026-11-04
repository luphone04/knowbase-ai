# 0005 — Milestone 3 Gemini provider design

- Status: Accepted (within SPEC.md §2, §8, §17.8; no architecture change)
- Date: 2 November 2026

## Shape

- **Interfaces, not SDK calls, in application code.** `app/providers/interfaces.py` defines `GenerationProvider` and `EmbeddingProvider` with provider-neutral results (`GenerationResult`, `EmbeddingResult`, `TokenUsage`). `app/providers/gemini.py` implements them. `app/providers/factory.py` is the only place that builds them. Ingestion (M4) and chat (M7) will depend on the interfaces only.
- **Errors are categories, not SDK exceptions.** Each `ProviderError` subclass carries a user-safe message plus diagnostic fields (HTTP code, provider status, quota ids). Gemini's own message text is never shown to users.
- **Mocks live in `tests/fakes/`.** `FakeGeminiClient` returns real `google-genai` response and error objects, so the real providers run unmodified in tests. `MockGenerationProvider` / `MockEmbeddingProvider` serve later milestones' tests. Nothing in `app/` can switch to a mock, so a mock can never pass for the live integration (SPEC.md §2.4).

## Generation

- The configured model, `GEMINI_THINKING_LEVEL`, and `GEMINI_MAX_OUTPUT_TOKENS` (1200, thinking included). The request sets no sampling parameters, tools, or grounding.
- **Complete or nothing.** Any finish reason other than `STOP`, an empty answer, missing candidates, or a blocked prompt raises `IncompleteGeneration`, with token usage kept for observability. Partial text is never returned.
- Thought parts are excluded from the answer text.
- Automatic function calling is disabled explicitly, so each call is exactly one request.

## Embeddings

- `gemini-embedding-001`, `output_dimensionality=768`. `RETRIEVAL_DOCUMENT` for content, `RETRIEVAL_QUERY` for questions. The model embeds the two asymmetrically, so they must not be swapped.
- Batches of `GEMINI_EMBEDDING_BATCH_SIZE` (8) per request, one list per call.
- **Every response is checked.** The response must contain exactly one embedding per input; an aggregated single vector (gemini-embedding-2 style) is refused. Each vector must have exactly 768 finite, numeric, non-zero values. It is then L2-normalised and re-checked for unit length.
- **Why normalise:** gemini-embedding-001 is unit-length only at 3072 dimensions. At 768 the raw norm measured about 0.59. With unit vectors, cosine similarity equals the dot product, and every stored vector is comparable.
- Results carry model, dimensions, and task type, so M4 can store them with the vector and refuse to mix models or sizes.
- Persistence, caching, and deduplication by content hash are M4.

## Reliability and free-tier protection

- **Gemini Developer API only.** `genai.Client(api_key=..., vertexai=False)`, even if `GOOGLE_GENAI_USE_VERTEXAI` is set. The key comes from `GEMINI_API_KEY` in settings, never from the SDK's own environment lookup.
- **One retry layer.** The SDK runs with `HttpRetryOptions(attempts=1)`; `app/providers/retry.py` owns retries, so they can't multiply.
- **Bounded exponential backoff with jitter** (default 3 retries, 1 s base, 20 s cap), for 429 per-minute rate limits, 500/502/503, and connection failures. A provider `RetryInfo` delay is honoured if it is under the cap; otherwise the call fails immediately rather than blocking a request.
- **Not retried:** 400/401/403/404, failed preconditions, an exhausted **daily** quota (a `…PerDay…` quota id, which resets at midnight Pacific), and **deadline exceeded** (504 or read timeout).
- **Why deadline-exceeded isn't retried (live finding).** The SDK sends the client timeout to Gemini as `X-Server-Timeout`. With a 30 s timeout, gemini-3.8-flash returned 504 `DEADLINE_EXCEEDED` on 8 of 8 attempts. The same request without a deadline succeeded in 50.7 s, so each retry only restarted the same wait. Google's API-errors reference says for 504: "Remove or increase client deadline setting to use the server default." The default timeout is therefore **90 s**, and a deadline miss is a `ProviderTimeout`, not a retry.
- **No paid fallback, by construction.** No other model, tier, batch API, Vertex AI, grounding, or File Search exists in the code path. Tests assert that only `models.generate_content` / `models.embed_content` are touched, always with the configured model, on every failure type.
- **Observability without leakage.** Logs record model, finish reason, token counts, latency, attempts, error category, HTTP status, and quota ids, never prompts, texts, vectors, or keys. A `RedactingFormatter` masks the key and app secrets in all log output, tracebacks and third-party loggers included.

## Status endpoint

`GET /api/v1/ai/status` requires authentication. It returns `configured` | `missing_key` | `quota_error`, non-secret model settings, `paid_fallback: false`, and the last provider outcome this API process saw (category and time only). It never calls Gemini, so the dashboard costs no quota.

## Deferred (with reasons)

- **Daily guardrails from SPEC.md §2.5:** 30 chat requests per user, 300 embedded texts per day. They count real usage, so they belong where usage happens: embeddings in M4 (after deduplication, so cached texts don't count) and chat in M7. The PostgreSQL limiter from M2 supports daily windows since its per-rule pruning fix.
- **Overall retry time budget:** the worst case is about 90 s for a timeout, or up to 4 attempts for overload errors (seen live: 41.6 s with 3 × 503). Chat (M7) may want a lower retry count than background ingestion; both are configurable.

## Amendment (4 November 2026, ADR 0010)

- "Exactly one request" was not true: google-genai 2.x also follows continuation tokens
  automatically. That is now disabled (`automatic_continuation=False`), which makes the claim hold.
- "About 90 s" was the per-attempt time. One call could take about 6 minutes (4 attempts plus
  backoff). `GEMINI_CALL_DEADLINE_SECONDS` (120) now bounds it: no retry starts after the
  deadline.
- 408 and 499 are retried; unreadable responses are `InvalidProviderResponse`.

## Amendment (4 November 2026, ADR 0012)

The generation model is now `gemini-3.5-flash-lite`. Generation retries are configured separately:
`GEMINI_GENERATION_MAX_RETRIES`, default 0, one attempt. The policy above (3 retries) still
applies to embeddings through `GEMINI_MAX_RETRIES`. Classification, backoff, and the deadline are
unchanged.
