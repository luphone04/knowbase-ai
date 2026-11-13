"""Per-workspace chunking strategies through the real worker (Experiment 005, no Gemini).

Only a workspace listed in INGESTION_WORKSPACE_CHUNKERS uses section-v1, only for
PDFs; every other workspace keeps paragraph-v1. Re-indexing stays idempotent, a
strategy change replaces chunks in one transaction, and a failure keeps the
previous index.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest

from app.config import get_settings
from app.ingestion.chunker import ChunkerConfig
from app.ingestion.pipeline import IngestionWorker
from tests.fakes.documents import make_pdf
from tests.integration.conftest import Tenant, WorkerHarness, make_worker
from tests.integration.test_ingestion_worker import (
    chunks_of,
    job,
    long_markdown,
    make_due,
    sql,
    upload,
)

pytestmark = pytest.mark.integration

PARAGRAPH = ChunkerConfig().fingerprint
SECTION = ChunkerConfig(strategy="section-v1").fingerprint
SECTIONS = [
    "2.1 Competition Law",
    "2.2 Health and Safety",
    "2.3 Anticorruption",
    "2.4 Gifts and Benefits",
]


def code_pdf() -> bytes:
    """One numbered section per page, about 450 characters each."""
    return make_pdf(
        [
            [
                title,
                *[
                    f"Rule {i} of {title.split(' ', 1)[1]}: employees always follow it."
                    for i in range(8)
                ],
            ]
            for title in SECTIONS
        ]
    )


@pytest.fixture
def assign(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Assign workspaces to section-v1 and build a worker that reads the setting."""

    def build(*workspaces: Any) -> WorkerHarness:
        monkeypatch.setenv(
            "INGESTION_WORKSPACE_CHUNKERS",
            json.dumps({str(w): "section-v1" for w in workspaces}),
        )
        return make_worker()

    yield build
    get_settings.cache_clear()


def document_of(source_id: str) -> Any:
    return sql("SELECT id, metadata FROM documents WHERE source_id = :s", s=source_id)[0]


def reindex(tenant: Tenant, source_id: str) -> str:
    response = tenant.client.post(f"/api/v1/sources/{source_id}/reindex")
    assert response.status_code in (200, 202), response.text
    job_id: str = response.json()["id"]
    return job_id


def test_only_the_assigned_workspace_chunks_pdfs_by_section(
    tenant_a: Tenant, tenant_b: Tenant, assign: Any
) -> None:
    worker = assign(tenant_a.workspace_id)
    a = upload(tenant_a, "code.pdf", code_pdf())["source"]["id"]
    b = upload(tenant_b, "code.pdf", code_pdf())["source"]["id"]
    worker.run_all()

    assert document_of(a).metadata["chunker"] == SECTION
    assert document_of(b).metadata["chunker"] == PARAGRAPH
    section, paragraph = chunks_of(a), chunks_of(b)
    assert [c.content.split("\n\n", 1)[0] for c in section] == SECTIONS  # one per section
    assert [c.metadata["heading"] for c in section] == SECTIONS
    assert len(paragraph) < len(section)
    assert all(c.metadata["heading"] is None for c in paragraph)  # numbered titles unseen


def test_non_pdf_files_keep_paragraph_v1_in_a_section_workspace(
    tenant_a: Tenant, assign: Any
) -> None:
    worker = assign(tenant_a.workspace_id)
    source = upload(tenant_a, "handbook.md", long_markdown())["source"]["id"]
    worker.run_all()
    assert document_of(source).metadata["chunker"] == PARAGRAPH


def test_reindexing_under_section_v1_is_idempotent(tenant_a: Tenant, assign: Any) -> None:
    worker = assign(tenant_a.workspace_id)
    source = upload(tenant_a, "code.pdf", code_pdf())["source"]["id"]
    worker.run_all()
    before, calls = chunks_of(source), len(worker.provider.calls)

    job_id = reindex(tenant_a, source)
    worker.run_all()
    assert job(job_id).result["outcome"] == "unchanged"
    assert [c.id for c in chunks_of(source)] == [c.id for c in before]  # not even rewritten
    assert len(worker.provider.calls) == calls  # no embedding calls
    assert sql("SELECT count(*) FROM documents")[0][0] == 1


def test_a_strategy_change_replaces_chunks_once_and_leaves_other_workspaces_alone(
    tenant_a: Tenant, tenant_b: Tenant, assign: Any
) -> None:
    first = assign()  # nobody assigned yet: paragraph-v1 everywhere
    a = upload(tenant_a, "code.pdf", code_pdf())["source"]["id"]
    b = upload(tenant_b, "code.pdf", code_pdf())["source"]["id"]
    first.run_all()
    old_a, old_b = chunks_of(a), chunks_of(b)

    worker = assign(tenant_a.workspace_id)  # now only A uses section-v1
    job_id = reindex(tenant_a, a)
    worker.run_all()

    new_a = chunks_of(a)
    assert job(job_id).result["outcome"] == "indexed"
    assert document_of(a).metadata["chunker"] == SECTION
    assert {c.id for c in new_a}.isdisjoint({c.id for c in old_a})  # replaced, not appended
    assert [c.metadata["heading"] for c in new_a] == SECTIONS
    indexes = sql("SELECT chunk_index FROM chunks WHERE document_id = :d", d=document_of(a).id)
    assert sorted(i[0] for i in indexes) == list(range(len(new_a)))  # no duplicates
    assert sql("SELECT count(*) FROM documents WHERE source_id = :s", s=a)[0][0] == 1
    # The other workspace is untouched: same chunks, same strategy.
    assert [(c.id, c.content) for c in chunks_of(b)] == [(c.id, c.content) for c in old_b]
    assert document_of(b).metadata["chunker"] == PARAGRAPH


def test_a_failed_section_reindex_keeps_the_previous_index_and_retries_cleanly(
    tenant_a: Tenant, assign: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = assign()
    source = upload(tenant_a, "code.pdf", code_pdf())["source"]["id"]
    first.run_all()
    good = chunks_of(source)

    def explode(*args: object) -> None:  # inside the publish transaction, after the swap
        raise RuntimeError("power cut during publish")

    worker = assign(tenant_a.workspace_id)
    original = IngestionWorker.__dict__["_mark_ready"]  # the staticmethod itself
    monkeypatch.setattr(IngestionWorker, "_mark_ready", staticmethod(explode))
    reindex(tenant_a, source)
    worker.worker.run_once()
    # Rolled back: the paragraph-v1 index is intact, nothing half-written.
    assert [(c.id, c.content) for c in chunks_of(source)] == [(c.id, c.content) for c in good]
    assert document_of(source).metadata["chunker"] == PARAGRAPH

    monkeypatch.setattr(IngestionWorker, "_mark_ready", original)
    make_due()
    worker.run_all()  # the retry publishes section-v1 exactly once
    assert document_of(source).metadata["chunker"] == SECTION
    assert [c.metadata["heading"] for c in chunks_of(source)] == SECTIONS
    assert sql("SELECT count(*) FROM chunks")[0][0] == len(SECTIONS)
