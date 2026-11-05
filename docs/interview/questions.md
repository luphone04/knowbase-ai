# Questions an interviewer may ask (with answers from the implementation)

## Retrieval

**Why RRF and not a weighted sum of scores?** Cosine distance and `ts_rank_cd` live on unrelated scales,
and `ts_rank_cd` isn't normalised across queries. RRF uses only ranks:
`score = Σ 1/(k + rank)` with k = 60. It is deterministic: ties are broken by chunk id
(`app/retrieval/fusion.py`).

**Is your keyword search BM25?** No. It is PostgreSQL full-text search: a generated `tsvector`
(`simple` configuration), `websearch_to_tsquery`, ranked by `ts_rank_cd`.

**Did hybrid retrieval help?** Not on this corpus. Production hybrid produced exactly the vector
ranking for every question, because the all-words query matched no chunk for 18 of 20 natural
questions. OR matching raised MRR from 0.917 to 1.00, but only through 4 questions on 10 chunks, and OR
matches stop-words. I kept the default (ADR 0009) and recorded `english` OR as the candidate for a
larger test.

**How do you handle follow-up questions?** No extra LLM call. A short question, or one with a
reference word ("it", "what about"…), is retrieved together with the previous user question. The
answer prompt keeps the original question and a bounded history (`app/retrieval/followup.py`).

**How do you search only one tenant's data?** Twice: the ORM session is bound to the caller's
workspace and fails closed when it isn't, and every search also filters `workspace_id` explicitly.
Composite foreign keys stop cross-workspace references in the database itself.

## Grounding and safety

**How do you know an answer is grounded?** The server numbers the excerpts it sends (S1…Sn). After
generation it keeps only those ids, removes URLs that aren't in an excerpt, and withholds any answer
left without a valid citation. That proves the cited documents were retrieved and relevant at
document level; it does not prove each sentence. The evaluation measures citation precision and
recall per document, and I reviewed the answers by hand.

**What about prompt injection in uploaded documents?** Excerpts, titles, file names, and history are
escaped inside their own elements, and the system instruction says excerpts are evidence, not
instructions. Tests use injection documents to show a forged `S9`, or obeying replies, never survive
validation. The model's behaviour itself can't be guaranteed; the server-side checks can.

**What happens when Gemini fails?** It is an error response (429, 502, or 503), never an answer or an
abstention. The turn isn't saved, the trace records the failure category, and budget slots are given
back when Gemini refused the call outright. In the evaluation, failures are their own outcome
(`provider_error`), excluded from accuracy, and quota or rate-limit refusals stop the run.

## Evaluation

**Why source level, not chunk level?** Expected sources are stable keys (file names, checked by
SHA-256). Chunk ids change on every re-index. Chunks are collapsed to documents by best rank, so
duplicate chunks can't inflate scores.

**Your HitRate@5 is 1.00. Is retrieval perfect?** No. With 10 chunks and a top 5, a random ranking
already scores 0.82; the run reports that baseline. MRR and Recall@5 are where the configurations
differ.

**How are undefined metrics handled?** As null ("Not measured"), never 0. Unanswerable questions have
no retrieval metrics; citation metrics exist only for answered answerable questions; a mode that wasn't
run has no answer metrics.

**Isn't comparing vector and hybrid answers unfair if errors hit different questions?** Yes, and I say
so. The two modes also received identical excerpts in this run, so their answer differences are model
variance. I don't rank them.

**Why not an LLM judge?** It would add cost and its own errors on the free tier, and it would compete
with deterministic metrics. SPEC.md allows one later, labelled as model-judged.

**How is the evaluation reproducible?** A versioned dataset that can't be edited in place. Each run
stores the corpus snapshot (keys, hashes, chunk counts, other documents) and the full configuration
(models, top-n, candidates, RRF k, variants). Retrieval is deterministic for fixed embeddings; answers
are not, so each run keeps its own outputs.

## Engineering

**Why PostgreSQL as the queue?** One dependency fewer, transactional with the data it changes, and
durable. `FOR UPDATE SKIP LOCKED` gives exclusive claims; a fencing token on every write stops a
stalled worker from overwriting its successor; a lease and heartbeat recover crashed jobs.

**Why does an evaluation run in the worker, one question per claim?** A 26-question run with Gemini
takes about 20 minutes. Holding an HTTP request or an in-process task for that long is fragile. One
question per claim lets ingestion jobs run between questions, paces Gemini requests, and limits a
crash to redoing one question.

**How do you stop the free tier from being exhausted?** Daily budgets in PostgreSQL counters, reserved
atomically before the call and given back when Gemini refuses it. An evaluation reserves its whole
budget up front, all or nothing. There are no automatic generation retries, and no paid fallback.

**What would you do next?** Test `english` OR keyword matching on a larger corpus with distractor
documents; add claim-level checks or a labelled model judge as a secondary signal; add streaming; and
calibrate a relevance threshold once there is enough data to separate the distance distributions.
