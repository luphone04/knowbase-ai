"""Retrieval diagnostics: hypothetical top-K, evidence coverage, and context fit (no I/O)."""

import uuid
from typing import Any

from app.evaluation.diagnostics import (
    TOP_KS,
    analyse,
    candidates,
    coverage,
    ranked,
    summarize_diagnostics,
)
from app.retrieval.fusion import reciprocal_rank_fusion
from app.retrieval.search import SearchHit

DOC = uuid.uuid4()
SOURCE = uuid.uuid4()


def hits(names: list[str], *, content: dict[str, str] | None = None) -> list[SearchHit]:
    """Ranked hits whose chunk ids are uuid5(name), so tests can refer to them by name."""
    return [
        SearchHit(
            rank=rank,
            score=rank / 100,
            chunk_id=uuid.uuid5(uuid.NAMESPACE_DNS, name),
            chunk_index=rank,
            content=(content or {}).get(name, f"distinct text of chunk {name} " * 5),
            heading=None,
            page_start=1,
            page_end=1,
            document_id=uuid.uuid5(uuid.NAMESPACE_DNS, f"doc-{name}"),
            document_title="Doc",
            source_id=SOURCE,
            source_name="doc.pdf",
            filename="doc.pdf",
        )
        for rank, name in enumerate(names, start=1)
    ]


def cid(name: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, name))


def mapped(*groups: list[str], source: str = "cobc") -> list[dict[str, Any]]:
    return [
        {"source_key": source, "status": "matched", "chunk_ids": [cid(n) for n in group]}
        for group in groups
    ]


def test_candidate_lists_keep_ranks_scores_sources_and_lengths_only() -> None:
    (entry,) = candidates(hits(["a"]))
    assert entry == {
        "chunk_id": cid("a"),
        "rank": 1,
        "score": 0.01,
        "source_id": str(SOURCE),
        "chars": len("distinct text of chunk a " * 5),
    }


def test_hybrid_top_k_is_production_reciprocal_rank_fusion() -> None:
    vector, lexical = hits([f"v{i}" for i in range(10)]), hits(["l0", "v3", "l1"])
    expected = reciprocal_rank_fusion(
        {"vector": [h.chunk_id for h in vector], "lexical": [h.chunk_id for h in lexical]},
        k=60,
        limit=7,
    )
    top = ranked("hybrid", vector, lexical, k=7, rrf_k=60)
    assert [r.hit.chunk_id for r in top] == [f.chunk_id for f in expected]
    assert top[0].hit.chunk_id == uuid.UUID(cid("v3"))  # in both lists: fused first
    assert (top[0].vector_rank, top[0].lexical_rank) == (4, 2)
    vector_only = ranked("vector", vector, lexical, k=5, rrf_k=60)
    assert [r.hit.chunk_id for r in vector_only] == [h.chunk_id for h in vector[:5]]


def test_coverage_is_per_quotation_and_per_source_and_never_guesses() -> None:
    evidence = [*mapped(["a"], ["b", "b2"]), *mapped(["c"], source="bpcc")]
    result = coverage([cid("x"), cid("b2"), cid("c")], evidence)
    assert result is not None
    assert (result["hit"], result["recall"], result["first_rank"]) == (True, 2 / 3, 2)
    assert result["by_source"] == {"cobc": True, "bpcc": True}
    assert [q["rank"] for q in result["by_quotation"]] == [None, 2, 3]
    split: dict[str, Any] = {"source_key": "cobc", "status": "split", "chunk_ids": []}
    unmapped = [*mapped(["a"]), split]
    partial = coverage([cid("a")], unmapped)
    assert partial is not None and partial["hit"] is None and partial["recall"] is None
    assert partial["by_quotation"][1]["mapped"] is False
    assert coverage([cid("a")], None) is None


def test_top_k_shows_evidence_found_later_and_whether_it_fits_the_context() -> None:
    names = [f"c{i}" for i in range(10)]
    vector = hits(names)
    result = analyse("vector", vector, [], evidence=mapped(["c5"]), rrf_k=60, max_chars=12_000)
    assert set(result) == {str(k) for k in TOP_KS}
    assert result["5"]["evidence"]["hit"] is False
    assert result["7"]["evidence"]["first_rank"] == 6
    assert result["7"]["context"]["evidence_in_context"] is True
    assert result["7"]["context"]["excerpts"] == 7


def test_evidence_beyond_the_character_limit_is_reported_as_dropped_or_cut() -> None:
    big = {f"c{i}": f"word{i} " * 450 for i in range(10)}  # 2,700 characters each: 4 fit
    vector = hits([f"c{i}" for i in range(10)], content=big)
    result = analyse("vector", vector, [], evidence=mapped(["c6"]), rrf_k=60, max_chars=12_000)
    seven = result["7"]
    assert seven["evidence"]["hit"] is True  # retrieved at rank 7 ...
    assert seven["context"]["evidence_in_context"] is False  # ... but past the budget
    assert seven["context"]["evidence_dropped"] == [cid("c6")]
    assert seven["context"]["chars"] <= 12_000
    cut = analyse("vector", vector, [], evidence=mapped(["c4"]), rrf_k=60, max_chars=12_000)
    assert cut["5"]["context"]["evidence_cut"] == [cid("c4")]  # in context, but shortened


def test_a_deeper_pool_is_a_different_analysis_from_a_larger_cut_off() -> None:
    # The evidence "e" is 10th by vector and 12th by keyword: outside the original top-10
    # keyword candidates, inside a 20-deep pool, where both lists then count for it.
    vector = hits([f"v{i}" for i in range(9)] + ["e"])
    lexical_deep = hits([f"l{i}" for i in range(11)] + ["e"] + [f"m{i}" for i in range(8)])
    evidence = mapped(["e"])
    settings = {"rrf_k": 60, "max_chars": 12_000}
    original = analyse("hybrid", vector, lexical_deep[:10], evidence=evidence, **settings)
    deeper = analyse("hybrid", vector, lexical_deep, evidence=evidence, **settings)
    assert original["10"]["evidence"]["hit"] is False  # a larger cut-off alone doesn't help
    assert deeper["5"]["evidence"]["first_rank"] == 1  # a larger pool re-fuses it to the top


def test_the_summary_averages_only_fully_mapped_questions() -> None:
    def diag(hit: bool | None, recall: float | None, in_context: bool) -> dict[str, object]:
        analysis = {
            "evidence": {"hit": hit, "recall": recall},
            "context": {"evidence_in_context": in_context},
        }
        return {"original": {"5": analysis}, "pool20": {"5": analysis}}

    summary = summarize_diagnostics(
        [
            ("vector", "AC-001", diag(True, 1.0, True)),
            ("vector", "AC-002", diag(False, 0.0, False)),
            ("vector", "AC-003", diag(None, None, False)),  # unmapped evidence
        ]
    )
    bucket = summary["vector"]["original"]["5"]
    assert (bucket["questions"], bucket["unmapped"]) == (2, 1)
    assert (bucket["hit_rate"], bucket["recall"], bucket["in_context_rate"]) == (0.5, 0.5, 0.5)
