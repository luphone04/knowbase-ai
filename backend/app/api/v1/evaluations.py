"""Evaluation endpoints (SPEC.md §11 "Evaluation and analytics", ADR 0014).

- `GET /evaluations/questions`: the dataset, whether this workspace's documents
  match it, and the budget a run needs and has left today;
- `POST /evaluations/run`: start a run (202). The worker evaluates it one
  question at a time; poll the run for progress;
- `GET /evaluations/runs`, `GET /evaluations/runs/{id}`: this workspace's runs,
  with metrics computed from the stored results, and every question's results.

Runs belong to the workspace: any member can start and read them, nobody else
(another workspace's run id gets the same 404 as a random id). The workspace
always comes from the session, never from the request.

Starting a run needs a configured Gemini key, the dataset's five documents
indexed in this workspace with their original content, no document still
being indexed, no other active run, and room in the daily budgets. The run
reserves its whole budget up front and gets back what it didn't use.
`GET /analytics/overview` stays deferred (ADR 0013).
"""

import uuid
from collections.abc import Sequence
from typing import Any

from fastapi import APIRouter
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.dependencies import CurrentAuth, DbSession, RateLimiterDep, SettingsDep
from app.api.v1.gemini_calls import INSTALLATION, provider_failure, query_embedding_rule
from app.api.v1.schemas import (
    CorpusOut,
    CorpusSourceOut,
    DatasetOut,
    EvaluationLimitsOut,
    EvaluationOverviewOut,
    EvaluationQuestionOut,
    EvaluationQuestionResultOut,
    EvaluationResultOut,
    EvaluationRunDetailOut,
    EvaluationRunIn,
    EvaluationRunOut,
    EvidenceOut,
    OtherSourceOut,
)
from app.config import Settings
from app.core.errors import AppError
from app.db.models import EvaluationQuestion, EvaluationResult, EvaluationRun
from app.evaluation import budget
from app.evaluation.dataset import Dataset, load_dataset
from app.evaluation.service import (
    CorpusSnapshot,
    DatasetChanged,
    corpus_snapshot,
    ensure_questions,
    run_questions,
    run_results,
    run_summary,
)
from app.evaluation.variants import VARIANTS
from app.providers.errors import ProviderNotConfigured

router = APIRouter(prefix="/evaluations", tags=["evaluations"])

LIST_LIMIT = 50
_STATUS_TEXT = {
    "missing": "not uploaded",
    "not_ready": "not indexed yet",
    "content_differs": "uploaded, but its content differs from the demo file",
}


class RunNotFound(AppError):
    def __init__(self) -> None:
        super().__init__(404, "not_found", "Evaluation run not found.")


def _dataset() -> Dataset:
    return load_dataset()


def _questions(db: DbSession, dataset: Dataset) -> list[EvaluationQuestion]:
    try:
        return ensure_questions(db, dataset)
    except DatasetChanged as exc:
        raise AppError(409, "evaluation_dataset_changed", str(exc)) from exc


def _snapshot(
    db: DbSession, auth: CurrentAuth, dataset: Dataset, settings: Settings
) -> CorpusSnapshot:
    return corpus_snapshot(
        db,
        workspace_id=auth.workspace_id,
        source_hashes=dataset.source_hashes,
        embedding_model=settings.gemini_embedding_model,
    )


def question_out(question: EvaluationQuestion) -> EvaluationQuestionOut:
    reference = question.reference or {}
    return EvaluationQuestionOut(
        id=question.id,
        key=question.question_key,
        position=question.position,
        category=question.category,
        question=question.question,
        previous_question=question.previous_question,
        answerable=not question.is_unanswerable,
        expected_source_keys=list(question.relevant_source_keys),
        expected_answer=question.expected_answer,
        evidence=[EvidenceOut.model_validate(e) for e in reference.get("evidence", [])],
        absent_terms=list(reference.get("absent_terms", [])),
        notes=reference.get("notes"),
    )


