"""Conversations and grounded chat on real PostgreSQL + pgvector (no Gemini).

The query embedding comes from a fixed fake and the answer from a scripted mock,
so these tests decide exactly what "the model" says and check what the API makes
of it: validated citations, abstentions, provider errors, and isolation.
"""

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.dependencies import AUTH_COOKIE
from app.api.v1.conversations import get_chat_service
from app.chat.prompt import ABSTENTION, SYSTEM_INSTRUCTION
from app.chat.service import ChatService
from app.config import get_settings
from app.core.security import create_access_token
from app.db.models import WorkspaceMember
from app.db.session import get_engine
from app.db.tenancy import bind_workspace
from app.main import app
from app.providers import factory, pricing
from app.providers.errors import (
    IncompleteGeneration,
    ProviderError,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderUnavailable,
)
from app.providers.health import provider_health
from app.providers.interfaces import TokenUsage
from app.retrieval.service import Retriever
from tests.fakes.gemini import (
    FakeGeminiClient,
    daily_quota_error,
    deadline_error,
    generation_response,
    unavailable_error,
)
from tests.fakes.providers import MockGenerationProvider
from tests.fakes.retrieval import FixedQueryProvider, add_source, basis
from tests.integration.conftest import Tenant, new_client

pytestmark = pytest.mark.integration

CONVERSATIONS = "/api/v1/conversations"
LAPTOPS = "Laptops can be returned within 14 days of delivery if the factory seal is intact."
WARRANTY = "Tents have a 2-year warranty against manufacturing defects."


@dataclass
class Fakes:
    embedder: FixedQueryProvider
    generator: MockGenerationProvider


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> Iterator[Fakes]:
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key-not-real")  # never sent anywhere
    get_settings.cache_clear()
    embedder = FixedQueryProvider(vector=basis((0, 1)))
    generator = MockGenerationProvider(reply="Laptops can be returned within 14 days [S1].")
    app.dependency_overrides[get_chat_service] = lambda: ChatService(
        get_settings(), Retriever(get_settings(), lambda: embedder), lambda: generator
    )
    yield Fakes(embedder, generator)
    app.dependency_overrides.pop(get_chat_service, None)
    get_settings.cache_clear()


def start(tenant: Tenant, **body: object) -> str:
    response = tenant.client.post(CONVERSATIONS, json=body)
    assert response.status_code == 201, response.text
    conversation_id: str = response.json()["id"]
    return conversation_id


def ask(tenant: Tenant, conversation_id: str, content: str, **body: object) -> Any:
    response = tenant.client.post(
        f"{CONVERSATIONS}/{conversation_id}/messages", json={"content": content, **body}
    )
    assert response.status_code == 201, response.text
    return response.json()


def scalar(sql: str, **params: object) -> Any:
    with get_engine().connect() as connection:
        return connection.execute(text(sql), params).scalar_one()


def slots_used(rule: str) -> int:
    return int(
        scalar(
            "SELECT coalesce(sum(count), 0) FROM rate_limit_counters WHERE bucket_key LIKE :k",
            k=f"{rule}:%",
        )
    )


# --- conversations -----------------------------------------------------------------------------


def test_create_list_and_read_conversations(tenant_a: Tenant) -> None:
    first = start(tenant_a)
    second = start(tenant_a, title="  Shipping questions ")
    listed = tenant_a.client.get(CONVERSATIONS).json()
    assert {c["id"] for c in listed} == {first, second}
    detail = tenant_a.client.get(f"{CONVERSATIONS}/{second}").json()
    assert detail["title"] == "Shipping questions" and detail["messages"] == []
    assert tenant_a.client.get(f"{CONVERSATIONS}/{first}").json()["title"] == "New conversation"


# --- grounded answers ---------------------------------------------------------------------------


