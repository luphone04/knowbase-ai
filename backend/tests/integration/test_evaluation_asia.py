"""The Asia Cement benchmark next to Northstar: dataset selection by workspace id, the
development split only, passage-level relevance, and isolation (no Gemini).

The real benchmark's held-out split is private (not in the repository), so these tests
register a full-shaped benchmark made of the public development split plus two synthetic
held-out questions (tests/fakes/asia_cement.py) under `asia-cement-realworld`.

The corpus is a small stand-in for the four PDFs: one source per benchmark file
(same original file name and SHA-256), one chunk per cited page holding that page's
evidence quotations, and two decoy chunks per document. Query embeddings point at a
chosen chunk, so each test decides exactly what retrieval returns.
"""

import json
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import pytest
from sqlalchemy import text

from app.chat.prompt import ABSTENTION
from app.config import get_settings
from app.db.session import get_engine
from app.evaluation import dataset as datasets
from app.evaluation.dataset import (
    ASIA_CEMENT_DEV_ID,
    ASIA_CEMENT_ID,
    Dataset,
    DatasetUnavailable,
    load_dataset,
)
from app.evaluation.runner import EvaluationWorker
from app.providers.interfaces import QUERY_TASK, EmbeddingResult, GenerationResult, TokenUsage
from app.retrieval.followup import contextualize
from tests.fakes.asia_cement import DEV as DEV_SPLIT
from tests.fakes.asia_cement import FULL
from tests.fakes.retrieval import DIMS, MODEL, basis, literal
from tests.integration.conftest import Tenant
from tests.integration.test_evaluation import (
    EVALUATIONS,
    detail,
    make_evaluator,
    seed_demo_corpus,
    slots_used,
    start,
)

pytestmark = pytest.mark.integration

ASIA = FULL  # the public development split + synthetic held-out questions
NORTHSTAR = load_dataset()
DEV = [q for q in ASIA.questions if q.split == "dev"]
HELD_OUT = [q for q in ASIA.questions if q.split == "heldout"]
QUERY_KEY = {contextualize(q.question, q.previous_question): q.key for q in ASIA.questions}
# Diagnostics also embed each follow-up on its own (experimental, never production scoring).
ALONE_KEY = {q.question: q.key for q in ASIA.questions if q.previous_question}
DEV_FOLLOW_UPS = [q.key for q in DEV if q.previous_question]


@dataclass
class AsiaCorpus:
    """Chunk ids by (source key, page) for evidence, and the decoys of each source."""

    evidence: dict[tuple[str, int], str] = field(default_factory=dict)
    decoys: dict[str, list[str]] = field(default_factory=dict)
    axis: dict[str, int] = field(default_factory=dict)  # chunk id -> its basis axis


