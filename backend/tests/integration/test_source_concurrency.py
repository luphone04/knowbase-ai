"""Uploads, deletes, recovery, and workers racing on one source (audit M1-M4, C-11, C-12, C-15).

Races are made deterministic by holding a real PostgreSQL row lock in the
test while a request runs in another thread, or by interleaving claims and
uploads by hand.
"""

import time
import uuid
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db.models import Source
from app.db.session import get_engine, get_sessionmaker
from app.db.tenancy import bind_workspace
from app.ingestion.pipeline import IngestionWorker, PermanentJobError
from app.ingestion.storage import FileStorage
from app.providers.interfaces import EmbeddingResult
from app.worker.queue import SUPERSEDED_MESSAGE, claim_next_job, enqueue_job, recover_stale_jobs
from tests.fakes.documents import RETURNS_POLICY
from tests.fakes.parsers import crash
from tests.integration.conftest import Tenant, WorkerHarness, make_worker

pytestmark = pytest.mark.integration

UPLOAD = "/api/v1/sources/files"
WARRANTY = (
    "# Warranty\n\n"
    + " ".join(
        f"Clause {i}: the NorthCare warranty covers manufacturing defects." for i in range(12)
    )
).encode()


def upload(tenant: Tenant, name: str, data: bytes) -> dict[str, Any]:
    response = tenant.client.post(UPLOAD, files={"file": (name, data)})
    assert response.status_code in (200, 202), response.text
    body: dict[str, Any] = response.json()
    return body


def sql(statement: str, **params: object) -> list[Any]:
    with get_engine().begin() as connection:
        result = connection.execute(text(statement), params)
        return list(result) if result.returns_rows else []


def status_of(source_id: str) -> str:
    return str(sql("SELECT status FROM sources WHERE id = :s", s=source_id)[0].status)


def chunk_texts(source_id: str) -> list[str]:
    rows = sql(
        "SELECT c.content FROM chunks c JOIN documents d ON d.id = c.document_id"
        " WHERE d.source_id = :s ORDER BY c.chunk_index",
        s=source_id,
    )
    return [row.content for row in rows]


def storage() -> FileStorage:
    return FileStorage(get_settings().upload_dir)


# --- M1: the API locks the source --------------------------------------------------------------


def test_an_upload_during_a_delete_waits_for_it_and_is_refused(
    tenant_a: Tenant, worker: WorkerHarness
) -> None:
    source_id = upload(tenant_a, "returns.md", RETURNS_POLICY.encode())["source"]["id"]
    worker.run_all()
    with get_sessionmaker()() as deleting, ThreadPoolExecutor(1) as pool:
        # A DELETE request mid-transaction: source locked, marked, delete job queued.
        bind_workspace(deleting, tenant_a.workspace_id)
        source = deleting.get(Source, uuid.UUID(source_id), with_for_update=True)
        assert source is not None
        source.status = "deleting"
        enqueue_job(
            deleting,
            workspace_id=tenant_a.workspace_id,
            source_id=source.id,
            job_type="delete",
            storage_key=None,
            max_attempts=3,
        )
        deleting.flush()
        racing = pool.submit(tenant_a.client.post, UPLOAD, files={"file": ("returns.md", WARRANTY)})
        time.sleep(0.5)
        assert not racing.done()  # the upload waits for the delete's row lock
        deleting.commit()
        response = racing.result(timeout=30)

    assert response.status_code == 409, response.text
    assert status_of(source_id) == "deleting"
    jobs = sql("SELECT job_type FROM ingestion_jobs WHERE status = 'queued'")
    assert [job.job_type for job in jobs] == ["delete"]  # never turned back into an ingest
    worker.run_all()
    assert sql("SELECT count(*) AS n FROM sources")[0].n == 0
    assert not (Path(get_settings().upload_dir) / str(tenant_a.workspace_id) / source_id).exists()


