"""One chat turn: fresh retrieval, grounded generation, validation, persistence.

    question (+ previous user question for a follow-up, SPEC.md §9.5)
        -> retrieve: query embedding, vector/hybrid search, trace     (phase 1)
        -> context: top excerpts S1..Sn, deduplicated, within budget
        -> no usable excerpt: abstain (no_evidence), no generation call
        -> generate: one Gemini request with the bounded history      (phase 2)
        -> validate citations; no valid citation: abstain
        -> save the question and the answer or abstention together

The two phases are separate calls so the route can commit in between: no
database connection or transaction is open while Gemini generates. A provider
failure is never turned into an answer or an abstention. It propagates, the
turn is not saved, and the trace records the failure (`record_failure`).

Abstention reasons (stored on the assistant message):
- `no_evidence`: nothing usable was retrieved (for example, an empty workspace);
- `model_insufficient`: the model said the excerpts were not enough;
- `no_valid_citation`: the model answered without citing any given excerpt, so
  its text is not shown (it might be general knowledge or invention).
"""

import logging
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.chat.citations import check_answer, is_abstention
from app.chat.context import SourceExcerpt, build_context
from app.chat.prompt import ABSTENTION, SYSTEM_INSTRUCTION, HistoryMessage, build_prompt
from app.config import Settings
from app.db.models import Conversation, Message, QueryTrace
from app.providers.errors import ProviderError
from app.providers.interfaces import GenerationProvider, GenerationResult
from app.retrieval.service import RetrievalMode, RetrievalResult, Retriever

logger = logging.getLogger(__name__)

DEFAULT_TITLE = "New conversation"
TITLE_CHARS = 80


class ConversationGone(Exception):
    """The conversation was removed while its turn was being answered."""


@dataclass(frozen=True)
class PreparedTurn:
    question: str
    retrieval: RetrievalResult
    excerpts: list[SourceExcerpt]
    history: list[HistoryMessage]
    started: float  # perf_counter at the start of the turn


@dataclass(frozen=True)
class Answer:
    outcome: str  # "answered" or "abstained"
    reason: str | None  # the abstention reason
    content: str  # the validated answer, or the abstention text
    citations: list[dict[str, Any]]  # citation_record() of each cited excerpt
    details: dict[str, Any]  # invalid citation ids and removed URLs
    result: GenerationResult | None  # None when no generation call was made


@dataclass(frozen=True)
class ChatTurn:
    conversation: Conversation
    user_message: Message
    assistant_message: Message
    retrieval: RetrievalResult


def citation_record(excerpt: SourceExcerpt) -> dict[str, Any]:
    """What a stored citation holds: where the excerpt came from, and the excerpt itself."""
    return {
        "id": excerpt.citation_id,
        "rank": excerpt.rank,
        "chunk_id": str(excerpt.chunk_id),
        "chunk_index": excerpt.chunk_index,
        "document_id": str(excerpt.document_id),
        "document_title": excerpt.document_title,
        "source_id": str(excerpt.source_id),
        "source_name": excerpt.source_name,
        "filename": excerpt.filename,
        "heading": excerpt.heading,
        "page_start": excerpt.page_start,
        "page_end": excerpt.page_end,
        "excerpt": excerpt.text,
    }


def _usage(result: GenerationResult | None, query_embeddings: int) -> dict[str, Any]:
    usage: dict[str, Any] = {"query_embeddings": query_embeddings}
    if result is None:
        usage["generation_requests"] = 0
        return usage
    usage.update(
        generation_requests=1,
        model=result.model,
        prompt_tokens=result.usage.prompt_tokens,
        output_tokens=result.usage.output_tokens,
        thinking_tokens=result.usage.thinking_tokens,
        total_tokens=result.usage.total_tokens,
        generation_latency_ms=result.latency_ms,
        generation_attempts=result.attempts,
    )
    return usage


