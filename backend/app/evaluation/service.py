"""What the API and the worker share: the dataset in the database, the corpus, summaries.

- `ensure_questions` loads a dataset version into `evaluation_questions` once,
  and refuses a dataset file that changed without a new version.
- `corpus_snapshot` describes the workspace's documents as the evaluation sees
  them: each dataset source key is ready (name and content hash match, indexed),
  missing, not ready, or different; other documents are listed too, because
  they compete in retrieval. Its fingerprint changes when any document does.
- `run_summary` computes a run's metrics from its stored results only, with
  its Gemini token usage and estimated cost (usage.py).
"""

import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.db.models import (
    Chunk,
    Document,
    EvaluationQuestion,
    EvaluationResult,
    EvaluationRun,
    Source,
)
from app.evaluation import budget
from app.evaluation.dataset import Dataset
from app.evaluation.metrics import ResultRow, chance_hit_rate, summarize
from app.evaluation.usage import UsageRow, usage_summary
from app.evaluation.variants import VARIANTS

INDEXING_STATUSES = ("pending", "processing")


class DatasetChanged(Exception):
    """The dataset file differs from the version already loaded: it needs a new version."""


def ensure_questions(db: Session, dataset: Dataset) -> list[EvaluationQuestion]:
    """The dataset's question rows, inserting them the first time. Caller commits."""
    rows = [
        {
            "dataset_name": dataset.name,
            "dataset_version": dataset.version,
            "dataset_sha256": dataset.sha256,
            "question_key": q.key,
            "position": q.position,
            "category": q.category,
            "question": q.question,
            "previous_question": q.previous_question,
            "expected_answer": q.expected_answer,
            "relevant_source_keys": list(q.expected_source_keys),
            "is_unanswerable": not q.answerable,
            "reference": {
                "evidence": [{"source_key": e.source_key, "quote": e.quote} for e in q.evidence],
                "absent_terms": list(q.absent_terms),
                "notes": q.notes,
            },
        }
        for q in dataset.questions
    ]
    db.execute(
        insert(EvaluationQuestion)
        .values(rows)
        .on_conflict_do_nothing(constraint="uq_evaluation_questions_key")
    )
    stored = list(
        db.scalars(
            select(EvaluationQuestion)
            .where(
                EvaluationQuestion.dataset_name == dataset.name,
                EvaluationQuestion.dataset_version == dataset.version,
            )
            .order_by(EvaluationQuestion.position)
        )
    )
    keys = [q.key for q in dataset.questions]
    if [q.question_key for q in stored] != keys or any(
        q.dataset_sha256 != dataset.sha256 for q in stored
    ):
        raise DatasetChanged(
            f"Dataset {dataset.name} version {dataset.version} changed after it was first "
            "loaded. Give the edited file a new version."
        )
    return stored


@dataclass(frozen=True)
class CorpusSource:
    key: str
    status: str  # ready, missing, not_ready, content_differs
    source_id: str | None = None
    chunks: int = 0


@dataclass(frozen=True)
class CorpusSnapshot:
    sources: list[CorpusSource]
    other_sources: list[dict[str, Any]]  # documents outside the dataset: name, chunks, status
    total_chunks: int  # searchable chunks in the workspace (configured embedding model)
    indexing: int  # documents still being indexed
    fingerprint: str
    source_hashes: Mapping[str, str]  # the dataset's source key -> SHA-256
    by_source_id: dict[str, str] = field(default_factory=dict)  # dataset source id -> key

    @property
    def ready(self) -> bool:
        return self.indexing == 0 and all(s.status == "ready" for s in self.sources)

    def as_json(self) -> dict[str, Any]:
        return {
            "sources": [
                {"key": s.key, "status": s.status, "source_id": s.source_id, "chunks": s.chunks}
                for s in self.sources
            ],
            "other_sources": self.other_sources,
            "total_chunks": self.total_chunks,
            "indexing": self.indexing,
            "fingerprint": self.fingerprint,
            "source_hashes": dict(self.source_hashes),
        }