def test_a_waiting_delete_is_never_turned_back_into_an_ingest(tenant_a: Tenant) -> None:
    source_id = upload(tenant_a, "returns.md", RETURNS_POLICY.encode())["source"]["id"]
    with get_sessionmaker()() as db:
        bind_workspace(db, tenant_a.workspace_id)
        common: dict[str, Any] = {
            "workspace_id": tenant_a.workspace_id,
            "source_id": uuid.UUID(source_id),
            "storage_key": None,
            "max_attempts": 3,
        }
        delete = enqueue_job(db, job_type="delete", **common)
        assert delete is not None and delete.job_type == "delete"
        assert enqueue_job(db, job_type="ingest", **common) is None
        assert enqueue_job(db, job_type="reindex", **common) is None
        db.commit()
    assert [r.job_type for r in sql("SELECT job_type FROM ingestion_jobs")] == ["delete"]


# --- M2: file cleanup ---------------------------------------------------------------------------


def test_cleanup_never_deletes_a_file_that_is_still_being_uploaded(
    tenant_a: Tenant, worker: WorkerHarness
) -> None:
    first = upload(tenant_a, "returns.md", RETURNS_POLICY.encode())
    source_id = uuid.UUID(first["source"]["id"])
    worker.run_all()
    (v1_key,) = [r.storage_key for r in sql("SELECT storage_key FROM ingestion_jobs")]
    # Another upload has saved its file but not committed its job yet.
    in_flight = storage().new_key(tenant_a.workspace_id, source_id, "md")
    storage().save(in_flight, b"# In flight\n\nNot committed yet.")

    upload(tenant_a, "returns.md", WARRANTY)
    worker.run_all()

    assert storage().exists(in_flight)
    assert not storage().exists(v1_key)  # the finished, superseded version is removed
    current = sql("SELECT storage_key FROM sources WHERE id = :s", s=source_id)[0].storage_key
    assert storage().exists(current)


# --- M3: one worker per source, and no stale publish ----------------------------------------


def test_a_source_with_a_running_job_is_not_claimed_by_another_worker(tenant_a: Tenant) -> None:
    upload(tenant_a, "returns.md", RETURNS_POLICY.encode())
    first = claim_next_job(get_sessionmaker(), "worker-1")
    assert first is not None
    upload(tenant_a, "returns.md", WARRANTY)  # a newer version waits
    upload(tenant_a, "other.md", RETURNS_POLICY.encode())
    second = claim_next_job(get_sessionmaker(), "worker-2")
    assert second is not None and second.source_id != first.source_id  # other sources still run
    assert claim_next_job(get_sessionmaker(), "worker-3") is None


@pytest.mark.parametrize("arrives", ["before_parsing", "while_embedding"])
def test_an_older_upload_is_never_published_over_a_newer_one(
    tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch, arrives: str
) -> None:
    source_id = upload(tenant_a, "returns.md", RETURNS_POLICY.encode())["source"]["id"]
    harness = make_worker()
    stale = claim_next_job(get_sessionmaker(), "test-worker")
    assert stale is not None
    if arrives == "before_parsing":
        upload(tenant_a, "returns.md", WARRANTY)
    else:
        embed = harness.provider.embed_documents

        def upload_then_embed(texts: Sequence[str]) -> EmbeddingResult:
            if not harness.provider.calls:
                upload(tenant_a, "returns.md", WARRANTY)
            return embed(texts)

        monkeypatch.setattr(harness.provider, "embed_documents", upload_then_embed)

    harness.worker.process(stale)

    assert chunk_texts(source_id) == []  # version 1 was not published
    row = sql("SELECT status, last_error FROM ingestion_jobs WHERE id = :j", j=stale.id)[0]
    assert (row.status, row.last_error) == ("failed", SUPERSEDED_MESSAGE)
    assert status_of(source_id) == "pending"
    assert harness.run_all() == 1
    assert status_of(source_id) == "ready"
    assert all("NorthCare" in chunk for chunk in chunk_texts(source_id))
    assert not storage().exists(stale.storage_key or "")


# --- M4: cache provenance ------------------------------------------------------------------------