def run_out(run: EvaluationRun, summary: dict[str, Any] | None = None) -> EvaluationRunOut:
    return EvaluationRunOut(
        id=run.id,
        status=run.status,
        dataset_name=run.dataset_name,
        dataset_version=run.dataset_version,
        dataset_sha256=run.dataset_sha256,
        questions_total=run.questions_total,
        questions_done=run.questions_done,
        answer_modes=list(run.config.get("answer_modes", [])),
        error_message=run.error_message,
        created_at=run.created_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        config={k: v for k, v in run.config.items() if k != "budget"},
        corpus={k: v for k, v in run.corpus.items() if k != "source_hashes"},
        summary=run.summary if summary is None else summary,
    )


def result_out(result: EvaluationResult) -> EvaluationResultOut:
    return EvaluationResultOut.model_validate(result, from_attributes=True)


@router.get("/questions")
def get_questions(
    auth: CurrentAuth, db: DbSession, settings: SettingsDep, limiter: RateLimiterDep
) -> EvaluationOverviewOut:
    """The dataset, this workspace's readiness for it, and today's remaining budget."""
    dataset = _dataset()
    questions = _questions(db, dataset)
    snapshot = _snapshot(db, auth, dataset, settings)
    db.commit()
    count = len(dataset.questions)
    return EvaluationOverviewOut(
        dataset=DatasetOut(
            name=dataset.name,
            version=dataset.version,
            sha256=dataset.sha256,
            description=dataset.description,
            questions=count,
            answerable=dataset.answerable,
            unanswerable=dataset.unanswerable,
            source_keys=sorted(dataset.source_hashes),
        ),
        corpus=CorpusOut(
            ready=snapshot.ready,
            sources=[
                CorpusSourceOut(key=s.key, status=s.status, chunks=s.chunks)
                for s in snapshot.sources
            ],
            other_sources=[OtherSourceOut.model_validate(o) for o in snapshot.other_sources],
            total_chunks=snapshot.total_chunks,
            indexing=snapshot.indexing,
        ),
        limits=EvaluationLimitsOut(
            query_embeddings_per_run=count,
            generations_per_answer_mode=count,
            runs_left=limiter.remaining(budget.run_rule(settings), str(auth.workspace_id)),
            query_embeddings_left=limiter.remaining(query_embedding_rule(settings), INSTALLATION),
            generations_left=limiter.remaining(budget.generation_rule(settings), INSTALLATION),
            question_interval_seconds=settings.evaluation_question_interval_seconds,
        ),
        questions=[question_out(q) for q in questions],
    )


def _corpus_problem(snapshot: CorpusSnapshot) -> AppError:
    missing = [s for s in snapshot.sources if s.status != "ready"]
    if missing:
        names = ", ".join(f"{s.key} ({_STATUS_TEXT[s.status]})" for s in missing)
        return AppError(
            409,
            "evaluation_corpus_incomplete",
            "The evaluation needs the five Northstar demo documents from "
            f"sample_data/demo_store, uploaded unchanged and indexed. Missing: {names}.",
            details=[{"key": s.key, "status": s.status} for s in snapshot.sources],
        )
    return AppError(
        409,
        "evaluation_corpus_indexing",
        f"{snapshot.indexing} document(s) are still being indexed. Start the evaluation once "
        "every document is ready, so the corpus doesn't change during the run.",
    )


