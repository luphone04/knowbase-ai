"""POST /sources/websites and the page endpoints: validation, SSRF, limits, isolation.

DNS is replaced by a fake resolver. The API must never fetch anything itself:
any attempt to build or use a fetcher in these tests fails.
"""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.v1.sources import get_resolver
from app.crawler import fetcher
from app.db.models import IngestionJob, Source
from app.db.tenancy import SKIP_WORKSPACE_SCOPE
from app.main import app
from app.providers import factory
from tests.integration.conftest import Tenant, new_client

pytestmark = pytest.mark.integration

WEBSITES = "/api/v1/sources/websites"
DNS = {
    "shop.example.com": ["93.184.216.34"],
    "www.example.org": ["2606:2800:220:1:248:1893:25c8:1946"],
    "rebind.example.com": ["10.0.0.5"],
    "mixed.example.com": ["93.184.216.34", "127.0.0.1"],
    "metadata.example.com": ["169.254.169.254"],
}


def fake_resolver(host: str, _port: int) -> list[str]:
    if host not in DNS:
        raise OSError("Name or service not known")
    return DNS[host]


@pytest.fixture(autouse=True)
def no_network_from_the_api(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("the API must never fetch a page or embed")

    monkeypatch.setattr(fetcher.SafeFetcher, "get", forbidden)
    monkeypatch.setattr(factory, "get_embedding_provider", forbidden)
    app.dependency_overrides[get_resolver] = lambda: fake_resolver
    yield
    app.dependency_overrides.pop(get_resolver, None)


def test_adding_a_website_creates_a_source_and_a_queued_crawl(
    tenant_a: Tenant, db: Session
) -> None:
    response = tenant_a.client.post(
        WEBSITES, json={"url": "HTTPS://Shop.Example.com/?utm_source=x"}
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["created"] is True
    source = body["source"]
    assert source["kind"] == "website"
    assert source["source_url"] == "https://shop.example.com/"
    assert source["status"] == "pending"
    assert source["settings"] == {"max_pages": 20, "max_depth": 2}
    assert body["job"]["status"] == "queued" and body["job"]["job_type"] == "ingest"
    row = db.get_one(
        IngestionJob, uuid.UUID(body["job"]["id"]), execution_options=SKIP_WORKSPACE_SCOPE
    )
    assert row.workspace_id == tenant_a.workspace_id


def test_http_and_https_are_accepted(tenant_a: Tenant) -> None:
    assert (
        tenant_a.client.post(WEBSITES, json={"url": "http://shop.example.com/"}).status_code == 202
    )
    assert (
        tenant_a.client.post(WEBSITES, json={"url": "https://www.example.org/docs"}).status_code
        == 202
    )


def test_adding_the_same_url_again_returns_the_existing_source(tenant_a: Tenant) -> None:
    first = tenant_a.client.post(WEBSITES, json={"url": "https://shop.example.com/"}).json()
    again = tenant_a.client.post(WEBSITES, json={"url": "https://shop.example.com:443/#top"})
    assert again.status_code == 200
    assert again.json()["created"] is False and again.json()["job"] is None
    assert again.json()["source"]["id"] == first["source"]["id"]


def test_limits_can_only_be_lowered(tenant_a: Tenant) -> None:
    body = tenant_a.client.post(
        WEBSITES, json={"url": "https://shop.example.com/", "max_pages": 500, "max_depth": 9}
    ).json()
    assert body["source"]["settings"] == {"max_pages": 20, "max_depth": 2}
    body = tenant_a.client.post(
        WEBSITES, json={"url": "https://www.example.org/", "max_pages": 3, "max_depth": 0}
    ).json()
    assert body["source"]["settings"] == {"max_pages": 3, "max_depth": 0}


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("ftp://shop.example.com/", "invalid_url"),
        ("file:///etc/passwd", "invalid_url"),
        ("javascript:alert(1)", "invalid_url"),
        ("https://admin:secret@shop.example.com/", "invalid_url"),
        ("not a url", "invalid_url"),
        ("https://shop.example.com/login", "invalid_url"),
        ("http://localhost:8000", "blocked_url"),
        ("http://localhost:8000/api/v1/sources", "blocked_url"),
        ("http://127.0.0.1/", "blocked_url"),
        ("http://127.1:5432/", "blocked_url"),
        ("http://2130706433/", "blocked_url"),
        ("http://[::1]/", "blocked_url"),
        ("http://[::ffff:127.0.0.1]/", "blocked_url"),
        ("http://10.0.0.1/", "blocked_url"),
        ("http://172.17.0.1/", "blocked_url"),
        ("http://192.168.0.10/admin", "blocked_url"),
        ("http://169.254.169.254/latest/meta-data/", "blocked_url"),
        ("http://[fe80::1]/", "blocked_url"),
        ("http://224.0.0.1/", "blocked_url"),
        ("http://240.0.0.1/", "blocked_url"),
        ("http://0.0.0.0/", "blocked_url"),
        ("http://db:5432/", "blocked_url"),
        ("http://api:8000/", "blocked_url"),
        ("http://host.docker.internal/", "blocked_url"),
        ("http://rebind.example.com/", "blocked_url"),  # DNS answers a private address
        ("http://mixed.example.com/", "blocked_url"),  # one private answer is enough
        ("http://metadata.example.com/", "blocked_url"),
        ("https://no-such-host.example.com/", "unresolvable_host"),
    ],
)
def test_unsafe_or_invalid_urls_are_refused_with_422(
    tenant_a: Tenant, db: Session, url: str, code: str
) -> None:
    response = tenant_a.client.post(WEBSITES, json={"url": url})
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == code
    assert db.scalar(select(Source).execution_options(**SKIP_WORKSPACE_SCOPE)) is None


