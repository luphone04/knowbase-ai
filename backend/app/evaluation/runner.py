"""The evaluation runner: a second durable queue in the worker (ADR 0014).

`evaluation_runs` rows are claimed like ingestion jobs: `FOR UPDATE SKIP
LOCKED`, a fresh fencing token per claim, and a lease renewed by a heartbeat.
Each claim evaluates **one question** and commits its results together with
the run's progress, then releases the run with `next_run_at` a little later:

- ingestion jobs run between questions (the worker checks them first);
- the pause paces Gemini requests (EVALUATION_QUESTION_INTERVAL_SECONDS);
- a crash loses at most the question in progress, which the next claim redoes.
  `attempts` counts claims since the last finished question, so a question that
  keeps killing the worker fails the run instead of looping.

One question, every configuration: the retrieval query (with the previous
question for a follow-up, exactly as chat builds it) is embedded once; each
configuration then ranks with that embedding; each answer mode builds the
context and answers through `ChatService.answer`, the code chat uses. Nothing
is written to conversations, and no database connection is held while Gemini
works.

Provider failures are results, not abstentions. Gemini's daily quota, a
missing key, or a rejected key stops the run: every further call would fail
too. If the workspace's documents change during a run, it stops, because its
results would mix two corpora.
"""

import logging
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.auth.rate_limit import RateLimiter
from app.chat.context import build_context
from app.chat.prompt import HistoryMessage
from app.chat.service import Answer, ChatService
from app.config import Settings
from app.db.models import EvaluationQuestion, EvaluationResult, EvaluationRun
from app.db.tenancy import SKIP_WORKSPACE_SCOPE, bind_workspace
from app.evaluation import budget
from app.evaluation.metrics import (
    SourceRef,
    abstention_correct,
    citation_score,
    rank_sources,
    retrieval_score,
)
from app.evaluation.service import corpus_snapshot, run_questions, run_results, run_summary
from app.evaluation.variants import BY_KEY
from app.providers.errors import (
    REFUSED_BEFORE_COUNTING,
    ProviderConfigurationError,
    ProviderError,
    ProviderNotConfigured,
    ProviderQuotaExhausted,
)
from app.providers.interfaces import EmbeddingProvider, GenerationProvider
from app.retrieval.followup import contextualize
from app.retrieval.service import Ranking, Retriever
from app.worker.lease import Deadline, LeaseHeartbeat

logger = logging.getLogger(__name__)

# Every later call would fail the same way, so the run stops instead of spending more.
STOPPING_ERRORS: tuple[type[ProviderError], ...] = (
    ProviderQuotaExhausted,
    ProviderNotConfigured,
    ProviderConfigurationError,
)
CORPUS_CHANGED = (
    "The workspace's documents changed during the evaluation (an upload, re-index, or "
    "delete), so its results would mix two corpora. Start a new run."
)
ABANDONED = "The worker stopped repeatedly while evaluating question {key}; the run was stopped."
SKIPPED = "Not attempted: the run stopped at an earlier request."


@dataclass(frozen=True)
class ClaimedRun:
    id: uuid.UUID
    workspace_id: uuid.UUID
    lock_token: uuid.UUID
    attempts: int


@dataclass(frozen=True)
class QuestionOutcome:
    rows: list[dict[str, Any]]  # EvaluationResult columns, one per configuration
    stop: str | None  # why the run must stop after this question, if it must


def _ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


def _refused(exc: ProviderError) -> bool:
    return isinstance(exc, REFUSED_BEFORE_COUNTING)


