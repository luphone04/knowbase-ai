"""The worker pipeline end to end, on PostgreSQL, with a deterministic embedding provider."""

import math
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text

from app.db.session import get_engine
from app.ingestion.pipeline import IngestionWorker
from app.providers.errors import (
    ProviderConfigurationError,
    ProviderNotConfigured,
    ProviderQuotaExhausted,
    ProviderUnavailable,
)
from app.providers.interfaces import EmbeddingResult
from tests.fakes.documents import RETURNS_POLICY, encrypt_pdf, make_docx, make_pdf
from tests.fakes.providers import MockEmbeddingProvider
from tests.integration.conftest import Tenant, WorkerHarness, make_worker

pytestmark = pytest.mark.integration

UPLOAD = "/api/v1/sources/files"


def upload(tenant: Tenant, name: str, data: bytes) -> dict[str, Any]:
    response = tenant.client.post(UPLOAD, files={"file": (name, data)})
    assert response.status_code in (200, 202), response.text
    body: dict[str, Any] = response.json()
    return body


def sql(statement: str, **params: object) -> list[Any]:
    with get_engine().connect() as connection:
        return list(connection.execute(text(statement), params))


def chunks_of(source_id: str) -> list[Any]:
    return sql(
        "SELECT c.id, c.chunk_index, c.content, c.content_hash, c.metadata, c.embedding_model,"
        " vector_dims(c.embedding) AS dims, vector_norm(c.embedding) AS norm, c.workspace_id"
        " FROM chunks c JOIN documents d ON d.id = c.document_id"
        " WHERE d.source_id = :s ORDER BY c.chunk_index",
        s=source_id,
    )


def job(job_id: str) -> Any:
    return sql("SELECT * FROM ingestion_jobs WHERE id = :j", j=job_id)[0]


def source(source_id: str) -> Any:
    return sql("SELECT * FROM sources WHERE id = :s", s=source_id)[0]


def make_due() -> None:
    sql_exec("UPDATE ingestion_jobs SET next_run_at = now() WHERE status = 'queued'")


def sql_exec(statement: str, **params: object) -> None:
    with get_engine().begin() as connection:
        connection.execute(text(statement), params)


def long_markdown(sections: int = 6, paragraphs: int = 4, marker: str = "") -> bytes:
    parts = ["# Northstar handbook"]
    for s in range(sections):
        parts.append(f"## Section {s}")
        for p in range(paragraphs):
            parts.append(
                " ".join(
                    f"Section {s} paragraph {p} sentence {i}{marker} explains a policy detail."
                    for i in range(8)
                )
            )
    return "\n\n".join(parts).encode()


# --- happy path -----------------------------------------------------------------------------------


def test_worker_indexes_an_upload_into_normalised_768_vectors(
    tenant_a: Tenant, worker: WorkerHarness
) -> None:
    body = upload(tenant_a, "handbook.md", long_markdown())
    assert worker.run_all() == 1

    rows = chunks_of(body["source"]["id"])
    assert len(rows) >= 3
    for row in rows:
        assert row.dims == 768
        assert math.isclose(row.norm, 1.0, rel_tol=1e-6)
        assert row.embedding_model == "gemini-embedding-001"
        assert row.workspace_id == tenant_a.workspace_id
        assert set(row.metadata) >= {"title", "heading", "char_start", "char_end", "page_start"}
    assert [r.chunk_index for r in rows] == list(range(len(rows)))

    # Documents only ever use the document task type, in batches of at most 8.
    assert {task for task, _ in worker.provider.calls} == {"RETRIEVAL_DOCUMENT"}
    assert all(len(texts) <= 8 for _, texts in worker.provider.calls)
    assert worker.provider.texts_embedded == len({r.content_hash for r in rows})

    finished = job(body["job"]["id"])
    assert finished.status == "succeeded" and finished.attempts == 1
    assert finished.result["outcome"] == "indexed"
    assert finished.result["embedded_texts"] == worker.provider.texts_embedded
    assert finished.lock_token is None and finished.finished_at is not None
    assert source(body["source"]["id"]).status == "ready"


