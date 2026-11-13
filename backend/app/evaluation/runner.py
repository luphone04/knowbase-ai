"""The evaluation runner: a second durable queue in the worker (ADR 0014).

`evaluation_runs` rows are claimed like ingestion jobs: `FOR UPDATE SKIP
LOCKED`, a fresh fencing token per claim, and a lease renewed by a heartbeat.
Each claim evaluates **one question** and commits its results together with
the run's progress, then releases the run with `next_run_at` a little later:

- ingestion jobs run between questions (the worker checks them first);
- the pause paces Gemini requests (EVALUATION_QUESTION_INTERVAL_SECONDS);
- a crash loses at most the question in progress, which the next claim redoes.
  `attempts` counts claims since the last finished question, so a question that
  keeps killing the worker fails the run instead of looping. A claim with
  `attempts > 1` is a retry: the earlier attempt may already have called Gemini,
  so before any call the retry takes an additional retry reservation (budget.py);
  if today's budget can't cover it, the run stops with its completed results.

The run's dataset, question selection (e.g. the development split only), and
passage mapping are all read from the run itself, never from the current
configuration, so a restarted worker continues exactly the run that was started.

One question, every configuration: the retrieval query (with the previous
question for a follow-up, exactly as chat builds it) is embedded once; each
configuration then ranks with that embedding; each answer mode builds the
context and answers through `ChatService.answer`, the code chat uses. Nothing
is written to conversations, and no database connection is held while Gemini
works.

Passage-level runs (Asia Cement) also record retrieval diagnostics
(diagnostics.py): deeper candidate lists, hypothetical top-K analyses, and for a
follow-up one extra query embedding of the follow-up alone. They come from
separate searches after the production-equivalent rankings and never change them.

Provider failures are results, not abstentions. A quota or rate-limit
refusal, a missing key, or a rejected key stops the run (partial results are
kept); other failures (5xx, timeouts, invalid responses) are recorded on that
question and the run continues. If the workspace's documents change during a
run, it stops, because its results would mix two corpora.
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

from app.auth.rate_limit import RateLimiter, RateLimitExceeded
from app.chat.context import build_context
from app.chat.prompt import HistoryMessage
from app.chat.service import Answer, ChatService
from app.config import Settings
from app.db.models import EvaluationQuestion, EvaluationResult, EvaluationRun
from app.db.tenancy import SKIP_WORKSPACE_SCOPE, bind_workspace
from app.evaluation import budget, diagnostics
from app.evaluation.metrics import (
    SourceRef,
    abstention_correct,
    citation_score,
    passage_score,
    rank_sources,
    retrieval_score,
)
from app.evaluation.passages import evidence_groups
from app.evaluation.service import corpus_snapshot, run_questions, run_results, run_summary
from app.evaluation.variants import BY_KEY
from app.providers.errors import (
    REFUSED_BEFORE_COUNTING,
    IncompleteGeneration,
    ProviderConfigurationError,
    ProviderError,
    ProviderNotConfigured,
    ProviderQuotaExhausted,
    ProviderRateLimited,
)
from app.providers.interfaces import (
    EmbeddingProvider,
    EmbeddingResult,
    GenerationProvider,
    TokenUsage,
)
from app.retrieval.followup import contextualize
from app.retrieval.search import lexical_search, vector_search
from app.retrieval.service import Ranking, Retriever
from app.worker.lease import Deadline, LeaseHeartbeat

logger = logging.getLogger(__name__)

# The run stops instead of spending more: a missing or rejected key fails every later call;
# a quota or rate-limit refusal means Gemini wants fewer requests, and pushing on would
# only consume quota trying to force completion (owner decision, 2026-11-04).
STOPPING_ERRORS: tuple[type[ProviderError], ...] = (
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderNotConfigured,
    ProviderConfigurationError,
)
CORPUS_CHANGED = (
    "The workspace's documents changed during the evaluation (an upload, re-index, or "
    "delete), so its results would mix two corpora. Start a new run."
)
ABANDONED = "The worker stopped repeatedly while evaluating question {key}; the run was stopped."
SKIPPED = "Not attempted: the run stopped at an earlier request."
RETRY_BUDGET = (
    "Question {key} had to be retried after an interrupted attempt, and today's Gemini budget "
    "has no room for the retry; the run was stopped. Completed results are kept."
)


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


def embeddings_per_attempt(config: dict[str, Any], question: EvaluationQuestion) -> int:
    """Query embeddings one attempt at a question may spend (a diagnosed follow-up: two)."""
    extra = config.get("diagnostics", {}).get("follow_up_alone") and question.previous_question
    return 2 if extra else 1


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
            # Terminal checks first: a claim that won't call Gemini reserves nothing.
            if run.attempts > self.settings.job_max_attempts:
                self._finish(
                    db,
                    run,
                    status="failed",
                    message=ABANDONED.format(key=question.question_key),
                    interrupted=True,
                    embeddings=embeddings_per_attempt(row.config, question),
                )
                return
            snapshot = corpus_snapshot(
                db,
                workspace_id=run.workspace_id,
                source_hashes=row.corpus.get("source_hashes", {}),
                embedding_model=self.settings.gemini_embedding_model,
                source_files=row.corpus.get("source_files"),
            )
            if snapshot.fingerprint != row.corpus.get("fingerprint"):
                self._finish(
                    db,
                    run,
                    status="failed",
                    message=CORPUS_CHANGED,
                    interrupted=run.attempts > 1,
                    embeddings=embeddings_per_attempt(row.config, question),
                )
                return
            config, corpus = dict(row.config), dict(row.corpus)
            if run.attempts > 1 and not self._reserve_retry(db, run, question, config):
                return
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
        # Passage relevance: each evidence passage's chunks, as mapped when the run started.
        evidence = (
            evidence_groups(corpus.get("passages", {}).get(question.question_key))
            if config.get("relevance") == "passage"
            else None
        )
        passage = config.get("relevance") == "passage"
        variants = [BY_KEY[key] for key in config["variants"]]
        base = {"question_id": question.id, "retrieval_query": query}

        started = time.perf_counter()
        try:
            embedded = self.retriever.embed_query_result(query)
        except ProviderError as exc:
            spent = 0 if _refused(exc) else 1
            # Shared by every configuration: recorded on the vector row only (usage.py).
            embedding_usage = {
                "query_embeddings": spent,
                "embedding_model": self.settings.gemini_embedding_model,
            }
            # Time until the failure, kept apart from successful embedding latency.
            failed_ms = _ms(started)
            failed: list[dict[str, Any]] = [
                {
                    **base,
                    "variant": v.key,
                    "retrieval_mode": v.mode,
                    "details": {"failed_embedding_ms": failed_ms}
                    | ({"failed_attempt_ms": failed_ms} if v.key in config["answer_modes"] else {}),
                    "answer_evaluated": v.key in config["answer_modes"],
                    "outcome": "provider_error" if v.key in config["answer_modes"] else None,
                    "error_stage": "embedding",
                    "error_category": exc.category,
                    "error_message": exc.message,
                    "token_usage": embedding_usage if v.key == "vector" else {},
                }
                for v in variants
            ]
            return QuestionOutcome(
                failed, exc.message if isinstance(exc, STOPPING_ERRORS) else None
            )
        embedding_ms = _ms(started)
        vector = embedded.vectors[0]

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
        diagnosed = (
            self._diagnose(db, run, query, vector, variants, rankings, config, corpus, question)
            if config.get("diagnostics")
            else {}
        )
        db.commit()  # end the search transaction before any generation

        rows: list[dict[str, Any]] = []
        for variant in variants:
            ranking, retrieval_ms = rankings[variant.key]
            rows.append(
                {
                    **base,
                    **_retrieval_fields(
                        ranking, keys, expected, passage=passage, evidence=evidence
                    ),
                    "variant": variant.key,
                    "retrieval_mode": variant.mode,
                    "lexical_candidates": len(ranking.lexical_hits)
                    if variant.mode == "hybrid"
                    else None,
                    "embedding_latency_ms": embedding_ms,
                    "retrieval_latency_ms": retrieval_ms,
                    # One embedding, shared: its usage goes on the vector row only (usage.py).
                    "token_usage": _embedding_usage(embedded) if variant.key == "vector" else {},
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
            generation_started = time.perf_counter()
            try:
                answer = self.chat.answer(question.question, excerpts, history)
            except ProviderError as exc:
                row.update(
                    outcome="provider_error",
                    error_stage="generation",
                    error_category=exc.category,
                    error_message=exc.message,
                )
                # Time until the failure; answer_latency_ms stays for successful answers only.
                row["details"] = {
                    "failed_attempt_ms": embedding_ms + retrieval_ms + _ms(started),
                    "failed_generation_ms": _ms(generation_started),
                }
                requests = 0 if _refused(exc) else 1
                row["token_usage"] = {**row["token_usage"], "generation_requests": requests}
                if requests:
                    row["token_usage"]["model"] = self.settings.gemini_chat_model
                if isinstance(exc, IncompleteGeneration):
                    # Gemini answered (and counted the tokens), just not completely.
                    row["token_usage"].update(_token_fields(exc.usage))
                if isinstance(exc, STOPPING_ERRORS):
                    stop = exc.message
                continue
            row.update(
                _answer_fields(answer, keys, expected, not question.is_unanswerable),
                answer_latency_ms=embedding_ms + retrieval_ms + _ms(started),
            )
            row["details"] = {**_answer_details(answer), "excerpts": len(excerpts)}
            row["token_usage"] = {**row["token_usage"], **_generation_usage(answer)}
        if diagnosed:
            follow_up = None
            if config["diagnostics"].get("follow_up_alone") and question.previous_question:
                follow_up, stop = self._follow_up_alone(
                    db, run, question, variants, config, corpus, by_variant, stop
                )
            for row in rows:
                extra = (
                    {"follow_up_alone": follow_up}
                    if row["variant"] == "vector" and follow_up is not None
                    else {}
                )
                row["details"] = {
                    **row.get("details", {}),
                    "diagnostics": {**diagnosed[row["variant"]], **extra},
                }
        return QuestionOutcome(rows, stop)

    # --- diagnostics (passage-level runs only; never change the results above) ------------

    def _analyses(
        self,
        db: Session,
        run: ClaimedRun,
        query: str,
        vector: list[float],
        variants: list[Any],
        rankings: dict[str, Ranking],
        config: dict[str, Any],
        evidence: list[dict[str, Any]] | None,
    ) -> dict[str, dict[str, Any]]:
        """Deeper candidate lists and top-K analyses per configuration (read-only searches)."""
        depth = int(config["diagnostics"].get("depth", diagnostics.DEPTH))
        deep_vector = vector_search(
            db,
            workspace_id=run.workspace_id,
            query_vector=vector,
            model=self.settings.gemini_embedding_model,
            limit=depth,
        )
        top_n = str(config.get("top_n", 5))
        settings = {
            "rrf_k": int(config.get("rrf_k", self.settings.rrf_k)),
            "max_chars": int(
                config.get("chat_context_max_chars", self.settings.chat_context_max_chars)
            ),
        }
        out: dict[str, dict[str, Any]] = {}
        for variant in variants:
            ranking = rankings[variant.key]
            deep_lexical = (
                lexical_search(
                    db,
                    workspace_id=run.workspace_id,
                    query=query,
                    limit=depth,
                    config=variant.lexical.config,
                    match=variant.lexical.match,
                )
                if variant.mode == "hybrid"
                else []
            )
            original = diagnostics.analyse(
                variant.mode,
                ranking.vector_hits,
                ranking.lexical_hits,
                evidence=evidence,
                **settings,
            )
            production = [str(r.hit.chunk_id) for r in ranking.results]
            out[variant.key] = {
                "depth": depth,
                "candidates": len(ranking.vector_hits),  # the original candidate pool per list
                "lexical": diagnostics.candidates(deep_lexical)
                if variant.mode == "hybrid"
                else None,
                "lexical_matches_at_depth": len(deep_lexical) if variant.mode == "hybrid" else None,
                "original": original,
                # K = the run's top N, recomputed from the original lists: must equal production.
                "matches_production": original.get(top_n, {}).get("chunk_ids") == production
                if top_n in original
                else None,
                "pool20": diagnostics.analyse(
                    variant.mode, deep_vector, deep_lexical, evidence=evidence, **settings
                ),
            }
        out["vector"]["vector"] = diagnostics.candidates(deep_vector)
        return out

    def _diagnose(
        self,
        db: Session,
        run: ClaimedRun,
        query: str,
        vector: list[float],
        variants: list[Any],
        rankings: dict[str, tuple[Ranking, int]],
        config: dict[str, Any],
        corpus: dict[str, Any],
        question: EvaluationQuestion,
    ) -> dict[str, dict[str, Any]]:
        evidence = corpus.get("passages", {}).get(question.question_key, {}).get("evidence")
        return self._analyses(
            db,
            run,
            query,
            vector,
            variants,
            {key: ranking for key, (ranking, _) in rankings.items()},
            config,
            evidence,
        )

    def _follow_up_alone(
        self,
        db: Session,
        run: ClaimedRun,
        question: EvaluationQuestion,
        variants: list[Any],
        config: dict[str, Any],
        corpus: dict[str, Any],
        by_variant: dict[str, dict[str, Any]],
        stop: str | None,
    ) -> tuple[dict[str, Any], str | None]:
        """The follow-up question on its own: experimental, never scored as production.

        One extra query embedding, counted on the vector row like the shared one. Skipped
        when the run is already stopping (Gemini wants fewer requests).
        """
        alone = question.question
        if stop is not None:
            return {"query": alone, "skipped": "the run is stopping"}, stop
        usage = by_variant["vector"]["token_usage"]
        try:
            embedded = self.retriever.embed_query_result(alone)
        except ProviderError as exc:
            spent = 0 if _refused(exc) else 1
            usage["query_embeddings"] = int(usage.get("query_embeddings", 0)) + spent
            usage["diagnostic_query_embeddings"] = spent
            if isinstance(exc, STOPPING_ERRORS):
                stop = exc.message
            return {"query": alone, "error": exc.category}, stop
        usage["query_embeddings"] = int(usage.get("query_embeddings", 0)) + 1
        usage["embedding_attempts"] = int(usage.get("embedding_attempts", 0)) + embedded.attempts
        usage["diagnostic_query_embeddings"] = 1
        vector = embedded.vectors[0]
        rankings = {
            v.key: self.retriever.rank(
                db,
                workspace_id=run.workspace_id,
                query=alone,
                query_vector=vector,
                mode=v.mode,
                lexical=v.lexical,
            )
            for v in variants
        }
        evidence = corpus.get("passages", {}).get(question.question_key, {}).get("evidence")
        analyses = self._analyses(db, run, alone, vector, variants, rankings, config, evidence)
        db.commit()
        return {"query": alone, "variants": analyses}, stop

    def _reserve_retry(
        self, db: Session, run: ClaimedRun, question: EvaluationQuestion, config: dict[str, Any]
    ) -> bool:
        """Take an additional retry reservation before a retry calls Gemini. False: stop here."""
        embeddings = embeddings_per_attempt(config, question)
        try:
            retry = budget.reserve_retry(
                self.limiter,
                self.settings,
                generations=len(config["answer_modes"]),
                query_embeddings=embeddings,
            )
        except RateLimitExceeded:
            self._finish(
                db,
                run,
                status="failed",
                message=RETRY_BUDGET.format(key=question.question_key),
                interrupted=True,
                embeddings=embeddings,
            )
            return False
        recorded = dict(config.get("budget", {}))
        recorded["retry_reservations"] = [
            *recorded.get("retry_reservations", []),
            {**retry, "question": question.question_key, "attempt": run.attempts},
        ]
        updated = db.scalar(
            update(EvaluationRun)
            .where(
                EvaluationRun.id == run.id,
                EvaluationRun.lock_token == run.lock_token,
                EvaluationRun.status == "running",
            )
            .values(config={**config, "budget": recorded})
            .returning(EvaluationRun.id)
        )
        if updated is None:  # the run changed hands: this worker makes no call
            db.rollback()
            budget.release_retry(self.limiter, self.settings, retry)
            return False
        logger.warning(
            "Evaluation run %s retries question %s (attempt %d): additional retry reservation "
            "of %d query embedding(s) and %d generation(s)",
            run.id,
            question.question_key,
            run.attempts,
            retry["query_embeddings"],
            retry["generations"],
        )
        return True

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
        interrupted: bool = False,
        embeddings: int = 1,
    ) -> None:
        """End the run, store its summary, and give back the budget it didn't use.

        `interrupted`: the run ends on a question whose earlier attempt never saved. That
        attempt may have spent the question's slots from the original reservation, so one
        attempt's worth is held back instead of being given back as unused.
        """
        row = row or self._owned(db, run, lock=True)
        if row is None:
            db.rollback()
            return
        modes = len(row.config.get("answer_modes", []))
        held = {
            "query_embeddings": embeddings * interrupted,
            "generation_requests": modes * interrupted,
        }
        row.config = {
            **row.config,
            "budget": {**row.config.get("budget", {}), "held_from_original": held},
        }
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
        recorded = summary["gemini_requests"]
        used = {name: recorded.get(name, 0) + held[name] for name in held}
        budget.give_back_unused(self.limiter, self.settings, reservation, used)


def _source(chunk_source_id: uuid.UUID, keys: dict[str, str], name: str) -> SourceRef:
    source_id = str(chunk_source_id)
    return SourceRef(source_id, keys.get(source_id), name)


def _retrieval_fields(
    ranking: Ranking,
    keys: dict[str, str],
    expected: list[str],
    *,
    passage: bool = False,
    evidence: list[set[str]] | None = None,
) -> dict[str, Any]:
    """Retrieved chunks and sources, scored at source level, or at passage level when
    the run judges relevance by evidence passage (then `evidence` holds each passage's
    chunk ids, or is None when they couldn't all be mapped: no score, never a guess)."""
    refs = [
        _source(r.hit.source_id, keys, r.hit.filename or r.hit.source_name) for r in ranking.results
    ]
    ranked = rank_sources(refs)
    chunk_ids = [str(r.hit.chunk_id) for r in ranking.results]
    score = (
        passage_score(chunk_ids, evidence)
        if passage and expected
        else retrieval_score(ranked, expected)
    )
    holds_evidence = set().union(*evidence) if evidence else set()
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
                **({"evidence": str(r.hit.chunk_id) in holds_evidence} if passage else {}),
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


def _embedding_usage(result: EmbeddingResult) -> dict[str, Any]:
    """The question's query embedding. Tokens are None: Gemini embeddings report none."""
    return {
        "query_embeddings": 1,
        "embedding_model": result.model,
        "embedding_input_tokens": result.input_tokens,
        "embedding_attempts": result.attempts,
    }


def _token_fields(usage: TokenUsage) -> dict[str, Any]:
    """Reported generation tokens; None where Gemini reported nothing (usage.py)."""
    return {
        "prompt_tokens": usage.prompt_tokens,
        "output_tokens": usage.output_tokens,
        "thinking_tokens": usage.thinking_tokens,
        "total_tokens": usage.total_tokens,
        "cached_tokens": usage.cached_tokens,
    }


def _generation_usage(answer: Answer) -> dict[str, Any]:
    result = answer.result
    if result is None:
        return {"generation_requests": 0}
    return {
        "generation_requests": 1,
        "model": result.model,
        **_token_fields(result.usage),
        "generation_latency_ms": result.latency_ms,
        "generation_attempts": result.attempts,
    }