def test_an_answerable_question_gets_a_validated_citation(tenant_a: Tenant, fakes: Fakes) -> None:
    (chunk,) = add_source(
        tenant_a.workspace_id, "returns-policy.pdf", [(LAPTOPS, basis((0, 1)))], page=2
    )
    conversation = start(tenant_a)
    turn = ask(tenant_a, conversation, "Can I return a laptop after 20 days?")

    answer = turn["assistant_message"]
    assert answer["outcome"] == "answered" and answer["abstain_reason"] is None
    assert answer["content"] == "Laptops can be returned within 14 days [S1]."
    (citation,) = answer["citations"]
    assert citation["id"] == "S1" and citation["chunk_id"] == str(chunk)
    assert citation["filename"] == "returns-policy.pdf"
    assert (citation["page_start"], citation["page_end"]) == (2, 2)
    assert citation["excerpt"] == LAPTOPS
    assert citation["document_title"] == "returns-policy.pdf title"
    # The source a citation names is a real one the user can open.
    source = tenant_a.client.get(f"/api/v1/sources/{citation['source_id']}")
    assert source.status_code == 200 and source.json()["display_name"] == "returns-policy.pdf"

    assert turn["user_message"]["content"] == "Can I return a laptop after 20 days?"
    assert turn["conversation"]["title"] == "Can I return a laptop after 20 days?"
    usage = answer["token_usage"]
    assert usage["query_embeddings"] == 1 and usage["generation_requests"] == 1
    assert usage["model"] == "mock-generation" and usage["prompt_tokens"] > 0
    assert answer["latency_ms"] >= 0
    assert answer["metadata"]["retrieval_mode"] == "hybrid"
    assert answer["metadata"]["invalid_citation_ids"] == []


def test_the_turn_is_persisted_with_its_trace(tenant_a: Tenant, fakes: Fakes) -> None:
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    conversation = start(tenant_a)
    turn = ask(tenant_a, conversation, "Can I return a laptop?")
    messages = tenant_a.client.get(f"{CONVERSATIONS}/{conversation}").json()["messages"]
    assert [(m["position"], m["role"]) for m in messages] == [(1, "user"), (2, "assistant")]
    assert messages[1] == turn["assistant_message"]

    trace_id = turn["retrieval"]["trace_id"]
    assert turn["assistant_message"]["metadata"]["trace_id"] == trace_id
    trace = tenant_a.client.get(f"/api/v1/retrieval/traces/{trace_id}").json()
    assert trace["token_usage"]["generation_requests"] == 1
    with get_engine().connect() as connection:
        row = connection.execute(
            text("SELECT conversation_id, message_id FROM query_traces WHERE id = :id"),
            {"id": trace_id},
        ).one()
    assert (str(row.conversation_id), str(row.message_id)) == (
        conversation,
        turn["assistant_message"]["id"],
    )


def test_the_prompt_holds_the_excerpts_and_the_untrusted_source_rules(
    tenant_a: Tenant, fakes: Fakes
) -> None:
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    ask(tenant_a, start(tenant_a), "Can I return a laptop?")
    (prompt,) = fakes.generator.prompts
    assert f'<source id="S1" title="returns.md title" file="returns.md">\n{LAPTOPS}\n</source>' in (
        prompt
    )
    assert "<question>\nCan I return a laptop?\n</question>" in prompt
    assert fakes.generator.system_instructions == [SYSTEM_INSTRUCTION]


def test_fabricated_citations_are_removed_from_an_answer(tenant_a: Tenant, fakes: Fakes) -> None:
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    fakes.generator.reply = "Laptops: 14 days [S1][S9]. Drones: never [S4]."
    answer = ask(tenant_a, start(tenant_a), "Can I return a laptop?")["assistant_message"]
    assert answer["outcome"] == "answered"
    assert answer["content"] == "Laptops: 14 days [S1]. Drones: never."
    assert [c["id"] for c in answer["citations"]] == ["S1"]
    assert answer["metadata"]["invalid_citation_ids"] == ["S9", "S4"]


