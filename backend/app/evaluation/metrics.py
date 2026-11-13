"""Evaluation metrics (SPEC.md §13.2, ADR 0014). Pure functions, no I/O.

Relevance is judged at **source level**: a retrieved chunk is relevant when its
source's key is one of the question's expected source keys. The top chunks are
first collapsed to sources by best rank, so several chunks of one source count
once.

Per question (answerable questions only; None for unanswerable ones):
- hit@5: at least one expected source among the top-5 chunks' sources;
- recall@5: expected sources retrieved / expected sources;
- reciprocal rank: 1 / (source rank of the first expected source), 0 if none.

Per answered answerable question (None otherwise):
- citation precision: cited sources that are expected / cited sources;
- citation recall: expected sources cited / expected sources.

Latency statistics describe successful calls. A provider failure's time until the
error is reported separately (`failed_*_latency_ms`), never mixed in; runs from
before these were recorded show them as not measured (None).

Passage relevance (datasets that cite evidence passages, e.g. Asia Cement): a
retrieved chunk is relevant when it holds one of the question's evidence
quotations (app/evaluation/passages.py). Chunks are not collapsed to sources:
- hit@5: at least one top-5 chunk holds evidence;
- recall@5: evidence passages held by a top-5 chunk / evidence passages;
- reciprocal rank: 1 / (chunk rank of the first chunk holding evidence), 0 if none.
A question whose evidence couldn't all be mapped has no passage score (None),
never a guessed one. The right document with the wrong chunk is a miss here;
document-level results are summarized separately (`summarize_documents`).

Abstention is correct when an answerable question is answered or an
unanswerable one is abstained on. A provider failure has no correctness: it is
neither an answer nor an abstention.

Run summaries average per-question values over the questions where the value
is defined, and report that count. A metric with no defined value is None
("not measured"), never 0.
"""

import math
import statistics
from collections import Counter
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SourceRef:
    """A retrieved or cited source: its identity, dataset key (None outside the dataset), name."""

    id: str
    key: str | None
    name: str


@dataclass(frozen=True)
class RankedSource:
    rank: int  # 1-based among distinct sources
    source: SourceRef
    chunk_ranks: tuple[int, ...]  # the chunk ranks that collapsed into it


def rank_sources(chunks: Sequence[SourceRef]) -> list[RankedSource]:
    """Collapse ranked chunks (best first) to sources, each at its best chunk's position."""
    order: list[SourceRef] = []
    ranks: dict[str, list[int]] = {}
    for chunk_rank, source in enumerate(chunks, start=1):
        if source.id not in ranks:
            order.append(source)
            ranks[source.id] = []
        ranks[source.id].append(chunk_rank)
    return [
        RankedSource(rank, source, tuple(ranks[source.id]))
        for rank, source in enumerate(order, start=1)
    ]


@dataclass(frozen=True)
class RetrievalScore:
    hit: bool
    recall: float
    reciprocal_rank: float


def retrieval_score(
    ranked: Sequence[RankedSource], expected: Collection[str]
) -> RetrievalScore | None:
    """Scores for one question's ranked sources. None when nothing is relevant (unanswerable)."""
    if not expected:
        return None
    expected = set(expected)
    found = {r.source.key for r in ranked if r.source.key in expected}
    first = next((r.rank for r in ranked if r.source.key in expected), None)
    return RetrievalScore(
        hit=first is not None,
        recall=len(found) / len(expected),
        reciprocal_rank=0.0 if first is None else 1.0 / first,
    )


def passage_score(
    retrieved_chunk_ids: Sequence[str], evidence: Sequence[Collection[str]] | None
) -> RetrievalScore | None:
    """Passage-level scores for one question's ranked chunks (best first).

    `evidence`: each evidence passage's chunk ids (any of them holds the whole
    passage). None when the evidence isn't fully mapped: no score, not a zero.
    """
    if not evidence:
        return None
    relevant = set().union(*evidence)
    top = list(retrieved_chunk_ids)
    first = next((rank for rank, cid in enumerate(top, start=1) if cid in relevant), None)
    found = sum(any(cid in group for cid in top) for group in evidence)
    return RetrievalScore(
        hit=first is not None,
        recall=found / len(evidence),
        reciprocal_rank=0.0 if first is None else 1.0 / first,
    )


@dataclass(frozen=True)
class CitationScore:
    precision: float
    recall: float


def citation_score(cited: Sequence[SourceRef], expected: Collection[str]) -> CitationScore | None:
    """Source-level citation scores. None without expected sources or without citations."""
    if not expected or not cited:
        return None
    expected = set(expected)
    sources = {source.id: source for source in cited}
    relevant = [s for s in sources.values() if s.key in expected]
    return CitationScore(
        precision=len(relevant) / len(sources),
        recall=len({s.key for s in relevant}) / len(expected),
    )