def seed_asia_corpus(
    workspace_id: uuid.UUID,
    *,
    drop_quote: str | None = None,
    wrong_hash: str | None = None,
    datasets: Sequence[Dataset] = (ASIA,),
) -> AsiaCorpus:
    """The benchmark's files as sources, with the evidence pages of `datasets` (the
    follow-up extension uses the same four files); `drop_quote` leaves one quotation out."""
    corpus = AsiaCorpus()
    pages: dict[tuple[str, int], list[str]] = {}
    for q in (q for dataset in datasets for q in dataset.questions):
        for e in q.evidence:
            quotes = pages.setdefault((e.source_key, e.page or 1), [])
            if e.quote not in quotes and e.quote != drop_quote:
                quotes.append(e.quote)
    with get_engine().begin() as connection:
        for key, digest in sorted(ASIA.source_hashes.items()):
            source_id, document_id = uuid.uuid4(), uuid.uuid4()
            name = ASIA.file_name(key)
            connection.execute(
                text(
                    "INSERT INTO sources (id, workspace_id, kind, display_name, original_filename,"
                    " file_type, status, file_sha256, last_indexed_at)"
                    " VALUES (:id, :ws, 'file', :name, :name, 'pdf', 'ready', :sha, now())"
                ),
                {
                    "id": source_id,
                    "ws": workspace_id,
                    "name": name,
                    "sha": "0" * 64 if key == wrong_hash else digest,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO documents (id, workspace_id, source_id, title, content_hash,"
                    " text_content) VALUES (:id, :ws, :src, :title, 'h', 't')"
                ),
                {"id": document_id, "ws": workspace_id, "src": source_id, "title": name},
            )
            chunks: list[tuple[str, int, str | None]] = [
                (" ".join(quotes) or "Intentionally blank page text.", page, None)
                for (source, page), quotes in sorted(pages.items())
                if source == key
            ]
            chunks += [(f"{key} unrelated decoy section {n}.", 1, "decoy") for n in range(2)]
            for index, (content, page, kind) in enumerate(chunks):
                chunk_id = str(uuid.uuid4())
                axis = len(corpus.axis)
                corpus.axis[chunk_id] = axis
                if kind == "decoy":
                    corpus.decoys.setdefault(key, []).append(chunk_id)
                else:
                    corpus.evidence[(key, page)] = chunk_id
                connection.execute(
                    text(
                        "INSERT INTO chunks (id, workspace_id, document_id, chunk_index, content,"
                        " content_hash, embedding, embedding_model, metadata) VALUES (:id, :ws,"
                        " :doc, :i, :content, :hash, CAST(:vec AS vector), :model,"
                        " CAST(:meta AS jsonb))"
                    ),
                    {
                        "id": chunk_id,
                        "ws": workspace_id,
                        "doc": document_id,
                        "i": index,
                        "content": content,
                        "hash": uuid.uuid4().hex,
                        "vec": literal(basis((axis, 1))),
                        "model": MODEL,
                        "meta": json.dumps({"page_start": page, "page_end": page}),
                    },
                )
    return corpus


@dataclass
class AsiaEmbedder:
    """Points each question's query at its first evidence chunk, or at `targets`."""

    corpus: AsiaCorpus
    targets: dict[str, list[tuple[str, float]]] = field(default_factory=dict)  # key -> weights
    alone_targets: dict[str, list[tuple[str, float]]] = field(default_factory=dict)
    queries: list[str] = field(default_factory=list)

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingResult:
        raise AssertionError("an evaluation never embeds documents")

    def embed_query(self, query: str) -> EmbeddingResult:
        self.queries.append(query)
        if query in QUERY_KEY:
            key = QUERY_KEY[query]
            weights = self.targets.get(key)
        else:  # a follow-up embedded on its own (diagnostics)
            key = ALONE_KEY[query]
            weights = self.alone_targets.get(key)
        question = next(q for q in ASIA.questions if q.key == key)
        if weights is None:
            first = question.evidence[0] if question.evidence else None
            chunk = (
                self.corpus.evidence[(first.source_key, first.page or 1)]
                if first
                else self.corpus.decoys["terms"][0]
            )
            weights = [(chunk, 1.0)]
        vector = basis(*[(self.corpus.axis[chunk], weight) for chunk, weight in weights])
        return EmbeddingResult(
            vectors=[vector],
            model=MODEL,
            dimensions=DIMS,
            task_type=QUERY_TASK,
            latency_ms=0,
            requests=1,
            attempts=1,
        )


@dataclass
class AsiaGenerator:
    prompts: list[str] = field(default_factory=list)

    def generate(
        self,
        prompt: str,
        *,
        system_instruction: str | None = None,
        max_output_tokens: int | None = None,
    ) -> GenerationResult:
        self.prompts.append(prompt)
        question = prompt.split("<question>\n", 1)[1].split("\n</question>", 1)[0]
        unanswerable = {q.question for q in ASIA.questions if not q.answerable}
        return GenerationResult(
            text=ABSTENTION if question in unanswerable else "It is stated [S1].",
            model="mock-generation",
            finish_reason="STOP",
            usage=TokenUsage(prompt_tokens=100, output_tokens=10, total_tokens=110),
            latency_ms=5,
            attempts=1,
        )


@dataclass
class AsiaHarness:
    worker: EvaluationWorker
    embedder: AsiaEmbedder
    generator: AsiaGenerator
    corpus: AsiaCorpus

    def run_all(self, limit: int = 100) -> int:
        ran = 0
        while ran < limit and self.worker.run_once():
            ran += 1
        return ran


def assign(monkeypatch: pytest.MonkeyPatch, assignments: dict[uuid.UUID, str]) -> None:
    monkeypatch.setenv(
        "EVALUATION_WORKSPACE_DATASETS",
        json.dumps({str(workspace): dataset for workspace, dataset in assignments.items()}),
    )
    get_settings.cache_clear()


def register(monkeypatch: pytest.MonkeyPatch, dataset: Dataset = ASIA) -> None:
    """Serve `dataset` as asia-cement-realworld (its real file is private)."""
    monkeypatch.setitem(datasets.DATASETS, ASIA_CEMENT_ID, lambda: dataset)


@pytest.fixture
def asia_env(monkeypatch: pytest.MonkeyPatch, tenant_a: Tenant) -> Iterator[None]:
    """Gemini "configured" (never called) and tenant A assigned to Asia Cement."""
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key-not-real")  # never sent anywhere
    monkeypatch.setenv("EVALUATION_QUESTION_INTERVAL_SECONDS", "0")
    register(monkeypatch)
    assign(monkeypatch, {tenant_a.workspace_id: ASIA_CEMENT_ID})
    yield
    get_settings.cache_clear()


def asia_harness(corpus: AsiaCorpus, name: str = "asia-worker") -> AsiaHarness:
    embedder, generator = AsiaEmbedder(corpus), AsiaGenerator()
    worker = make_evaluator(embedder, generator, name)  # type: ignore[arg-type]
    return AsiaHarness(worker, embedder, generator, corpus)


def overview(tenant: Tenant) -> dict[str, Any]:
    response = tenant.client.get(f"{EVALUATIONS}/questions")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def rows_for(run: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    item = next(q for q in run["questions"] if q["question"]["key"] == key)
    return {r["variant"]: r for r in item["results"]}


# --- dataset selection ----------------------------------------------------------------------


def test_each_workspace_gets_its_own_dataset(
    tenant_a: Tenant, tenant_b: Tenant, asia_env: None
) -> None:
    asia = overview(tenant_a)["dataset"]
    assert (asia["id"], asia["name"], asia["version"]) == (ASIA_CEMENT_ID, ASIA.name, ASIA.version)
    assert asia["sha256"] == ASIA.sha256
    assert asia["relevance"] == "passage" and asia["split"] == "dev"
    assert (asia["questions"], asia["held_out_questions"]) == (16, len(HELD_OUT)) == (16, 2)
    assert (asia["answerable"], asia["unanswerable"]) == (13, 3)
    assert asia["source_files"]["cobc"] == "ACC_Code of Business Conduct 2024_0.pdf"
    listed = [q["key"] for q in overview(tenant_a)["questions"]]
    assert listed == [q.key for q in DEV]  # the held-out questions aren't listed
    assert all(q["split"] == "dev" for q in overview(tenant_a)["questions"])
    # What a run reserves: 16 questions plus each dev follow-up embedded alone (diagnostics).
    assert overview(tenant_a)["limits"]["query_embeddings_per_run"] == 18
    assert overview(tenant_a)["limits"]["generations_per_answer_mode"] == 16

    northstar = overview(tenant_b)["dataset"]  # not assigned: Northstar, as before
    assert (northstar["id"], northstar["name"]) == ("northstar-demo", NORTHSTAR.name)
    assert northstar["questions"] == len(NORTHSTAR.questions) == 26
    assert (northstar["relevance"], northstar["split"], northstar["held_out_questions"]) == (
        "source",
        None,
        0,
    )
    assert northstar["evidence_mapping"] is None
    assert overview(tenant_b)["limits"]["query_embeddings_per_run"] == 26  # no diagnostics


def test_without_configuration_every_workspace_uses_northstar(
    tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EVALUATION_WORKSPACE_DATASETS", raising=False)
    get_settings.cache_clear()
    try:
        assert overview(tenant_a)["dataset"]["name"] == NORTHSTAR.name
    finally:
        get_settings.cache_clear()


def test_the_corpus_check_uses_the_benchmark_file_names_and_hashes(
    tenant_a: Tenant, asia_env: None
) -> None:
    corpus = overview(tenant_a)["corpus"]
    assert corpus["ready"] is False
    assert {s["key"]: s["status"] for s in corpus["sources"]} == dict.fromkeys(
        ASIA.source_hashes, "missing"
    )
    response = tenant_a.client.post(f"{EVALUATIONS}/run", json={"answer_modes": []})
    assert response.status_code == 409
    assert "Anti-Corruption Policy.pdf (not uploaded)" in response.json()["error"]["message"]

    seed_asia_corpus(tenant_a.workspace_id, wrong_hash="acp")
    statuses = {s["key"]: s["status"] for s in overview(tenant_a)["corpus"]["sources"]}
    assert statuses["acp"] == "content_differs" and statuses["cobc"] == "ready"
    assert overview(tenant_a)["dataset"]["evidence_mapping"] is None  # not until ready


def test_every_development_quotation_maps_to_a_chunk(tenant_a: Tenant, asia_env: None) -> None:
    seed_asia_corpus(tenant_a.workspace_id)
    body = overview(tenant_a)
    assert body["corpus"]["ready"] is True and body["corpus"]["other_sources"] == []
    mapping = body["dataset"]["evidence_mapping"]
    dev_quotes = sum(len(q.evidence) for q in DEV)
    assert dev_quotes == 28
    assert mapping["quotations"] == mapping["matched"] == dev_quotes
    assert mapping["questions"] == mapping["questions_fully_mapped"] == 13
    assert mapping["problems"] == []


def test_a_quotation_that_cannot_be_found_is_reported_not_guessed(
    tenant_a: Tenant, asia_env: None
) -> None:
    lost = "Never accept money or gift cards as a gift."
    seed_asia_corpus(tenant_a.workspace_id, drop_quote=lost)
    mapping = overview(tenant_a)["dataset"]["evidence_mapping"]
    problems = {(p["question"], p["status"]) for p in mapping["problems"]}
    # AC-001, AC-014 (its second quote), and AC-017 cite it.
    assert problems == {("AC-001", "missing"), ("AC-014", "missing"), ("AC-017", "missing")}
    assert mapping["questions_fully_mapped"] == 10

    harness = asia_harness(seed_corpus_ids(tenant_a))
    run = start(tenant_a, answer_modes=[])
    harness.run_all()
    body = detail(tenant_a, run["id"])
    vector = rows_for(body, "AC-001")["vector"]
    # No passage score at all, rather than a miss or a hit on a guessed chunk.
    assert vector["hit_at_5"] is None and vector["reciprocal_rank"] is None
    assert body["summary"]["retrieval"]["vector"]["questions"] == 10
    assert body["summary"]["evidence_mapping"]["questions_fully_mapped"] == 10


def seed_corpus_ids(tenant: Tenant) -> AsiaCorpus:
    """The seeded corpus's chunk ids and axes, read back from the database."""
    corpus = AsiaCorpus()
    keys = {name.lower(): key for key, name in (ASIA.source_files or {}).items()}
    with get_engine().connect() as connection:
        rows = connection.execute(
            text(
                "SELECT c.id::text AS id, c.content, c.metadata, s.original_filename AS name,"
                " c.embedding::text AS vec FROM chunks c JOIN documents d ON d.id = c.document_id"
                " JOIN sources s ON s.id = d.source_id WHERE c.workspace_id = :ws"
            ),
            {"ws": tenant.workspace_id},
        ).all()
    for row in rows:
        values = [float(v) for v in row.vec.strip("[]").split(",")]
        corpus.axis[row.id] = values.index(max(values))
        key = keys[row.name.lower()]
        if "decoy" in row.content:
            corpus.decoys.setdefault(key, []).append(row.id)
        else:
            corpus.evidence[(key, row.metadata["page_start"])] = row.id
    return corpus


# --- runs -------------------------------------------------------------------------------


def test_a_run_evaluates_the_development_split_only(tenant_a: Tenant, asia_env: None) -> None:
    harness = asia_harness(seed_asia_corpus(tenant_a.workspace_id))
    run = start(tenant_a, answer_modes=["hybrid"])
    assert run["dataset_name"] == ASIA.name and run["questions_total"] == 16
    config = run["config"]
    assert config["dataset_id"] == ASIA_CEMENT_ID and config["question_split"] == "dev"
    assert config["relevance"] == "passage"
    assert config["question_keys"] == [q.key for q in DEV]
    # The budget is reserved for the development questions only: 16 embeddings plus one
    # diagnostic embedding per development follow-up, and 16 answers.
    assert DEV_FOLLOW_UPS == ["AC-017", "AC-019"]
    assert config["diagnostics"] == {"depth": 20, "top_ks": [5, 7, 10], "follow_up_alone": True}
    assert slots_used("query-embeddings-daily") == 16 + 2
    assert slots_used("evaluation-generations-daily") == 16
    # The page's estimate is exactly what was reserved.
    limits = overview(tenant_a)["limits"]
    assert limits["query_embeddings_per_run"] == 18 == slots_used("query-embeddings-daily")

    harness.run_all()
    body = detail(tenant_a, run["id"])
    assert body["status"] == "succeeded" and body["questions_done"] == 16
    assert [q["question"]["key"] for q in body["questions"]] == [q.key for q in DEV]
    assert len(harness.embedder.queries) == 18 and len(harness.generator.prompts) == 16
    alone = [q for q in harness.embedder.queries if q in ALONE_KEY]
    assert sorted(ALONE_KEY[q] for q in alone) == DEV_FOLLOW_UPS
    held_out_text = {q.question for q in HELD_OUT}
    assert not any(t in query for query in harness.embedder.queries for t in held_out_text)
    assert body["summary"]["gemini_requests"] == {
        "query_embeddings": 18,
        "generation_requests": 16,
    }
    # Every reserved slot was used: nothing left over to give back.
    assert slots_used("query-embeddings-daily") == 18
    # Every development question's evidence was mapped and stored with the run.
    assert set(body["corpus"]["passages"]) == {q.key for q in DEV if q.answerable}


def test_passage_relevance_counts_only_chunks_holding_the_evidence(
    tenant_a: Tenant, asia_env: None
) -> None:
    corpus = seed_asia_corpus(tenant_a.workspace_id)
    harness = asia_harness(corpus)
    cobc_decoy, other_decoys = (
        corpus.decoys["cobc"][0],
        [
            corpus.decoys["cobc"][1],
            *corpus.decoys["acp"],
            *corpus.decoys["bpcc"][:1],
        ],
    )
    # AC-001 lands on the right document (the Code) but a chunk without the quotation.
    harness.embedder.targets["AC-001"] = [(cobc_decoy, 10.0)] + [(d, 1.0) for d in other_decoys]
    run = start(tenant_a, answer_modes=[])
    harness.run_all()
    body = detail(tenant_a, run["id"])

    wrong_chunk = rows_for(body, "AC-001")["vector"]
    assert wrong_chunk["hit_at_5"] is False and wrong_chunk["reciprocal_rank"] == 0
    assert wrong_chunk["retrieved_sources"][0]["source_key"] == "cobc"  # right document
    assert wrong_chunk["retrieved_sources"][0]["relevant"] is True
    assert not any(c["evidence"] for c in wrong_chunk["retrieved"])

    right_chunk = rows_for(body, "AC-002")["vector"]  # evidence chunk ranked first
    assert right_chunk["hit_at_5"] is True and right_chunk["reciprocal_rank"] == 1.0
    assert right_chunk["retrieved"][0]["evidence"] is True

    summary = body["summary"]
    assert summary["relevance"] == "passage"
    passage, document = summary["retrieval"]["vector"], summary["document_retrieval"]["vector"]
    assert passage["questions"] == document["questions"] == 13
    assert passage["hit_rate_at_5"] == pytest.approx(12 / 13)  # AC-001 missed its passage
    assert document["hit_rate_at_5"] == 1.0  # every question found its document


def test_the_worker_follows_the_run_after_a_restart_and_a_configuration_change(
    tenant_a: Tenant, asia_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = seed_asia_corpus(tenant_a.workspace_id)
    first = asia_harness(corpus, "worker-1")
    run = start(tenant_a, answer_modes=[])
    assert first.worker.run_once()  # one question, then this worker "stops"

    assign(monkeypatch, {})  # the workspace is no longer assigned: new runs would be Northstar
    second = asia_harness(corpus, "worker-2")
    second.run_all()
    body = detail(tenant_a, run["id"])
    assert body["status"] == "succeeded" and body["questions_done"] == 16
    assert body["dataset_name"] == ASIA.name
    assert [q["question"]["key"] for q in body["questions"]] == [q.key for q in DEV]
    assert len(first.embedder.queries) + len(second.embedder.queries) == 16 + 2


def test_asia_and_northstar_runs_are_isolated(
    tenant_a: Tenant, tenant_b: Tenant, asia_env: None
) -> None:
    harness = asia_harness(seed_asia_corpus(tenant_a.workspace_id))
    asia_run = start(tenant_a, answer_modes=[])
    harness.run_all()
    seed_demo_corpus(tenant_b.workspace_id)
    northstar_run = start(tenant_b, answer_modes=[])
    assert northstar_run["dataset_name"] == NORTHSTAR.name
    assert northstar_run["questions_total"] == 26

    assert [r["id"] for r in tenant_a.client.get(f"{EVALUATIONS}/runs").json()] == [asia_run["id"]]
    assert [r["id"] for r in tenant_b.client.get(f"{EVALUATIONS}/runs").json()] == [
        northstar_run["id"]
    ]
    for client, foreign in (
        (tenant_a.client, northstar_run["id"]),
        (tenant_b.client, asia_run["id"]),
    ):
        response = client.get(f"{EVALUATIONS}/runs/{foreign}")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


def test_runs_recorded_before_question_selection_still_read_every_question(
    tenant_b: Tenant, asia_env: None
) -> None:
    from tests.integration.test_evaluation import (  # noqa: PLC0415
        RoutingEmbedder,
        ScriptedGenerator,
    )

    seed_demo_corpus(tenant_b.workspace_id)
    worker = make_evaluator(RoutingEmbedder(), ScriptedGenerator(), "northstar-worker")
    run = start(tenant_b, answer_modes=[])
    while worker.run_once():
        pass
    with get_engine().begin() as connection:  # what a run from before this change holds
        connection.execute(
            text(
                "UPDATE evaluation_runs SET config = config - 'question_keys' - 'question_split'"
                " - 'dataset_id' WHERE id = :id"
            ),
            {"id": run["id"]},
        )
    body = detail(tenant_b, run["id"])
    assert body["status"] == "succeeded"
    assert len(body["questions"]) == 26
    assert body["summary"]["retrieval"]["vector"]["questions"] == NORTHSTAR.answerable
    assert "document_retrieval" not in body["summary"]


# --- retrieval diagnostics (Experiment 004) ---------------------------------------------------

VARIANTS = ["vector", "hybrid", "hybrid_or", "hybrid_english", "hybrid_english_or"]
PRODUCTION_FIELDS = ("hit_at_5", "recall_at_5", "reciprocal_rank", "lexical_candidates")


def everything_but(corpus: AsiaCorpus, *excluded: str) -> list[tuple[str, float]]:
    """Weights that rank every chunk except `excluded` above them (they end up last)."""
    return [(chunk, 1.0) for chunk in corpus.axis if chunk not in excluded]


def looks_like_a_vector(value: Any) -> bool:
    """A long list of numbers anywhere in a stored value (an embedding would be 768)."""
    if isinstance(value, dict):
        return any(looks_like_a_vector(v) for v in value.values())
    if isinstance(value, list):
        if len(value) >= 100 and all(isinstance(v, int | float) for v in value):
            return True
        return any(looks_like_a_vector(v) for v in value)
    return False


def diagnostics_of(run: dict[str, Any], key: str, variant: str) -> dict[str, Any]:
    found: dict[str, Any] = rows_for(run, key)[variant]["details"]["diagnostics"]
    return found


def production_view(run: dict[str, Any]) -> dict[tuple[str, str], Any]:
    return {
        (q["question"]["key"], r["variant"]): (
            [c["chunk_id"] for c in r["retrieved"]],
            *(r[name] for name in PRODUCTION_FIELDS),
        )
        for q in run["questions"]
        for r in q["results"]
    }


def test_diagnostics_never_change_the_production_ranking(tenant_a: Tenant, asia_env: None) -> None:
    corpus = seed_asia_corpus(tenant_a.workspace_id)
    first = asia_harness(corpus, "with-diagnostics")
    diagnosed = start(tenant_a, answer_modes=[])
    first.run_all()
    plain = start(tenant_a, answer_modes=[])
    with get_engine().begin() as connection:  # the same run, without diagnostics
        connection.execute(
            text(
                "UPDATE evaluation_runs SET config = jsonb_set("
                "config - 'diagnostics', '{budget,query_embeddings}', '16') WHERE id = :id"
            ),
            {"id": plain["id"]},
        )
    second = asia_harness(corpus, "without-diagnostics")
    second.run_all()
    with_diag, without = detail(tenant_a, diagnosed["id"]), detail(tenant_a, plain["id"])
    assert production_view(with_diag) == production_view(without)

    def scores(run: dict[str, Any]) -> dict[str, Any]:  # timings naturally differ
        return {
            v: {k: x for k, x in m.items() if k != "latency_ms"}
            for v, m in run["summary"]["retrieval"].items()
        }

    assert scores(with_diag) == scores(without)
    assert len(second.embedder.queries) == 16  # no diagnostic embedding without diagnostics
    assert all(
        "diagnostics" not in r["details"] for q in without["questions"] for r in q["results"]
    )
    # Recomputing the production top 5 from the stored candidate lists gives the same result.
    for q in with_diag["questions"]:
        for r in q["results"]:
            assert r["details"]["diagnostics"]["matches_production"] is True, q["question"]["key"]


def test_diagnostic_candidate_lists_hold_ranks_scores_and_sources_but_no_vectors(
    tenant_a: Tenant, asia_env: None
) -> None:
    corpus = seed_asia_corpus(tenant_a.workspace_id)
    harness = asia_harness(corpus)
    run = start(tenant_a, answer_modes=[])
    harness.run_all()
    body = detail(tenant_a, run["id"])
    vector = diagnostics_of(body, "AC-002", "vector")
    assert vector["depth"] == 20 and vector["candidates"] == 10
    assert len(vector["vector"]) == 20
    entry = vector["vector"][0]
    assert set(entry) == {"chunk_id", "rank", "score", "source_id", "chars"}
    assert [e["rank"] for e in vector["vector"]] == list(range(1, 21))
    hybrid_or = diagnostics_of(body, "AC-002", "hybrid_or")
    assert hybrid_or["lexical"] and hybrid_or["lexical_matches_at_depth"] <= 20
    assert "vector" not in hybrid_or  # the shared vector list is stored once, on the vector row
    assert not any(looks_like_a_vector(r) for q in body["questions"] for r in q["results"])


def test_ac_001_keyword_match_rescues_hybrid_while_vector_misses(
    tenant_a: Tenant, asia_env: None
) -> None:
    corpus = seed_asia_corpus(tenant_a.workspace_id)
    assert len(corpus.axis) > 20  # more chunks than the 20-deep candidate list holds
    evidence = corpus.evidence[("cobc", 9)]
    harness = asia_harness(corpus)
    harness.embedder.targets["AC-001"] = everything_but(corpus, evidence)
    run = start(tenant_a, answer_modes=[])
    harness.run_all()
    body = detail(tenant_a, run["id"])
    rows = rows_for(body, "AC-001")
    assert rows["vector"]["hit_at_5"] is False  # the evidence is last by vector
    assert rows["hybrid"]["hit_at_5"] is True and rows["hybrid"]["lexical_candidates"] == 1
    vector = diagnostics_of(body, "AC-001", "vector")
    assert evidence not in [c["chunk_id"] for c in vector["vector"]]  # not even in the top 20
    assert all(not vector["original"][k]["evidence"]["hit"] for k in ("5", "7", "10"))
    hybrid = diagnostics_of(body, "AC-001", "hybrid")
    assert [c["chunk_id"] for c in hybrid["lexical"]] == [evidence]  # the only keyword match
    assert hybrid["original"]["5"]["evidence"]["hit"] is True
    assert hybrid["original"]["5"]["context"]["evidence_in_context"] is True


def test_ac_017_follow_up_alone_is_compared_but_never_scored_as_production(
    tenant_a: Tenant, asia_env: None
) -> None:
    corpus = seed_asia_corpus(tenant_a.workspace_id)
    evidence = corpus.evidence[("cobc", 9)]
    harness = asia_harness(corpus)
    harness.embedder.targets["AC-017"] = everything_but(corpus, evidence)  # the joined query
    harness.embedder.alone_targets["AC-017"] = [(evidence, 1.0)]  # the follow-up alone
    run = start(tenant_a, answer_modes=[])
    harness.run_all()
    body = detail(tenant_a, run["id"])
    rows = rows_for(body, "AC-017")
    # Production-equivalent scoring uses the joined query and misses, as in Test 002.
    assert rows["hybrid"]["retrieval_query"].startswith("According to Asia Cement")
    assert rows["hybrid"]["hit_at_5"] is False and rows["hybrid"]["lexical_candidates"] == 0
    assert rows["vector"]["hit_at_5"] is False
    alone = diagnostics_of(body, "AC-017", "vector")["follow_up_alone"]
    assert alone["query"] == next(q.question for q in ASIA.questions if q.key == "AC-017")
    hybrid_alone = alone["variants"]["hybrid"]["original"]["5"]
    assert hybrid_alone["evidence"]["hit"] is True and hybrid_alone["evidence"]["first_rank"] == 1
    assert hybrid_alone["context"]["evidence_in_context"] is True
    # Only the vector row carries it; its extra embedding is counted, nothing else changes.
    assert "follow_up_alone" not in diagnostics_of(body, "AC-017", "hybrid")
    usage = rows["vector"]["token_usage"]
    assert usage["query_embeddings"] == 2 and usage["diagnostic_query_embeddings"] == 1
    assert "follow_up_alone" not in diagnostics_of(body, "AC-001", "vector")


def test_a_larger_top_k_is_analysed_from_the_original_candidates(
    tenant_a: Tenant, asia_env: None
) -> None:
    corpus = seed_asia_corpus(tenant_a.workspace_id)
    evidence = corpus.evidence[("acp", 12)]
    decoys = [d for key in ("cobc", "acp", "bpcc") for d in corpus.decoys[key]][:5]
    harness = asia_harness(corpus)
    # AC-004's evidence is 6th by vector: missed at 5, found at 7.
    harness.embedder.targets["AC-004"] = [(d, 10.0) for d in decoys] + [(evidence, 5.0)]
    run = start(tenant_a, answer_modes=[])
    harness.run_all()
    body = detail(tenant_a, run["id"])
    assert rows_for(body, "AC-004")["vector"]["hit_at_5"] is False
    vector = diagnostics_of(body, "AC-004", "vector")
    original = vector["original"]
    assert original["5"]["evidence"]["hit"] is False
    assert original["7"]["evidence"]["hit"] is True and original["7"]["evidence"]["first_rank"] == 6
    assert original["10"]["evidence"]["hit"] is True
    assert original["7"]["context"]["evidence_in_context"] is True  # small chunks: it fits
    assert len(original["7"]["chunk_ids"]) == 7 and len(original["10"]["chunk_ids"]) == 10
    assert vector["pool20"]["7"]["evidence"]["first_rank"] == 6


def test_multi_source_coverage_is_reported_per_source_and_per_quotation(
    tenant_a: Tenant, asia_env: None
) -> None:
    corpus = seed_asia_corpus(tenant_a.workspace_id)
    harness = asia_harness(corpus)
    harness.embedder.targets["AC-014"] = [(corpus.evidence[("cobc", 9)], 1.0)]  # Code only
    run = start(tenant_a, answer_modes=[])
    harness.run_all()
    body = detail(tenant_a, run["id"])
    coverage = diagnostics_of(body, "AC-014", "vector")["original"]["5"]["evidence"]
    assert set(coverage["by_source"]) == {"cobc", "bpcc"}
    assert coverage["by_source"]["cobc"] is True
    quotations = coverage["by_quotation"]
    assert [q["source_key"] for q in quotations] == ["cobc", "cobc", "bpcc"]
    assert all(q["mapped"] for q in quotations)
    assert quotations[1]["rank"] == 1  # the Code's page-9 passage, at the top
    # The production row still scores passage recall over all three quotations.
    assert rows_for(body, "AC-014")["vector"]["recall_at_5"] == coverage["recall"]


def test_the_summary_keeps_production_scores_and_diagnostics_apart(
    tenant_a: Tenant, asia_env: None
) -> None:
    corpus = seed_asia_corpus(tenant_a.workspace_id)
    harness = asia_harness(corpus)
    run = start(tenant_a, answer_modes=[])
    harness.run_all()
    summary = detail(tenant_a, run["id"])["summary"]
    for variant in VARIANTS:
        production = summary["retrieval"][variant]
        hypothetical = summary["diagnostics"][variant]["original"]["5"]
        assert hypothetical["questions"] == production["questions"] == 13
        assert hypothetical["hit_rate"] == pytest.approx(production["hit_rate_at_5"])
        assert hypothetical["recall"] == pytest.approx(production["recall_at_5"])
        assert set(summary["diagnostics"][variant]) == {"original", "pool20"}
        assert set(summary["diagnostics"][variant]["original"]) == {"5", "7", "10"}


def test_northstar_runs_record_no_diagnostics(tenant_b: Tenant, asia_env: None) -> None:
    from tests.integration.test_evaluation import (  # noqa: PLC0415
        RoutingEmbedder,
        ScriptedGenerator,
    )

    seed_demo_corpus(tenant_b.workspace_id)
    embedder = RoutingEmbedder()
    worker = make_evaluator(embedder, ScriptedGenerator(), "northstar-worker")
    run = start(tenant_b, answer_modes=[])
    assert "diagnostics" not in run["config"]
    assert slots_used("query-embeddings-daily") == 26
    while worker.run_once():
        pass
    body = detail(tenant_b, run["id"])
    assert len(embedder.queries) == 26
    assert all("diagnostics" not in r["details"] for q in body["questions"] for r in q["results"])
    assert "diagnostics" not in body["summary"]


# --- chunking traceability (Experiment 005) -----------------------------------------------------


def test_a_run_records_the_chunking_strategy_of_every_document(
    tenant_a: Tenant, asia_env: None
) -> None:
    from app.ingestion.chunker import ChunkerConfig  # noqa: PLC0415

    section = ChunkerConfig(strategy="section-v1").fingerprint
    seed_asia_corpus(tenant_a.workspace_id)
    with get_engine().begin() as connection:  # as the worker records it after indexing
        connection.execute(
            text(
                "UPDATE documents SET metadata = jsonb_build_object('chunker', CAST(:c AS text))"
                " WHERE workspace_id = :ws"
            ),
            {"c": section, "ws": tenant_a.workspace_id},
        )
    corpus = overview(tenant_a)["corpus"]
    assert {s["key"]: s["chunker"] for s in corpus["sources"]} == dict.fromkeys(
        ASIA.source_hashes, section
    )
    run = start(tenant_a, answer_modes=[])
    assert run["corpus"]["chunkers"] == [section]
    assert all(s["chunker"] == section for s in run["corpus"]["sources"])
    # Selection is unchanged by the chunking strategy: the 16 development questions only.
    assert run["questions_total"] == 16 and run["config"]["question_keys"] == [q.key for q in DEV]


def test_documents_without_a_recorded_chunker_are_reported_as_unknown(
    tenant_b: Tenant, asia_env: None
) -> None:
    seed_demo_corpus(tenant_b.workspace_id)  # test documents carry no chunker metadata
    run = start(tenant_b, answer_modes=[])
    assert run["corpus"]["chunkers"] == []
    assert all(s["chunker"] is None for s in run["corpus"]["sources"])


# --- the private benchmark and its public development split --------------------------------------


def unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """As on a server without the private file (CI, production): the real loader, no file."""
    missing = datasets.ASIA_CEMENT_DATASET.with_name("not-installed.json")
    monkeypatch.setitem(
        datasets.DATASETS, ASIA_CEMENT_ID, lambda: datasets.load_asia_cement(missing)
    )


def test_the_public_development_split_runs_without_the_private_file(
    tenant_a: Tenant, asia_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    unavailable(monkeypatch)
    assign(monkeypatch, {tenant_a.workspace_id: ASIA_CEMENT_DEV_ID})
    harness = asia_harness(seed_asia_corpus(tenant_a.workspace_id, datasets=[DEV_SPLIT]))
    body = overview(tenant_a)
    dataset = body["dataset"]
    assert (dataset["id"], dataset["name"]) == (ASIA_CEMENT_DEV_ID, DEV_SPLIT.name)
    assert (dataset["questions"], dataset["held_out_questions"]) == (16, 0)
    assert dataset["evidence_mapping"]["matched"] == dataset["evidence_mapping"]["quotations"] == 28
    assert body["limits"]["query_embeddings_per_run"] == 18
    run = start(tenant_a, answer_modes=[])
    harness.run_all()
    finished = detail(tenant_a, run["id"])
    assert finished["status"] == "succeeded" and finished["questions_done"] == 16
    assert finished["dataset_name"] == DEV_SPLIT.name


def test_a_missing_private_benchmark_is_a_clear_error_and_old_runs_stay_readable(
    tenant_a: Tenant, asia_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = asia_harness(seed_asia_corpus(tenant_a.workspace_id))
    run = start(tenant_a, answer_modes=[])
    harness.run_all()
    before = detail(tenant_a, run["id"])

    unavailable(monkeypatch)  # the private file is gone (or was never installed)
    with pytest.raises(DatasetUnavailable):
        datasets.DATASETS[ASIA_CEMENT_ID]()
    for response in (
        tenant_a.client.get(f"{EVALUATIONS}/questions"),
        tenant_a.client.post(f"{EVALUATIONS}/run", json={"answer_modes": []}),
    ):
        assert response.status_code == 409
        error = response.json()["error"]
        assert error["code"] == "evaluation_dataset_unavailable"
        assert "asia-cement-realworld-dev" in error["message"]
    # Stored runs never need the dataset file: they read exactly as before.
    assert detail(tenant_a, run["id"]) == before
    listed = [r["id"] for r in tenant_a.client.get(f"{EVALUATIONS}/runs").json()]
    assert listed == [run["id"]]