@pytest.mark.parametrize(
    "reply",
    ["Express shipping costs $25 [S7].", "Express shipping costs $25.", "Costs $25 [S1-ish]."],
    ids=["fabricated-only", "missing", "malformed"],
)
def test_an_answer_without_a_valid_citation_is_not_shown(
    tenant_a: Tenant, fakes: Fakes, reply: str
) -> None:
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    fakes.generator.reply = reply
    answer = ask(tenant_a, start(tenant_a), "How much is express shipping?")["assistant_message"]
    assert answer["outcome"] == "abstained"
    assert answer["abstain_reason"] == "no_valid_citation"
    assert answer["content"] == ABSTENTION
    assert "$25" not in answer["content"] and answer["citations"] == []


def test_an_invented_url_is_removed(tenant_a: Tenant, fakes: Fakes) -> None:
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    fakes.generator.reply = "Start at https://returns.evil.example/login [S1]."
    answer = ask(tenant_a, start(tenant_a), "How do I return?")["assistant_message"]
    assert answer["content"] == "Start at [link removed] [S1]."
    assert answer["metadata"]["removed_urls"] == 1


# --- abstention ----------------------------------------------------------------------------------


def test_the_model_saying_the_excerpts_are_not_enough_is_an_abstention(
    tenant_a: Tenant, fakes: Fakes
) -> None:
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    fakes.generator.reply = ABSTENTION
    answer = ask(tenant_a, start(tenant_a), "Do you sell drones?")["assistant_message"]
    assert (answer["outcome"], answer["abstain_reason"]) == ("abstained", "model_insufficient")
    assert answer["content"] == ABSTENTION and answer["citations"] == []


def test_no_evidence_abstains_without_any_gemini_call(tenant_a: Tenant, fakes: Fakes) -> None:
    turn = ask(tenant_a, start(tenant_a), "What is the refund policy?")
    answer = turn["assistant_message"]
    assert (answer["outcome"], answer["abstain_reason"]) == ("abstained", "no_evidence")
    assert answer["content"] == ABSTENTION
    assert fakes.embedder.queries == [] and fakes.generator.prompts == []
    assert answer["token_usage"] == {"query_embeddings": 0, "generation_requests": 0}
    assert slots_used("chat-requests-daily") == 0
    assert turn["retrieval"]["results"] == 0


def test_another_workspaces_documents_are_never_evidence(
    tenant_a: Tenant, tenant_b: Tenant, fakes: Fakes
) -> None:
    add_source(tenant_b.workspace_id, "secret.md", [(LAPTOPS, basis((0, 1)))])
    answer = ask(tenant_a, start(tenant_a), "Can I return a laptop?")["assistant_message"]
    assert answer["abstain_reason"] == "no_evidence"
    assert fakes.generator.prompts == []


# --- history and follow-ups -----------------------------------------------------------------------


def test_a_follow_up_retrieves_fresh_evidence_with_the_previous_question(
    tenant_a: Tenant, fakes: Fakes
) -> None:
    add_source(
        tenant_a.workspace_id,
        "policies.md",
        [(LAPTOPS, basis((0, 1))), (WARRANTY, basis((0, 1), (1, 1)))],
    )
    conversation = start(tenant_a)
    ask(tenant_a, conversation, "How long do I have to return a Trailblazer 14 laptop?")
    fakes.generator.reply = "Tents have a 2-year warranty [S2]."
    turn = ask(tenant_a, conversation, "And the warranty?")

    assert fakes.embedder.queries == [
        "How long do I have to return a Trailblazer 14 laptop?",
        "How long do I have to return a Trailblazer 14 laptop? And the warranty?",
    ]
    assert turn["retrieval"]["contextualized"] is True
    assert turn["user_message"]["content"] == "And the warranty?"  # the original question
    second_prompt = fakes.generator.prompts[1]
    assert "<question>\nAnd the warranty?\n</question>" in second_prompt
    assert (
        '<message role="assistant">Laptops can be returned within 14 days.</message>'
        in second_prompt
    )  # earlier answers are context, without their old citation ids
    messages = tenant_a.client.get(f"{CONVERSATIONS}/{conversation}").json()["messages"]
    assert [m["position"] for m in messages] == [1, 2, 3, 4]


