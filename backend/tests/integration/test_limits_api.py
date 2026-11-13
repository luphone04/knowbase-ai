"""GET /limits: the caller's daily chat and search allowances, read from the enforced counters."""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from app.api.dependencies import AUTH_COOKIE
from app.api.v1.retrieval import get_retriever
from app.config import get_settings
from app.core.security import create_access_token
from app.db.models import WorkspaceMember
from app.db.tenancy import bind_workspace
from app.main import app
from app.providers.errors import ProviderRateLimited, ProviderUnavailable
from app.retrieval.service import Retriever
from tests.fakes.retrieval import add_source, basis
from tests.integration.conftest import Tenant, new_client
from tests.integration.test_chat_api import LAPTOPS, Fakes, fakes, start  # noqa: F401

pytestmark = pytest.mark.integration

LIMITS = "/api/v1/limits"
SEARCH = "/api/v1/retrieval/search"
CONVERSATIONS = "/api/v1/conversations"


@pytest.fixture
def searchable(fakes: Fakes) -> Iterator[Fakes]:  # noqa: F811
    """The search route uses the same fake embedder as chat."""
    app.dependency_overrides[get_retriever] = lambda: Retriever(
        get_settings(), lambda: fakes.embedder
    )
    yield fakes
    app.dependency_overrides.pop(get_retriever, None)


def limits(tenant: Tenant) -> Any:
    response = tenant.client.get(LIMITS)
    assert response.status_code == 200, response.text
    return response.json()


def left(tenant: Tenant) -> tuple[int, int]:
    body = limits(tenant)
    return body["chat"]["remaining"], body["retrieval"]["remaining"]


def question(tenant: Tenant, conversation: str) -> int:
    return tenant.client.post(
        f"{CONVERSATIONS}/{conversation}/messages", json={"content": "Can I return a laptop?"}
    ).status_code


def test_a_fresh_day_shows_the_configured_limits_and_the_next_utc_midnight(
    tenant_a: Tenant,
) -> None:
    body = limits(tenant_a)
    settings = get_settings()
    assert body["chat"] == {
        "limit": settings.daily_chat_request_limit,
        "remaining": settings.daily_chat_request_limit,
    }
    assert body["retrieval"] == {
        "limit": settings.daily_retrieval_request_limit,
        "remaining": settings.daily_retrieval_request_limit,
    }
    assert set(body) == {"chat", "retrieval", "resets_at"}  # nothing else is exposed
    resets = datetime.fromisoformat(body["resets_at"])
    now = datetime.now(UTC)
    assert resets.utcoffset() == timedelta(0)
    assert (resets.hour, resets.minute, resets.second) == (0, 0, 0)
    assert timedelta(0) < resets - now <= timedelta(days=1)


def test_limits_come_from_the_configuration(
    tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_CHAT_REQUEST_LIMIT", "7")
    monkeypatch.setenv("DAILY_RETRIEVAL_REQUEST_LIMIT", "11")
    get_settings.cache_clear()
    try:
        body = limits(tenant_a)
    finally:
        get_settings.cache_clear()
    assert body["chat"] == {"limit": 7, "remaining": 7}
    assert body["retrieval"] == {"limit": 11, "remaining": 11}


def test_a_question_and_a_search_each_use_their_own_allowance(
    tenant_a: Tenant, searchable: Fakes
) -> None:
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    chat_limit, search_limit = left(tenant_a)
    conversation = start(tenant_a)
    assert question(tenant_a, conversation) == 201
    assert left(tenant_a) == (chat_limit - 1, search_limit)
    assert tenant_a.client.post(SEARCH, json={"query": "laptop"}).status_code == 200
    assert left(tenant_a) == (chat_limit - 1, search_limit - 1)


def test_calls_that_cost_nothing_take_nothing(tenant_a: Tenant, searchable: Fakes) -> None:
    chat_limit, search_limit = left(tenant_a)
    conversation = start(tenant_a)
    # Nothing indexed: no Gemini call, so no slot.
    assert question(tenant_a, conversation) == 201
    assert tenant_a.client.post(SEARCH, json={"query": "laptop"}).status_code == 200
    assert left(tenant_a) == (chat_limit, search_limit)
    # Gemini refused the call outright: the slots are given back.
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    searchable.embedder.failure = ProviderRateLimited("Slow down.")
    assert question(tenant_a, conversation) == 429
    assert tenant_a.client.post(SEARCH, json={"query": "laptop"}).status_code == 429
    assert left(tenant_a) == (chat_limit, search_limit)


def test_a_failed_answer_that_used_gemini_stays_counted(
    tenant_a: Tenant, searchable: Fakes
) -> None:
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    chat_limit, _ = left(tenant_a)
    conversation = start(tenant_a)
    searchable.generator.failures.append(ProviderUnavailable("Gemini is down."))
    assert question(tenant_a, conversation) == 503
    assert left(tenant_a)[0] == chat_limit - 1  # the query embedding really happened


def test_an_exhausted_allowance_shows_zero_and_rejections_take_nothing(
    tenant_a: Tenant, searchable: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_CHAT_REQUEST_LIMIT", "1")
    get_settings.cache_clear()
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    conversation = start(tenant_a)
    assert question(tenant_a, conversation) == 201
    assert limits(tenant_a)["chat"] == {"limit": 1, "remaining": 0}
    assert question(tenant_a, conversation) == 429
    assert limits(tenant_a)["chat"] == {"limit": 1, "remaining": 0}


def test_chat_is_per_user_and_search_is_shared_by_the_workspace(
    tenant_a: Tenant, tenant_b: Tenant, searchable: Fakes, db: Session
) -> None:
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    bind_workspace(db, tenant_a.workspace_id)
    db.add(
        WorkspaceMember(workspace_id=tenant_a.workspace_id, user_id=tenant_b.user_id, role="member")
    )
    db.commit()
    token, _ = create_access_token(
        tenant_b.user_id,
        tenant_a.workspace_id,
        secret=get_settings().jwt_secret.get_secret_value(),
        lifetime=timedelta(hours=1),
    )
    member = new_client("198.51.100.20")
    member.cookies.set(AUTH_COOKIE, token)
    before_member = member.get(LIMITS).json()
    before_b = left(tenant_b)  # B's own workspace

    assert question(tenant_a, start(tenant_a)) == 201
    assert tenant_a.client.post(SEARCH, json={"query": "laptop"}).status_code == 200

    after_member = member.get(LIMITS).json()
    assert after_member["chat"] == before_member["chat"]  # A's questions aren't the member's
    assert after_member["retrieval"]["remaining"] == before_member["retrieval"]["remaining"] - 1
    assert left(tenant_b) == before_b  # another workspace is untouched


def test_limits_require_a_session(tenant_a: Tenant) -> None:
    assert new_client("198.51.100.21").get(LIMITS).status_code == 401