def test_many_chunks_are_embedded_in_batches_of_eight(
    tenant_a: Tenant, worker: WorkerHarness
) -> None:
    upload(tenant_a, "big.md", long_markdown(sections=12, paragraphs=5))
    worker.run_all()
    sizes = [len(texts) for _, texts in worker.provider.calls]
    assert len(sizes) > 1 and sizes[:-1] == [8] * (len(sizes) - 1) and 1 <= sizes[-1] <= 8


def test_pdf_chunks_carry_page_numbers(tenant_a: Tenant, worker: WorkerHarness) -> None:
    pages = [
        [f"Page {p} line {i} about the Northstar warranty and returns process." for i in range(30)]
        for p in range(1, 5)
    ]
    body = upload(tenant_a, "policy.pdf", make_pdf(pages, title="Policies"))
    worker.run_all()
    rows = chunks_of(body["source"]["id"])
    assert rows[0].metadata["page_start"] == 1
    assert rows[-1].metadata["page_end"] == 4
    document = sql(
        "SELECT title, metadata FROM documents WHERE source_id = :s", s=body["source"]["id"]
    )[0]
    assert document.title == "Policies" and document.metadata["page_count"] == 4


def test_docx_tables_are_indexed(tenant_a: Tenant, worker: WorkerHarness) -> None:
    data = make_docx(
        [
            ("heading", ("Shipping rates", 1)),
            ("paragraph", "Rates depend on the destination region and parcel weight."),
            ("table", [["Region", "Price"], ["Domestic", "5 USD"], ["International", "25 USD"]]),
        ]
    )
    body = upload(tenant_a, "shipping.docx", data)
    worker.run_all()
    (row,) = chunks_of(body["source"]["id"])
    assert "International | 25 USD" in row.content
    assert row.metadata["heading"] == "Shipping rates"


# --- idempotency ----------------------------------------------------------------------------------


def test_reindexing_unchanged_content_makes_no_embedding_calls(
    tenant_a: Tenant, worker: WorkerHarness
) -> None:
    body = upload(tenant_a, "handbook.md", long_markdown())
    worker.run_all()
    before = chunks_of(body["source"]["id"])
    calls = len(worker.provider.calls)

    reindex = tenant_a.client.post(f"/api/v1/sources/{body['source']['id']}/reindex").json()
    worker.run_all()

    assert len(worker.provider.calls) == calls  # no Gemini calls
    after = chunks_of(body["source"]["id"])
    assert [r.id for r in after] == [r.id for r in before]  # chunks not even rewritten
    assert job(reindex["id"]).result["outcome"] == "unchanged"
    assert sql("SELECT count(*) FROM documents")[0][0] == 1


def test_same_content_under_a_new_upload_is_not_duplicated(
    tenant_a: Tenant, worker: WorkerHarness
) -> None:
    data = long_markdown()
    body = upload(tenant_a, "handbook.md", data)
    worker.run_all()
    count = len(chunks_of(body["source"]["id"]))
    # Same text, different bytes (trailing whitespace): a new job runs, finds no change.
    upload(tenant_a, "handbook.md", data + b"\n\n   \n")
    worker.run_all()
    assert len(chunks_of(body["source"]["id"])) == count
    assert sql("SELECT count(*) FROM chunks")[0][0] == count


def test_changed_content_replaces_chunks_and_reuses_unchanged_embeddings(
    tenant_a: Tenant, worker: WorkerHarness
) -> None:
    body = upload(tenant_a, "handbook.md", long_markdown())
    worker.run_all()
    old = chunks_of(body["source"]["id"])
    first_run = worker.provider.texts_embedded

    changed = long_markdown().replace(
        b"Section 5 paragraph 3 sentence 7", b"Section 5 now says 60 days"
    )
    upload(tenant_a, "handbook.md", changed)
    worker.run_all()
    new = chunks_of(body["source"]["id"])

    assert any("60 days" in r.content for r in new)
    assert not any("Section 5 paragraph 3 sentence 7" in r.content for r in new)
    assert {r.id for r in new}.isdisjoint({r.id for r in old})  # replaced, not appended
    newly_embedded = worker.provider.texts_embedded - first_run
    assert 1 <= newly_embedded < len(new)  # only the changed chunks went to Gemini
    assert sql("SELECT count(*) FROM documents")[0][0] == 1


