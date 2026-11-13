"""The Asia Cement follow-up extension (Experiment 006) through the real API and worker.

No Gemini: query embeddings point at chosen chunks of a stand-in corpus (see
test_evaluation_asia.py). Covers dataset selection for both experiment workspaces,
the 40-embedding reservation, mandatory-only evidence scoring, historical runs, and
Northstar.
"""

from collections import Counter
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db.session import get_engine
from app.evaluation.dataset import (
    ASIA_CEMENT_ID,
    ASIA_FOLLOWUPS_ID,
    load_asia_followups,
    load_dataset,
)
from app.evaluation.runner import EvaluationWorker
from app.providers.interfaces import QUERY_TASK, EmbeddingResult
from app.retrieval.followup import contextualize
from tests.fakes.asia_cement import FULL
from tests.fakes.retrieval import DIMS, MODEL, basis
from tests.integration.conftest import Tenant
from tests.integration.test_evaluation import (
    EVALUATIONS,
    detail,
    make_evaluator,
    seed_demo_corpus,
    slots_used,
    start,
)
from tests.integration.test_evaluation_asia import (
    AsiaCorpus,
    AsiaGenerator,
    asia_harness,
    assign,
    overview,
    register,
    rows_for,
    seed_asia_corpus,
)

pytestmark = pytest.mark.integration

FOLLOWUPS = load_asia_followups()
ASIA = FULL  # public development split + synthetic held-out (the real file is private)
NORTHSTAR = load_dataset()
JOINED = {contextualize(q.question, q.previous_question): q.key for q in FOLLOWUPS.questions}
ALONE = {q.question: q.key for q in FOLLOWUPS.questions}
ALONE_TEXT = {q.key: q.question for q in FOLLOWUPS.questions}
KEYS = [q.key for q in FOLLOWUPS.questions]
ANSWERABLE = {q.key for q in FOLLOWUPS.questions if q.answerable}


@dataclass
class FollowupEmbedder:
    """Points each query (joined, or the follow-up alone) at its first evidence chunk."""

    corpus: AsiaCorpus
    targets: dict[str, str] = field(default_factory=dict)  # question key -> chunk id
    queries: list[str] = field(default_factory=list)

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingResult:
        raise AssertionError("an evaluation never embeds documents")

    def embed_query(self, query: str) -> EmbeddingResult:
        self.queries.append(query)
        key = JOINED[query] if query in JOINED else ALONE[query]
        question = next(q for q in FOLLOWUPS.questions if q.key == key)
        first = question.evidence[0] if question.evidence else None
        chunk = self.targets.get(key) or (
            self.corpus.evidence[(first.source_key, first.page or 1)]
            if first
            else self.corpus.decoys["terms"][0]
        )
        return EmbeddingResult(
            vectors=[basis((self.corpus.axis[chunk], 1.0))],
            model=MODEL,
            dimensions=DIMS,
            task_type=QUERY_TASK,
            latency_ms=0,
            requests=1,
            attempts=1,
        )


@dataclass
class FollowupHarness:
    worker: EvaluationWorker
    embedder: FollowupEmbedder

    def run_all(self) -> None:
        while self.worker.run_once():
            pass


def followup_harness(corpus: AsiaCorpus, name: str = "followup-worker") -> FollowupHarness:
    embedder = FollowupEmbedder(corpus)
    worker = make_evaluator(embedder, AsiaGenerator(), name)  # type: ignore[arg-type]
    return FollowupHarness(worker, embedder)