def abstention_correct(outcome: str, answerable: bool) -> bool | None:
    """Did it answer an answerable question, or abstain on an unanswerable one?"""
    if outcome == "answered":
        return answerable
    if outcome == "abstained":
        return not answerable
    return None  # a provider failure is not a decision


def chance_hit_rate(total_chunks: int, relevant_chunks: int, k: int) -> float | None:
    """P(a random ranking puts at least one relevant chunk in the top k): hypergeometric."""
    if total_chunks <= 0 or relevant_chunks <= 0:
        return None
    k = min(k, total_chunks)
    misses = math.comb(total_chunks - relevant_chunks, k)
    return 1 - misses / math.comb(total_chunks, k)


def nearest_rank(values: Sequence[float], percentile: float) -> float:
    """The nearest-rank percentile: the smallest value with at least p% of values at or below."""
    ordered = sorted(values)
    index = max(0, math.ceil(percentile / 100 * len(ordered)) - 1)
    return ordered[index]


def latency_stats(values: Iterable[int | None]) -> dict[str, Any] | None:
    """Count, min, median, p95 (nearest rank), and max in milliseconds; None without values."""
    measured = [v for v in values if v is not None]
    if not measured:
        return None
    return {
        "count": len(measured),
        "min": min(measured),
        "median": statistics.median(measured),
        "p95": nearest_rank(measured, 95),
        "max": max(measured),
    }


def distance_stats(values: Iterable[float | None]) -> dict[str, Any] | None:
    """Count, min, median, and max of cosine distances; None without values."""
    measured = [v for v in values if v is not None]
    if not measured:
        return None
    return {
        "count": len(measured),
        "min": min(measured),
        "median": statistics.median(measured),
        "max": max(measured),
    }


def _mean(values: Iterable[float | None]) -> tuple[float | None, int]:
    defined = [v for v in values if v is not None]
    return (sum(defined) / len(defined) if defined else None), len(defined)


@dataclass(frozen=True)
class ResultRow:
    """What summaries need from one stored result (one question under one configuration)."""

    question_key: str
    answerable: bool
    variant: str
    retrieval_error: bool
    hit_at_5: bool | None
    recall_at_5: float | None
    reciprocal_rank: float | None
    lexical_candidates: int | None
    top_vector_distance: float | None
    embedding_latency_ms: int | None
    retrieval_latency_ms: int | None
    answer_evaluated: bool
    outcome: str | None
    abstain_reason: str | None
    citation_precision: float | None
    citation_recall: float | None
    abstention_correct: bool | None
    answer_latency_ms: int | None
    generation_latency_ms: int | None
    # Time until a provider failure (provider_error rows only); never mixed with the above.
    failed_attempt_ms: int | None = None
    failed_generation_ms: int | None = None
    failed_embedding_ms: int | None = None


def summarize_retrieval(
    rows: Sequence[ResultRow], *, chance: Mapping[str, float | None]
) -> dict[str, Any]:
    """One configuration's retrieval summary. `chance`: question key -> chance hit@5."""
    answerable = [r for r in rows if r.answerable]
    scored = [r for r in answerable if not r.retrieval_error]
    hit_rate, hit_n = _mean(None if r.hit_at_5 is None else float(r.hit_at_5) for r in scored)
    recall, _ = _mean(r.recall_at_5 for r in scored)
    mrr, _ = _mean(r.reciprocal_rank for r in scored)
    chance_rate, _ = _mean(chance.get(r.question_key) for r in scored)
    unanswerable = [r for r in rows if not r.answerable and not r.retrieval_error]

    def no_lexical(group: Sequence[ResultRow]) -> int | None:
        counted = [r for r in group if r.lexical_candidates is not None]
        return sum(r.lexical_candidates == 0 for r in counted) if counted else None

    return {
        "questions": hit_n,  # answerable questions with retrieval metrics
        "errors": sum(r.retrieval_error for r in rows),
        "hit_rate_at_5": hit_rate,
        "recall_at_5": recall,
        "mrr": mrr,
        "chance_hit_rate_at_5": chance_rate,
        "no_lexical_candidates": {
            "answerable": no_lexical(scored),
            "unanswerable": no_lexical(unanswerable),
        },
        "latency_ms": latency_stats(r.retrieval_latency_ms for r in rows),
        "unanswerable_questions": len(unanswerable),
    }


