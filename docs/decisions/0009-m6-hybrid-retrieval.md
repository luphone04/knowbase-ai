# 0009 — Milestone 6 hybrid retrieval and RRF

- Status: Accepted (within SPEC.md §6.1 `query_traces`, §9, §17.11; no architecture change)
- Date: 3 November 2026

## Flow

```
POST /retrieval/search {query, mode, previous_question?}
  -> per-workspace daily limit (before any Gemini call)
  -> contextualize a follow-up (previous question + current, no Gemini call)
  -> Gemini RETRIEVAL_QUERY embedding (1 request, model and dimensions checked)
  -> vector search: top RETRIEVAL_CANDIDATES (10) by cosine distance
  -> hybrid only: full-text search, top 10, then RRF (k = RRF_K, 60)
  -> top RETRIEVAL_TOP_N (5) chunks with ranks, scores, and source metadata
  -> one query_traces row
```

## Decisions

- **Two separate modes.** `vector` returns nearest chunks in distance order.
  `hybrid` fuses the vector and full-text rankings with RRF. They are
  separate code paths, so evaluation (M9) can compare them, and every result
  shows its vector rank and distance and its lexical rank and `ts_rank_cd`.
- **RRF on ranks only:** `score = Σ 1/(k + rank)` over the lists containing
  the chunk. A chunk repeated within a list keeps its best rank. Raw cosine
  distances and `ts_rank_cd` values are never added; their scales are
  unrelated. Exact ties are ordered by chunk id. An extra "best rank"
  tie-break was removed after mutation testing showed it could never change
  an order.
- **Workspace isolation, twice.** Both searches are ORM statements on a
  workspace-bound session, so the M2 tenancy guard filters them, and they
  also filter `workspace_id` explicitly on chunks, documents, and sources.
  An unbound session fails closed (tested). Chunks of a source that is being
  deleted are excluded.
- **One embedding model per index.** Vector search compares only chunks
  whose `embedding_model` matches the configured model.
- **Index-friendly SQL.** Vector search orders by distance alone (the shape
  HNSW serves) and enables `hnsw.iterative_scan = strict_order`, so the
  workspace filter can't silently return fewer than k rows. Exact ties are
  then ordered by chunk id.
- **Lexical search** uses `websearch_to_tsquery('simple', q)` on the
  generated `search_tsv`, ranked by `ts_rank_cd`. This is PostgreSQL
  full-text ranking, **not BM25**. Queries with no word characters return
  nothing; input is always a bound parameter.
- **Follow-ups (§9.5).** A question under 10 words, or one containing a
  reference word (it, that, those, what about, and if, …), is retrieved as
  "previous question + current question". Only one previous question is
  used, and the query is capped at 2,000 characters. The original question
  is kept for the answer prompt (M7).
- **Traces.** Every search writes `query_traces`, following SPEC.md §6.1:
  the mode, the query actually used, the retrieved ids, every candidate's
  rank and raw score, the fused scores and settings, latency, and token
  usage. `conversation_id` and `message_id` are nullable without foreign keys
  until M7 creates the conversation tables. Traces cascade with their
  workspace.
- **Cost guardrail (an addition to §2.5).** Each search embeds the query with
  one Gemini request. Searches are limited per workspace per UTC day
  (`DAILY_RETRIEVAL_REQUEST_LIMIT`, default 100), reserved before Gemini is
  called. Query embeddings don't count against the document budget
  (`DAILY_EMBEDDING_TEXT_LIMIT`); M7 sets the chat limits.
- **Provider errors are explicit:** no key → 503 `ai_not_configured`; quota
  or rate limit → 429 `ai_quota`; unavailable or timeout → 503
  `ai_unavailable`; a mismatched embedding → 502 `ai_error`. There is no
  fallback.
- **Demo corpus.** `sample_data/demo_store/` holds five fictional Northstar
  Outfitters documents (§1.3, §5).

## Query plans (demo corpus, 18 chunks)

- **Both searches** filter by the `(workspace_id, document_id)` B-tree index
  and then sort, which gives an exact nearest-neighbour search within the
  workspace. That is the best plan for small per-workspace corpora.
- **With sequential scans disabled,** to check the indexes:
  - `ORDER BY embedding <=> q LIMIT 10` uses `ix_chunks_embedding_hnsw`;
  - `search_tsv @@ tsquery` uses a bitmap scan on `ix_chunks_search_tsv`
    (GIN).

## Known limitation (decided: keep AND)

`websearch_to_tsquery` ANDs every word, and the `simple` configuration keeps
stop-words. So a natural question rarely matches lexically: "Can I return a
laptop after 20 days?" becomes `'can' & 'i' & 'return' & 'a' & 'laptop' &
'after' & '20' & 'days'`, which matches 0 chunks. In the live review, the
lexical list contributed to 2 of 7 questions: those with exact terms such as
"NorthCare Plus" and "gift cards expire". Hybrid still never ranked worse
than vector, but for most natural questions it equals vector.

**Options for the owner** (SPEC.md §9.3 currently mandates the current
query):
1. OR the terms (for example, `to_tsquery` built from the words joined with
   `|`) and let `ts_rank_cd` rank them;
2. use the `english` configuration (stop-words and stemming) for the query
   and the generated column, which would need a migration;
3. leave it as it is, and let M9's evaluation measure it.

## Amendment (4 November 2026, ADR 0010)

- The demo corpus is **10 chunks**; the "18 chunks" above also counted another uploaded file.
- The iterative-scan guarantee is now tested through the HNSW index. A leading dash is no longer
  negation. Exact ties at the cut-off are settled by chunk id.
- The cost guardrail changed:
  - searches also count against an installation-wide `DAILY_QUERY_EMBEDDING_LIMIT` (500);
  - no slot is spent without a key, for an empty workspace, or when Gemini refused the call;
  - no database connection is held during the query embedding;
  - the Retriever no longer commits.
- The keyword AND/OR decision above is still open.

## Owner decision (4 November 2026, with `APPROVE MILESTONE 6`)

Option 3: keyword search keeps `websearch_to_tsquery` (every word required), unchanged, as
SPEC.md §9.3 specifies. The low lexical recall for natural questions is a known limitation. M9's
evaluation measures AND against OR and against the `english` configuration before any change.