@pytest.fixture
def followups_env(
    monkeypatch: pytest.MonkeyPatch, tenant_a: Tenant, tenant_b: Tenant
) -> Iterator[None]:
    """Gemini "configured" (never called); both workspaces assigned to the extension,
    as the paragraph-v1 and section-v1 experiment workspaces will be."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key-not-real")  # never sent anywhere
    register(monkeypatch)  # asia-cement-realworld, for the historical-run test
    monkeypatch.setenv("EVALUATION_QUESTION_INTERVAL_SECONDS", "0")
    assign(
        monkeypatch,
        {tenant_a.workspace_id: ASIA_FOLLOWUPS_ID, tenant_b.workspace_id: ASIA_FOLLOWUPS_ID},
    )
    yield
    get_settings.cache_clear()


def test_both_workspaces_get_the_extension_and_others_keep_northstar(
    tenant_a: Tenant, tenant_b: Tenant, followups_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    for tenant in (tenant_a, tenant_b):
        body = overview(tenant)
        dataset = body["dataset"]
        assert (dataset["id"], dataset["name"], dataset["version"]) == (
            ASIA_FOLLOWUPS_ID,
            FOLLOWUPS.name,
            "1.0.0",
        )
        assert dataset["sha256"] == FOLLOWUPS.sha256
        assert (dataset["questions"], dataset["held_out_questions"]) == (20, 0)
        assert (dataset["answerable"], dataset["unanswerable"]) == (18, 2)
        assert [q["key"] for q in body["questions"]] == KEYS
        assert body["limits"]["query_embeddings_per_run"] == 40  # 20 production + 20 alone
        assert body["limits"]["generations_per_answer_mode"] == 20

    assign(monkeypatch, {tenant_a.workspace_id: ASIA_FOLLOWUPS_ID})  # B unassigned again
    northstar = overview(tenant_b)
    assert northstar["dataset"]["name"] == NORTHSTAR.name
    assert northstar["limits"]["query_embeddings_per_run"] == 26  # unchanged, no diagnostics
    assert overview(tenant_a)["dataset"]["name"] == FOLLOWUPS.name


def test_a_retrieval_only_run_reserves_and_uses_exactly_40_query_embeddings(
    tenant_a: Tenant, followups_env: None
) -> None:
    harness = followup_harness(seed_asia_corpus(tenant_a.workspace_id, datasets=[FOLLOWUPS]))
    mapping = overview(tenant_a)["dataset"]["evidence_mapping"]
    assert mapping["quotations"] == mapping["matched"] == 29  # mandatory quotations only
    assert mapping["questions"] == mapping["questions_fully_mapped"] == 18

    run = start(tenant_a, answer_modes=[])
    assert run["dataset_name"] == FOLLOWUPS.name and run["questions_total"] == 20
    config = run["config"]
    assert config["dataset_id"] == ASIA_FOLLOWUPS_ID and config["question_keys"] == KEYS
    assert config["diagnostics"]["follow_up_alone"] is True
    with get_engine().connect() as connection:  # what the run reserved, as stored
        reserved = connection.execute(
            text("SELECT config->'budget'->>'query_embeddings' FROM evaluation_runs WHERE id = :i"),
            {"i": run["id"]},
        ).scalar_one()
    assert reserved == "40"
    assert slots_used("query-embeddings-daily") == 40
    assert slots_used("evaluation-generations-daily") == 0

    harness.run_all()
    body = detail(tenant_a, run["id"])
    assert body["status"] == "succeeded" and body["questions_done"] == 20
    assert len(harness.embedder.queries) == 40
    # Two embeddings per question: the production query and the follow-up alone. For the
    # 7 long questions without a follow-up cue, production doesn't add the previous
    # question (retrieval/followup.py), so both embeddings are of the same text.
    resolved = Counter(JOINED.get(q) or ALONE[q] for q in harness.embedder.queries)
    assert resolved == dict.fromkeys(KEYS, 2)
    same_text = sorted(k for k, q in ALONE_TEXT.items() if q in JOINED)
    assert same_text == ["FU-004", "FU-007", "FU-013", "FU-014", "FU-015", "FU-016", "FU-018"]
    assert body["summary"]["gemini_requests"] == {"query_embeddings": 40, "generation_requests": 0}
    assert slots_used("query-embeddings-daily") == 40  # all used, none extra
    # Every follow-up, new FU- ones included, gets the follow-up-alone diagnostic.
    for key in KEYS:
        vector = rows_for(body, key)["vector"]["details"]["diagnostics"]
        assert vector["follow_up_alone"]["query"] == ALONE_TEXT[key], key
    assert set(body["corpus"]["passages"]) == ANSWERABLE


def test_fu_013_is_scored_on_its_mandatory_evidence_only(
    tenant_a: Tenant, followups_env: None
) -> None:
    harness = followup_harness(seed_asia_corpus(tenant_a.workspace_id, datasets=[FOLLOWUPS]))
    run = start(tenant_a, answer_modes=[])
    harness.run_all()
    body = detail(tenant_a, run["id"])
    passages = body["corpus"]["passages"]["FU-013"]["evidence"]
    assert [(e["source_key"], e["page"], e["status"]) for e in passages] == [("acp", 13, "matched")]
    # The Anti-Corruption Policy passage alone is a full hit: the corroborating Business
    # Partner Code sentence isn't in the corpus at all, and it isn't required.
    vector = rows_for(body, "FU-013")["vector"]
    assert vector["hit_at_5"] is True and vector["recall_at_5"] == 1.0
    item = next(q for q in body["questions"] if q["question"]["key"] == "FU-013")
    assert [e["source_key"] for e in item["question"]["evidence"]] == ["acp"]
    assert item["question"]["expected_source_keys"] == ["acp"]
    with get_engine().connect() as connection:  # recorded apart, for reviewers
        reference = connection.execute(
            text(
                "SELECT reference FROM evaluation_questions WHERE dataset_name = :d"
                " AND question_key = 'FU-013'"
            ),
            {"d": FOLLOWUPS.name},
        ).scalar_one()
    assert [e["source_key"] for e in reference["evidence"]] == ["acp"]
    assert [e["source_key"] for e in reference["corroborating_evidence"]] == ["bpcc"]
    assert reference["follow_up_type"] == "self_contained"


def test_historical_runs_keep_their_dataset_when_the_mapping_changes(
    tenant_a: Tenant, followups_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = seed_asia_corpus(tenant_a.workspace_id, datasets=[ASIA, FOLLOWUPS])
    assign(monkeypatch, {tenant_a.workspace_id: ASIA_CEMENT_ID})
    old = start(tenant_a, answer_modes=[])
    asia_harness(corpus).run_all()
    before = detail(tenant_a, old["id"])
    assert before["dataset_name"] == ASIA.name and len(before["questions"]) == 16

    assign(monkeypatch, {tenant_a.workspace_id: ASIA_FOLLOWUPS_ID})
    new = start(tenant_a, answer_modes=[])
    followup_harness(corpus).run_all()

    assert detail(tenant_a, old["id"]) == before  # the old run reads exactly as before
    after = detail(tenant_a, new["id"])
    assert after["dataset_name"] == FOLLOWUPS.name and len(after["questions"]) == 20
    listed = [r["id"] for r in tenant_a.client.get(f"{EVALUATIONS}/runs").json()]
    assert listed == [new["id"], old["id"]]
    with get_engine().connect() as connection:  # each dataset keeps its own question rows
        stored: dict[str, int] = dict(
            connection.execute(
                text(
                    "SELECT dataset_name, count(*) FROM evaluation_questions GROUP BY dataset_name"
                )
            ).all()
        )
    assert stored == {ASIA.name: len(ASIA.questions), FOLLOWUPS.name: 20}


def test_northstar_runs_are_unchanged_next_to_the_extension(
    tenant_b: Tenant, followups_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.integration.test_evaluation import (  # noqa: PLC0415
        RoutingEmbedder,
        ScriptedGenerator,
    )

    assign(monkeypatch, {})
    seed_demo_corpus(tenant_b.workspace_id)
    embedder = RoutingEmbedder()
    worker = make_evaluator(embedder, ScriptedGenerator(), "northstar-worker")
    run = start(tenant_b, answer_modes=[])
    assert run["dataset_name"] == NORTHSTAR.name and run["questions_total"] == 26
    assert "diagnostics" not in run["config"]
    assert slots_used("query-embeddings-daily") == 26
    while worker.run_once():
        pass
    assert len(embedder.queries) == 26