class ChatService:
    def __init__(
        self,
        settings: Settings,
        retriever: Retriever,
        generation_provider_factory: Callable[[], GenerationProvider],
    ) -> None:
        self.settings = settings
        self.retriever = retriever
        self._generation_factory = generation_provider_factory

    # --- reads --------------------------------------------------------------------------

    def recent_history(self, db: Session, conversation_id: uuid.UUID) -> list[HistoryMessage]:
        """The last CHAT_HISTORY_MESSAGES messages, oldest first, as plain values."""
        limit = self.settings.chat_history_messages
        if limit == 0:
            return []
        rows = db.execute(
            select(Message.role, Message.content)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.position.desc())
            .limit(limit)
        ).all()
        return [HistoryMessage(role, content) for role, content in reversed(rows)]

    def previous_question(self, db: Session, conversation_id: uuid.UUID) -> str | None:
        content: str | None = db.scalar(
            select(Message.content)
            .where(Message.conversation_id == conversation_id, Message.role == "user")
            .order_by(Message.position.desc())
            .limit(1)
        )
        return content

    # --- phase 1: retrieval ------------------------------------------------------------------

    def prepare(
        self,
        db: Session,
        *,
        workspace_id: uuid.UUID,
        conversation_id: uuid.UUID,
        question: str,
        mode: RetrievalMode,
        previous_question: str | None,
        history: Sequence[HistoryMessage],
        embed: bool,
    ) -> PreparedTurn:
        """Retrieve fresh evidence and build the excerpts. The caller commits the trace."""
        started = time.perf_counter()
        retrieval = self.retriever.retrieve(
            db,
            workspace_id=workspace_id,
            question=question,
            mode=mode,
            previous_question=previous_question,
            embed=embed,
            conversation_id=conversation_id,
        )
        excerpts = build_context(retrieval.results, max_chars=self.settings.chat_context_max_chars)
        return PreparedTurn(question, retrieval, excerpts, list(history), started)

    # --- phase 2: answer and save ---------------------------------------------------------

    def answer(
        self, question: str, excerpts: Sequence[SourceExcerpt], history: Sequence[HistoryMessage]
    ) -> Answer:
        """Generate (unless there is no evidence) and validate citations. Saves nothing.

        Chat and the evaluation (ADR 0014) both answer through here, so they share
        the prompt, the model call, citation validation, and the abstention rules.
        A provider failure propagates.
        """
        if not excerpts:
            return Answer("abstained", "no_evidence", ABSTENTION, [], {}, None)
        prompt = build_prompt(question, excerpts, history)
        result = self._generation_factory().generate(prompt, system_instruction=SYSTEM_INSTRUCTION)
        checked = check_answer(
            result.text,
            valid_ids=[e.citation_id for e in excerpts],
            excerpt_text="\n".join(e.text for e in excerpts),
        )
        details: dict[str, Any] = {
            "invalid_citation_ids": checked.invalid_ids,
            "removed_urls": checked.removed_urls,
        }
        if checked.cited_ids:
            by_id = {e.citation_id: e for e in excerpts}
            citations = [citation_record(by_id[i]) for i in checked.cited_ids]
            return Answer("answered", None, checked.text, citations, details, result)
        reason = "model_insufficient" if is_abstention(result.text) else "no_valid_citation"
        return Answer("abstained", reason, ABSTENTION, [], details, result)

    def respond(
        self, db: Session, prepared: PreparedTurn, *, conversation_id: uuid.UUID
    ) -> ChatTurn:
        """Generate (unless there is no evidence), validate, and save the turn. Commits."""
        excerpts = prepared.excerpts
        answer = self.answer(prepared.question, excerpts, prepared.history)
        outcome, reason, content = answer.outcome, answer.reason, answer.content
        citations, details, result = answer.citations, answer.details, answer.result

        retrieval = prepared.retrieval
        trace = db.get(QueryTrace, retrieval.trace_id)
        query_embeddings = int(trace.token_usage.get("query_embeddings", 0)) if trace else 0
        usage = _usage(result, query_embeddings)
        latency_ms = round((time.perf_counter() - prepared.started) * 1000)

        conversation = db.execute(
            select(Conversation).where(Conversation.id == conversation_id).with_for_update()
        ).scalar_one_or_none()
        if conversation is None:
            raise ConversationGone()
        last = db.scalar(
            select(func.coalesce(func.max(Message.position), 0)).where(
                Message.conversation_id == conversation_id
            )
        )
        position = int(last or 0)
        user_message = Message(
            workspace_id=conversation.workspace_id,
            conversation_id=conversation_id,
            position=position + 1,
            role="user",
            content=prepared.question,
        )
        assistant_message = Message(
            workspace_id=conversation.workspace_id,
            conversation_id=conversation_id,
            position=position + 2,
            role="assistant",
            content=content,
            outcome=outcome,
            abstain_reason=reason,
            citations=citations,
            token_usage=usage,
            latency_ms=latency_ms,
            meta={
                "retrieval_mode": retrieval.mode,
                "retrieval_query": retrieval.retrieval_query,
                "contextualized": retrieval.contextualized,
                "trace_id": str(retrieval.trace_id),
                "excerpts": len(excerpts),
                **details,
            },
        )
        db.add_all([user_message, assistant_message])
        if position == 0 and conversation.title == DEFAULT_TITLE:
            conversation.title = _title(prepared.question)
        conversation.updated_at = func.now()
        db.flush()  # the messages exist before the trace points at one
        if trace is not None:
            trace.message_id = assistant_message.id
            trace.token_usage = {**trace.token_usage, **usage}
        db.commit()
        db.refresh(conversation)
        logger.info(
            "Chat turn workspace=%s conversation=%s outcome=%s reason=%s excerpts=%d "
            "citations=%d invalid_citations=%d mode=%s latency_ms=%d trace=%s",
            conversation.workspace_id,
            conversation_id,
            outcome,
            reason or "-",
            len(excerpts),
            len(citations),
            len(details.get("invalid_citation_ids", [])),
            retrieval.mode,
            latency_ms,
            retrieval.trace_id,
        )
        return ChatTurn(conversation, user_message, assistant_message, retrieval)

    def record_failure(self, db: Session, prepared: PreparedTurn, error: ProviderError) -> None:
        """Note on the turn's trace that generation failed. The turn itself is not saved."""
        trace = db.get(QueryTrace, prepared.retrieval.trace_id)
        if trace is not None:
            trace.token_usage = {
                **trace.token_usage,
                "generation_requests": 1,
                "generation_error": error.category,
            }
            db.commit()


def _title(question: str) -> str:
    title = " ".join(question.split())
    return title if len(title) <= TITLE_CHARS else title[: TITLE_CHARS - 1].rstrip() + "…"
