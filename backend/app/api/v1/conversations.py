"""Conversations and grounded chat (SPEC.md §10, §11 "Conversations").

A conversation belongs to one user in one workspace. Another user's id, in this
workspace or any other, gets the same 404 as a random id. Each question:

1. needs a configured Gemini key (503 otherwise, before any slot is spent);
2. takes a daily chat slot for the user (DAILY_CHAT_REQUEST_LIMIT) and an
   installation-wide query-embedding slot, unless nothing is indexed and so no
   Gemini call will be made;
3. retrieves fresh evidence, then commits the trace, so no database connection
   is held while Gemini generates;
4. answers with validated citations or abstains, and saves the question and
   the reply together (201).

A provider failure is an error response (429 quota, 503 unavailable, 502 for an
incomplete or invalid response), never an answer or an abstention; that turn is
not saved, and its trace records the failure. Slots are given back when Gemini
refused a call outright.
"""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.api.dependencies import AuthContext, CurrentAuth, DbSession, RateLimiterDep, SettingsDep
from app.api.v1.gemini_calls import (
    DAY_SECONDS,
    INSTALLATION,
    DailySlots,
    provider_failure,
    query_embedding_rule,
)
from app.api.v1.schemas import (
    ChatTurnOut,
    CitationOut,
    ConversationCreateIn,
    ConversationDetailOut,
    ConversationOut,
    ConversationUsageOut,
    MessageIn,
    MessageOut,
    TurnRetrievalOut,
)
from app.auth.rate_limit import RateLimitExceeded, RateLimitRule
from app.chat.service import DEFAULT_TITLE, ChatService, ConversationGone
from app.chat.usage import conversation_usage, message_usage
from app.config import Settings, get_settings
from app.core.errors import AppError
from app.db.models import Conversation, Message, QueryTrace
from app.providers import factory
from app.providers.errors import REFUSED_BEFORE_COUNTING, ProviderError, ProviderNotConfigured
from app.retrieval.service import Retriever

router = APIRouter(prefix="/conversations", tags=["conversations"])

LIST_LIMIT = 100


class ConversationNotFound(AppError):
    def __init__(self) -> None:
        super().__init__(404, "not_found", "Conversation not found.")


def get_chat_service(settings: SettingsDep) -> ChatService:
    return ChatService(
        settings,
        Retriever(settings, factory.get_embedding_provider),
        factory.get_generation_provider,
    )


ChatServiceDep = Annotated[ChatService, Depends(get_chat_service)]


def chat_rule(settings: Settings) -> RateLimitRule:
    return RateLimitRule("chat-requests-daily", settings.daily_chat_request_limit, DAY_SECONDS)


def _owned(db: DbSession, conversation_id: uuid.UUID, auth: AuthContext) -> Conversation:
    # The session filters by workspace; the user filter keeps members out of each other's chats.
    conversation = db.scalar(
        select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == auth.user_id
        )
    )
    if conversation is None:
        raise ConversationNotFound()
    return conversation


def conversation_out(conversation: Conversation) -> ConversationOut:
    return ConversationOut(
        id=conversation.id,
        title=conversation.title,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
    )


def message_out(message: Message) -> MessageOut:
    citations: list[dict[str, Any]] = message.citations or []
    settings = get_settings()
    usage = (
        message_usage(
            message.token_usage,
            embedding_model=settings.gemini_embedding_model,
            chat_model=settings.gemini_chat_model,
        )
        if message.role == "assistant"
        else None
    )
    return MessageOut(
        id=message.id,
        position=message.position,
        role=message.role,
        content=message.content,
        outcome=message.outcome,
        abstain_reason=message.abstain_reason,
        citations=[CitationOut.model_validate(c) for c in citations],
        token_usage=message.token_usage,
        usage=usage,
        latency_ms=message.latency_ms,
        metadata=message.meta,
        created_at=message.created_at,
    )


@router.get("")
def list_conversations(auth: CurrentAuth, db: DbSession) -> list[ConversationOut]:
    """The caller's conversations in this workspace, most recently active first."""
    conversations = db.scalars(
        select(Conversation)
        .where(Conversation.user_id == auth.user_id)
        .order_by(Conversation.updated_at.desc(), Conversation.id)
        .limit(LIST_LIMIT)
    )
    return [conversation_out(c) for c in conversations]


@router.post("", status_code=201)
def create_conversation(
    body: ConversationCreateIn, auth: CurrentAuth, db: DbSession
) -> ConversationOut:
    """A new, empty conversation. Without a title, the first question becomes its title."""
    conversation = Conversation(
        workspace_id=auth.workspace_id, user_id=auth.user_id, title=body.title or DEFAULT_TITLE
    )
    db.add(conversation)
    db.commit()
    db.refresh(conversation)
    return conversation_out(conversation)


