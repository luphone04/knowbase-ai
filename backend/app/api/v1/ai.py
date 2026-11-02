"""AI configuration status for the dashboard (SPEC.md §12.1, §17.8).

It reports whether a key is configured, never what it is, plus non-secret
model settings and the last outcome this API process saw. It never calls
Gemini, so loading the dashboard costs no quota.
"""

from datetime import datetime
from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.dependencies import CurrentAuth, SettingsDep
from app.providers.errors import QUOTA_CATEGORIES
from app.providers.health import provider_health
from app.providers.interfaces import DOCUMENT_TASK, QUERY_TASK

router = APIRouter(prefix="/ai", tags=["ai"])

AIStatus = Literal["configured", "missing_key", "quota_error"]

_MESSAGES: dict[AIStatus, str] = {
    "configured": (
        "A Gemini API key is configured. This check does not call Gemini; "
        "live access is confirmed by real requests and the smoke test."
    ),
    "missing_key": (
        "GEMINI_API_KEY is not set in the backend .env. AI features are unavailable; "
        "everything else works."
    ),
    "quota_error": (
        "The most recent Gemini request hit the free-tier rate limit or daily quota. "
        "Try again later. No paid model or tier is used instead."
    ),
}


class LastError(BaseModel):
    category: str
    at: datetime


class AIStatusOut(BaseModel):
    provider: Literal["gemini"] = "gemini"
    status: AIStatus
    configured: bool
    message: str
    chat_model: str
    thinking_level: str
    max_output_tokens: int
    embedding_model: str
    embedding_dimensions: int
    embedding_batch_size: int
    document_task_type: str = DOCUMENT_TASK
    query_task_type: str = QUERY_TASK
    max_retries: int
    paid_fallback: Literal[False] = False
    last_error: LastError | None
    last_success_at: datetime | None


@router.get("/status")
def ai_status(_: CurrentAuth, settings: SettingsDep) -> AIStatusOut:
    health = provider_health.snapshot()
    status: AIStatus
    if not settings.gemini_configured:
        status = "missing_key"
    elif health.last_error_category in QUOTA_CATEGORIES:
        status = "quota_error"
    else:
        status = "configured"

    last_error = None
    if health.last_error_category is not None and health.last_error_at is not None:
        last_error = LastError(category=health.last_error_category, at=health.last_error_at)

    return AIStatusOut(
        status=status,
        configured=settings.gemini_configured,
        message=_MESSAGES[status],
        chat_model=settings.gemini_chat_model,
        thinking_level=settings.gemini_thinking_level,
        max_output_tokens=settings.gemini_max_output_tokens,
        embedding_model=settings.gemini_embedding_model,
        embedding_dimensions=settings.embedding_dimensions,
        embedding_batch_size=settings.gemini_embedding_batch_size,
        max_retries=settings.gemini_max_retries,
        last_error=last_error,
        last_success_at=health.last_success_at,
    )
