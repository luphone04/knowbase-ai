# 0011 — Milestone 7 grounded chat, citations, and abstention

- Status: Accepted (within SPEC.md §6.1, §9.5, §9.6, §10, §11 "Conversations", §17.12; no
  architecture change)
- Date: 4 November 2026

## Flow

```
POST /conversations/{id}/messages {content, mode?}
  -> the caller's own conversation (404 otherwise)
  -> Gemini key configured? (503 before any slot is spent)
  -> read the bounded history and the previous user question, then commit
  -> anything indexed? If so, take a per-user chat slot and an installation-wide
     query-embedding slot
  -> retrieve fresh evidence (M6; a follow-up adds the previous question); commit the trace
  -> excerpts S1..Sn (dedupe, budget); none usable -> abstain (no_evidence), no generation call
  -> one Gemini generation request with the system instruction (no DB connection held)
  -> validate citations and URLs
  -> save the question and the answer or abstention together (201), link the trace
```

## Decisions

- **Schema (migration 0009).** `conversations` reference the owner's membership through
  `(workspace_id, user_id)`, so only a member can own one, and leaving a workspace removes its
  chats. `messages` reference their conversation through `(workspace_id, id)`. `query_traces`
  gain tenant-safe keys to the conversation and the assistant message. Extensions to SPEC.md
  §6.1:
  - `position`: orders messages, because a question and its answer are saved in one
    transaction and so share a timestamp;
  - `outcome`: `answered` or `abstained`;
  - `abstain_reason`: `no_evidence`, `model_insufficient`, or `no_valid_citation`;
  - `metadata`: retrieval mode, trace id, removed ids and URLs.

  The check constraints use `CASE` and `coalesce`, because a NULL check passes in PostgreSQL;
  the first draft did, and the tests caught it.
- **Conversations are per user.** The session filters by workspace (the M2 guard), and every
  conversation query also filters by `user_id`. Another member of the same workspace, another
  workspace, and a random id all get the same 404.
- **Fresh evidence every turn.** The retrieval query is §9.5's: the previous *user* question plus
  the current one for a short or referring question. Earlier answers are never evidence.
- **Context (§9.6):**
  - the top `RETRIEVAL_TOP_N` (5) chunks, in rank order;
  - empty chunks are skipped, and so are near-duplicates from the same document (same
    normalised text, one inside the other, or at least 90% of word 3-grams shared);
  - a shared character budget, `CHAT_CONTEXT_MAX_CHARS` (12,000); the last excerpt is cut at a
    word boundary;
  - only the id, title, file name, pages, and text are sent.
- **Untrusted text is escaped.** Excerpts, titles, file names, history, and the question each sit
  in their own element (`<source id="S1" ...>`, `<message>`, `<question>`). `&`, `<`, `>`, and
  `"` in attributes are escaped, so no document can close its `<source>` element or forge
  another. The system instruction is SPEC.md §10.1's text plus two sentences: that rule, and
  that history is not evidence.
- **History (§10.4):** the last `CHAT_HISTORY_MESSAGES` (6) messages, each cut to 1,500
  characters. Old `[S…]` markers are stripped: they named that turn's sources.
- **Citation validation (§10.2):**
  - a group keeps only ids that were given (`[S1, S9]` becomes `[S1]`), and a group left empty
    is removed;
  - URLs that don't appear verbatim in an excerpt become `[link removed]`;
  - citations are returned apart from the text: id, chunk, document, source, file name, pages,
    and the excerpt (a snapshot, so the message stays inspectable after re-indexing).
- **Abstention (§10.3):** an answer is shown only when at least one valid citation is left.
  Otherwise the reply is the fixed sentence "I couldn't find enough information in this knowledge
  base to answer that." The reason is stored:
  - `no_evidence`: nothing usable was retrieved, and no generation call is made;
  - `model_insufficient`: the model said the excerpts were not enough;
  - `no_valid_citation`: the model answered without a valid citation, so its text is withheld.

  §10.2 allows a corrective retry instead; abstaining costs no extra free-tier request and keeps
  the behaviour deterministic. No similarity threshold is used (§10.3: calibrate one in M9).
- **Provider failures are not answers.** Gemini errors become error responses through the shared
  mapping (`app/api/v1/gemini_calls.py`):
  - 429 `ai_quota`;
  - 503 `ai_unavailable` and `ai_not_configured`;
  - 502 `ai_incomplete` (a finish reason other than STOP, or an empty reply);
  - 502 `ai_error`.

  The turn is not saved. Its trace stays, linked to the conversation, with `generation_error`
  set to the category.
- **Cost guardrails (§2.5):**
  - `DAILY_CHAT_REQUEST_LIMIT` (30) per user per UTC day, plus the installation-wide
    `DAILY_QUERY_EMBEDDING_LIMIT`;
  - an empty workspace takes no slot and makes no Gemini call;
  - a refused query embedding gives both slots back; a refused generation gives the chat slot
    back (the query embedding did count);
  - no connection is held during either Gemini call (tested).

## Live review (4 November 2026): blocked by Gemini, not verified

With a temporary account and real Gemini, all 7 files indexed: 5 demo documents and 2
injection documents. The query embedding succeeded for every question, and retrieval returned 5
excerpts each time. Generation with `gemini-3.8-flash` did not succeed even once:

- 504 `DEADLINE_EXCEEDED` after the 90 s timeout, or 503 `UNAVAILABLE` through 3 retries;
- after 21 generation attempts in total, 429 `GenerateRequestsPerDayPerProjectPerModel-FreeTier`.

The app answered each of the 9 questions with 503 `ai_unavailable` or 429 `ai_quota`. It saved no
message, and recorded the category on each of the 9 traces. This confirms live that an outage or
the quota is never shown as an answer. **A live grounded answer, citation, abstention, and
injection check are still pending** until the quota resets at midnight Pacific (08:00 UTC).

Observation: 5xx attempts appear to count toward the daily free-tier request quota. The quota
ran out after 21 attempts across 7 questions; Gemini doesn't say what the limit is. With
`GEMINI_MAX_RETRIES=3`, one question can use up to 4 requests.
