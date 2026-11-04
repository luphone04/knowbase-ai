"""Upload and source/job endpoints: validation, storage safety, idempotency, isolation."""

import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import IngestionJob, Source
from app.db.tenancy import SKIP_WORKSPACE_SCOPE
from app.main import create_app
from app.providers import factory
from tests.fakes.documents import RETURNS_POLICY, make_docx, make_pdf
from tests.integration.conftest import Tenant, WorkerHarness, new_client

pytestmark = pytest.mark.integration

UPLOAD = "/api/v1/sources/files"


def upload(client: TestClient, name: str, data: bytes) -> httpx2.Response:
    return client.post(UPLOAD, files={"file": (name, data, "application/octet-stream")})


@pytest.fixture(autouse=True)
def no_gemini_in_the_api(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API must never embed or parse: only the worker does."""

    def forbidden() -> None:
        raise AssertionError("the API must not call the embedding provider")

    monkeypatch.setattr(factory, "get_embedding_provider", forbidden)


def stored_files() -> list[Path]:
    root = Path(get_settings().upload_dir)
    return [p for p in root.rglob("*") if p.is_file()]


# --- upload ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "data", "file_type"),
    [
        (
            "policy.pdf",
            make_pdf([["Returns within 30 days of delivery for unused laptops."]]),
            "pdf",
        ),
        ("guide.docx", make_docx([("paragraph", "The Trailblazer 14 weighs 1.3 kg.")]), "docx"),
        ("faq.txt", b"How long is the warranty? Two years.", "txt"),
        ("returns.md", RETURNS_POLICY.encode(), "md"),
    ],
)
def test_upload_creates_a_source_and_a_queued_job(
    tenant_a: Tenant, db: Session, name: str, data: bytes, file_type: str
) -> None:
    response = upload(tenant_a.client, name, data)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["unchanged"] is False
    assert body["source"]["status"] == "pending"
    assert body["source"]["file_type"] == file_type
    assert body["source"]["original_filename"] == name
    assert body["source"]["file_size"] == len(data)
    assert body["job"]["status"] == "queued" and body["job"]["job_type"] == "ingest"

    job = db.get_one(
        IngestionJob, uuid.UUID(body["job"]["id"]), execution_options=SKIP_WORKSPACE_SCOPE
    )
    assert job.workspace_id == tenant_a.workspace_id  # from the session, not the client
    (path,) = stored_files()
    assert path.read_bytes() == data
    assert path.name != name
    assert path.resolve().is_relative_to(Path(get_settings().upload_dir).resolve())


def test_responses_never_expose_storage_keys_or_paths(tenant_a: Tenant) -> None:
    response = upload(tenant_a.client, "returns.md", RETURNS_POLICY.encode())
    source_id = response.json()["source"]["id"]
    texts = [
        response.text,
        tenant_a.client.get("/api/v1/sources").text,
        tenant_a.client.get(f"/api/v1/sources/{source_id}").text,
    ]
    (path,) = stored_files()
    for text in texts:
        assert "storage_key" not in text
        assert get_settings().upload_dir not in text
        assert path.name not in text


@pytest.mark.parametrize(
    ("name", "data", "status", "code"),
    [
        ("tool.exe", b"MZ\x90\x00", 415, "unsupported_file_type"),
        ("empty.txt", b"", 422, "empty_file"),
        ("fake.pdf", b"not a pdf at all", 422, "file_content_mismatch"),
        ("fake.docx", b"not a zip", 422, "file_content_mismatch"),
        ("latin.txt", "café".encode("latin-1"), 422, "invalid_text_encoding"),
    ],
)
def test_invalid_uploads_are_rejected_and_nothing_is_stored(
    tenant_a: Tenant, db: Session, name: str, data: bytes, status: int, code: str
) -> None:
    response = upload(tenant_a.client, name, data)
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert stored_files() == []
    assert (
        db.scalar(
            select(func.count()).select_from(Source).execution_options(**SKIP_WORKSPACE_SCOPE)
        )
        == 0
    )


def test_oversized_upload_is_refused_with_413(
    tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    get_settings.cache_clear()
    try:
        small_app = TestClient(
            create_app(), base_url="http://localhost", client=("198.51.100.7", 1)
        )
        small_app.cookies.update(tenant_a.client.cookies)
        exactly_over = b"a" * (1024 * 1024 + 1)
        response = small_app.post(UPLOAD, files={"file": ("big.txt", exactly_over)})
        assert response.status_code == 413
        way_over = b"a" * (3 * 1024 * 1024)  # rejected by the body limit before parsing
        assert small_app.post(UPLOAD, files={"file": ("huge.txt", way_over)}).status_code == 413
    finally:
        monkeypatch.setenv("MAX_UPLOAD_MB", "10")
        get_settings.cache_clear()
    assert stored_files() == []


def test_an_oversized_chunked_upload_is_refused_with_413(
    tenant_a: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Content-Length: the limit is enforced while streaming, and still answers 413."""
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    get_settings.cache_clear()
    try:
        small_app = TestClient(
            create_app(), base_url="http://localhost", client=("198.51.100.8", 1)
        )
        small_app.cookies.update(tenant_a.client.cookies)
        boundary = "knowbase-test-boundary"

        def chunked_body() -> Iterator[bytes]:
            yield (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file";'
                ' filename="big.txt"\r\nContent-Type: text/plain\r\n\r\n'
            ).encode()
            for _ in range(3 * 16):
                yield b"a" * 65536  # 3 MB in 64 KB chunks
            yield f"\r\n--{boundary}--\r\n".encode()

        response = small_app.post(
            UPLOAD,
            content=chunked_body(),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        assert response.status_code == 413, response.text
        assert response.json()["error"]["code"] == "payload_too_large"
    finally:
        monkeypatch.setenv("MAX_UPLOAD_MB", "10")
        get_settings.cache_clear()
    assert stored_files() == []


def test_path_traversal_names_are_neutralised(tenant_a: Tenant) -> None:
    response = upload(tenant_a.client, "../../../../etc/cron.d/evil.txt", b"harmless text content")
    assert response.status_code == 202
    assert response.json()["source"]["original_filename"] == "evil.txt"
    (path,) = stored_files()
    assert path.resolve().is_relative_to(Path(get_settings().upload_dir).resolve())
    assert not Path("/etc/cron.d/evil.txt").exists()


def test_upload_requires_authentication_and_origin(tenant_a: Tenant) -> None:
    assert upload(new_client(), "a.txt", b"text").status_code == 401
    response = tenant_a.client.post(
        UPLOAD, files={"file": ("a.txt", b"text")}, headers={"Origin": "http://evil.example"}
    )
    assert response.status_code == 403


# --- re-upload ---------------------------------------------------------------------------


def test_same_name_reupload_is_a_new_version_of_one_source(tenant_a: Tenant, db: Session) -> None:
    first = upload(tenant_a.client, "Returns.md", b"Version one of the returns policy text.").json()
    second = upload(
        tenant_a.client, "returns.MD", b"Version two of the returns policy text."
    ).json()
    assert second["source"]["id"] == first["source"]["id"]  # case-insensitive name match
    assert second["job"]["id"] == first["job"]["id"]  # the waiting job is updated, not duplicated
    count = db.scalar(
        select(func.count()).select_from(Source).execution_options(**SKIP_WORKSPACE_SCOPE)
    )
    assert count == 1


def test_identical_reupload_of_a_ready_source_changes_nothing(
    tenant_a: Tenant, worker: WorkerHarness
) -> None:
    data = RETURNS_POLICY.encode()
    upload(tenant_a.client, "returns.md", data)
    worker.run_all()
    calls_before = len(worker.provider.calls)
    files_before = stored_files()

    response = upload(tenant_a.client, "returns.md", data)
    assert response.status_code == 200
    body = response.json()
    assert body["unchanged"] is True and body["job"] is None
    assert worker.run_all() == 0  # nothing queued
    assert len(worker.provider.calls) == calls_before
    assert stored_files() == files_before


# --- inspection, reindex, delete ------------------------------------------------------------


def test_source_detail_shows_documents_jobs_and_counts(
    tenant_a: Tenant, worker: WorkerHarness
) -> None:
    source_id = upload(tenant_a.client, "returns.md", RETURNS_POLICY.encode()).json()["source"][
        "id"
    ]
    worker.run_all()
    detail = tenant_a.client.get(f"/api/v1/sources/{source_id}").json()
    assert detail["status"] == "ready" and detail["last_indexed_at"]
    assert detail["document_count"] == 1 and detail["chunk_count"] >= 1
    (document,) = detail["documents"]
    assert document["title"] == "Returns policy"
    assert document["metadata"]["embedding_model"] == "gemini-embedding-001"
    assert document["metadata"]["embedding_dimensions"] == 768
    assert detail["jobs"][0]["status"] == "succeeded"
    job = tenant_a.client.get(f"/api/v1/jobs/{detail['jobs'][0]['id']}").json()
    assert (
        job["result"]["outcome"] == "indexed" and job["result"]["chunks"] == detail["chunk_count"]
    )


def test_reindex_queues_a_job_for_the_stored_file(tenant_a: Tenant, worker: WorkerHarness) -> None:
    source_id = upload(tenant_a.client, "returns.md", RETURNS_POLICY.encode()).json()["source"][
        "id"
    ]
    worker.run_all()
    response = tenant_a.client.post(f"/api/v1/sources/{source_id}/reindex")
    assert response.status_code == 202 and response.json()["job_type"] == "reindex"
    worker.run_all()
    job = tenant_a.client.get(f"/api/v1/jobs/{response.json()['id']}").json()
    assert job["status"] == "succeeded" and job["result"]["outcome"] == "unchanged"


def test_delete_removes_everything_and_then_404s(tenant_a: Tenant, worker: WorkerHarness) -> None:
    source_id = upload(tenant_a.client, "returns.md", RETURNS_POLICY.encode()).json()["source"][
        "id"
    ]
    worker.run_all()
    response = tenant_a.client.delete(f"/api/v1/sources/{source_id}")
    assert response.status_code == 202 and response.json()["job_type"] == "delete"
    assert tenant_a.client.get(f"/api/v1/sources/{source_id}").json()["status"] == "deleting"
    assert upload(tenant_a.client, "returns.md", b"new version text").status_code == 409
    worker.run_all()
    assert tenant_a.client.get(f"/api/v1/sources/{source_id}").status_code == 404
    assert stored_files() == []


# --- workspace isolation ----------------------------------------------------------------


def test_other_workspaces_cannot_see_or_touch_a_source(
    tenant_a: Tenant, tenant_b: Tenant, worker: WorkerHarness, db: Session
) -> None:
    body = upload(tenant_a.client, "returns.md", RETURNS_POLICY.encode()).json()
    source_id, job_id = body["source"]["id"], body["job"]["id"]
    worker.run_all()

    b = tenant_b.client
    random_id = uuid.uuid4()
    for path in (f"/api/v1/sources/{source_id}", f"/api/v1/jobs/{job_id}"):
        foreign = b.get(path)
        guess = b.get(path.rsplit("/", 1)[0] + f"/{random_id}")
        assert foreign.status_code == guess.status_code == 404
    assert b.post(f"/api/v1/sources/{source_id}/reindex").status_code == 404
    assert b.delete(f"/api/v1/sources/{source_id}").status_code == 404
    assert b.get("/api/v1/sources").json() == []

    # A's source is untouched: still ready, nothing queued.
    assert tenant_a.client.get(f"/api/v1/sources/{source_id}").json()["status"] == "ready"
    assert worker.run_all() == 0


def test_same_file_name_in_two_workspaces_makes_two_independent_sources(
    tenant_a: Tenant, tenant_b: Tenant
) -> None:
    a = upload(tenant_a.client, "returns.md", RETURNS_POLICY.encode()).json()
    b = upload(tenant_b.client, "returns.md", RETURNS_POLICY.encode()).json()
    assert a["source"]["id"] != b["source"]["id"]


def test_plain_member_cannot_delete(tenant_a: Tenant, tenant_b: Tenant, db: Session) -> None:
    from datetime import timedelta

    from app.api.dependencies import AUTH_COOKIE
    from app.core.security import create_access_token
    from app.db.models import WorkspaceMember
    from app.db.tenancy import bind_workspace

    source_id = upload(tenant_a.client, "returns.md", RETURNS_POLICY.encode()).json()["source"][
        "id"
    ]
    bind_workspace(db, tenant_a.workspace_id)
    db.add(
        WorkspaceMember(workspace_id=tenant_a.workspace_id, user_id=tenant_b.user_id, role="member")
    )
    db.commit()
    token, _ = create_access_token(
        tenant_b.user_id,
        tenant_a.workspace_id,
        secret=get_settings().jwt_secret.get_secret_value(),
        lifetime=timedelta(hours=1),
    )
    member = new_client()
    member.cookies.set(AUTH_COOKIE, token)
    assert member.get(f"/api/v1/sources/{source_id}").status_code == 200
    assert member.delete(f"/api/v1/sources/{source_id}").status_code == 403


# --- file-upload-only scope (website crawling removed 2026-11-03) ------------------------


def test_website_endpoints_no_longer_exist(tenant_a: Tenant) -> None:
    body = upload(tenant_a.client, "returns.md", RETURNS_POLICY.encode()).json()
    source_id = body["source"]["id"]
    client = tenant_a.client
    assert (
        client.post("/api/v1/sources/websites", json={"url": "https://a.example/"}).status_code
        == 404
    )
    assert client.get("/api/v1/sources/websites").status_code == 404
    assert client.get(f"/api/v1/sources/{source_id}/pages").status_code == 404
    assert client.get(f"/api/v1/sources/{source_id}/pages?status=failed").status_code == 404


def test_responses_carry_no_website_fields(tenant_a: Tenant, worker: WorkerHarness) -> None:
    body = upload(tenant_a.client, "returns.md", RETURNS_POLICY.encode()).json()
    worker.run_all()
    detail = tenant_a.client.get(f"/api/v1/sources/{body['source']['id']}").json()
    website_fields = {"source_url", "settings", "page_counts", "canonical_url", "fetched_at"}
    assert not website_fields & set(detail)
    assert detail["kind"] == "file"  # the generic source kind stays
    assert not website_fields & set(detail["documents"][0])
    listed = tenant_a.client.get("/api/v1/sources").json()[0]
    assert not website_fields & set(listed)
