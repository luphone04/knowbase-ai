"""Evaluation runs end to end on real PostgreSQL + pgvector, with fake providers (no Gemini).

The demo corpus is seeded directly: five sources named after the dataset's
source keys, with the dataset's content hashes and hand-built vectors (source
i's chunks sit on axis i). The fake query embedder puts each dataset question
on the axis of its first expected source and unanswerable ones on an unused
axis, so vector-only retrieval is known exactly: every answerable question
finds its first expected source at rank 1. The fake generator cites [S1]
unless the question is unanswerable, where it says the excerpts aren't enough.
"""

import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from app.chat.prompt import ABSTENTION
from app.config import get_settings
from app.db.models import EvaluationRun
from app.db.session import get_engine, get_sessionmaker
from app.db.tenancy import bind_workspace
from app.evaluation import budget
from app.evaluation.dataset import load_dataset
from app.evaluation.runner import CORPUS_CHANGED, EvaluationWorker
from app.providers.errors import (
    ProviderError,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderUnavailable,
)
from app.providers.interfaces import QUERY_TASK, EmbeddingResult, GenerationResult, TokenUsage
from app.retrieval.followup import contextualize
from app.worker.lease import Deadline
from tests.fakes.retrieval import DIMS, MODEL, add_source, basis
from tests.integration.conftest import Tenant, new_client

pytestmark = pytest.mark.integration

EVALUATIONS = "/api/v1/evaluations"
DATASET = load_dataset()
KEYS = sorted(DATASET.source_hashes)
UNANSWERABLE_AXIS = 50
QUESTIONS = len(DATASET.questions)


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


def seed_demo_corpus(workspace_id: uuid.UUID, *, skip: str | None = None) -> None:
    for index, key in enumerate(KEYS):
        if key == skip:
            continue
        add_source(
            workspace_id,
            key,
            [
                (f"{key} section one about policy {index}.", basis((index, 1))),
                (
                    f"{key} section two with more detail {index}.",
                    basis((index, 1), (index + 10, 1)),
                ),
            ],
        )
        with get_engine().begin() as connection:
            connection.execute(
                text(
                    "UPDATE sources SET file_sha256 = :h, last_indexed_at = now() "
                    "WHERE workspace_id = :ws AND original_filename = :name"
                ),
                {"h": DATASET.source_hashes[key], "ws": workspace_id, "name": key},
            )


@dataclass
class RoutingEmbedder:
    """Each dataset question's retrieval query lands on its first expected source's axis."""

    queries: list[str] = field(default_factory=list)
    failures: dict[str, ProviderError] = field(default_factory=dict)  # question key -> error
    on_call: Any = None
    model: str = MODEL
    dimensions: int = DIMS

    def __post_init__(self) -> None:
        self.axis: dict[str, int] = {}
        self.key_of: dict[str, str] = {}
        for q in DATASET.questions:
            query = contextualize(q.question, q.previous_question)
            self.key_of[query] = q.key
            first = q.expected_source_keys[0] if q.expected_source_keys else None
            self.axis[query] = KEYS.index(first) if first else UNANSWERABLE_AXIS

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingResult:
        raise AssertionError("an evaluation never embeds documents")

    def embed_query(self, query: str) -> EmbeddingResult:
        if self.on_call is not None:
            self.on_call()
        self.queries.append(query)
        failure = self.failures.get(self.key_of[query])
        if failure is not None:
            raise failure
        return EmbeddingResult(
            vectors=[basis((self.axis[query], 1))],
            model=MODEL,
            dimensions=DIMS,
            task_type=QUERY_TASK,
            latency_ms=0,
            requests=1,
            attempts=1,
        )


UNANSWERABLE_TEXT = {q.question for q in DATASET.questions if not q.answerable}


@dataclass
class ScriptedGenerator:
    """Cites [S1] for answerable questions; abstains for unanswerable ones."""

    prompts: list[str] = field(default_factory=list)
    failures: dict[str, ProviderError] = field(default_factory=dict)  # question text -> error
    on_call: Any = None
    model: str = "mock-generation"

    def generate(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        max_output_tokens: int | None = None,
    ) -> GenerationResult:
        if self.on_call is not None:
            self.on_call()
        self.prompts.append(prompt)
        question = prompt.split("<question>\n", 1)[1].split("\n</question>", 1)[0]
        failure = self.failures.get(question)
        if failure is not None:
            raise failure
        reply = ABSTENTION if question in UNANSWERABLE_TEXT else "Here is the policy [S1]."
        return GenerationResult(
            text=reply,
            model="mock-generation",
            finish_reason="STOP",
            usage=TokenUsage(prompt_tokens=100, output_tokens=10),
            latency_ms=5,
            attempts=1,
        )


@dataclass
class Harness:
    worker: EvaluationWorker
    embedder: RoutingEmbedder
    generator: ScriptedGenerator

    def run_all(self, limit: int = 200) -> int:
        ran = 0
        while ran < limit and self.worker.run_once():
            ran += 1
        return ran


def make_evaluator(embedder: RoutingEmbedder, generator: ScriptedGenerator, name: str) -> Any:
    settings = get_settings()
    return EvaluationWorker(
        settings=settings,
        session_factory=get_sessionmaker(),
        engine=get_engine(),
        embedding_provider_factory=lambda: embedder,
        generation_provider_factory=lambda: generator,
        worker_id=name,
    )