def test_the_client_cannot_choose_the_workspace(tenant_a: Tenant, tenant_b: Tenant) -> None:
    response = tenant_a.client.post(
        WEBSITES,
        json={"url": "https://shop.example.com/", "workspace_id": str(tenant_b.workspace_id)},
    )
    assert response.status_code == 422


def test_adding_a_website_requires_authentication_and_origin(tenant_a: Tenant) -> None:
    assert new_client().post(WEBSITES, json={"url": "https://shop.example.com/"}).status_code == 401
    response = tenant_a.client.post(
        WEBSITES,
        json={"url": "https://shop.example.com/"},
        headers={"Origin": "http://evil.example"},
    )
    assert response.status_code == 403


def test_recrawl_while_a_crawl_is_queued_is_a_conflict(tenant_a: Tenant) -> None:
    body = tenant_a.client.post(WEBSITES, json={"url": "https://shop.example.com/"}).json()
    response = tenant_a.client.post(f"/api/v1/sources/{body['source']['id']}/reindex")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "crawl_in_progress"


def test_crawl_requests_are_rate_limited_per_workspace(
    tenant_a: Tenant, tenant_b: Tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.config import get_settings

    monkeypatch.setenv("DAILY_CRAWL_REQUEST_LIMIT", "1")
    get_settings.cache_clear()
    try:
        ok = tenant_a.client.post(WEBSITES, json={"url": "https://shop.example.com/"})
        assert ok.status_code == 202
        limited = tenant_a.client.post(WEBSITES, json={"url": "https://www.example.org/"})
        assert limited.status_code == 429 and "Retry-After" in limited.headers
        # Another workspace has its own allowance.
        assert (
            tenant_b.client.post(WEBSITES, json={"url": "https://www.example.org/"}).status_code
            == 202
        )
    finally:
        get_settings.cache_clear()


def test_pages_endpoint_is_for_website_sources_only(tenant_a: Tenant) -> None:
    upload = tenant_a.client.post(
        "/api/v1/sources/files", files={"file": ("a.txt", b"A plain text file with enough words.")}
    ).json()
    response = tenant_a.client.get(f"/api/v1/sources/{upload['source']['id']}/pages")
    assert response.status_code == 409
    body = tenant_a.client.post(WEBSITES, json={"url": "https://shop.example.com/"}).json()
    pages = tenant_a.client.get(f"/api/v1/sources/{body['source']['id']}/pages")
    assert pages.status_code == 200 and pages.json() == []
    bad = tenant_a.client.get(
        f"/api/v1/sources/{body['source']['id']}/pages", params={"status": "nonsense"}
    )
    assert bad.status_code == 422


def test_other_workspaces_cannot_see_or_touch_a_website(tenant_a: Tenant, tenant_b: Tenant) -> None:
    body = tenant_a.client.post(WEBSITES, json={"url": "https://shop.example.com/"}).json()
    source_id, job_id = body["source"]["id"], body["job"]["id"]
    b = tenant_b.client
    assert b.get(f"/api/v1/sources/{source_id}").status_code == 404
    assert b.get(f"/api/v1/sources/{source_id}/pages").status_code == 404
    assert b.get(f"/api/v1/jobs/{job_id}").status_code == 404
    assert b.post(f"/api/v1/sources/{source_id}/reindex").status_code == 404
    assert b.delete(f"/api/v1/sources/{source_id}").status_code == 404
    assert b.get("/api/v1/sources").json() == []
    # Same URL in B: an independent source, not A's.
    other = b.post(WEBSITES, json={"url": "https://shop.example.com/"})
    assert other.status_code == 202 and other.json()["source"]["id"] != source_id


def test_a_website_being_deleted_cannot_be_re_added(tenant_a: Tenant) -> None:
    body = tenant_a.client.post(WEBSITES, json={"url": "https://shop.example.com/"}).json()
    assert tenant_a.client.delete(f"/api/v1/sources/{body['source']['id']}").status_code == 202
    again = tenant_a.client.post(WEBSITES, json={"url": "https://shop.example.com/"})
    assert again.status_code == 409 and again.json()["error"]["code"] == "source_deleting"