def test_history_sent_to_the_model_is_bounded(
    tenant_a: Tenant, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CHAT_HISTORY_MESSAGES", "2")
    get_settings.cache_clear()
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    conversation = start(tenant_a)
    for number in range(3):
        ask(tenant_a, conversation, f"Question number {number} about laptop returns please")
    last_prompt = fakes.generator.prompts[-1]
    assert last_prompt.count("<message ") == 2
    assert "Question number 1" in last_prompt and "Question number 0" not in last_prompt


# --- provider failures and limits -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("failure", "status", "code", "slot_given_back"),
    [
        (ProviderQuotaExhausted("Daily quota used up."), 429, "ai_quota", True),
        (ProviderRateLimited("Slow down."), 429, "ai_quota", True),
        (ProviderUnavailable("Gemini is temporarily unavailable."), 503, "ai_unavailable", False),
        (IncompleteGeneration("MAX_TOKENS", TokenUsage(), 5), 502, "ai_incomplete", False),
    ],
    ids=["daily-quota", "rate-limited", "unavailable", "incomplete"],
)
def test_a_generation_failure_is_an_error_not_an_answer(
    tenant_a: Tenant,
    fakes: Fakes,
    failure: ProviderError,
    status: int,
    code: str,
    slot_given_back: bool,
) -> None:
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    conversation = start(tenant_a)
    fakes.generator.failures.append(failure)
    response = tenant_a.client.post(
        f"{CONVERSATIONS}/{conversation}/messages", json={"content": "Can I return a laptop?"}
    )
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert "answer" not in response.json()
    assert tenant_a.client.get(f"{CONVERSATIONS}/{conversation}").json()["messages"] == []
    assert scalar("SELECT count(*) FROM messages") == 0
    assert scalar("SELECT token_usage->>'generation_error' FROM query_traces") == failure.category
    assert slots_used("chat-requests-daily") == (0 if slot_given_back else 1)
    assert slots_used("query-embeddings-daily") == 1  # the query embedding was really made


def test_a_refused_query_embedding_gives_both_slots_back(tenant_a: Tenant, fakes: Fakes) -> None:
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    conversation = start(tenant_a)
    fakes.embedder.failure = ProviderQuotaExhausted("Daily quota used up.")
    response = tenant_a.client.post(
        f"{CONVERSATIONS}/{conversation}/messages", json={"content": "Can I return a laptop?"}
    )
    assert response.status_code == 429 and response.json()["error"]["code"] == "ai_quota"
    assert fakes.generator.prompts == []
    assert slots_used("chat-requests-daily") == 0 and slots_used("query-embeddings-daily") == 0
    assert scalar("SELECT count(*) FROM messages") == 0


