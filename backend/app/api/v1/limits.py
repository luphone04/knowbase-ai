"""The caller's daily allowances: chat questions (per user) and searches (per workspace).

Read-only and Gemini-free. The numbers come from the same PostgreSQL counters the
routes enforce (app.auth.rate_limit), through the same rules, so reserved slots
count as used and slots given back (Gemini refused the call) count as left.
A caller sees only their own chat allowance and their workspace's search
allowance; nothing else is exposed. Windows are UTC days.
"""

from datetime import UTC, datetime

from fastapi import APIRouter
from pydantic import BaseModel

from app.api.dependencies import CurrentAuth, RateLimiterDep, SettingsDep
from app.api.v1.conversations import chat_rule
from app.api.v1.retrieval import retrieval_rule

router = APIRouter(prefix="/limits", tags=["limits"])


class AllowanceOut(BaseModel):
    limit: int
    remaining: int


class DailyLimitsOut(BaseModel):
    chat: AllowanceOut  # questions the caller can still ask today
    retrieval: AllowanceOut  # searches the caller's workspace can still run today
    resets_at: datetime  # the next UTC midnight, when both start again


@router.get("")
def daily_limits(
    auth: CurrentAuth, settings: SettingsDep, limiter: RateLimiterDep
) -> DailyLimitsOut:
    now = datetime.now(UTC)
    chat, retrieval = chat_rule(settings), retrieval_rule(settings)
    return DailyLimitsOut(
        chat=AllowanceOut(
            limit=chat.limit, remaining=limiter.remaining(chat, str(auth.user_id), now=now)
        ),
        retrieval=AllowanceOut(
            limit=retrieval.limit,
            remaining=limiter.remaining(retrieval, str(auth.workspace_id), now=now),
        ),
        resets_at=limiter.window_end(chat, now=now),
    )
