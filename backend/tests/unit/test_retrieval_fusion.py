"""Reciprocal Rank Fusion: the formula, ordering, deduplication, and edge cases."""

import uuid

import pytest

from app.retrieval.fusion import reciprocal_rank_fusion


def ids(n: int) -> list[uuid.UUID]:
    return [uuid.UUID(int=i + 1) for i in range(n)]


A, B, C, D, E = ids(5)


def test_scores_follow_the_formula() -> None:
    fused = reciprocal_rank_fusion({"vector": [A, B], "lexical": [B, C]}, k=60, limit=10)
    scores = {hit.chunk_id: hit.score for hit in fused}
    assert scores[B] == pytest.approx(1 / 62 + 1 / 61)  # rank 2 in vector, 1 in lexical
    assert scores[A] == pytest.approx(1 / 61)
    assert scores[C] == pytest.approx(1 / 62)
    assert [hit.chunk_id for hit in fused] == [B, A, C]
    assert [hit.rank for hit in fused] == [1, 2, 3]
    assert fused[0].source_ranks == {"vector": 2, "lexical": 1}


def test_a_chunk_in_both_lists_beats_one_that_tops_only_one() -> None:
    fused = reciprocal_rank_fusion({"vector": [A, C, B], "lexical": [D, E, B]}, k=60, limit=10)
    assert fused[0].chunk_id == B  # 1/63 + 1/63 > 1/61


def test_missing_lists_and_empty_lists() -> None:
    assert reciprocal_rank_fusion({}, limit=5) == []
    assert reciprocal_rank_fusion({"vector": [], "lexical": []}, limit=5) == []
    only_vector = reciprocal_rank_fusion({"vector": [A, B], "lexical": []}, limit=5)
    assert [hit.chunk_id for hit in only_vector] == [A, B]
    assert only_vector[0].source_ranks == {"vector": 1}


def test_duplicates_within_a_list_keep_their_best_rank() -> None:
    fused = reciprocal_rank_fusion({"vector": [A, B, A]}, k=60, limit=10)
    assert [hit.chunk_id for hit in fused] == [A, B]
    assert fused[0].score == pytest.approx(1 / 61)


def test_ties_are_broken_deterministically() -> None:
    # A is rank 1 in vector, C rank 1 in lexical: equal scores, equal best rank.
    fused = reciprocal_rank_fusion({"vector": [C], "lexical": [A]}, k=60, limit=10)
    assert [hit.chunk_id for hit in fused] == [A, C]  # then by id
    again = reciprocal_rank_fusion({"lexical": [A], "vector": [C]}, k=60, limit=10)
    assert [hit.chunk_id for hit in again] == [A, C]  # input order doesn't matter


def test_limit_and_k() -> None:
    fused = reciprocal_rank_fusion({"vector": ids(20)}, k=60, limit=5)
    assert len(fused) == 5
    assert reciprocal_rank_fusion({"vector": [A]}, limit=0) == []
    small_k = reciprocal_rank_fusion({"vector": [A]}, k=1, limit=1)
    assert small_k[0].score == pytest.approx(1 / 2)
    with pytest.raises(ValueError):
        reciprocal_rank_fusion({"vector": [A]}, k=0, limit=1)