def test_without_a_key_chat_is_unavailable_and_free(
    tenant_a: Tenant, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "")
    get_settings.cache_clear()
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    conversation = start(tenant_a)
    response = tenant_a.client.post(
        f"{CONVERSATIONS}/{conversation}/messages", json={"content": "Can I return a laptop?"}
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ai_not_configured"
    assert slots_used("chat-requests-daily") == 0
    assert scalar("SELECT count(*) FROM query_traces") == 0


def test_chat_is_limited_per_user_per_day(
    tenant_a: Tenant, tenant_b: Tenant, fakes: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_CHAT_REQUEST_LIMIT", "1")
    get_settings.cache_clear()
    add_source(tenant_a.workspace_id, "a.md", [(LAPTOPS, basis((0, 1)))])
    add_source(tenant_b.workspace_id, "b.md", [(LAPTOPS, basis((0, 1)))])
    conversation = start(tenant_a)
    ask(tenant_a, conversation, "Can I return a laptop?")
    limited = tenant_a.client.post(
        f"{CONVERSATIONS}/{conversation}/messages", json={"content": "And a tent?"}
    )
    assert limited.status_code == 429 and "Retry-After" in limited.headers
    assert len(fakes.embedder.queries) == 1 and len(fakes.generator.prompts) == 1
    assert slots_used("query-embeddings-daily") == 1  # not taken for the refused question
    ask(tenant_b, start(tenant_b), "Can I return a laptop?")  # B has its own allowance


def test_no_database_connection_is_held_while_gemini_generates(
    tenant_a: Tenant, fakes: Fakes
) -> None:
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    held: list[int] = []
    pool = get_engine().pool
    fakes.embedder.on_call = lambda: held.append(pool.checkedout())  # type: ignore[attr-defined]
    fakes.generator.on_call = lambda: held.append(pool.checkedout())  # type: ignore[attr-defined]
    ask(tenant_a, start(tenant_a), "Can I return a laptop?")
    assert held == [0, 0]


# --- isolation and validation --------------------------------------------------------------------


def test_conversations_are_isolated_between_workspaces(
    tenant_a: Tenant, tenant_b: Tenant, fakes: Fakes
) -> None:
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    conversation = start(tenant_a)
    ask(tenant_a, conversation, "Can I return a laptop?")
    random_id = str(uuid.uuid4())
    for target in (conversation, random_id):  # a real foreign id looks like a random one
        assert tenant_b.client.get(f"{CONVERSATIONS}/{target}").status_code == 404
        response = tenant_b.client.post(
            f"{CONVERSATIONS}/{target}/messages", json={"content": "Can I return a laptop?"}
        )
        assert response.status_code == 404
        assert response.json() == {
            "error": {"code": "not_found", "message": "Conversation not found."}
        }
    assert tenant_b.client.get(CONVERSATIONS).json() == []
    assert len(fakes.generator.prompts) == 1  # B's attempts never reached Gemini
    assert scalar("SELECT count(*) FROM messages") == 2


def test_members_of_one_workspace_cannot_read_each_others_chats(
    tenant_a: Tenant, tenant_b: Tenant, fakes: Fakes, db: Session
) -> None:
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    conversation = start(tenant_a)
    ask(tenant_a, conversation, "Can I return a laptop?")
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
    member = new_client("198.51.100.9")
    member.cookies.set(AUTH_COOKIE, token)
    assert member.get(CONVERSATIONS).json() == []
    assert member.get(f"{CONVERSATIONS}/{conversation}").status_code == 404
    response = member.post(
        f"{CONVERSATIONS}/{conversation}/messages", json={"content": "What did they ask?"}
    )
    assert response.status_code == 404
    # The member can chat in the shared workspace in a conversation of their own.
    own = member.post(CONVERSATIONS, json={}).json()["id"]
    reply = member.post(f"{CONVERSATIONS}/{own}/messages", json={"content": "Laptop returns?"})
    assert reply.status_code == 201
    assert reply.json()["assistant_message"]["outcome"] == "answered"


def test_chat_requires_authentication_and_origin(tenant_a: Tenant) -> None:
    conversation = start(tenant_a)
    assert new_client().get(CONVERSATIONS).status_code == 401
    assert new_client().post(CONVERSATIONS, json={}).status_code == 401
    response = tenant_a.client.post(
        f"{CONVERSATIONS}/{conversation}/messages",
        json={"content": "hi"},
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403


@pytest.mark.parametrize(
    "body",
    [
        {"content": ""},
        {"content": "   "},
        {"content": "x" * 1001},
        {"content": "tent", "mode": "bm25"},
        {"content": "tent", "workspace_id": str(uuid.uuid4())},
        {"content": "tent", "role": "assistant"},
        {"content": "tent\u0000"},
        {"content": "tent\u001b[2J"},
    ],
    ids=[
        "empty",
        "blank",
        "too-long",
        "unknown-mode",
        "client-workspace",
        "client-role",
        "nul",
        "control-character",
    ],
)
def test_invalid_questions_are_rejected_before_gemini(
    tenant_a: Tenant, fakes: Fakes, body: dict[str, object]
) -> None:
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    conversation = start(tenant_a)
    response = tenant_a.client.post(f"{CONVERSATIONS}/{conversation}/messages", json=body)
    assert response.status_code == 422
    assert fakes.embedder.queries == [] and fakes.generator.prompts == []
    assert slots_used("chat-requests-daily") == 0


@pytest.mark.parametrize(
    "body",
    [{"title": "   "}, {"title": "x" * 201}, {"title": "a\u0000b"}, {"user_id": str(uuid.uuid4())}],
    ids=["blank-title", "long-title", "nul-title", "client-user"],
)
def test_invalid_conversations_are_rejected(tenant_a: Tenant, body: dict[str, object]) -> None:
    assert tenant_a.client.post(CONVERSATIONS, json=body).status_code == 422


# --- the real Gemini provider with the configured retries (a scripted client) ---------------


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [
        (unavailable_error(), 503, "ai_unavailable"),
        (deadline_error(), 503, "ai_unavailable"),
        (daily_quota_error(), 429, "ai_quota"),
    ],
    ids=["503", "504", "daily-quota"],
)
def test_one_failed_generation_attempt_is_an_error_and_nothing_is_saved(
    tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch, failure: Exception, status: int, code: str
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key-not-real")  # never sent anywhere
    get_settings.cache_clear()
    settings = get_settings()
    assert settings.gemini_generation_max_retries == 0  # the configured default
    client = FakeGeminiClient()
    # A retry would get this answer: it must never be requested.
    client.models.generate_script.extend([failure, generation_response("Laptops: 14 days [S1].")])
    generator = factory.build_generation_provider(settings, client)  # type: ignore[arg-type]
    embedder = FixedQueryProvider(vector=basis((0, 1)))
    app.dependency_overrides[get_chat_service] = lambda: ChatService(
        settings, Retriever(settings, lambda: embedder), lambda: generator
    )
    try:
        add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
        conversation = start(tenant_a)
        response = tenant_a.client.post(
            f"{CONVERSATIONS}/{conversation}/messages", json={"content": "Can I return a laptop?"}
        )
    finally:
        app.dependency_overrides.pop(get_chat_service, None)
        provider_health.reset()
        get_settings.cache_clear()
    assert response.status_code == status and response.json()["error"]["code"] == code
    assert sum(c.method == "generate_content" for c in client.models.calls) == 1
    assert scalar("SELECT count(*) FROM messages") == 0
    assert tenant_a.client.get(f"{CONVERSATIONS}/{conversation}").json()["messages"] == []
    assert scalar("SELECT token_usage->>'generation_requests' FROM query_traces") == "1"


def test_one_successful_generation_attempt_is_saved_with_its_citation(
    tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key-not-real")
    get_settings.cache_clear()
    settings = get_settings()
    client = FakeGeminiClient()
    client.models.generate_script.append(generation_response("Laptops: 14 days [S1]."))
    generator = factory.build_generation_provider(settings, client)  # type: ignore[arg-type]
    embedder = FixedQueryProvider(vector=basis((0, 1)))
    app.dependency_overrides[get_chat_service] = lambda: ChatService(
        settings, Retriever(settings, lambda: embedder), lambda: generator
    )
    try:
        (chunk,) = add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
        answer = ask(tenant_a, start(tenant_a), "Can I return a laptop?")["assistant_message"]
    finally:
        app.dependency_overrides.pop(get_chat_service, None)
        provider_health.reset()
        get_settings.cache_clear()
    assert answer["outcome"] == "answered" and answer["citations"][0]["chunk_id"] == str(chunk)
    assert answer["token_usage"]["model"] == "gemini-3.5-flash-lite"
    assert answer["token_usage"]["generation_attempts"] == 1
    assert sum(c.method == "generate_content" for c in client.models.calls) == 1


# --- API usage & estimated cost ------------------------------------------------------------------

CHAT_MODEL = "gemini-3.5-flash-lite"
REPORTED = TokenUsage(
    prompt_tokens=1000, output_tokens=50, thinking_tokens=30, total_tokens=1080, cached_tokens=400
)
# 600 input at $0.30, 400 cached at $0.03, 50 output + 30 thinking at $2.50, per 1M tokens.
REPLY_USD = round((600 * 0.30 + 400 * 0.03 + 80 * 2.50) / 1_000_000, 10)


def usage_of(tenant: Tenant, conversation_id: str) -> Any:
    response = tenant.client.get(f"{CONVERSATIONS}/{conversation_id}/usage")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def reported(fakes: Fakes) -> Fakes:
    fakes.generator.model = CHAT_MODEL
    fakes.generator.usage = REPORTED
    return fakes


def test_a_reply_stores_its_usage_and_pricing_and_shows_its_estimate(
    tenant_a: Tenant, reported: Fakes
) -> None:
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    conversation = start(tenant_a)
    answer = ask(tenant_a, conversation, "Can I return a laptop?")["assistant_message"]

    stored = answer["token_usage"]
    assert stored["cached_tokens"] == 400 and stored["thinking_tokens"] == 30
    assert stored["embedding_model"] == "gemini-embedding-001"
    assert stored["embedding_input_tokens"] is None and stored["embedding_attempts"] == 1
    assert stored["pricing"]["verified_on"] == pricing.VERIFIED_ON
    assert set(stored["pricing"]["models"]) == {CHAT_MODEL, "gemini-embedding-001"}

    usage = answer["usage"]  # shown with the new reply, no refresh needed
    assert usage["generation"]["cost_usd"] == REPLY_USD
    assert usage["generation"]["cached_input_tokens"] == 400
    assert usage["embedding"]["requests"] == 1 and usage["embedding"]["input_tokens"] is None
    assert usage["total"]["cost_usd"] == REPLY_USD and usage["total"]["cost_complete"] is False
    assert "api_key" not in str(answer).lower()

    # The same, after a reload.
    messages = tenant_a.client.get(f"{CONVERSATIONS}/{conversation}").json()["messages"]
    assert messages[1]["usage"] == usage
    assert messages[0]["usage"] is None  # the question itself made no call


def test_a_later_price_change_does_not_change_a_saved_reply(
    tenant_a: Tenant, reported: Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    conversation = start(tenant_a)
    ask(tenant_a, conversation, "Can I return a laptop?")
    monkeypatch.setitem(
        pricing.PRICES, CHAT_MODEL, pricing.ModelPrice("generation", Decimal(9), Decimal(99))
    )
    messages = tenant_a.client.get(f"{CONVERSATIONS}/{conversation}").json()["messages"]
    assert messages[1]["usage"]["generation"]["cost_usd"] == REPLY_USD
    assert usage_of(tenant_a, conversation)["generation"]["cost_usd"] == REPLY_USD


def test_conversation_usage_adds_up_replies_and_failed_questions(
    tenant_a: Tenant, reported: Fakes
) -> None:
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    conversation = start(tenant_a)
    assert usage_of(tenant_a, conversation)["total"]["requests"] == 0
    ask(tenant_a, conversation, "Can I return a laptop?")
    first = usage_of(tenant_a, conversation)
    assert first["replies"] == 1 and first["total"]["requests"] == 2

    reported.generator.failures.append(ProviderUnavailable("Gemini is temporarily unavailable."))
    failed = tenant_a.client.post(
        f"{CONVERSATIONS}/{conversation}/messages", json={"content": "And a tent?"}
    )
    assert failed.status_code == 503
    reported.generator.failures.append(IncompleteGeneration("MAX_TOKENS", REPORTED, 5))
    cut_off = tenant_a.client.post(
        f"{CONVERSATIONS}/{conversation}/messages", json={"content": "And shoes?"}
    )
    assert cut_off.status_code == 502
    ask(tenant_a, conversation, "What about a laptop bag?")
    prompts = len(reported.generator.prompts)

    usage = usage_of(tenant_a, conversation)
    assert len(reported.generator.prompts) == prompts  # totals make no Gemini call
    assert usage["replies"] == 2 and usage["replies_with_usage"] == 2
    assert usage["failed_questions"] == 2
    assert usage["embedding"]["requests"] == 4  # every question was embedded
    assert usage["embedding"]["input_tokens"] is None
    generation = usage["generation"]
    assert generation["requests"] == 4
    assert generation["requests_without_usage"] == 1  # the 503; the cut-off one reported
    assert generation["input_tokens"] == 3000 and generation["cached_input_tokens"] == 1200
    assert generation["output_tokens"] == 150 and generation["thinking_tokens"] == 90
    assert usage["total"]["cost_usd"] == pytest.approx(3 * REPLY_USD)
    assert usage["total"]["cost_complete"] is False
    codes = {(i["code"], i.get("kind")) for i in usage["issues"]}
    assert codes == {("usage_missing", "embedding"), ("usage_missing", "generation")}
    assert usage["pricing"]["tier_label"] == pricing.TIER_LABEL


def test_retried_attempts_are_disclosed_in_reply_and_conversation_usage(
    tenant_a: Tenant, reported: Fakes
) -> None:
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    reported.embedder.attempts = 2
    reported.generator.attempts = 3
    conversation = start(tenant_a)
    answer = ask(tenant_a, conversation, "Can I return a laptop?")["assistant_message"]
    usage = answer["usage"]
    assert usage["embedding"]["requests"] == 1 and usage["embedding"]["unmeasured_attempts"] == 1
    assert usage["generation"]["requests"] == 1
    assert usage["generation"]["unmeasured_attempts"] == 2
    assert usage["generation"]["cost_usd"] == REPLY_USD  # the retries aren't priced
    assert usage["generation"]["cost_complete"] is False
    totals = usage_of(tenant_a, conversation)
    assert totals["total"]["unmeasured_attempts"] == 3


def test_a_reply_without_evidence_made_no_call_and_costs_nothing(
    tenant_a: Tenant, reported: Fakes
) -> None:
    conversation = start(tenant_a)  # nothing indexed: no embedding, no generation
    answer = ask(tenant_a, conversation, "Can I return a laptop?")["assistant_message"]
    assert answer["usage"]["total"]["requests"] == 0
    assert answer["usage"]["total"]["cost_usd"] == 0.0
    assert "pricing" not in answer["token_usage"]


def test_replies_saved_before_usage_was_recorded_still_load(
    tenant_a: Tenant, reported: Fakes
) -> None:
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    conversation = start(tenant_a)
    ask(tenant_a, conversation, "Can I return a laptop?")
    ask(tenant_a, conversation, "Within how many days?")
    with get_engine().begin() as connection:  # one reply from before usage, one before prices
        connection.execute(text("UPDATE messages SET token_usage = '{}'::jsonb WHERE position = 2"))
        connection.execute(
            text("UPDATE messages SET token_usage = token_usage - 'pricing' WHERE position = 4")
        )
    detail = tenant_a.client.get(f"{CONVERSATIONS}/{conversation}")
    assert detail.status_code == 200
    first, second = detail.json()["messages"][1], detail.json()["messages"][3]
    assert first["usage"] is None and first["content"]
    assert second["usage"]["generation"]["input_tokens"] == 1000
    assert second["usage"]["generation"]["cost_usd"] is None
    totals = usage_of(tenant_a, conversation)
    assert totals["replies"] == 2 and totals["replies_with_usage"] == 1
    assert totals["total"]["cost_usd"] is None  # nothing left with a known cost
    assert {"code": "cost_unavailable", "requests": 1} in totals["issues"]


def test_conversation_usage_respects_ownership(
    tenant_a: Tenant, tenant_b: Tenant, reported: Fakes, db: Session
) -> None:
    add_source(tenant_a.workspace_id, "returns.md", [(LAPTOPS, basis((0, 1)))])
    conversation = start(tenant_a)
    ask(tenant_a, conversation, "Can I return a laptop?")
    for target in (conversation, str(uuid.uuid4())):
        response = tenant_b.client.get(f"{CONVERSATIONS}/{target}/usage")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"
    # Another member of the same workspace can't read it either.
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
    member = new_client("198.51.100.10")
    member.cookies.set(AUTH_COOKIE, token)
    assert member.get(f"{CONVERSATIONS}/{conversation}/usage").status_code == 404
    anonymous = new_client("198.51.100.11")
    assert anonymous.get(f"{CONVERSATIONS}/{conversation}/usage").status_code == 401
