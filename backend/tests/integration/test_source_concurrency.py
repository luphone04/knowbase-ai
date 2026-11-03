"""Uploads, deletes, recovery, and workers racing on one source (audit M2).

Races are made deterministic by holding a real PostgreSQL row lock in the
test while a request runs in another thread, or by interleaving claims and
uploads by hand.
"""

import uuid
from typing import Any

import pytest
from sqlalchemy import text

from app.config import get_settings
from app.db.session import get_engine
from app.ingestion.storage import FileStorage
from tests.fakes.documents import RETURNS_POLICY
from tests.integration.conftest import Tenant, WorkerHarness

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
