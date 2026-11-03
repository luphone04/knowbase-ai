"""Reciprocal Rank Fusion (SPEC.md §9.4).

    RRF(chunk) = sum over lists containing it of 1 / (k + rank)

Only ranks are combined. Raw cosine distances and `ts_rank_cd` values have
unrelated scales, so they're never added. Ranks start at 1.

Deterministic: exact score ties are broken by chunk id, so the same inputs
always give the same order.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

DEFAULT_RRF_K = 60


@dataclass(frozen=True)
class FusedHit:
    chunk_id: uuid.UUID
    score: float
    rank: int  # 1-based position in the fused list
    # Rank in each input list that contained the chunk, e.g. {"vector": 2, "lexical": 1}.
    source_ranks: dict[str, int] = field(default_factory=dict)


def reciprocal_rank_fusion(
    ranked_lists: Mapping[str, Sequence[uuid.UUID]], *, k: int = DEFAULT_RRF_K, limit: int
) -> list[FusedHit]:
    """Fuse ranked chunk-id lists (best first). A repeated id keeps its best rank."""
    if k <= 0:
        raise ValueError("RRF k must be positive")
    if limit <= 0:
        return []
    ranks: dict[uuid.UUID, dict[str, int]] = {}
    for name, chunk_ids in ranked_lists.items():
        for position, chunk_id in enumerate(chunk_ids, start=1):
            ranks.setdefault(chunk_id, {}).setdefault(name, position)  # dedupe: first wins

    scored = [
        (sum(1.0 / (k + rank) for rank in by_list.values()), chunk_id)
        for chunk_id, by_list in ranks.items()
    ]
    scored.sort(key=lambda item: (-item[0], str(item[1])))
    return [
        FusedHit(chunk_id=chunk_id, score=score, rank=position, source_ranks=dict(ranks[chunk_id]))
        for position, (score, chunk_id) in enumerate(scored[:limit], start=1)
    ]
