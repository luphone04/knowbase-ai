"""Retrieval diagnostics for passage-level evaluations (Experiment 004).

Why is verified evidence missing from the top 5? The production-equivalent results
of a run (ranking, columns, metrics) are computed exactly as before; diagnostics
are extra, separate, read-only searches and analyses stored next to them, in the
result's `details["diagnostics"]`. Nothing here feeds back into those results.

Per question and retrieval configuration:

- `vector` / `lexical`: diagnostic candidate lists to DEPTH (20) chunks, from
  separate searches with the run's query embedding (never stored) and query text:
  chunk id, rank, score, source id, and the chunk's length.
- `original`: hypothetical top-K (5, 7, 10) from the run's **own** candidate lists
  (the production top 10 vector and keyword candidates), fused exactly as
  production fuses them. K=5 is the production result (`matches_production`).
- `pool20`: the same, but fusion **recomputed** from the depth-20 lists: a larger
  candidate pool, not just a larger cut-off. The two must not be confused.

For each K: where each evidence passage ranks (per quotation and per expected
source), hit and recall at K, and whether the evidence survives context
building: the real `build_context`, within the chat context limit
(CHAT_CONTEXT_MAX_CHARS), which also skips near-duplicates and cuts the last
excerpt that doesn't fit.

Follow-up questions also get `follow_up_alone`: the same analyses for the
follow-up question on its own (one extra query embedding), kept apart from the
production-equivalent scoring, which always uses the joined query.
"""

from collections.abc import Mapping, Sequence
from typing import Any

from app.chat.context import build_context
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.search import SearchHit
from app.retrieval.service import RetrievedChunk

DEPTH = 20
TOP_KS = (5, 7, 10)


def candidates(hits: Sequence[SearchHit]) -> list[dict[str, Any]]:
    """A ranked candidate list as stored: no text, no vectors."""
    return [
        {
            "chunk_id": str(hit.chunk_id),
            "rank": hit.rank,
            "score": hit.score,
            "source_id": str(hit.source_id),
            "chars": len(hit.content),
        }
        for hit in hits
    ]


def ranked(
    mode: str,
    vector: Sequence[SearchHit],
    lexical: Sequence[SearchHit],
    *,
    k: int,
    rrf_k: int,
) -> list[RetrievedChunk]:
    """Top `k` exactly as production ranks these candidate lists (Retriever.rank)."""
    if mode == "vector":
        return [
            RetrievedChunk(
                rank=hit.rank,
                score=hit.score,
                vector_rank=hit.rank,
                vector_distance=hit.score,
                lexical_rank=None,
                lexical_score=None,
                hit=hit,
            )
            for hit in vector[:k]
        ]
    by_id = {hit.chunk_id: hit for hit in lexical}
    by_id.update({hit.chunk_id: hit for hit in vector})
    vector_by_id = {hit.chunk_id: hit for hit in vector}
    lexical_by_id = {hit.chunk_id: hit for hit in lexical}
    fused = reciprocal_rank_fusion(
        {"vector": [h.chunk_id for h in vector], "lexical": [h.chunk_id for h in lexical]},
        k=rrf_k,
        limit=k,
    )
    return [
        RetrievedChunk(
            rank=item.rank,
            score=item.score,
            vector_rank=item.source_ranks.get("vector"),
            vector_distance=vector_by_id[item.chunk_id].score
            if item.chunk_id in vector_by_id
            else None,
            lexical_rank=item.source_ranks.get("lexical"),
            lexical_score=lexical_by_id[item.chunk_id].score
            if item.chunk_id in lexical_by_id
            else None,
            hit=by_id[item.chunk_id],
        )
        for item in fused
    ]