def test_identical_text_in_two_workspaces_shares_nothing(
    tenant_a: Tenant, tenant_b: Tenant, worker: WorkerHarness
) -> None:
    a = upload(tenant_a, "returns.md", RETURNS_POLICY.encode())
    worker.run_all()
    calls_after_a = worker.provider.texts_embedded
    b = upload(tenant_b, "returns.md", RETURNS_POLICY.encode())
    worker.run_all()

    a_rows, b_rows = chunks_of(a["source"]["id"]), chunks_of(b["source"]["id"])
    assert {r.workspace_id for r in a_rows} == {tenant_a.workspace_id}
    assert {r.workspace_id for r in b_rows} == {tenant_b.workspace_id}
    # The embedding cache is per workspace: B pays for its own embeddings.
    assert worker.provider.texts_embedded == 2 * calls_after_a
    cache = sql("SELECT workspace_id, count(*) FROM embedding_cache GROUP BY workspace_id")
    assert {row[0] for row in cache} == {tenant_a.workspace_id, tenant_b.workspace_id}


# --- the previous good index survives failed replacements -----------------------------------------


def test_embedding_failure_keeps_the_previous_index(tenant_a: Tenant) -> None:
    harness = make_worker()
    body = upload(tenant_a, "handbook.md", long_markdown())
    harness.run_all()
    good = chunks_of(body["source"]["id"])

    harness.provider.failures.extend([ProviderUnavailable("down")] * 10)
    second = upload(tenant_a, "handbook.md", long_markdown(marker=" (v2)"))
    for _ in range(3):  # every attempt fails
        harness.run_all()
        make_due()

    failed = job(second["job"]["id"])
    assert failed.status == "failed" and failed.attempts == 3
    assert "Gave up after 3 attempts" in failed.last_error
    current = source(body["source"]["id"])
    assert current.status == "failed"
    assert "previously indexed version is still available" in current.error_message
    assert [(r.id, r.content) for r in chunks_of(body["source"]["id"])] == [
        (r.id, r.content) for r in good
    ]


def test_crash_inside_the_publish_transaction_rolls_back(
    tenant_a: Tenant, worker: WorkerHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    body = upload(tenant_a, "handbook.md", long_markdown())
    worker.run_all()
    good = chunks_of(body["source"]["id"])

    def explode(*args: object) -> None:  # after old chunks were deleted and new ones inserted
        raise RuntimeError("power cut during publish")

    monkeypatch.setattr(IngestionWorker, "_mark_ready", staticmethod(explode))
    upload(tenant_a, "handbook.md", long_markdown(marker=" (v2)"))
    worker.run_all()

    assert [(r.id, r.content) for r in chunks_of(body["source"]["id"])] == [
        (r.id, r.content) for r in good
    ]
    retry = sql(
        "SELECT status, last_error FROM ingestion_jobs"
        " WHERE job_type = 'ingest' AND status = 'queued'"
    )
    assert retry and "unexpected error" in retry[0].last_error
    assert "power cut" not in retry[0].last_error  # internal details never reach users


# --- permanent failures ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "data", "message"),
    [
        ("broken.pdf", b"%PDF-1.7\ngarbage that is not a pdf", "could not be read"),
        ("scanned.pdf", make_pdf([[], []]), "OCR"),
        ("locked.pdf", encrypt_pdf(make_pdf([["Confidential text here."]])), "Password-protected"),
        ("tiny.txt", b"Too short.", "too little text"),
    ],
    ids=["malformed", "scanned", "encrypted", "too-short"],
)
def test_unprocessable_files_fail_once_with_a_clear_message(
    tenant_a: Tenant, worker: WorkerHarness, name: str, data: bytes, message: str
) -> None:
    body = upload(tenant_a, name, data)
    worker.run_all()
    make_due()
    assert worker.run_all() == 0  # not retried
    failed = job(body["job"]["id"])
    assert failed.status == "failed" and failed.attempts == 1
    assert message in failed.last_error
    assert message in source(body["source"]["id"]).error_message
    assert chunks_of(body["source"]["id"]) == []
    assert worker.provider.calls == []


