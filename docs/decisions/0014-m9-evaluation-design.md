# 0014 — Milestone 9: evaluation design

- Status: Accepted (implementation decisions within the owner's M9 brief, 4 November 2026).
  Changes SPEC.md §5 (dataset location), §6.1 (evaluation tables), §11 (evaluation endpoints), and
  §13 (metric definitions) (version 1.6).
- Date: 4 November 2026

## Context

M9 must show, with reproducible measurements, how well Knowbase's retrieval, citations, and
abstention work, and compare vector-only retrieval with hybrid retrieval. ADR 0009 kept the
lexical query as all-words AND (`websearch_to_tsquery('simple', …)`) and asked M9 to measure
AND against OR and against the `english` configuration before any change. Gemini is used on the
free tier only, and SPEC.md §6.1 sketches three evaluation tables.

## Decision

### Dataset

1. **Versioned file, inside the backend package:** `backend/app/evaluation/datasets/
   northstar-demo-v1.json`. The backend image is built from `backend/` only, so a file under
   `sample_data/` (SPEC.md §5's sketch) wouldn't reach the API or the worker.
2. **Stable source keys:** the demo file names (`returns-policy.md`, …), with each file's
   SHA-256. A workspace source counts as a dataset source only if its file name *and* content
   hash match, so re-indexing never changes the keys, and an edited file can't pass for the
   original.
3. **Checked against the corpus by tests:** every reference fact carries an `evidence` quote that
   must appear word for word in the named demo file. Every unanswerable question lists terms
   that must not appear anywhere in the corpus. Follow-up questions carry the previous question.
4. **Immutable versions:** questions are loaded into `evaluation_questions` from the file. If the
   file's content changes without a version bump, loading refuses to start a run.

### What a run measures

5. **One run evaluates every retrieval configuration on the same corpus at the same time,** so
   comparisons inside a run are fair by construction:
   - `vector`: production vector-only;
   - `hybrid`: production hybrid (vector + `simple` AND lexical + RRF);
   - `hybrid_or`, `hybrid_english`, `hybrid_english_or`: **experimental** lexical variants,
     used only by the evaluation. The production default does not change.
6. **One query embedding per question,** shared by all configurations (they differ only after
   the embedding). The lexical experiments cost no Gemini calls.
7. **Answer evaluation is opt-in per production mode** (`answer_modes`: none, `hybrid`, or
   `vector` and `hybrid`), at one generation per question per mode. A mode that wasn't
   answered has no answer metrics. They are shown as not measured, never as zero. Experimental
   variants are retrieval-only.
8. **The production components are reused:** the same retrieval code (refactored so that the
   embedding and the ranking are separate steps), the same context builder, prompt,
   generation provider, citation validation, and abstention rules (the answer step is factored
   out of `ChatService.respond`). Nothing is saved to conversations. For a follow-up question,
   the history holds the previous user question only, with no generated earlier answer.

### Metrics (source level)

9. **Retrieval** is judged at source level. The top `RETRIEVAL_TOP_N` (5) chunks, which are the
   chunks chat would use, are collapsed to sources by best rank, so duplicate chunks can't
   inflate a score. Over answerable questions whose retrieval succeeded:
   - HitRate@5 = questions with at least one expected source in the top 5, divided by those
     questions;
   - Recall@5 = the mean, per question, of expected sources retrieved divided by expected
     sources;
   - MRR = the mean of 1 / (source rank of the first expected source), with 0 when none is
     retrieved.

   Unanswerable questions have no relevant sources. Their retrieval metrics are null, and
   their retrieval is reported separately (nearest distance, lexical candidates).
10. **Chance baseline:** each run reports the HitRate@5 a random ranking of the workspace's
    chunks would get (hypergeometric over the chunk counts). With 10 demo chunks and a top 5,
    chance is about 0.78, so HitRate@5 and Recall@5 are close to saturated on this corpus, and
    MRR is the more informative comparison.
11. **Citations** (source level, answered answerable questions only): precision = cited
    expected sources / cited sources, and recall = cited expected sources / expected sources.
    Both are null for abstentions, provider failures, and unanswerable questions. An answer
    always has at least one citation, since an uncited answer is withheld as
    `no_valid_citation`.
12. **Abstention accuracy** = (answerable questions answered + unanswerable questions
    abstained) / questions with an outcome. Provider failures are their own outcome
    (`provider_error`), never an abstention, and are left out of the denominator and counted
    separately. A confusion matrix is kept per mode.
13. **Latency, measured in process:** query embedding; search per configuration; end to end per
    answer mode (embedding, search, context, generation, and validation; no HTTP and no
    message save). Min, median, and p95 (nearest rank).
14. **Not measured:** whether each claim is entailed by its citation. Source-level citation
    relevance is not claim verification, and there is no LLM judge.

### Execution

15. **Durable, in the existing worker:** `evaluation_runs` rows are a second queue, claimed with
    `FOR UPDATE SKIP LOCKED`, a fencing token, and a lease renewed by a heartbeat, the same
    pattern as `ingestion_jobs` (ADR 0006, 0010). The worker processes **one question per
    claim**, commits that question's results, and puts the run back with a short pause
    (`EVALUATION_QUESTION_INTERVAL_SECONDS`). Ingestion jobs are served between questions, the
    pause paces Gemini requests, and a crash loses at most one question, which is then redone.
    No Redis, Celery, or in-process background task.
16. **Budgets are reserved up front, all or nothing:** a run takes its query embeddings from
    `DAILY_QUERY_EMBEDDING_LIMIT` (installation-wide), its generations from a new
    installation-wide `DAILY_EVALUATION_GENERATION_LIMIT`, and one slot from a per-workspace
    `DAILY_EVALUATION_RUN_LIMIT`. Unused slots are given back when the run ends. Only one run is
    active per workspace (a partial unique index).
17. **Failure handling:** a quota or rate-limit refusal (owner decision, 4 November 2026), a
    missing key, or a rejected key stops the run (`failed`, with its partial results kept):
    pushing on would only consume quota trying to force completion. Another provider failure
    (5xx, timeout, invalid response) is recorded on that question's results, and the run
    continues. If the corpus changes during a run (an upload, re-index, or delete), the run
    stops: its results would mix two corpora.
18. **Workspace isolation:** runs and results belong to a workspace (composite foreign keys, the
    tenancy guard, and explicit filters). Questions are a shared, read-only dataset. Any member
    of a workspace can see and start that workspace's runs.

## Consequences

- Results are reproducible against a named dataset version, a recorded corpus snapshot (source
  keys, hashes, chunk counts, other sources), and a recorded configuration (models, top-n,
  candidates, RRF k, lexical variants, git revision when known).
- Gemini is non-deterministic, so answer metrics can vary between runs, and each run records
  its own outputs. Retrieval metrics are deterministic for a fixed corpus and query embedding.
- The small corpus limits what any metric can show. The chance baseline makes that visible.
- `GET /analytics/overview` stays deferred (ADR 0013).

## Amendment (M9 review fixes, 5 November 2026)

An independent review found two gaps, both fixed without a migration (the run's `config.budget` JSON
and each result's `details` JSON):

- **Retried questions and the budget (decision 16):** a question redone after an interrupted attempt
  (a worker crash or a lost lease) may repeat Gemini calls the interrupted attempt already made, and
  the summary counts only saved results.
  - A retry now takes an **additional retry reservation**: one query embedding and one generation per
    answer mode, all or nothing, in the current UTC day, taken before any call. It is never given back.
    If it can't be taken, the run stops with its completed results and makes no call.
  - Terminal claims (attempt limit, corpus change) reserve nothing. When a run ends on a question that
    never saved, one attempt's worth of the original reservation is held back instead of returned.
  - The counters can over-count (an interrupted attempt that never reached Gemini), never under-count.
- **Failed-attempt latency (decision 13):** a failed generation or query embedding stores its time
  until the error in the result's details. Summaries report successful-call latency and
  failed-attempt latency as separate statistics; runs from before the fix show the latter as not
  measured.

The 4 November run is unaffected by the first gap: the worker log shows single attempts and no
retried questions.
