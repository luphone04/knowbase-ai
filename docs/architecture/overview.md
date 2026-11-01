# Architecture overview

Status: planned design from [SPEC.md](../../SPEC.md) §4. Nothing below is
implemented yet (Milestone 0 sets up the repository only).

Knowbase AI runs locally as four long-running Docker Compose services plus a
one-shot migration job. All services share one PostgreSQL database; the API
and worker also share one upload volume.

```mermaid
flowchart LR
    B["Browser"] --> FE["frontend<br/>Next.js :3000"]
    B -->|"REST + HttpOnly cookie"| API["api<br/>FastAPI :8000"]
    API --> DB[("db<br/>PostgreSQL 18 + pgvector")]
    W["worker<br/>python -m app.worker.runner"] --> DB
    API --> UP[("uploads_data volume")]
    W --> UP
    API -->|"query embeddings,<br/>answer generation"| G["Gemini API<br/>(free tier)"]
    W -->|"document embeddings"| G
    W -->|"bounded crawl"| WEB["Public websites"]
    M["migrate<br/>alembic upgrade head"] --> DB
```

## Service boundaries

| Service | Owns | Does not do |
|---|---|---|
| `frontend` | UI pages, calls to `/api/v1` with `credentials: "include"` | Hold any secret; call Gemini; compute metrics |
| `api` | Auth, workspace scoping, source CRUD, enqueueing jobs, retrieval, chat, citation validation, evaluation endpoints, health | Long crawls or bulk embedding inside a request |
| `worker` | Claims `ingestion_jobs` with `FOR UPDATE SKIP LOCKED`; crawls, parses, cleans, chunks, embeds, and indexes; stale-lock recovery | Serve HTTP |
| `db` | Relational data, `vector(768)` embeddings (HNSW), full-text `tsvector` (GIN), the job queue | Expose a public port (localhost-only if bound) |
| `migrate` | Applies Alembic migrations once, then exits | Run at request time |

## Boundaries that matter for security and cost

- **Gemini key:** read only by `api` and `worker` from the backend `.env`.
  Never in a `NEXT_PUBLIC_*` variable, the browser bundle, or an API response.
- **Tenant isolation:** the API derives `workspace_id` from the authenticated
  user. Every workspace-owned table carries `workspace_id`, and composite
  foreign keys block cross-workspace references.
- **Untrusted inputs:** uploaded files, crawled HTML, and retrieved excerpts
  are data. The crawler enforces SSRF checks; the prompt treats excerpts as
  evidence, not instructions.
- **No paid services:** no Redis/Celery, hosted vector DB, reranker, or
  billing fallback. When Gemini quota runs out, the user sees an error.

## RAG pipeline stages (explicit, no orchestration framework)

1. Ingest: crawl or parse, then clean, chunk, embed (`RETRIEVAL_DOCUMENT`, 768 dims, normalized), and store.
2. Retrieve: vector (`<=>` cosine) and lexical (`websearch_to_tsquery` + `ts_rank_cd`, not BM25).
3. Fuse: Reciprocal Rank Fusion with `k=60`, top 5.
4. Generate: Gemini answer from `[S#]`-keyed excerpts.
5. Validate: drop unknown citation IDs; abstain when there is no valid evidence.
6. Evaluate: compare vector-only and hybrid modes on the seeded dataset.