def corpus_snapshot(
    db: Session,
    *,
    workspace_id: uuid.UUID,
    source_hashes: Mapping[str, str],
    embedding_model: str,
) -> CorpusSnapshot:
    """The workspace's documents, matched against the dataset's source keys and hashes."""
    chunk_counts = (
        select(Document.source_id, func.count(Chunk.id).label("chunks"))
        .join(Chunk, Chunk.document_id == Document.id)
        .where(
            Document.workspace_id == workspace_id,
            Chunk.workspace_id == workspace_id,
            Chunk.embedding_model == embedding_model,
        )
        .group_by(Document.source_id)
        .subquery()
    )
    rows = db.execute(
        select(
            Source.id,
            Source.original_filename,
            Source.display_name,
            Source.file_sha256,
            Source.status,
            Source.last_indexed_at,
            func.coalesce(chunk_counts.c.chunks, 0).label("chunks"),
        )
        .outerjoin(chunk_counts, chunk_counts.c.source_id == Source.id)
        .where(Source.workspace_id == workspace_id, Source.status != "deleting")
        .order_by(Source.id)
    ).all()

    by_name = {(row.original_filename or "").lower(): row for row in rows}
    sources = []
    dataset_ids: dict[str, str] = {}
    for key, digest in sorted(source_hashes.items()):
        row = by_name.get(key)
        if row is None:
            sources.append(CorpusSource(key, "missing"))
            continue
        chunks = int(row.chunks)
        if row.file_sha256 != digest:
            status = "content_differs"
        elif row.status != "ready" or chunks == 0:
            status = "not_ready"
        else:
            status = "ready"
            dataset_ids[str(row.id)] = key
        sources.append(CorpusSource(key, status, str(row.id), chunks))
    other_sources = [
        {
            "name": row.original_filename or row.display_name,
            "chunks": int(row.chunks),
            "status": row.status,
        }
        for row in rows
        if str(row.id) not in dataset_ids
        and (row.original_filename or "").lower() not in source_hashes
    ]
    state = [
        [str(row.id), row.file_sha256, row.status, int(row.chunks), str(row.last_indexed_at)]
        for row in rows
    ]
    return CorpusSnapshot(
        sources=sources,
        other_sources=other_sources,
        total_chunks=sum(int(row.chunks) for row in rows),
        indexing=sum(row.status in INDEXING_STATUSES for row in rows),
        fingerprint=hashlib.sha256(json.dumps([embedding_model, state]).encode()).hexdigest(),
        source_hashes=dict(source_hashes),
        by_source_id=dataset_ids,
    )


def chance_by_question(
    questions: list[EvaluationQuestion], corpus: dict[str, Any], top_n: int
) -> dict[str, float | None]:
    """Chance hit@k per answerable question, from the corpus recorded when the run started."""
    chunks = {s["key"]: s["chunks"] for s in corpus.get("sources", [])}
    total = int(corpus.get("total_chunks", 0))
    return {
        q.question_key: chance_hit_rate(
            total, sum(chunks.get(key, 0) for key in q.relevant_source_keys), top_n
        )
        for q in questions
        if not q.is_unanswerable
    }


def result_row(result: EvaluationResult, question: EvaluationQuestion) -> ResultRow:
    return ResultRow(
        question_key=question.question_key,
        answerable=not question.is_unanswerable,
        variant=result.variant,
        retrieval_error=result.error_stage == "embedding",
        hit_at_5=result.hit_at_5,
        recall_at_5=result.recall_at_5,
        reciprocal_rank=result.reciprocal_rank,
        lexical_candidates=result.lexical_candidates,
        top_vector_distance=result.top_vector_distance,
        embedding_latency_ms=result.embedding_latency_ms,
        retrieval_latency_ms=result.retrieval_latency_ms,
        answer_evaluated=result.answer_evaluated,
        outcome=result.outcome,
        abstain_reason=result.abstain_reason,
        citation_precision=result.citation_precision,
        citation_recall=result.citation_recall,
        abstention_correct=result.abstention_correct,
        answer_latency_ms=result.answer_latency_ms,
        generation_latency_ms=result.token_usage.get("generation_latency_ms"),
        failed_attempt_ms=result.details.get("failed_attempt_ms"),
        failed_generation_ms=result.details.get("failed_generation_ms"),
        failed_embedding_ms=result.details.get("failed_embedding_ms"),
    )


def run_questions(db: Session, run: EvaluationRun) -> list[EvaluationQuestion]:
    return list(
        db.scalars(
            select(EvaluationQuestion)
            .where(
                EvaluationQuestion.dataset_name == run.dataset_name,
                EvaluationQuestion.dataset_version == run.dataset_version,
            )
            .order_by(EvaluationQuestion.position)
        )
    )


def run_results(db: Session, run: EvaluationRun) -> list[EvaluationResult]:
    return list(
        db.scalars(
            select(EvaluationResult)
            .where(EvaluationResult.run_id == run.id)
            .order_by(EvaluationResult.created_at, EvaluationResult.variant)
        )
    )


def run_summary(
    run: EvaluationRun, questions: list[EvaluationQuestion], results: list[EvaluationResult]
) -> dict[str, Any]:
    """Metrics for the run, from its stored results (partial while it runs)."""
    by_id = {q.id: q for q in questions}
    rows = [result_row(r, by_id[r.question_id]) for r in results if r.question_id in by_id]
    config = run.config
    summary = summarize(
        rows,
        variants=config.get("variants", [v.key for v in VARIANTS]),
        answer_modes=config.get("answer_modes", []),
        chance=chance_by_question(questions, run.corpus, int(config.get("top_n", 5))),
    )
    usage: dict[str, int] = defaultdict(int)
    for r in results:
        for name in ("query_embeddings", "generation_requests"):
            usage[name] += int(r.token_usage.get(name, 0))
    summary["gemini_requests"] = dict(usage)
    # Budget reserved for retried questions (an upper bound on unrecorded calls; ADR 0014).
    summary["retry_reservations"] = budget.retry_totals(run.config.get("budget", {}))
    # Tokens and estimated cost, priced with the snapshot stored when the run started.
    summary["usage"] = usage_summary(
        [
            UsageRow(by_id[r.question_id].question_key, r.variant, r.token_usage)
            for r in results
            if r.question_id in by_id
        ],
        pricing=config.get("pricing"),
        embedding_model=config.get("embedding_model"),
        chat_model=config.get("chat_model"),
    )
    return summary