class EvaluationWorker:
    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: sessionmaker[Session],
        engine: Engine,
        embedding_provider_factory: Callable[[], EmbeddingProvider],
        generation_provider_factory: Callable[[], GenerationProvider],
        worker_id: str,
    ) -> None:
        self.settings = settings
        self._session_factory = session_factory
        self._engine = engine
        self.worker_id = worker_id
        self.retriever = Retriever(settings, embedding_provider_factory)
        self.chat = ChatService(settings, self.retriever, generation_provider_factory)
        self.limiter = RateLimiter(engine, settings.app_secret_key.get_secret_value())

    # --- queue ------------------------------------------------------------------------

    def claim(self) -> ClaimedRun | None:
        """Take the oldest due run (new, between questions, or abandoned by a crashed worker)."""
        token = uuid.uuid4()
        lease = timedelta(seconds=self.settings.job_lease_seconds)
        next_due = (
            select(EvaluationRun.id)
            .where(
                EvaluationRun.status.in_(("queued", "running")),
                EvaluationRun.next_run_at <= func.now(),
                or_(
                    EvaluationRun.lock_token.is_(None),
                    EvaluationRun.locked_at < func.now() - lease,  # its worker crashed
                ),
            )
            .order_by(EvaluationRun.next_run_at, EvaluationRun.created_at)
            .limit(1)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        statement = (
            update(EvaluationRun)
            .where(EvaluationRun.id == next_due)
            .values(
                status="running",
                attempts=EvaluationRun.attempts + 1,
                locked_at=func.now(),
                lock_token=token,
                locked_by=self.worker_id,
                started_at=func.coalesce(EvaluationRun.started_at, func.now()),
            )
            .returning(EvaluationRun.id, EvaluationRun.workspace_id, EvaluationRun.attempts)
            # Cross-workspace by design: the queue serves every workspace.
            .execution_options(**SKIP_WORKSPACE_SCOPE, synchronize_session=False)
        )
        with self._session_factory() as session:
            row = session.execute(statement).one_or_none()
            session.commit()
        if row is None:
            return None
        return ClaimedRun(row.id, row.workspace_id, token, row.attempts)

    def _renew(self, run: ClaimedRun) -> bool:
        with self._engine.begin() as connection:
            result = connection.execute(
                update(EvaluationRun)
                .where(
                    EvaluationRun.id == run.id,
                    EvaluationRun.lock_token == run.lock_token,
                    EvaluationRun.status == "running",
                )
                .values(locked_at=func.now())
            )
            return result.rowcount == 1

    @contextmanager
    def session(self, run: ClaimedRun) -> Iterator[Session]:
        with self._session_factory() as session:
            bind_workspace(session, run.workspace_id)
            yield session

    def _owned(self, db: Session, run: ClaimedRun, *, lock: bool) -> EvaluationRun | None:
        statement = select(EvaluationRun).where(
            EvaluationRun.id == run.id,
            EvaluationRun.lock_token == run.lock_token,
            EvaluationRun.status == "running",
        )
        return db.scalars(statement.with_for_update() if lock else statement).one_or_none()

    def run_once(self) -> bool:
        """Evaluate one question of the next due run. False if no run was due."""
        run = self.claim()
        if run is None:
            return False
        deadline = Deadline(self.settings.job_max_runtime_seconds)
        with LeaseHeartbeat(
            lambda: self._renew(run),
            interval=self.settings.job_heartbeat_seconds,
            deadline=deadline,
            name=f"Evaluation run {run.id}",
        ):
            self.process(run, deadline)
        return True

    # --- one question -----------------------------------------------------------------

    def process(self, run: ClaimedRun, deadline: Deadline) -> None:
        with self.session(run) as db:
            row = self._owned(db, run, lock=False)
            if row is None:
                return
            questions = run_questions(db, row)
            done = set(
                db.scalars(
                    select(EvaluationResult.question_id)
                    .where(EvaluationResult.run_id == run.id)
                    .distinct()
                )
            )
            pending = [q for q in questions if q.id not in done]
            if not pending:
                self._finish(db, run, status="succeeded")
                return
            question = pending[0]
            if run.attempts > self.settings.job_max_attempts:
                self._finish(
                    db, run, status="failed", message=ABANDONED.format(key=question.question_key)
                )
                return
            snapshot = corpus_snapshot(
                db,
                workspace_id=run.workspace_id,
                source_hashes=row.corpus.get("source_hashes", {}),
                embedding_model=self.settings.gemini_embedding_model,
            )
            if snapshot.fingerprint != row.corpus.get("fingerprint"):
                self._finish(db, run, status="failed", message=CORPUS_CHANGED)
                return
            config, corpus = dict(row.config), dict(row.corpus)
            db.commit()  # end the read: nothing is held while Gemini works

            outcome = self.evaluate(db, run, question, config, corpus)
            if deadline.lost.is_set():
                logger.warning("Evaluation run %s was taken over; results discarded", run.id)
                return
            self._save(db, run, question, outcome)

    def evaluate(
        self,
        db: Session,
        run: ClaimedRun,
        question: EvaluationQuestion,
        config: dict[str, Any],
        corpus: dict[str, Any],
    ) -> QuestionOutcome:
        """Embed once, rank with every configuration, answer with each answer mode."""
        query = contextualize(question.question, question.previous_question)
        expected = list(question.relevant_source_keys)
        keys = {s["source_id"]: s["key"] for s in corpus["sources"] if s["status"] == "ready"}
        variants = [BY_KEY[key] for key in config["variants"]]
        base = {"question_id": question.id, "retrieval_query": query}

        started = time.perf_counter()
        try:
            vector = self.retriever.embed_query(query)
        except ProviderError as exc:
            spent = 0 if _refused(exc) else 1
            failed: list[dict[str, Any]] = [
                {
                    **base,
                    "variant": v.key,
                    "retrieval_mode": v.mode,
                    "embedding_latency_ms": _ms(started),
                    "answer_evaluated": v.key in config["answer_modes"],
                    "outcome": "provider_error" if v.key in config["answer_modes"] else None,
                    "error_stage": "embedding",
                    "error_category": exc.category,
                    "error_message": exc.message,
                    "token_usage": {"query_embeddings": spent} if v.key == "vector" else {},
                }
                for v in variants
            ]
            return QuestionOutcome(
                failed, exc.message if isinstance(exc, STOPPING_ERRORS) else None
            )
        embedding_ms = _ms(started)

        rankings: dict[str, tuple[Ranking, int]] = {}
        for variant in variants:
            started = time.perf_counter()
            ranking = self.retriever.rank(
                db,
                workspace_id=run.workspace_id,
                query=query,
                query_vector=vector,
                mode=variant.mode,
                lexical=variant.lexical,
            )
            rankings[variant.key] = (ranking, _ms(started))
        db.commit()  # end the search transaction before any generation

        rows: list[dict[str, Any]] = []
        for variant in variants:
            ranking, retrieval_ms = rankings[variant.key]
            rows.append(
                {
                    **base,
                    **_retrieval_fields(ranking, keys, expected),
                    "variant": variant.key,
                    "retrieval_mode": variant.mode,
                    "lexical_candidates": len(ranking.lexical_hits)
                    if variant.mode == "hybrid"
                    else None,
                    "embedding_latency_ms": embedding_ms,
                    "retrieval_latency_ms": retrieval_ms,
                    "token_usage": {"query_embeddings": 1} if variant.key == "vector" else {},
                }
            )

        by_variant = {row["variant"]: row for row in rows}
        history = (
            [HistoryMessage("user", question.previous_question)]
            if question.previous_question
            else []
        )
        stop: str | None = None
        for mode in config["answer_modes"]:
            row = by_variant[mode]
            row["answer_evaluated"] = True
            if stop is not None:
                row.update(
                    outcome="provider_error",
                    error_stage="generation",
                    error_category="skipped",
                    error_message=SKIPPED,
                )
                continue
            ranking, retrieval_ms = rankings[mode]
            started = time.perf_counter()
            excerpts = build_context(
                ranking.results, max_chars=self.settings.chat_context_max_chars
            )
            try:
                answer = self.chat.answer(question.question, excerpts, history)
            except ProviderError as exc:
                row.update(
                    outcome="provider_error",
                    error_stage="generation",
                    error_category=exc.category,
                    error_message=exc.message,
                )
                row["token_usage"] = {
                    **row["token_usage"],
                    "generation_requests": 0 if _refused(exc) else 1,
                }
                if isinstance(exc, STOPPING_ERRORS):
                    stop = exc.message
                continue
            row.update(
                _answer_fields(answer, keys, expected, not question.is_unanswerable),
                answer_latency_ms=embedding_ms + retrieval_ms + _ms(started),
            )
            row["details"] = {**_answer_details(answer), "excerpts": len(excerpts)}
            row["token_usage"] = {**row["token_usage"], **_generation_usage(answer)}
        return QuestionOutcome(rows, stop)

    # --- writes -----------------------------------------------------------------------

    def _save(
        self, db: Session, run: ClaimedRun, question: EvaluationQuestion, outcome: QuestionOutcome
    ) -> None:
        """Store the question's results and progress, if this worker still owns the run."""
        row = self._owned(db, run, lock=True)
        if row is None:
            db.rollback()
            logger.warning("Evaluation run %s is no longer ours; results discarded", run.id)
            return
        db.add_all(
            EvaluationResult(workspace_id=run.workspace_id, run_id=run.id, **values)
            for values in outcome.rows
        )
        row.questions_done += 1
        row.attempts = 0
        db.flush()
        logger.info(
            "Evaluation run=%s question=%s (%d/%d) workspace=%s",
            run.id,
            question.question_key,
            row.questions_done,
            row.questions_total,
            run.workspace_id,
        )
        if outcome.stop is not None:
            self._finish(db, run, status="failed", message=outcome.stop, row=row)
        elif row.questions_done >= row.questions_total:
            self._finish(db, run, status="succeeded", row=row)
        else:
            row.lock_token = row.locked_at = row.locked_by = None
            # The database clock, the one `claim` compares against (as in the job queue).
            row.next_run_at = func.now() + timedelta(
                seconds=self.settings.evaluation_question_interval_seconds
            )
            db.commit()

    def _finish(
        self,
        db: Session,
        run: ClaimedRun,
        *,
        status: str,
        message: str | None = None,
        row: EvaluationRun | None = None,
    ) -> None:
        """End the run, store its summary, and give back the budget it didn't use."""
        row = row or self._owned(db, run, lock=True)
        if row is None:
            db.rollback()
            return
        summary = run_summary(row, run_questions(db, row), run_results(db, row))
        row.summary = summary
        row.status = status
        row.error_message = message
        row.finished_at = datetime.now(UTC)
        row.lock_token = row.locked_at = row.locked_by = None
        reservation = dict(row.config.get("budget", {}))
        db.commit()
        logger.info(
            "Evaluation run %s %s (%d/%d questions)%s",
            run.id,
            status,
            row.questions_done,
            row.questions_total,
            f": {message}" if message else "",
        )
        used = summary["gemini_requests"]
        budget.give_back_unused(self.limiter, self.settings, reservation, used)


def _source(chunk_source_id: uuid.UUID, keys: dict[str, str], name: str) -> SourceRef:
    source_id = str(chunk_source_id)
    return SourceRef(source_id, keys.get(source_id), name)


def _retrieval_fields(
    ranking: Ranking, keys: dict[str, str], expected: list[str]
) -> dict[str, Any]:
    refs = [
        _source(r.hit.source_id, keys, r.hit.filename or r.hit.source_name) for r in ranking.results
    ]
    ranked = rank_sources(refs)
    score = retrieval_score(ranked, expected)
    return {
        "retrieved": [
            {
                "rank": r.rank,
                "chunk_id": str(r.hit.chunk_id),
                "chunk_index": r.hit.chunk_index,
                "source_id": ref.id,
                "source_key": ref.key,
                "name": ref.name,
                "heading": r.hit.heading,
                "score": r.score,
                "vector_rank": r.vector_rank,
                "vector_distance": r.vector_distance,
                "lexical_rank": r.lexical_rank,
                "lexical_score": r.lexical_score,
            }
            for r, ref in zip(ranking.results, refs, strict=True)
        ],
        "retrieved_sources": [
            {
                "rank": s.rank,
                "source_key": s.source.key,
                "name": s.source.name,
                "relevant": s.source.key in expected,
                "chunk_ranks": list(s.chunk_ranks),
            }
            for s in ranked
        ],
        "vector_candidates": len(ranking.vector_hits),
        "top_vector_distance": ranking.vector_hits[0].score if ranking.vector_hits else None,
        "hit_at_5": score.hit if score else None,
        "recall_at_5": score.recall if score else None,
        "reciprocal_rank": score.reciprocal_rank if score else None,
    }


def _answer_fields(
    answer: Answer, keys: dict[str, str], expected: list[str], answerable: bool
) -> dict[str, Any]:
    citations = [{**c, "source_key": keys.get(c["source_id"])} for c in answer.citations]
    cited = [
        _source(uuid.UUID(c["source_id"]), keys, c["filename"] or c["source_name"])
        for c in citations
    ]
    score = citation_score(cited, expected) if answer.outcome == "answered" else None
    return {
        "outcome": answer.outcome,
        "abstain_reason": answer.reason,
        "answer": answer.content,
        "citations": citations,
        "citation_precision": score.precision if score else None,
        "citation_recall": score.recall if score else None,
        "abstention_correct": abstention_correct(answer.outcome, answerable),
    }


def _answer_details(answer: Answer) -> dict[str, Any]:
    details = dict(answer.details)
    if answer.outcome == "abstained" and answer.result is not None:
        # Withheld from chat users; kept here so a reviewer can see why.
        details["model_text"] = answer.result.text
    return details


def _generation_usage(answer: Answer) -> dict[str, Any]:
    result = answer.result
    if result is None:
        return {"generation_requests": 0}
    return {
        "generation_requests": 1,
        "model": result.model,
        "prompt_tokens": result.usage.prompt_tokens,
        "output_tokens": result.usage.output_tokens,
        "thinking_tokens": result.usage.thinking_tokens,
        "total_tokens": result.usage.total_tokens,
        "generation_latency_ms": result.latency_ms,
        "generation_attempts": result.attempts,
    }