@router.get("/{conversation_id:uuid}")
def get_conversation(
    conversation_id: uuid.UUID, auth: CurrentAuth, db: DbSession
) -> ConversationDetailOut:
    """The conversation and all of its messages, in order."""
    conversation = _owned(db, conversation_id, auth)
    messages = db.scalars(
        select(Message).where(Message.conversation_id == conversation.id).order_by(Message.position)
    )
    return ConversationDetailOut(
        **conversation_out(conversation).model_dump(),
        messages=[message_out(m) for m in messages],
    )


@router.get("/{conversation_id:uuid}/usage")
def get_conversation_usage(
    conversation_id: uuid.UUID, auth: CurrentAuth, db: DbSession, settings: SettingsDep
) -> ConversationUsageOut:
    """Tokens and estimated cost of the whole conversation, from its stored usage records.

    Covers every saved reply and every question that failed at Gemini (its trace
    recorded the calls; the turn itself wasn't saved). No Gemini call is made.
    """
    conversation = _owned(db, conversation_id, auth)
    replies = db.scalars(
        select(Message.token_usage)
        .where(Message.conversation_id == conversation.id, Message.role == "assistant")
        .order_by(Message.position)
    ).all()
    failed = db.scalars(
        select(QueryTrace.token_usage)
        .where(
            QueryTrace.conversation_id == conversation.id,
            QueryTrace.message_id.is_(None),
            QueryTrace.token_usage.has_key("generation_error"),
        )
        .order_by(QueryTrace.created_at)
    ).all()
    totals = conversation_usage(
        replies,
        failed,
        embedding_model=settings.gemini_embedding_model,
        chat_model=settings.gemini_chat_model,
    )
    return ConversationUsageOut(conversation_id=conversation.id, **totals)


@router.post("/{conversation_id:uuid}/messages", status_code=201)
def post_message(
    conversation_id: uuid.UUID,
    body: MessageIn,
    auth: CurrentAuth,
    db: DbSession,
    settings: SettingsDep,
    limiter: RateLimiterDep,
    chat: ChatServiceDep,
) -> ChatTurnOut:
    """Ask a question: retrieve, generate, validate citations, save the turn and its trace."""
    conversation = _owned(db, conversation_id, auth)
    if not settings.gemini_configured:
        raise provider_failure(ProviderNotConfigured())  # before spending a daily slot
    workspace_id, user_id = auth.workspace_id, auth.user_id
    embed = chat.retriever.has_indexed_chunks(db, workspace_id=workspace_id)
    history = chat.recent_history(db, conversation.id)
    previous_question = chat.previous_question(db, conversation.id)
    db.commit()  # end the read: nothing is held while Gemini embeds the question

    chat_slot = DailySlots(limiter, [(chat_rule(settings), str(user_id))])
    embedding_slot = DailySlots(limiter, [(query_embedding_rule(settings), INSTALLATION)])
    if embed:
        chat_slot.take()
        try:
            embedding_slot.take()
        except RateLimitExceeded:
            chat_slot.give_back()
            raise
    try:
        prepared = chat.prepare(
            db,
            workspace_id=workspace_id,
            conversation_id=conversation.id,
            question=body.content,
            mode=body.mode,
            previous_question=previous_question,
            history=history,
            embed=embed,
        )
    except ProviderError as exc:
        if isinstance(exc, REFUSED_BEFORE_COUNTING):
            chat_slot.give_back()  # Gemini refused it, so it cost nothing
            embedding_slot.give_back()
        raise provider_failure(exc) from exc
    db.commit()  # the trace; nothing is held while Gemini generates

    try:
        turn = chat.respond(db, prepared, conversation_id=conversation.id)
    except ProviderError as exc:
        db.rollback()
        if isinstance(exc, REFUSED_BEFORE_COUNTING):
            chat_slot.give_back()  # the query embedding did count, so its slot stays
        chat.record_failure(db, prepared, exc)
        raise provider_failure(exc) from exc
    except ConversationGone as exc:
        db.rollback()
        raise ConversationNotFound() from exc
    retrieval = turn.retrieval
    return ChatTurnOut(
        conversation=conversation_out(turn.conversation),
        user_message=message_out(turn.user_message),
        assistant_message=message_out(turn.assistant_message),
        retrieval=TurnRetrievalOut(
            mode=retrieval.mode,
            retrieval_query=retrieval.retrieval_query,
            contextualized=retrieval.contextualized,
            trace_id=retrieval.trace_id,
            results=len(retrieval.results),
        ),
    )