def coverage(
    chunk_ids: Sequence[str], evidence: Sequence[Mapping[str, Any]] | None
) -> dict[str, Any] | None:
    """Where each evidence passage sits in `chunk_ids` (best first); None without mapping.

    `evidence`: the run's stored mapping items (source_key, status, chunk_ids). An
    unmapped passage has rank None and `mapped: false`: it is never guessed.
    """
    if not evidence:
        return None
    position = {cid: rank for rank, cid in enumerate(chunk_ids, start=1)}
    quotations = []
    for index, item in enumerate(evidence, start=1):
        ranks = sorted(position[c] for c in item.get("chunk_ids", []) if c in position)
        quotations.append(
            {
                "evidence": index,
                "source_key": item.get("source_key"),
                "mapped": item.get("status") == "matched",
                "rank": ranks[0] if ranks else None,
            }
        )
    sources: dict[str, bool] = {}
    for q in quotations:
        key = str(q["source_key"])
        sources[key] = sources.get(key, False) or q["rank"] is not None
    mapped = [q for q in quotations if q["mapped"]]
    found: list[int] = [int(q["rank"]) for q in mapped if q["rank"] is not None]
    complete = len(mapped) == len(quotations)
    return {
        "hit": bool(found) if complete else None,
        "recall": len(found) / len(mapped) if complete else None,
        "first_rank": min(found, default=None) if complete else None,
        "by_source": sources,
        "by_quotation": quotations,
    }


def analyse(
    mode: str,
    vector: Sequence[SearchHit],
    lexical: Sequence[SearchHit],
    *,
    evidence: Sequence[Mapping[str, Any]] | None,
    rrf_k: int,
    max_chars: int,
) -> dict[str, Any]:
    """Hypothetical top-K results for these candidate lists, with context fit."""
    relevant = {c for item in evidence or [] for c in item.get("chunk_ids", [])}
    result: dict[str, Any] = {}
    for k in TOP_KS:
        top = ranked(mode, vector, lexical, k=k, rrf_k=rrf_k)
        chunk_ids = [str(r.hit.chunk_id) for r in top]
        excerpts = build_context(top, max_chars=max_chars)
        in_context = [str(e.chunk_id) for e in excerpts]
        result[str(k)] = {
            "chunk_ids": chunk_ids,
            "context": {
                "excerpts": len(excerpts),
                "chars": sum(len(e.text) for e in excerpts),
                "truncated": [str(e.chunk_id) for e in excerpts if e.truncated],
                "evidence_in_context": bool(relevant & set(in_context)) if relevant else None,
                # Evidence retrieved in the top K but dropped or cut by context building.
                "evidence_dropped": sorted((relevant & set(chunk_ids)) - set(in_context)),
                "evidence_cut": sorted(
                    relevant & {str(e.chunk_id) for e in excerpts if e.truncated}
                ),
            },
            "evidence": coverage(chunk_ids, evidence),
        }
    return result


# --- run summary ------------------------------------------------------------------------


def summarize_diagnostics(rows: Sequence[tuple[str, str, Mapping[str, Any]]]) -> dict[str, Any]:
    """Hit rate, recall, and evidence-in-context rate per configuration, pool, and K.

    `rows`: (variant, question key, details["diagnostics"]) for answerable questions.
    Questions whose evidence isn't fully mapped are left out (counted apart).
    """
    out: dict[str, Any] = {}
    for variant, _key, diagnostics in rows:
        for pool in ("original", "pool20"):
            for k, analysis in (diagnostics.get(pool) or {}).items():
                bucket = (
                    out.setdefault(variant, {})
                    .setdefault(pool, {})
                    .setdefault(
                        k,
                        {"questions": 0, "unmapped": 0, "hits": 0, "recall": 0.0, "in_context": 0},
                    )
                )
                evidence = analysis.get("evidence")
                if not evidence or evidence["hit"] is None:
                    bucket["unmapped"] += 1
                    continue
                bucket["questions"] += 1
                bucket["hits"] += int(evidence["hit"])
                bucket["recall"] += evidence["recall"]
                bucket["in_context"] += int(bool(analysis["context"]["evidence_in_context"]))
    for pools in out.values():
        for by_k in pools.values():
            for bucket in by_k.values():
                n = bucket["questions"]
                bucket["hit_rate"] = bucket["hits"] / n if n else None
                bucket["recall"] = bucket["recall"] / n if n else None
                bucket["in_context_rate"] = bucket["in_context"] / n if n else None
    return out