@pytest.mark.parametrize(
    "error",
    [ProviderNotConfigured(), ProviderConfigurationError("Gemini rejected the API key.")],
    ids=["no-key", "bad-key"],
)
def test_configuration_problems_fail_without_retries(tenant_a: Tenant, error: Exception) -> None:
    harness = make_worker()
    harness.provider.failures.append(error)
    body = upload(tenant_a, "returns.md", RETURNS_POLICY.encode())
    harness.run_all()
    failed = job(body["job"]["id"])
    assert failed.status == "failed" and failed.attempts == 1
    assert str(error) in failed.last_error


def test_unchanged_reindex_works_without_a_gemini_key(
    tenant_a: Tenant, worker: WorkerHarness
) -> None:
    body = upload(tenant_a, "returns.md", RETURNS_POLICY.encode())
    worker.run_all()

    def no_key() -> None:
        raise ProviderNotConfigured()

    keyless = make_worker()
    keyless.worker._provider_factory = no_key  # type: ignore[assignment]
    reindex = tenant_a.client.post(f"/api/v1/sources/{body['source']['id']}/reindex").json()
    keyless.run_all()
    assert job(reindex["id"]).result["outcome"] == "unchanged"


# --- transient failures and deferrals -------------------------------------------------------------


def test_temporary_provider_failure_is_retried_with_backoff(tenant_a: Tenant) -> None:
    harness = make_worker()
    harness.provider.failures.append(ProviderUnavailable("Gemini is temporarily unavailable."))
    body = upload(tenant_a, "returns.md", RETURNS_POLICY.encode())
    harness.run_all()

    waiting = job(body["job"]["id"])
    assert waiting.status == "queued" and waiting.attempts == 1
    assert waiting.next_run_at > datetime.now(UTC) + timedelta(seconds=20)  # 30 s backoff
    assert "Retrying in 30 s (attempt 1 of 3)" in waiting.last_error
    assert source(body["source"]["id"]).status == "pending"
    assert harness.run_all() == 0  # not due yet

    make_due()
    harness.run_all()
    done = job(body["job"]["id"])
    assert done.status == "succeeded" and done.attempts == 2


def test_gemini_daily_quota_defers_without_using_an_attempt(tenant_a: Tenant) -> None:
    harness = make_worker()
    harness.provider.failures.append(
        ProviderQuotaExhausted("Gemini's free-tier daily quota is used up.")
    )
    body = upload(tenant_a, "returns.md", RETURNS_POLICY.encode())
    harness.run_all()
    deferred = job(body["job"]["id"])
    assert deferred.status == "queued" and deferred.attempts == 0
    assert deferred.next_run_at > datetime.now(UTC) + timedelta(minutes=50)
    assert "daily quota" in deferred.last_error


def test_unexpected_errors_are_retried_with_a_generic_message(tenant_a: Tenant) -> None:
    harness = make_worker()
    harness.provider.failures.append(RuntimeError("secret internal detail /etc/passwd"))
    body = upload(tenant_a, "returns.md", RETURNS_POLICY.encode())
    harness.run_all()
    waiting = job(body["job"]["id"])
    assert waiting.status == "queued"
    assert "secret internal detail" not in waiting.last_error


# --- delete ---------------------------------------------------------------------------------------


