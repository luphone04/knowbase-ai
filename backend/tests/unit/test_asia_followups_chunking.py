"""Experiment 006: every follow-up quotation maps to a chunk under both chunking strategies.

Uses the four real PDFs (ASIA_CEMENT_PDF_DIR; skipped otherwise), chunked exactly as
the two indexed workspaces are: paragraph-v1 (72 chunks) and section-v1 (87 chunks).
Only the extension's own evidence is read; it holds no held-out question.
"""

from typing import Any

import pytest

from app.evaluation.dataset import load_asia_followups
from app.evaluation.passages import ChunkText, map_evidence
from tests.unit.test_asia_cement_chunking import AVAILABLE, FIXTURE, chunks

pytestmark = pytest.mark.skipif(
    not AVAILABLE, reason="set ASIA_CEMENT_PDF_DIR to the four Asia Cement PDFs to run these"
)

FOLLOWUPS = load_asia_followups()


def texts(strategy: str) -> list[ChunkText]:
    return [
        ChunkText(f"{key}#{c.index}", key, c.index, c.page_start, c.page_end, c.content)
        for key in FIXTURE["documents"]
        for c in chunks(key, strategy)
    ]


@pytest.mark.parametrize(("strategy", "total"), [("paragraph-v1", 72), ("section-v1", 87)])
def test_every_quotation_maps_on_both_corpora(strategy: str, total: int) -> None:
    corpus = texts(strategy)
    assert len(corpus) == total
    mandatory: dict[tuple[str, int], Any] = {
        (q.key, i): map_evidence(e.quote, source_key=e.source_key, page=e.page, chunks=corpus)
        for q in FOLLOWUPS.questions
        for i, e in enumerate(q.evidence, 1)
    }
    assert len(mandatory) == 29
    unmatched = sorted(key for key, m in mandatory.items() if m.status != "matched")
    assert unmatched == []
    corroborating = [
        map_evidence(e.quote, source_key=e.source_key, page=e.page, chunks=corpus)
        for q in FOLLOWUPS.questions
        for e in q.corroborating_evidence
    ]
    assert [m.status for m in corroborating] == ["matched"]
    # PDF spacing artifacts ("line -manager", "w ill", "givin g") match at the spacing level.
    spacing = sorted(key for key, m in mandatory.items() if m.level == "spacing")
    assert spacing == [("FU-003", 1), ("FU-008", 1), ("FU-015", 2)]