@pytest.fixture
def evaluator(monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key-not-real")  # never sent anywhere
    monkeypatch.setenv("EVALUATION_QUESTION_INTERVAL_SECONDS", "0")
    get_settings.cache_clear()
    embedder, generator = RoutingEmbedder(), ScriptedGenerator()
    yield Harness(make_evaluator(embedder, generator, "eval-worker"), embedder, generator)
    get_settings.cache_clear()


def start(tenant: Tenant, **body: object) -> dict[str, Any]:
    response = tenant.client.post(f"{EVALUATIONS}/run", json=body)
    assert response.status_code == 202, response.text
    run: dict[str, Any] = response.json()
    return run


def detail(tenant: Tenant, run_id: str) -> dict[str, Any]:
    response = tenant.client.get(f"{EVALUATIONS}/runs/{run_id}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def results_for(run: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    item = next(q for q in run["questions"] if q["question"]["key"] == key)
    return {r["variant"]: r for r in item["results"]}


# --- the dataset and the corpus --------------------------------------------------------------


def test_the_overview_reports_the_dataset_and_a_missing_corpus(
    tenant_a: Tenant, evaluator: Harness
) -> None:
    response = tenant_a.client.get(f"{EVALUATIONS}/questions")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dataset"]["name"] == "northstar-demo" and body["dataset"]["version"] == "1"
    assert body["dataset"]["questions"] == QUESTIONS
    assert body["dataset"]["answerable"] == DATASET.answerable
    assert body["dataset"]["unanswerable"] == DATASET.unanswerable
    assert body["corpus"]["ready"] is False
    assert {s["status"] for s in body["corpus"]["sources"]} == {"missing"}
    assert body["limits"]["query_embeddings_per_run"] == QUESTIONS
    first = body["questions"][0]
    assert first["key"] == "q01" and first["expected_source_keys"] == ["returns-policy.md"]
    assert first["evidence"][0]["source_key"] == "returns-policy.md"
    follow_up = next(q for q in body["questions"] if q["category"] == "follow_up")
    assert follow_up["previous_question"]
    # Loading twice doesn't duplicate the shared dataset.
    tenant_a.client.get(f"{EVALUATIONS}/questions")
    assert scalar("SELECT count(*) FROM evaluation_questions") == QUESTIONS


def test_the_corpus_must_hold_the_unchanged_demo_documents(
    tenant_a: Tenant, evaluator: Harness
) -> None:
    seed_demo_corpus(tenant_a.workspace_id, skip="support-faq.md")
    add_source(tenant_a.workspace_id, "support-faq.md", [("edited faq", basis((3, 1)))])
    response = tenant_a.client.post(f"{EVALUATIONS}/run", json={})
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "evaluation_corpus_incomplete"
    assert "support-faq.md (uploaded, but its content differs" in error["message"]
    assert {d["key"]: d["status"] for d in error["details"]}["support-faq.md"] == (
        "content_differs"
    )
    assert slots_used("evaluation-runs-daily") == 0
    assert scalar("SELECT count(*) FROM evaluation_runs") == 0


def test_documents_still_being_indexed_block_a_run(tenant_a: Tenant, evaluator: Harness) -> None:
    seed_demo_corpus(tenant_a.workspace_id)
    add_source(tenant_a.workspace_id, "notes.md", [("draft", basis((40, 1)))], status="processing")
    response = tenant_a.client.post(f"{EVALUATIONS}/run", json={})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "evaluation_corpus_indexing"


def test_other_documents_are_reported_and_compete_in_retrieval(
    tenant_a: Tenant, evaluator: Harness
) -> None:
    seed_demo_corpus(tenant_a.workspace_id)
    add_source(tenant_a.workspace_id, "thesis.pdf", [("thesis", basis((UNANSWERABLE_AXIS, 1)))])
    overview = tenant_a.client.get(f"{EVALUATIONS}/questions").json()
    assert overview["corpus"]["ready"] is True
    assert overview["corpus"]["other_sources"] == [
        {"name": "thesis.pdf", "chunks": 1, "status": "ready"}
    ]
    assert overview["corpus"]["total_chunks"] == 11
    run = start(tenant_a, answer_modes=[])
    evaluator.run_all()
    body = detail(tenant_a, run["id"])
    top = results_for(body, "u01")["vector"]["retrieved_sources"][0]
    assert top == {
        "rank": 1,
        "source_key": None,
        "name": "thesis.pdf",
        "relevant": False,
        "chunk_ranks": [1],
    }


def test_a_run_needs_a_configured_key(
    tenant_a: Tenant, evaluator: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "")
    get_settings.cache_clear()
    seed_demo_corpus(tenant_a.workspace_id)
    response = tenant_a.client.post(f"{EVALUATIONS}/run", json={})
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "ai_not_configured"
    assert slots_used("evaluation-runs-daily") == 0


# --- a full run ------------------------------------------------------------------------------


def test_a_full_run_measures_every_configuration_and_both_answer_modes(
    tenant_a: Tenant, evaluator: Harness
) -> None:
    seed_demo_corpus(tenant_a.workspace_id)
    run = start(tenant_a, answer_modes=["hybrid", "vector"])
    assert run["status"] == "queued" and run["questions_total"] == QUESTIONS
    assert run["answer_modes"] == ["vector", "hybrid"]
    assert "budget" not in run["config"]
    assert run["config"]["embedding_model"] == get_settings().gemini_embedding_model
    assert run["config"]["variant_details"]["hybrid"]["lexical_match"] == "all"
    assert run["config"]["variant_details"]["hybrid_or"]["experimental"] is True

    assert evaluator.run_all() == QUESTIONS  # one question per claim
    body = detail(tenant_a, run["id"])
    assert body["status"] == "succeeded" and body["questions_done"] == QUESTIONS
    assert body["finished_at"] is not None and body["error_message"] is None
    assert all(len(q["results"]) == 5 for q in body["questions"])

    # One query embedding per question, shared by all five configurations.
    assert len(evaluator.embedder.queries) == QUESTIONS
    assert len(evaluator.generator.prompts) == 2 * QUESTIONS
    summary = body["summary"]
    assert summary["gemini_requests"] == {
        "query_embeddings": QUESTIONS,
        "generation_requests": 2 * QUESTIONS,
    }
    vector = summary["retrieval"]["vector"]
    assert vector["questions"] == DATASET.answerable
    assert vector["hit_rate_at_5"] == 1.0 and vector["mrr"] == 1.0  # by construction
    multi = [q for q in DATASET.questions if len(q.expected_source_keys) > 1]
    assert vector["recall_at_5"] is not None and vector["recall_at_5"] <= 1.0
    # Chance with 10 chunks, 2 per source, top 5: 1 - C(8,5)/C(10,5) for one source.
    assert vector["chance_hit_rate_at_5"] > 0.75
    assert summary["retrieval"]["vector"]["no_lexical_candidates"] == {
        "answerable": None,
        "unanswerable": None,
    }
    assert summary["retrieval"]["hybrid"]["no_lexical_candidates"]["answerable"] is not None

    for mode in ("vector", "hybrid"):
        answers = summary["answers"][mode]
        assert answers["confusion"] == {
            "answerable": {"answered": DATASET.answerable, "abstained": 0, "provider_error": 0},
            "unanswerable": {
                "answered": 0,
                "abstained": DATASET.unanswerable,
                "provider_error": 0,
            },
        }
        assert answers["abstention_accuracy"] == 1.0
        assert answers["citation_questions"] == DATASET.answerable
        assert answers["abstain_reasons"] == {"model_insufficient": DATASET.unanswerable}
        assert answers["latency_ms"]["count"] == QUESTIONS

    # Per question: what was retrieved, cited, and decided.
    q01 = results_for(body, "q01")
    assert q01["vector"]["retrieved_sources"][0]["source_key"] == "returns-policy.md"
    assert q01["vector"]["hit_at_5"] is True and q01["vector"]["reciprocal_rank"] == 1.0
    assert q01["hybrid"]["outcome"] == "answered"
    assert q01["hybrid"]["citations"][0]["source_key"] == "returns-policy.md"
    assert q01["hybrid"]["citation_precision"] == 1.0
    assert q01["hybrid_or"]["answer_evaluated"] is False and q01["hybrid_or"]["outcome"] is None
    u01 = results_for(body, "u01")
    assert u01["vector"]["hit_at_5"] is None and u01["vector"]["recall_at_5"] is None
    assert u01["hybrid"]["outcome"] == "abstained"
    assert u01["hybrid"]["abstention_correct"] is True
    assert u01["hybrid"]["citation_precision"] is None
    assert u01["hybrid"]["details"]["model_text"] == ABSTENTION
    q12 = results_for(body, multi[0].key)
    assert q12["vector"]["recall_at_5"] in (0.5, 1.0)

    # Follow-ups retrieve with the previous question, as chat does, and the prompt has it.
    q16 = next(q for q in DATASET.questions if q.key == "q16")
    assert results_for(body, "q16")["hybrid"]["retrieval_query"] == (
        f"{q16.previous_question} {q16.question}"
    )
    prompt = next(p for p in evaluator.generator.prompts if f"\n{q16.question}\n" in p)
    assert f'<message role="user">{q16.previous_question}</message>' in prompt

    # The budget: one run, one embedding per question, one generation per question and mode.
    assert slots_used("evaluation-runs-daily") == 1
    assert slots_used("query-embeddings-daily") == QUESTIONS
    assert slots_used("evaluation-generations-daily") == 2 * QUESTIONS
    # Nothing goes into chat history or retrieval traces.
    assert scalar("SELECT count(*) FROM messages") == 0
    assert scalar("SELECT count(*) FROM query_traces") == 0

    listed = tenant_a.client.get(f"{EVALUATIONS}/runs").json()
    assert [r["id"] for r in listed] == [run["id"]]
    assert listed[0]["summary"]["retrieval"]["vector"]["mrr"] == 1.0  # stored at the end


def test_a_retrieval_only_run_makes_no_generation_requests(
    tenant_a: Tenant, evaluator: Harness
) -> None:
    seed_demo_corpus(tenant_a.workspace_id)
    run = start(tenant_a, answer_modes=[])
    evaluator.run_all()
    body = detail(tenant_a, run["id"])
    assert body["status"] == "succeeded"
    assert evaluator.generator.prompts == []
    assert body["summary"]["answers"] == {}
    assert slots_used("evaluation-generations-daily") == 0
    q01 = results_for(body, "q01")
    assert all(not r["answer_evaluated"] for r in q01.values())


# --- failures --------------------------------------------------------------------------------


def test_a_provider_failure_is_recorded_and_is_never_an_abstention(
    tenant_a: Tenant, evaluator: Harness
) -> None:
    seed_demo_corpus(tenant_a.workspace_id)
    unanswerable = next(q for q in DATASET.questions if not q.answerable)
    evaluator.generator.failures[unanswerable.question] = ProviderUnavailable("Gemini is down.")
    run = start(tenant_a, answer_modes=["hybrid"])
    evaluator.run_all()
    body = detail(tenant_a, run["id"])
    assert body["status"] == "succeeded"
    failed = results_for(body, unanswerable.key)["hybrid"]
    assert failed["outcome"] == "provider_error"
    assert failed["abstention_correct"] is None and failed["abstain_reason"] is None
    assert (failed["error_stage"], failed["error_category"]) == ("generation", "unavailable")
    answers = body["summary"]["answers"]["hybrid"]
    assert answers["provider_errors"] == 1
    assert answers["decided"] == QUESTIONS - 1
    assert answers["confusion"]["unanswerable"] == {
        "answered": 0,
        "abstained": DATASET.unanswerable - 1,
        "provider_error": 1,
    }
    assert answers["abstention_accuracy"] == 1.0  # over decided questions only
    # A 5xx may have been counted by Gemini: its slot stays used.
    assert slots_used("evaluation-generations-daily") == QUESTIONS


def test_a_failed_query_embedding_leaves_retrieval_metrics_unmeasured(
    tenant_a: Tenant, evaluator: Harness
) -> None:
    seed_demo_corpus(tenant_a.workspace_id)
    evaluator.embedder.failures["q02"] = ProviderUnavailable("Gemini is down.")
    run = start(tenant_a, answer_modes=["hybrid"])
    evaluator.run_all()
    body = detail(tenant_a, run["id"])
    q02 = results_for(body, "q02")
    assert {r["error_stage"] for r in q02.values()} == {"embedding"}
    assert all(r["hit_at_5"] is None and r["reciprocal_rank"] is None for r in q02.values())
    assert q02["hybrid"]["outcome"] == "provider_error"
    vector = body["summary"]["retrieval"]["vector"]
    assert vector["errors"] == 1 and vector["questions"] == DATASET.answerable - 1
    assert len(evaluator.generator.prompts) == QUESTIONS - 1  # nothing to answer with


def test_the_daily_quota_stops_the_run_and_gives_back_the_rest(
    tenant_a: Tenant, evaluator: Harness
) -> None:
    seed_demo_corpus(tenant_a.workspace_id)
    third = DATASET.questions[2]
    evaluator.generator.failures[third.question] = ProviderQuotaExhausted("Daily quota used up.")
    run = start(tenant_a, answer_modes=["vector", "hybrid"])
    assert slots_used("evaluation-generations-daily") == 2 * QUESTIONS  # reserved up front
    evaluator.run_all()
    body = detail(tenant_a, run["id"])
    assert body["status"] == "failed"
    assert body["error_message"] == "Daily quota used up."
    assert body["questions_done"] == 3
    stopped = results_for(body, third.key)
    assert stopped["vector"]["error_category"] == "quota_exhausted"
    assert stopped["hybrid"]["error_category"] == "skipped"  # never sent
    assert [q for q in body["questions"] if q["results"]][-1]["question"]["key"] == third.key
    # 4 generations ran (2 questions x 2 modes); the refused one and the rest are given back.
    assert slots_used("evaluation-generations-daily") == 4
    assert slots_used("query-embeddings-daily") == 3
    assert evaluator.run_all() == 0  # a stopped run is not picked up again


def test_a_change_to_the_documents_stops_the_run(tenant_a: Tenant, evaluator: Harness) -> None:
    seed_demo_corpus(tenant_a.workspace_id)
    run = start(tenant_a, answer_modes=[])
    assert evaluator.worker.run_once()
    add_source(tenant_a.workspace_id, "new.md", [("new", basis((30, 1)))])
    evaluator.run_all()
    body = detail(tenant_a, run["id"])
    assert body["status"] == "failed" and body["error_message"] == CORPUS_CHANGED
    assert body["questions_done"] == 1


# --- the queue -------------------------------------------------------------------------------


def test_one_active_run_per_workspace(
    tenant_a: Tenant, tenant_b: Tenant, evaluator: Harness
) -> None:
    seed_demo_corpus(tenant_a.workspace_id)
    seed_demo_corpus(tenant_b.workspace_id)
    start(tenant_a, answer_modes=[])
    second = tenant_a.client.post(f"{EVALUATIONS}/run", json={"answer_modes": []})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "evaluation_running"
    start(tenant_b, answer_modes=[])  # another workspace is independent


def test_the_daily_budget_is_reserved_all_or_nothing(
    tenant_a: Tenant, evaluator: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_EVALUATION_GENERATION_LIMIT", str(QUESTIONS + 1))
    get_settings.cache_clear()
    seed_demo_corpus(tenant_a.workspace_id)
    refused = tenant_a.client.post(
        f"{EVALUATIONS}/run", json={"answer_modes": ["vector", "hybrid"]}
    )
    assert refused.status_code == 429 and "Retry-After" in refused.headers
    assert slots_used("evaluation-runs-daily") == 0
    assert slots_used("query-embeddings-daily") == 0
    assert slots_used("evaluation-generations-daily") == 0
    start(tenant_a, answer_modes=["hybrid"])  # one mode fits


def test_runs_per_workspace_per_day_are_limited(
    tenant_a: Tenant, evaluator: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_EVALUATION_RUN_LIMIT", "1")
    get_settings.cache_clear()
    seed_demo_corpus(tenant_a.workspace_id)
    start(tenant_a, answer_modes=[])
    evaluator.run_all()
    again = tenant_a.client.post(f"{EVALUATIONS}/run", json={"answer_modes": []})
    assert again.status_code == 429


def test_a_crashed_workers_question_is_redone_and_a_stale_worker_cannot_write(
    tenant_a: Tenant, evaluator: Harness
) -> None:
    seed_demo_corpus(tenant_a.workspace_id)
    run = start(tenant_a, answer_modes=[])
    crashed = evaluator.worker.claim()  # claimed, then the worker "dies"
    assert crashed is not None
    assert evaluator.worker.claim() is None  # still leased
    with get_engine().begin() as connection:
        connection.execute(
            text("UPDATE evaluation_runs SET locked_at = :t WHERE id = :id"),
            {"t": datetime.now(UTC) - timedelta(hours=1), "id": run["id"]},
        )
    other = make_evaluator(evaluator.embedder, evaluator.generator, "other-worker")
    assert other.run_once()  # takes over and finishes the first question
    # The stale worker comes back: its fencing token no longer matches, so it writes nothing.
    evaluator.worker.process(crashed, Deadline(60))
    body = detail(tenant_a, run["id"])
    assert body["questions_done"] == 1
    assert all(len(q["results"]) in (0, 5) for q in body["questions"])


def test_a_question_that_keeps_crashing_the_worker_fails_the_run(
    tenant_a: Tenant, evaluator: Harness
) -> None:
    seed_demo_corpus(tenant_a.workspace_id)
    run = start(tenant_a, answer_modes=[])
    for _ in range(get_settings().job_max_attempts + 1):
        assert evaluator.worker.claim() is not None
        with get_engine().begin() as connection:
            connection.execute(
                text("UPDATE evaluation_runs SET locked_at = :t WHERE id = :id"),
                {"t": datetime.now(UTC) - timedelta(hours=1), "id": run["id"]},
            )
    assert evaluator.worker.run_once()
    body = detail(tenant_a, run["id"])
    assert body["status"] == "failed"
    assert "q01" in body["error_message"]


def test_no_database_connection_is_held_while_gemini_works(
    tenant_a: Tenant, evaluator: Harness
) -> None:
    seed_demo_corpus(tenant_a.workspace_id)
    held: list[int] = []
    pool = get_engine().pool
    evaluator.embedder.on_call = lambda: held.append(pool.checkedout())  # type: ignore[attr-defined]
    evaluator.generator.on_call = lambda: held.append(pool.checkedout())  # type: ignore[attr-defined]
    start(tenant_a, answer_modes=["vector", "hybrid"])
    assert evaluator.worker.run_once()
    assert held == [0, 0, 0]


def test_a_dataset_file_edited_without_a_new_version_is_refused(
    tenant_a: Tenant, evaluator: Harness
) -> None:
    tenant_a.client.get(f"{EVALUATIONS}/questions")
    with get_engine().begin() as connection:
        connection.execute(text("UPDATE evaluation_questions SET dataset_sha256 = 'old'"))
    response = tenant_a.client.get(f"{EVALUATIONS}/questions")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "evaluation_dataset_changed"


# --- isolation and validation ----------------------------------------------------------------


def test_runs_are_isolated_between_workspaces(
    tenant_a: Tenant, tenant_b: Tenant, evaluator: Harness
) -> None:
    seed_demo_corpus(tenant_a.workspace_id)
    run = start(tenant_a, answer_modes=[])
    evaluator.run_all()
    assert tenant_b.client.get(f"{EVALUATIONS}/runs").json() == []
    response = tenant_b.client.get(f"{EVALUATIONS}/runs/{run['id']}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert tenant_b.client.get(f"{EVALUATIONS}/runs/{uuid.uuid4()}").status_code == 404
    # B's corpus check sees only B's documents.
    assert tenant_b.client.get(f"{EVALUATIONS}/questions").json()["corpus"]["ready"] is False


def test_evaluation_endpoints_require_authentication(evaluator: Harness) -> None:
    anonymous = new_client()
    for method, path in (
        ("GET", "/questions"),
        ("POST", "/run"),
        ("GET", "/runs"),
        ("GET", f"/runs/{uuid.uuid4()}"),
    ):
        response = anonymous.request(method, f"{EVALUATIONS}{path}", json={})
        assert response.status_code == 401, path


@pytest.mark.parametrize(
    "body",
    [
        {"answer_modes": ["hybrid_or"]},
        {"answer_modes": ["hybrid", "hybrid"]},
        {"answer_modes": "hybrid"},
        {"workspace_id": str(uuid.uuid4())},
    ],
)
def test_invalid_run_requests_are_rejected(
    tenant_a: Tenant, evaluator: Harness, body: dict[str, object]
) -> None:
    seed_demo_corpus(tenant_a.workspace_id)
    response = tenant_a.client.post(f"{EVALUATIONS}/run", json=body)
    assert response.status_code == 422
    assert scalar("SELECT count(*) FROM evaluation_runs") == 0


def test_the_run_belongs_to_the_session_workspace(tenant_a: Tenant, evaluator: Harness) -> None:
    seed_demo_corpus(tenant_a.workspace_id)
    run = start(tenant_a, answer_modes=[])
    with get_sessionmaker()() as session:
        bind_workspace(session, tenant_a.workspace_id)
        stored = session.get(EvaluationRun, uuid.UUID(run["id"]))
        assert stored is not None
        assert stored.workspace_id == tenant_a.workspace_id
        assert stored.created_by_user_id == tenant_a.user_id


def test_results_computed_after_losing_the_run_are_discarded(
    tenant_a: Tenant, evaluator: Harness
) -> None:
    """The fence on the final write: the run changed hands while Gemini was working."""
    seed_demo_corpus(tenant_a.workspace_id)
    run = start(tenant_a, answer_modes=[])
    slow = evaluator.worker.claim()
    assert slow is not None

    def taken_over() -> None:
        with get_engine().begin() as connection:
            connection.execute(
                text("UPDATE evaluation_runs SET lock_token = :t WHERE id = :id"),
                {"t": uuid.uuid4(), "id": run["id"]},
            )

    evaluator.embedder.on_call = taken_over
    evaluator.worker.process(slow, Deadline(60))
    assert scalar("SELECT count(*) FROM evaluation_results") == 0
    assert scalar("SELECT questions_done FROM evaluation_runs") == 0


def test_starting_a_run_from_another_origin_is_blocked(
    tenant_a: Tenant, evaluator: Harness
) -> None:
    """The Origin check (CSRF) covers evaluation runs like every other state change."""
    seed_demo_corpus(tenant_a.workspace_id)
    response = tenant_a.client.post(
        f"{EVALUATIONS}/run", json={}, headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "origin_not_allowed"
    assert scalar("SELECT count(*) FROM evaluation_runs") == 0
    assert slots_used("evaluation-runs-daily") == 0


def test_a_rate_limit_stops_the_run_instead_of_pushing_on(
    tenant_a: Tenant, evaluator: Harness
) -> None:
    """Owner decision: a 429 means stop and keep the partial results, not retry or continue."""
    seed_demo_corpus(tenant_a.workspace_id)
    second = DATASET.questions[1]
    evaluator.generator.failures[second.question] = ProviderRateLimited("Too many requests.")
    run = start(tenant_a, answer_modes=["hybrid"])
    evaluator.run_all()
    body = detail(tenant_a, run["id"])
    assert body["status"] == "failed" and body["error_message"] == "Too many requests."
    assert body["questions_done"] == 2
    stopped = results_for(body, second.key)["hybrid"]
    assert stopped["outcome"] == "provider_error"
    assert stopped["error_category"] == "rate_limited"
    assert stopped["abstention_correct"] is None
    assert len(evaluator.generator.prompts) == 2  # nothing after the refusal
    assert slots_used("evaluation-generations-daily") == 1  # the refused call is given back


# --- budget accounting for retried questions (M9 review fix) ---------------------------------


def expire_lease(run_id: str) -> None:
    with get_engine().begin() as connection:
        connection.execute(
            text("UPDATE evaluation_runs SET locked_at = :t WHERE id = :id"),
            {"t": datetime.now(UTC) - timedelta(hours=1), "id": run_id},
        )


def crash_before_saving(monkeypatch: pytest.MonkeyPatch, times: int = 1) -> None:
    """The next `times` attempts call Gemini, then the worker "dies" before saving."""
    original = EvaluationWorker._save
    left = {"n": times}

    def save(self: EvaluationWorker, *args: Any, **kwargs: Any) -> None:
        if left["n"] > 0:
            left["n"] -= 1
            raise RuntimeError("worker killed")
        original(self, *args, **kwargs)

    monkeypatch.setattr(EvaluationWorker, "_save", save)


def actual_calls(evaluator: Harness) -> tuple[int, int]:
    """Gemini calls the fake providers received (failed and discarded ones included)."""
    return len(evaluator.embedder.queries), len(evaluator.generator.prompts)


def counted() -> tuple[int, int]:
    return slots_used("query-embeddings-daily"), slots_used("evaluation-generations-daily")


def test_a_crash_after_provider_calls_is_covered_by_a_retry_reservation(
    tenant_a: Tenant, evaluator: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_demo_corpus(tenant_a.workspace_id)
    run = start(tenant_a, answer_modes=["vector", "hybrid"])
    crash_before_saving(monkeypatch)
    with pytest.raises(RuntimeError):
        evaluator.worker.run_once()  # q01: 1 embedding + 2 generations, never saved
    assert actual_calls(evaluator) == (1, 2)
    expire_lease(run["id"])
    evaluator.run_all()
    body = detail(tenant_a, run["id"])
    assert body["status"] == "succeeded" and body["questions_done"] == QUESTIONS
    assert actual_calls(evaluator) == (QUESTIONS + 1, 2 * QUESTIONS + 2)
    # Recorded usage covers the saved attempts; the retry reservation covers the lost one.
    assert body["summary"]["gemini_requests"] == {
        "query_embeddings": QUESTIONS,
        "generation_requests": 2 * QUESTIONS,
    }
    assert body["summary"]["retry_reservations"] == {
        "count": 1,
        "query_embeddings": 1,
        "generations": 2,
        "held_from_original": {"query_embeddings": 0, "generation_requests": 0},
    }
    assert counted() == actual_calls(evaluator)  # never fewer than the real calls


def test_a_crash_before_any_provider_call_over_counts_by_one_attempt_at_most(
    tenant_a: Tenant, evaluator: Harness
) -> None:
    seed_demo_corpus(tenant_a.workspace_id)
    run = start(tenant_a, answer_modes=["hybrid"])
    assert evaluator.worker.claim() is not None  # claimed, then died before calling Gemini
    expire_lease(run["id"])
    evaluator.run_all()
    assert detail(tenant_a, run["id"])["status"] == "succeeded"
    embeddings, generations = actual_calls(evaluator)
    assert (embeddings, generations) == (QUESTIONS, QUESTIONS)
    assert counted() == (embeddings + 1, generations + 1)  # conservative, bounded


def test_a_retry_without_budget_stops_the_run_without_calling_gemini(
    tenant_a: Tenant, evaluator: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DAILY_EVALUATION_GENERATION_LIMIT", str(QUESTIONS))
    get_settings.cache_clear()
    evaluator.worker = make_evaluator(evaluator.embedder, evaluator.generator, "eval-worker")
    seed_demo_corpus(tenant_a.workspace_id)
    run = start(tenant_a, answer_modes=["hybrid"])  # reserves every generation slot
    assert evaluator.worker.run_once() and evaluator.worker.run_once()  # q01, q02 saved
    crash_before_saving(monkeypatch)
    with pytest.raises(RuntimeError):
        evaluator.worker.run_once()  # q03 calls Gemini, never saved
    before = actual_calls(evaluator)
    expire_lease(run["id"])
    assert evaluator.worker.run_once()  # the retry: no room for its reservation
    body = detail(tenant_a, run["id"])
    assert body["status"] == "failed"
    assert body["error_message"].startswith("Question q03 had to be retried")
    assert body["questions_done"] == 2  # completed results are kept
    assert actual_calls(evaluator) == before  # no provider call for the retry
    assert body["summary"]["retry_reservations"]["count"] == 0
    assert body["summary"]["retry_reservations"]["held_from_original"] == {
        "query_embeddings": 1,
        "generation_requests": 1,
    }
    # Only genuinely unused original slots went back: q01, q02, and q03's interrupted attempt stay.
    assert counted() == (3, 3) == actual_calls(evaluator)


def test_maximum_attempts_end_the_run_without_another_reservation(
    tenant_a: Tenant, evaluator: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_demo_corpus(tenant_a.workspace_id)
    run = start(tenant_a, answer_modes=["hybrid"])
    attempts = get_settings().job_max_attempts
    crash_before_saving(monkeypatch, times=attempts)
    for _ in range(attempts):  # every allowed attempt calls Gemini and dies
        with pytest.raises(RuntimeError):
            evaluator.worker.run_once()
        expire_lease(run["id"])
    calls = actual_calls(evaluator)
    assert calls == (attempts, attempts)
    assert evaluator.worker.run_once()  # the terminal claim: no Gemini, no reservation
    body = detail(tenant_a, run["id"])
    assert body["status"] == "failed" and "q01" in body["error_message"]
    assert actual_calls(evaluator) == calls
    assert body["summary"]["retry_reservations"]["count"] == attempts - 1
    assert counted() == calls  # original slot held for attempt 1, retries for the rest


def test_a_terminal_claim_after_an_interrupted_attempt_reserves_nothing(
    tenant_a: Tenant, evaluator: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The corpus changed after a crash: the run stops before any reservation or call."""
    seed_demo_corpus(tenant_a.workspace_id)
    run = start(tenant_a, answer_modes=["hybrid"])
    crash_before_saving(monkeypatch)
    with pytest.raises(RuntimeError):
        evaluator.worker.run_once()
    expire_lease(run["id"])
    add_source(tenant_a.workspace_id, "new.md", [("new", basis((30, 1)))])
    assert evaluator.worker.run_once()
    body = detail(tenant_a, run["id"])
    assert body["status"] == "failed" and body["error_message"] == CORPUS_CHANGED
    assert body["summary"]["retry_reservations"]["count"] == 0
    assert counted() == actual_calls(evaluator) == (1, 1)


def test_a_retry_reservation_is_given_back_when_ownership_is_lost_before_it_is_recorded(
    tenant_a: Tenant, evaluator: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ownership is lost before the retry reservation is recorded on the run.

    The reservation is taken, then the fenced update that records it finds another lock token, so
    this worker makes no provider call and releases the reservation. (A reservation already recorded
    is kept, even if ownership is lost before the first call.)
    """
    seed_demo_corpus(tenant_a.workspace_id)
    run = start(tenant_a, answer_modes=["hybrid"])
    assert evaluator.worker.claim() is not None
    expire_lease(run["id"])
    retry = evaluator.worker.claim()
    assert retry is not None and retry.attempts == 2
    with get_engine().begin() as connection:  # another worker took it over meanwhile
        connection.execute(
            text("UPDATE evaluation_runs SET lock_token = :t WHERE id = :id"),
            {"t": uuid.uuid4(), "id": run["id"]},
        )
    evaluator.worker.process(retry, Deadline(60))
    assert actual_calls(evaluator) == (0, 0)
    assert counted() == (QUESTIONS, QUESTIONS)  # only the original reservation


def window_counts(rule: str) -> dict[str, int]:
    with get_engine().connect() as connection:
        rows = connection.execute(
            text("SELECT window_start, count FROM rate_limit_counters WHERE bucket_key LIKE :k"),
            {"k": f"{rule}:%"},
        ).all()
    return {window.date().isoformat(): int(count) for window, count in rows}


def test_a_retry_after_midnight_is_charged_to_the_new_day_and_kept(evaluator: Harness) -> None:
    """Retry reservations land in the current UTC day; giving back the original never touches
    them. (The previous day's counters are pruned once a new day starts: they no longer limit.)"""
    settings, limiter = get_settings(), evaluator.worker.limiter
    late = datetime(2026, 11, 4, 23, 59, tzinfo=UTC)
    original = budget.reserve(
        limiter, settings, workspace_id="ws", query_embeddings=26, generations=26, now=late
    )
    assert window_counts("evaluation-generations-daily") == {"2026-11-04": 26}
    budget.reserve_retry(limiter, settings, generations=1, now=late + timedelta(minutes=2))
    assert window_counts("evaluation-generations-daily") == {"2026-11-05": 1}
    budget.give_back_unused(
        limiter,
        settings,
        original.as_json(),
        {"query_embeddings": 3, "generation_requests": 3},
    )
    assert window_counts("evaluation-generations-daily") == {"2026-11-05": 1}
    assert window_counts("query-embeddings-daily") == {"2026-11-05": 1}


def test_giving_back_within_one_day_returns_only_the_original_unused_slots(
    evaluator: Harness,
) -> None:
    settings, limiter = get_settings(), evaluator.worker.limiter
    now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
    original = budget.reserve(
        limiter, settings, workspace_id="ws", query_embeddings=26, generations=52, now=now
    )
    budget.reserve_retry(limiter, settings, generations=2, now=now + timedelta(minutes=1))
    budget.give_back_unused(
        limiter, settings, original.as_json(), {"query_embeddings": 5, "generation_requests": 10}
    )
    assert window_counts("evaluation-generations-daily") == {"2026-11-04": 10 + 2}
    assert window_counts("query-embeddings-daily") == {"2026-11-04": 5 + 1}


# --- failed-attempt latency (M9 review fix) -------------------------------------------------


def test_a_failed_generation_keeps_its_time_apart_from_successful_answers(
    tenant_a: Tenant, evaluator: Harness
) -> None:
    seed_demo_corpus(tenant_a.workspace_id)
    unanswerable = next(q for q in DATASET.questions if not q.answerable)
    evaluator.generator.failures[unanswerable.question] = ProviderUnavailable("Gemini is down.")
    run = start(tenant_a, answer_modes=["hybrid"])
    evaluator.run_all()
    body = detail(tenant_a, run["id"])
    failed = results_for(body, unanswerable.key)["hybrid"]
    assert failed["answer_latency_ms"] is None
    assert failed["details"]["failed_attempt_ms"] >= failed["details"]["failed_generation_ms"] >= 0
    answers = body["summary"]["answers"]["hybrid"]
    assert answers["latency_ms"]["count"] == QUESTIONS - 1  # successful answers only
    assert answers["generation_latency_ms"]["count"] == QUESTIONS - 1
    assert answers["failed_attempt_latency_ms"]["count"] == 1
    assert answers["failed_generation_latency_ms"]["count"] == 1


def test_a_failed_query_embedding_stays_out_of_embedding_latency(
    tenant_a: Tenant, evaluator: Harness
) -> None:
    seed_demo_corpus(tenant_a.workspace_id)
    evaluator.embedder.failures["q02"] = ProviderUnavailable("Gemini is down.")
    run = start(tenant_a, answer_modes=["hybrid"])
    evaluator.run_all()
    body = detail(tenant_a, run["id"])
    q02 = results_for(body, "q02")
    assert all(r["embedding_latency_ms"] is None for r in q02.values())
    assert q02["vector"]["details"]["failed_embedding_ms"] >= 0
    assert "failed_attempt_ms" in q02["hybrid"]["details"]  # the answer attempt failed too
    assert "failed_attempt_ms" not in q02["vector"]["details"]  # vector wasn't answered
    summary = body["summary"]
    assert summary["embedding_latency_ms"]["count"] == QUESTIONS - 1
    assert summary["failed_embedding_latency_ms"]["count"] == 1
    assert summary["answers"]["hybrid"]["failed_attempt_latency_ms"]["count"] == 1
