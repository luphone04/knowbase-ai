# Knowbase AI: a 5–7 minute demonstration

For a technical interview. Everything shown exists in the repository and was run on 4 November 2026.
No demo video has been recorded.

**Before you start:** run the stack (`docker compose up --build -d`) and sign in to a workspace that
holds the five demo documents and the evaluation run. Have the chat page and
`docs/architecture/overview.md` open in tabs. Each chat question costs one query embedding and one
Gemini answer; keep to two or three.

## 1. The problem (30 s)

> "Teams want to ask questions of their own documents. A general chatbot answers fluently whether or not
> it knows. I built Knowbase so that answers come only from the uploaded documents, every claim is
> cited, the server validates those citations, and the system says 'not found' when the documents
> don't support an answer. And because 'it seems to work' isn't evidence, I built an evaluation that
> measures it."

## 2. Architecture (1 min)

Open the "whole system" diagram in `docs/architecture/overview.md`.

- A Next.js frontend talks to a FastAPI API with an HttpOnly cookie. It holds no secrets.
- PostgreSQL does everything stateful: users and workspaces, pgvector embeddings (HNSW), full-text
  search (GIN), and **the job queue**. There is no Redis or Celery: workers claim rows with `FOR UPDATE
  SKIP LOCKED`, each claim carries a fencing token, and a heartbeat renews the lease.
- Every workspace-owned query passes through a session guard that fails closed, plus explicit filters
  and composite foreign keys: tenant isolation is enforced twice.
- Gemini's free tier provides embeddings and generation. Daily budgets are reserved before each call,
  and there is no paid fallback.

## 3. Document ingestion (1 min)

On **Documents**, upload a file (or show a ready one and its detail page).

- The upload is validated (type, signature, size), stored under a UUID, and queued. The API answers
  202 straight away; the worker does the slow work.
- The worker parses in a child process with time and memory limits, because uploads are untrusted. It
  cleans and chunks paragraph-aware, embeds only chunks it hasn't seen, and swaps in the new chunks in
  one transaction.
- Re-uploading the same file does nothing: it is recognised by its hash.

## 4. Retrieval and RRF (1 min)

- The question is embedded once. Vector search and PostgreSQL full-text search each return 10
  candidates.
- Reciprocal Rank Fusion combines **ranks**, 1/(60 + rank) summed across the lists, never raw scores.
  Cosine distance and `ts_rank_cd` have unrelated scales. Full-text ranking is `ts_rank_cd`, not BM25.
- A follow-up like "What about express?" is retrieved together with the previous question, with no
  extra LLM call.

## 5. Grounded chat and citations (1 min)

In **Chat**, ask "I bought a laptop 20 days ago. Can I return it?"

- The answer cites `[S1]`; open it to show the stored excerpt.
- Explain the guardrails: excerpts are escaped inside `<source>` elements and declared untrusted.
  Citation ids the server didn't assign are removed. An answer with no valid citation is never shown.
- Ask something the documents don't cover to show the abstention card. If Gemini fails, the user sees
  an error, never a fake answer.

## 6. Evaluation results (1.5 min)

Open **Evaluation** and the 4 November run.

- 26 versioned questions with stable source keys. Every reference fact's quote is checked against the
  files by a test, and the unanswerable questions are checked to have no supporting text.
- One run compares five retrieval configurations on the same corpus and query embedding: the two
  production modes plus three keyword experiments.
- Results, read honestly:
  - HitRate@5 is 1.00 everywhere, but a random ranking would get 0.82 on 10 chunks, so it tells us
    nothing;
  - MRR: vector-only and production hybrid both 0.917, because the all-words keyword query matched
    nothing for 18 of 20 questions;
  - OR matching reached 1.00, but the evidence is 4 questions and OR matches stop-words, so I didn't
    change the default.
- Answers: every decided question was decided correctly (37 answers, 11 abstentions); citation
  precision 0.97. Four Gemini 503/504 errors were recorded as provider errors, not abstentions.
- Open q06 ("What is an RMA number…"): vector ranks the right document 3rd, and AND finds no keyword
  match even for "RMA", because "what", "is", and "an" must all match as well.

## 7. Trade-offs and limitations (1 min)

- The corpus is far too small for strong conclusions. The next step is a larger corpus with distractor
  documents, where `english` OR matching is the candidate to test.
- There is no relevance threshold: in this run, distances of answerable and unanswerable questions
  overlap, so abstention relies on the model plus the citation check.
- Citations are verified per document, not per claim, and some answers omit details.
- Free-tier latency: median 15 s per answer during a busy period, with no streaming.
- Deliberately not built: OCR, URL ingestion (removed with SSRF in mind), streaming, analytics, MFA.