@router.post("/run", status_code=202)
def start_run(
    body: EvaluationRunIn,
    auth: CurrentAuth,
    db: DbSession,
    settings: SettingsDep,
    limiter: RateLimiterDep,
) -> EvaluationRunOut:
    """Queue a run of the whole dataset against this workspace's documents."""
    if not settings.gemini_configured:
        raise provider_failure(ProviderNotConfigured())
    dataset = _dataset()
    questions = _questions(db, dataset)
    snapshot = _snapshot(db, auth, dataset, settings)
    if not snapshot.ready:
        raise _corpus_problem(snapshot)
    active = db.scalar(
        select(EvaluationRun.id).where(EvaluationRun.status.in_(("queued", "running")))
    )
    if active is not None:
        raise AppError(409, "evaluation_running", "An evaluation is already running here.")
    db.commit()  # end the reads before the budget's own transactions

    count = len(questions)
    reservation = budget.reserve(
        limiter,
        settings,
        workspace_id=str(auth.workspace_id),
        query_embeddings=count,
        generations=count * len(body.answer_modes),
    )
    run = EvaluationRun(
        workspace_id=auth.workspace_id,
        created_by_user_id=auth.user_id,
        dataset_name=dataset.name,
        dataset_version=dataset.version,
        dataset_sha256=dataset.sha256,
        status="queued",
        questions_total=count,
        config=_run_config(settings, body.answer_modes, reservation.as_json()),
        corpus=snapshot.as_json(),
    )
    db.add(run)
    try:
        db.commit()
    except IntegrityError as exc:  # another request started a run at the same moment
        db.rollback()
        budget.give_back_unused(limiter, settings, reservation.as_json(), {})
        raise AppError(409, "evaluation_running", "An evaluation is already running here.") from exc
    db.refresh(run)
    return run_out(run)


def _run_config(
    settings: Settings, answer_modes: Sequence[str], reserved: dict[str, Any]
) -> dict[str, Any]:
    """Everything that shaped the results, recorded with the run."""
    return {
        "relevance": "source",
        "variants": [v.key for v in VARIANTS],
        "variant_details": {
            v.key: {
                "label": v.label,
                "mode": v.mode,
                "experimental": v.experimental,
                "lexical_config": v.lexical.config if v.mode == "hybrid" else None,
                "lexical_match": v.lexical.match if v.mode == "hybrid" else None,
            }
            for v in VARIANTS
        },
        "answer_modes": list(answer_modes),
        "embedding_model": settings.gemini_embedding_model,
        "embedding_dimensions": settings.embedding_dimensions,
        "chat_model": settings.gemini_chat_model,
        "thinking_level": settings.gemini_thinking_level,
        "max_output_tokens": settings.gemini_max_output_tokens,
        "generation_max_retries": settings.gemini_generation_max_retries,
        "embedding_max_retries": settings.gemini_max_retries,
        "top_n": settings.retrieval_top_n,
        "candidates": settings.retrieval_candidates,
        "rrf_k": settings.rrf_k,
        "chat_context_max_chars": settings.chat_context_max_chars,
        "chunk_target_chars": settings.chunk_target_chars,
        "chunk_max_chars": settings.chunk_max_chars,
        "chunk_overlap_chars": settings.chunk_overlap_chars,
        "question_interval_seconds": settings.evaluation_question_interval_seconds,
        "budget": reserved,
    }


@router.get("/runs")
def list_runs(auth: CurrentAuth, db: DbSession) -> list[EvaluationRunOut]:
    """This workspace's runs, newest first."""
    runs = db.scalars(
        select(EvaluationRun)
        .where(EvaluationRun.workspace_id == auth.workspace_id)
        .order_by(EvaluationRun.created_at.desc(), EvaluationRun.id)
        .limit(LIST_LIMIT)
    )
    return [run_out(run) for run in runs]


@router.get("/runs/{run_id:uuid}")
def get_run(run_id: uuid.UUID, auth: CurrentAuth, db: DbSession) -> EvaluationRunDetailOut:
    """One run: metrics from its stored results, and each question's results."""
    run = db.scalar(
        select(EvaluationRun).where(
            EvaluationRun.id == run_id, EvaluationRun.workspace_id == auth.workspace_id
        )
    )
    if run is None:
        raise RunNotFound()
    questions = run_questions(db, run)
    results = run_results(db, run)
    by_question: dict[uuid.UUID, list[EvaluationResult]] = {}
    for result in results:
        by_question.setdefault(result.question_id, []).append(result)
    order = {v.key: i for i, v in enumerate(VARIANTS)}
    summary = run_summary(run, questions, results)
    return EvaluationRunDetailOut(
        **run_out(run, summary).model_dump(),
        questions=[
            EvaluationQuestionResultOut(
                question=question_out(q),
                results=[
                    result_out(r)
                    for r in sorted(by_question.get(q.id, []), key=lambda r: order[r.variant])
                ],
            )
            for q in questions
        ],
    )