def test_delete_removes_source_data_and_orphaned_cache_only(
    tenant_a: Tenant, tenant_b: Tenant, worker: WorkerHarness
) -> None:
    keep = upload(tenant_a, "keep.md", RETURNS_POLICY.encode())
    drop = upload(
        tenant_a,
        "drop.md",
        RETURNS_POLICY.encode() + b"\n\nExtra unique paragraph about gift cards.",
    )
    other = upload(
        tenant_b,
        "drop.md",
        RETURNS_POLICY.encode() + b"\n\nExtra unique paragraph about gift cards.",
    )
    worker.run_all()
    drop_hashes = {r.content_hash for r in chunks_of(drop["source"]["id"])}
    keep_hashes = {r.content_hash for r in chunks_of(keep["source"]["id"])}

    tenant_a.client.delete(f"/api/v1/sources/{drop['source']['id']}")
    worker.run_all()

    assert sql("SELECT count(*) FROM sources WHERE id = :s", s=drop["source"]["id"])[0][0] == 0
    assert chunks_of(drop["source"]["id"]) == []
    a_cache = {
        r[0]
        for r in sql(
            "SELECT content_hash FROM embedding_cache WHERE workspace_id = :w",
            w=tenant_a.workspace_id,
        )
    }
    assert keep_hashes <= a_cache  # still used by another source
    assert not (drop_hashes - keep_hashes) & a_cache  # orphans removed
    assert chunks_of(other["source"]["id"])  # the other workspace is untouched
    assert (
        sql(
            "SELECT count(*) FROM embedding_cache WHERE workspace_id = :w", w=tenant_b.workspace_id
        )[0][0]
        > 0
    )


def test_unknown_source_job_is_dropped_cleanly(tenant_a: Tenant, worker: WorkerHarness) -> None:
    body = upload(tenant_a, "returns.md", RETURNS_POLICY.encode())
    sql_exec("DELETE FROM sources WHERE id = :s", s=body["source"]["id"])  # job cascades away
    assert worker.run_all() == 0
    assert uuid.UUID(body["job"]["id"])


class MisbehavingProvider(MockEmbeddingProvider):
    """Breaks the provider contract, to prove the pipeline checks it independently."""

    def __init__(self, mode: str) -> None:
        super().__init__(model="gemini-embedding-001")
        self.mode = mode

    def embed_documents(self, texts: Sequence[str]) -> EmbeddingResult:
        result = super().embed_documents(texts)
        if self.mode == "fewer-vectors":
            return replace(result, vectors=result.vectors[:-1])
        if self.mode == "wrong-model":
            return replace(result, model="some-other-model")
        return replace(result, task_type="RETRIEVAL_QUERY")


@pytest.mark.parametrize("mode", ["fewer-vectors", "wrong-model", "wrong-task-type"])
def test_a_provider_breaking_its_contract_publishes_nothing(tenant_a: Tenant, mode: str) -> None:
    harness = make_worker(provider=MisbehavingProvider(mode))
    body = upload(tenant_a, "handbook.md", long_markdown())
    harness.run_all()
    assert chunks_of(body["source"]["id"]) == []
    waiting = job(body["job"]["id"])
    assert waiting.status == "queued"  # retried as a provider problem, never published
    assert "does not match the request" in waiting.last_error
    assert sql("SELECT count(*) FROM embedding_cache")[0][0] == 0  # bad vectors never cached


def test_a_source_kind_without_a_handler_fails_once_with_a_clear_message(
    tenant_a: Tenant, worker: WorkerHarness
) -> None:
    """Ingestion is dispatched by source kind; an unknown kind is a permanent failure."""
    body = upload(tenant_a, "returns.md", RETURNS_POLICY.encode())
    worker.worker._handlers.pop("file")
    worker.run_all()
    failed = job(body["job"]["id"])
    assert failed.status == "failed" and failed.attempts == 1
    assert 'Sources of type "file" are not supported' in failed.last_error
    assert chunks_of(body["source"]["id"]) == []