def test_deleting_a_source_removes_cache_rows_of_its_older_versions(
    tenant_a: Tenant, worker: WorkerHarness
) -> None:
    source_id = upload(tenant_a, "returns.md", RETURNS_POLICY.encode())["source"]["id"]
    worker.run_all()
    upload(tenant_a, "returns.md", WARRANTY)
    worker.run_all()
    owned = sql("SELECT count(*) AS n FROM embedding_cache WHERE source_id = :s", s=source_id)
    assert owned[0].n > len(chunk_texts(source_id))  # includes version 1's unused vectors

    tenant_a.client.delete(f"/api/v1/sources/{source_id}")
    worker.run_all()
    assert sql("SELECT count(*) AS n FROM embedding_cache")[0].n == 0


# --- recovery and failure edge cases (C-11, C-12, C-15) -------------------------------------


def test_recovery_never_overwrites_a_source_being_deleted(
    tenant_a: Tenant, worker: WorkerHarness
) -> None:
    source_id = upload(tenant_a, "returns.md", RETURNS_POLICY.encode())["source"]["id"]
    assert claim_next_job(get_sessionmaker(), "dead-worker") is not None
    assert tenant_a.client.delete(f"/api/v1/sources/{source_id}").status_code == 202
    # The worker died on its last attempt.
    sql(
        "UPDATE ingestion_jobs SET locked_at = now() - interval '10 minutes',"
        " attempts = max_attempts WHERE status = 'running'"
    )
    assert recover_stale_jobs(get_sessionmaker(), lease_seconds=120) == 1
    assert status_of(source_id) == "deleting"
    worker.run_all()
    assert sql("SELECT count(*) AS n FROM sources")[0].n == 0


def test_recovery_skips_a_source_locked_by_a_request(tenant_a: Tenant) -> None:
    source_id = upload(tenant_a, "returns.md", RETURNS_POLICY.encode())["source"]["id"]
    assert claim_next_job(get_sessionmaker(), "dead-worker") is not None
    sql("UPDATE ingestion_jobs SET locked_at = now() - interval '10 minutes'")
    with get_engine().connect() as request:
        request.execute(text("SELECT 1 FROM sources WHERE id = :s FOR UPDATE"), {"s": source_id})
        started = time.perf_counter()
        assert recover_stale_jobs(get_sessionmaker(), lease_seconds=120) == 0
        assert time.perf_counter() - started < 5  # skipped, not blocked
        request.rollback()
    assert recover_stale_jobs(get_sessionmaker(), lease_seconds=120) == 1
    assert status_of(source_id) == "pending"


def test_a_failed_job_leaves_the_source_pending_while_a_newer_request_waits(
    tenant_a: Tenant,
) -> None:
    source_id = upload(tenant_a, "returns.md", RETURNS_POLICY.encode())["source"]["id"]
    broken = make_worker(parse_target=crash)
    claim = claim_next_job(get_sessionmaker(), "test-worker")
    assert claim is not None
    reindex = tenant_a.client.post(f"/api/v1/sources/{source_id}/reindex")
    assert reindex.status_code == 202, reindex.text

    broken.worker.process(claim)

    assert sql("SELECT status FROM ingestion_jobs WHERE id = :j", j=claim.id)[0].status == "failed"
    assert status_of(source_id) == "pending"  # not "failed": the newer request is still to run
    assert make_worker().run_all() == 1
    assert status_of(source_id) == "ready"


def test_a_failed_delete_can_be_retried(
    tenant_a: Tenant, worker: WorkerHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_id = upload(tenant_a, "returns.md", RETURNS_POLICY.encode())["source"]["id"]
    worker.run_all()

    def broken_delete(self: IngestionWorker, job: object) -> None:
        raise PermanentJobError("The disk is read-only.")

    with monkeypatch.context() as patch:
        patch.setattr(IngestionWorker, "_delete", broken_delete)
        assert tenant_a.client.delete(f"/api/v1/sources/{source_id}").status_code == 202
        worker.run_all()
    detail = tenant_a.client.get(f"/api/v1/sources/{source_id}").json()
    assert detail["status"] == "failed"  # not stranded in "deleting"
    assert "Delete it again" in detail["error_message"]

    assert tenant_a.client.delete(f"/api/v1/sources/{source_id}").status_code == 202
    worker.run_all()
    assert tenant_a.client.get(f"/api/v1/sources/{source_id}").status_code == 404