def summarize_answers(rows: Sequence[ResultRow]) -> dict[str, Any]:
    """One answer mode's summary: confusion matrix, abstention accuracy, citations, latency."""
    evaluated = [r for r in rows if r.answer_evaluated]
    confusion = {
        group: dict.fromkeys(("answered", "abstained", "provider_error"), 0)
        for group in ("answerable", "unanswerable")
    }
    for r in evaluated:
        if r.outcome in confusion["answerable"]:
            confusion["answerable" if r.answerable else "unanswerable"][r.outcome] += 1
    decided = [r for r in evaluated if r.abstention_correct is not None]
    accuracy, _ = _mean(float(bool(r.abstention_correct)) for r in decided)
    precision, citation_n = _mean(r.citation_precision for r in evaluated)
    recall, _ = _mean(r.citation_recall for r in evaluated)
    reasons = Counter(r.abstain_reason for r in evaluated if r.abstain_reason)
    return {
        "questions": len(evaluated),
        "decided": len(decided),  # questions with an answer or an abstention
        "provider_errors": sum(r.outcome == "provider_error" for r in evaluated),
        "confusion": confusion,
        "abstention_accuracy": accuracy,
        "citation_questions": citation_n,  # answered answerable questions
        "citation_precision": precision,
        "citation_recall": recall,
        "abstain_reasons": dict(sorted(reasons.items())),
        # Successful answers (answered or abstained) only.
        "latency_ms": latency_stats(
            r.answer_latency_ms for r in evaluated if r.outcome != "provider_error"
        ),
        "generation_latency_ms": latency_stats(
            r.generation_latency_ms for r in evaluated if r.outcome != "provider_error"
        ),
        # Provider failures, until the error: their own statistics.
        "failed_attempt_latency_ms": latency_stats(
            r.failed_attempt_ms for r in evaluated if r.outcome == "provider_error"
        ),
        "failed_generation_latency_ms": latency_stats(
            r.failed_generation_ms for r in evaluated if r.outcome == "provider_error"
        ),
    }


def summarize_documents(
    rows: Iterable[tuple[str, str, Sequence[Mapping[str, Any]]]],
    *,
    expected: Mapping[str, Sequence[str]],
    variants: Sequence[str],
) -> dict[str, Any]:
    """Document-level hit@5, recall@5, and MRR per configuration, from stored results.

    `rows`: (variant, question key, retrieved_sources) for answerable questions whose
    retrieval ran. For passage datasets, where these are not the headline metrics.
    """
    scores: dict[str, list[RetrievalScore]] = {v: [] for v in variants}
    for variant, key, retrieved_sources in rows:
        ranked = [
            RankedSource(int(s["rank"]), SourceRef(str(s["rank"]), s.get("source_key"), ""), ())
            for s in retrieved_sources
        ]
        score = retrieval_score(ranked, expected.get(key, []))
        if score is not None:
            scores.setdefault(variant, []).append(score)
    return {
        variant: {
            "questions": len(items),
            "hit_rate_at_5": _mean(float(s.hit) for s in items)[0],
            "recall_at_5": _mean(s.recall for s in items)[0],
            "mrr": _mean(s.reciprocal_rank for s in items)[0],
        }
        for variant, items in scores.items()
    }


def summarize(
    rows: Sequence[ResultRow],
    *,
    variants: Sequence[str],
    answer_modes: Sequence[str],
    chance: Mapping[str, float | None],
) -> dict[str, Any]:
    """The whole run's summary, computed from its stored results only."""
    by_variant: dict[str, list[ResultRow]] = {v: [] for v in variants}
    for row in rows:
        by_variant.setdefault(row.variant, []).append(row)
    # The query embedding and the vector list are shared: read them from one configuration.
    shared = by_variant.get("vector") or next(iter(by_variant.values()), [])
    distances = {
        group: distance_stats(
            r.top_vector_distance for r in shared if r.answerable == (group == "answerable")
        )
        for group in ("answerable", "unanswerable")
    }
    return {
        "questions_processed": len({r.question_key for r in rows}),
        # Successful query embeddings only; failed ones are reported on their own.
        "embedding_latency_ms": latency_stats(
            r.embedding_latency_ms for r in shared if not r.retrieval_error
        ),
        "failed_embedding_latency_ms": latency_stats(
            r.failed_embedding_ms for r in shared if r.retrieval_error
        ),
        "nearest_vector_distance": distances,
        "retrieval": {
            variant: summarize_retrieval(by_variant[variant], chance=chance) for variant in variants
        },
        "answers": {mode: summarize_answers(by_variant.get(mode, [])) for mode in answer_modes},
    }
