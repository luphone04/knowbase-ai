"""Website crawling end to end: API -> queue -> worker -> crawl -> M4 indexing -> pgvector.

Each test serves a FakeSite (no network) and uses a fake clock (no waiting).
"""

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import text

from app.api.v1.sources import get_resolver
from app.db.session import get_engine
from app.main import app
from app.providers.errors import ProviderUnavailable
from app.providers.interfaces import DOCUMENT_TASK
from app.worker.queue import claim_next_job, recover_stale_jobs
from tests.fakes.websites import SITE, FakeSite, public_resolver
from tests.integration.conftest import Tenant, WorkerHarness, make_worker

pytestmark = pytest.mark.integration

WEBSITES = "/api/v1/sources/websites"
FOOTER = (
    "Northstar Outfitters is a fictional demo store. Free shipping on orders over $50, "
    "and our support team answers within one business day."
)
PAGES = {
    "returns": "Unused items can be returned within 30 days of delivery for a full refund. "
    "Laptops can be returned within 14 days if the factory seal is intact.",
    "shipping": "Standard shipping takes three to five business days within the country. "
    "Express shipping arrives the next business day when ordered before noon.",
    "warranty": "Every tent carries a two-year warranty against manufacturing defects. "
    "The warranty does not cover damage from misuse or normal wear.",
    "faq": "You can track an order from the link in the confirmation email. "
    "Gift cards never expire and can be used on any product in the store.",
}


@pytest.fixture(autouse=True)
def public_dns() -> Iterator[None]:
    app.dependency_overrides[get_resolver] = lambda: public_resolver
    yield
    app.dependency_overrides.pop(get_resolver, None)


def page_html(title: str, body: str, extra: str = "") -> str:
    return (
        f"<html><head><title>{title} | Northstar</title></head><body>"
        f'<nav><a href="/">Home</a><a href="/returns">Returns</a></nav>'
        f"<main><h1>{title}</h1><p>{body}</p>{extra}"
        # Site chrome inside <main>: only the repeated-block filter can catch it.
        f'<div class="promo"><p>{FOOTER}</p></div></main>'
        "<footer>Copyright Northstar</footer></body></html>"
    )


def northstar(**overrides: str) -> FakeSite:
    """Home links to returns, shipping, warranty, a login page, an external site, and
    /internal (disallowed by robots.txt). The sitemap adds /faq."""
    texts = {**PAGES, **overrides}
    site = FakeSite()
    site.add(
        f"{SITE}/robots.txt",
        f"User-agent: *\nDisallow: /internal\nSitemap: {SITE}/sitemap.xml\n",
        content_type="text/plain",
    )
    site.add(
        f"{SITE}/sitemap.xml",
        f"<urlset><url><loc>{SITE}/faq</loc></url><url><loc>{SITE}/internal/x</loc></url></urlset>",
        content_type="application/xml",
    )
    home_links = (
        '<a href="/returns">r</a><a href="/shipping">s</a><a href="/warranty">w</a>'
        '<a href="/login">sign in</a><a href="https://partner.example.net/">partner</a>'
        '<a href="/internal/admin-notes">internal</a>'
    )
    site.add(
        f"{SITE}/",
        page_html("Welcome", "Northstar sells tents, packs, and laptops for travel.", home_links),
    )
    for name, body in texts.items():
        site.add(f"{SITE}/{name}", page_html(name.title(), body))
    return site


def add_site(tenant: Tenant, url: str = SITE + "/", **body: Any) -> dict[str, Any]:
    response = tenant.client.post(WEBSITES, json={"url": url, **body})
    assert response.status_code == 202, response.text
    result: dict[str, Any] = response.json()
    return result


def recrawl(tenant: Tenant, source_id: str) -> dict[str, Any]:
    response = tenant.client.post(f"/api/v1/sources/{source_id}/reindex")
    assert response.status_code == 202, response.text
    result: dict[str, Any] = response.json()
    return result


def sql(statement: str, **params: object) -> list[Any]:
    with get_engine().connect() as connection:
        return list(connection.execute(text(statement), params))


def sql_exec(statement: str, **params: object) -> None:
    with get_engine().begin() as connection:
        connection.execute(text(statement), params)


def job(job_id: str) -> Any:
    return sql("SELECT * FROM ingestion_jobs WHERE id = :j", j=job_id)[0]


def documents(source_id: str) -> dict[str, Any]:
    rows = sql("SELECT * FROM documents WHERE source_id = :s", s=source_id)
    return {row.canonical_url: row for row in rows}


def chunk_ids(source_id: str) -> dict[str, list[uuid.UUID]]:
    rows = sql(
        "SELECT d.canonical_url, c.id FROM chunks c JOIN documents d ON d.id = c.document_id"
        " WHERE d.source_id = :s ORDER BY d.canonical_url, c.chunk_index",
        s=source_id,
    )
    out: dict[str, list[uuid.UUID]] = {}
    for row in rows:
        out.setdefault(row.canonical_url, []).append(row.id)
    return out


def pages(source_id: str) -> dict[str, Any]:
    rows = sql("SELECT * FROM crawl_pages WHERE source_id = :s", s=source_id)
    return {row.url: row for row in rows}


def make_due() -> None:
    sql_exec("UPDATE ingestion_jobs SET next_run_at = now() WHERE status = 'queued'")


def crawl_once(harness: WorkerHarness) -> None:
    assert harness.run_all() >= 1


# --- first crawl ---------------------------------------------------------------------------


def test_first_crawl_indexes_every_page_into_pgvector(tenant_a: Tenant) -> None:
    site = northstar()
    harness = make_worker(site=site)
    body = add_site(tenant_a)
    source_id = body["source"]["id"]
    crawl_once(harness)

    done = job(body["job"]["id"])
    assert done.status == "succeeded", done.last_error
    result = done.result
    assert result["outcome"] == "crawled"
    assert result["pages_indexed"] == 5  # home, returns, shipping, warranty, faq
    assert result["pages_failed"] == 0
    assert result["robots"] == "parsed"
    assert result["embedded_texts"] == harness.provider.texts_embedded > 0
    assert {task for task, _ in harness.provider.calls} == {DOCUMENT_TASK}

    docs = documents(source_id)
    assert set(docs) == {f"{SITE}/", *(f"{SITE}/{name}" for name in PAGES)}
    returns = docs[f"{SITE}/returns"]
    assert returns.title == "Returns | Northstar"
    assert returns.fetched_at is not None
    assert returns.metadata["url"] == f"{SITE}/returns"
    assert returns.metadata["discovered_via"] == "link"
    assert docs[f"{SITE}/faq"].metadata["discovered_via"] == "sitemap"
    assert "30 days of delivery" in returns.text_content

    chunks = sql(
        "SELECT c.metadata, c.content, vector_dims(c.embedding) AS dims,"
        " vector_norm(c.embedding) AS norm, c.workspace_id FROM chunks c"
        " JOIN documents d ON d.id = c.document_id WHERE d.source_id = :s",
        s=source_id,
    )
    assert chunks and all(c.dims == 768 and abs(c.norm - 1) < 1e-6 for c in chunks)
    assert all(c.workspace_id == tenant_a.workspace_id for c in chunks)
    assert all(c.metadata["url"].startswith(SITE) for c in chunks)
    # Navigation, footer, and the promo repeated on every page never reach the index.
    assert not any(FOOTER in c.content or "Copyright" in c.content for c in chunks)
    assert result["boilerplate_blocks_removed"] >= 5

    crawl = pages(source_id)
    assert crawl[f"{SITE}/returns"].status == "indexed"
    assert crawl[f"{SITE}/internal/admin-notes"].reason == "robots"
    assert crawl[f"{SITE}/internal/x"].reason == "robots"  # from the sitemap
    assert crawl[f"{SITE}/login"].reason == "auth_page"
    assert not any("/internal" in url or "/login" in url for url in site.urls)
    assert not any("partner.example.net" in url for url in site.urls)

    detail = tenant_a.client.get(f"/api/v1/sources/{source_id}").json()
    assert detail["status"] == "ready" and detail["error_message"] is None
    assert detail["document_count"] == 5
    assert detail["page_counts"]["indexed"] == 5
    assert detail["page_counts"]["skipped"] == 3


def test_page_and_depth_limits_from_the_source_are_enforced(tenant_a: Tenant) -> None:
    site = northstar()
    harness = make_worker(site=site)
    body = add_site(tenant_a, max_pages=2, max_depth=1)
    crawl_once(harness)
    result = job(body["job"]["id"]).result
    assert result["pages_requested"] == 2 and result["stopped_reason"] == "page_limit"
    assert len(documents(body["source"]["id"])) == 2
    page_requests = [u for u in site.urls if not u.endswith((".txt", ".xml"))]
    assert len(page_requests) == 2


# --- incremental re-crawls ---------------------------------------------------------------


def test_unchanged_recrawl_reuses_everything_and_calls_gemini_zero_times(
    tenant_a: Tenant,
) -> None:
    harness = make_worker(site=northstar())
    body = add_site(tenant_a)
    source_id = body["source"]["id"]
    crawl_once(harness)
    before_ids = chunk_ids(source_id)
    before_fetch = {url: d.fetched_at for url, d in documents(source_id).items()}
    calls_before = harness.provider.texts_embedded

    second = recrawl(tenant_a, source_id)
    crawl_once(harness)

    result = job(second["id"]).result
    assert result["pages_unchanged"] == 5 and result["pages_indexed"] == 0
    assert result["embedded_texts"] == 0 and result["embedding_requests"] == 0
    assert harness.provider.texts_embedded == calls_before  # no Gemini calls at all
    assert chunk_ids(source_id) == before_ids  # the same rows, untouched
    after = documents(source_id)
    assert all(after[url].fetched_at > before_fetch[url] for url in after)  # fetch recorded
    rows = pages(source_id)
    assert rows[f"{SITE}/returns"].status == "unchanged"
    assert rows[f"{SITE}/returns"].last_job_id == uuid.UUID(second["id"])
    assert sql("SELECT count(*) AS n FROM documents")[0].n == 5  # no duplicates


def test_only_the_changed_page_is_reindexed(tenant_a: Tenant) -> None:
    site = northstar()
    harness = make_worker(site=site)
    body = add_site(tenant_a)
    source_id = body["source"]["id"]
    crawl_once(harness)
    before = chunk_ids(source_id)
    old_changed_at = pages(source_id)[f"{SITE}/returns"].changed_at

    new_policy = "Unused items can now be returned within 60 days of delivery for a full refund."
    site.add(f"{SITE}/returns", page_html("Returns", new_policy))
    calls_before = harness.provider.texts_embedded
    second = recrawl(tenant_a, source_id)
    crawl_once(harness)

    result = job(second["id"]).result
    assert result["pages_indexed"] == 1 and result["pages_unchanged"] == 4
    assert result["embedded_texts"] == harness.provider.texts_embedded - calls_before == 1
    after = chunk_ids(source_id)
    assert after[f"{SITE}/returns"] != before[f"{SITE}/returns"]
    for url in before:
        if url != f"{SITE}/returns":
            assert after[url] == before[url]
    assert "60 days" in documents(source_id)[f"{SITE}/returns"].text_content
    row = pages(source_id)[f"{SITE}/returns"]
    assert row.status == "indexed" and row.changed_at > old_changed_at


def test_a_title_change_alone_reindexes_without_new_embeddings(tenant_a: Tenant) -> None:
    site = northstar()
    harness = make_worker(site=site)
    body = add_site(tenant_a)
    crawl_once(harness)
    site.add(f"{SITE}/faq", page_html("Faq", PAGES["faq"]).replace("Faq | Northstar", "Help"))
    calls_before = harness.provider.texts_embedded
    recrawl(tenant_a, body["source"]["id"])
    crawl_once(harness)
    assert documents(body["source"]["id"])[f"{SITE}/faq"].title == "Help"
    assert harness.provider.texts_embedded == calls_before  # chunk text unchanged: cached


def test_pages_that_disappear_are_removed_from_the_index(tenant_a: Tenant) -> None:
    site = northstar()
    harness = make_worker(site=site)
    body = add_site(tenant_a)
    source_id = body["source"]["id"]
    crawl_once(harness)

    site.add(f"{SITE}/warranty", "gone", status=404)
    site.add(
        f"{SITE}/robots.txt",
        f"User-agent: *\nDisallow: /internal\nDisallow: /shipping\nSitemap: {SITE}/sitemap.xml\n",
        content_type="text/plain",
    )
    requests_before = len(site.urls)
    second = recrawl(tenant_a, source_id)
    crawl_once(harness)

    assert job(second["id"]).result["pages_removed"] == 2
    docs = documents(source_id)
    assert f"{SITE}/warranty" not in docs and f"{SITE}/shipping" not in docs
    assert f"{SITE}/returns" in docs
    rows = pages(source_id)
    assert (
        rows[f"{SITE}/warranty"].reason == "not_found"
        and rows[f"{SITE}/warranty"].document_url is None
    )
    assert rows[f"{SITE}/shipping"].reason == "robots"
    assert f"{SITE}/shipping" not in site.urls[requests_before:]  # not fetched once disallowed


def test_duplicate_urls_and_identical_pages_make_one_document(tenant_a: Tenant) -> None:
    site = FakeSite()
    site.add(
        f"{SITE}/",
        page_html(
            "Welcome",
            "Northstar sells tents, packs, and laptops for travel.",
            '<a href="/faq">a</a><a href="/faq/">b</a><a href="/faq?utm_source=nl">c</a>'
            '<a href="/FAQ/../faq#top">d</a><a href="/help">e</a>',
        ),
    )
    site.add(f"{SITE}/faq", page_html("Faq", PAGES["faq"]))
    site.add(f"{SITE}/help", page_html("Faq", PAGES["faq"]))  # same content, other URL
    harness = make_worker(site=site)
    body = add_site(tenant_a)
    crawl_once(harness)
    docs = documents(body["source"]["id"])
    assert set(docs) == {f"{SITE}/", f"{SITE}/faq"}
    assert site.urls.count(f"{SITE}/faq") == 1
    help_row = pages(body["source"]["id"])[f"{SITE}/help"]
    assert help_row.status == "duplicate" and help_row.duplicate_of == f"{SITE}/faq"
    assert help_row.document_url is None  # a duplicate owns no document


# --- page ownership: duplicates never own documents ---------------------------------------


def two_page_site(**pages: str) -> FakeSite:
    """Home links to the given paths (in order); each path serves its HTML."""
    site = FakeSite()
    links = "".join(f'<a href="/{path}">{path}</a>' for path in pages)
    site.add(f"{SITE}/", page_html("Welcome", "Northstar sells tents, packs, and laptops.", links))
    for path, html in pages.items():
        site.add(f"{SITE}/{path}", html)
    return site


def relink_home(site: FakeSite, *paths: str) -> None:
    links = "".join(f'<a href="/{path}">{path}</a>' for path in paths)
    site.add(f"{SITE}/", page_html("Welcome", "Northstar sells tents, packs, and laptops.", links))


def chunk_texts(source_id: str) -> list[str]:
    rows = sql(
        "SELECT c.content FROM chunks c JOIN documents d ON d.id = c.document_id"
        " WHERE d.source_id = :s",
        s=source_id,
    )
    return [row.content for row in rows]


FAQ = page_html("Faq", PAGES["faq"])
WARRANTY = page_html("Warranty", PAGES["warranty"])


def test_a_page_that_becomes_a_duplicate_gives_up_its_document(tenant_a: Tenant) -> None:
    site = two_page_site(faq=FAQ, help=WARRANTY)
    harness = make_worker(site=site)
    body = add_site(tenant_a)
    source_id = body["source"]["id"]
    crawl_once(harness)
    assert set(documents(source_id)) == {f"{SITE}/", f"{SITE}/faq", f"{SITE}/help"}
    faq_chunks = chunk_ids(source_id)[f"{SITE}/faq"]

    site.add(f"{SITE}/help", FAQ)  # /help now serves exactly what /faq serves
    second = recrawl(tenant_a, source_id)
    crawl_once(harness)

    assert job(second["id"]).result["pages_removed"] == 1
    assert set(documents(source_id)) == {f"{SITE}/", f"{SITE}/faq"}
    assert chunk_ids(source_id)[f"{SITE}/faq"] == faq_chunks  # the original is untouched
    assert not any("two-year warranty" in text for text in chunk_texts(source_id))
    row = pages(source_id)[f"{SITE}/help"]
    assert row.status == "duplicate" and row.document_url is None
    assert row.duplicate_of == f"{SITE}/faq"


def test_a_disappearing_alias_keeps_an_original_the_crawl_did_not_reach(
    tenant_a: Tenant,
) -> None:
    site = two_page_site(faq=FAQ, help=FAQ)  # /help is a duplicate of /faq
    harness = make_worker(site=site)
    body = add_site(tenant_a)
    source_id = body["source"]["id"]
    crawl_once(harness)
    assert pages(source_id)[f"{SITE}/help"].status == "duplicate"
    faq_chunks = chunk_ids(source_id)[f"{SITE}/faq"]

    relink_home(site, "help")  # /faq is no longer reached
    site.add(f"{SITE}/help", "gone", status=404)
    requests_before = len(site.urls)
    second = recrawl(tenant_a, source_id)
    crawl_once(harness)

    assert f"{SITE}/faq" not in site.urls[requests_before:]
    assert job(second["id"]).result["pages_removed"] == 0
    assert chunk_ids(source_id)[f"{SITE}/faq"] == faq_chunks
    assert pages(source_id)[f"{SITE}/help"].reason == "not_found"
    assert pages(source_id)[f"{SITE}/faq"].document_url == f"{SITE}/faq"


def test_an_alias_that_becomes_unique_keeps_an_original_the_crawl_did_not_reach(
    tenant_a: Tenant,
) -> None:
    site = two_page_site(faq=FAQ, help=FAQ)
    harness = make_worker(site=site)
    body = add_site(tenant_a)
    source_id = body["source"]["id"]
    crawl_once(harness)
    faq_chunks = chunk_ids(source_id)[f"{SITE}/faq"]

    relink_home(site, "help")
    site.add(f"{SITE}/help", WARRANTY)  # now its own content
    second = recrawl(tenant_a, source_id)
    crawl_once(harness)

    assert job(second["id"]).result["pages_removed"] == 0
    assert set(documents(source_id)) == {f"{SITE}/", f"{SITE}/faq", f"{SITE}/help"}
    assert chunk_ids(source_id)[f"{SITE}/faq"] == faq_chunks
    row = pages(source_id)[f"{SITE}/help"]
    assert row.status == "indexed" and row.document_url == f"{SITE}/help"
    assert row.duplicate_of is None


def test_a_document_two_pages_own_survives_while_one_still_owns_it(tenant_a: Tenant) -> None:
    """Both pages declare one canonical URL; crawls reach them in turn."""
    canonical = f'<link rel="canonical" href="{SITE}/faq">'
    site = two_page_site(**{"faq-a": canonical + FAQ})
    harness = make_worker(site=site)
    body = add_site(tenant_a)
    source_id = body["source"]["id"]
    crawl_once(harness)  # faq-a owns /faq

    relink_home(site, "faq-b")
    site.add(f"{SITE}/faq-b", canonical + FAQ)
    recrawl(tenant_a, source_id)
    crawl_once(harness)  # faq-b owns /faq too; faq-a not reached
    rows = pages(source_id)
    assert rows[f"{SITE}/faq-a"].document_url == rows[f"{SITE}/faq-b"].document_url == f"{SITE}/faq"

    site.add(f"{SITE}/faq-b", "gone", status=404)
    recrawl(tenant_a, source_id)
    crawl_once(harness)  # faq-b is gone, but faq-a (not reached) still owns /faq
    assert f"{SITE}/faq" in documents(source_id)


def test_a_canonical_claim_cannot_take_over_a_real_page_with_other_content(
    tenant_a: Tenant,
) -> None:
    """/a (content A) declares /b canonical; /b is a real page with content B; /a comes first."""
    site = two_page_site(
        a=page_html("Faq", PAGES["faq"], f'<link rel="canonical" href="{SITE}/b">'),
        b=WARRANTY,
    )
    harness = make_worker(site=site)
    body = add_site(tenant_a)
    source_id = body["source"]["id"]
    crawl_once(harness)

    result = job(body["job"]["id"]).result
    assert result["pages_indexed"] == 3 and result["pages_duplicate"] == 0
    docs = documents(source_id)
    assert set(docs) == {f"{SITE}/", f"{SITE}/a", f"{SITE}/b"}  # both distinct pages kept
    assert "Gift cards never expire" in docs[f"{SITE}/a"].text_content
    assert "two-year warranty" in docs[f"{SITE}/b"].text_content
    by_doc = {
        row.canonical_url: row.content
        for row in sql(
            "SELECT d.canonical_url, string_agg(c.content, ' ') AS content FROM chunks c"
            " JOIN documents d ON d.id = c.document_id WHERE d.source_id = :s"
            " GROUP BY d.canonical_url",
            s=source_id,
        )
    }
    assert "Gift cards" in by_doc[f"{SITE}/a"] and "warranty" not in by_doc[f"{SITE}/a"]
    assert "two-year warranty" in by_doc[f"{SITE}/b"] and "Gift" not in by_doc[f"{SITE}/b"]
    rows = pages(source_id)
    for path in ("a", "b"):
        row = rows[f"{SITE}/{path}"]
        assert row.status == "indexed"
        assert row.document_url == f"{SITE}/{path}"  # each owns its own document
        assert row.duplicate_of is None
    assert not any(row.duplicate_of == row.url for row in rows.values())  # no self-reference


def test_a_canonical_alias_with_identical_content_is_a_duplicate_of_the_real_page(
    tenant_a: Tenant,
) -> None:
    """A genuine alias (/a declares /b canonical, same content), /a processed first: one
    document, owned by the real page /b. (Before the fix /b became a duplicate of itself.)"""
    site = two_page_site(
        a=page_html("Faq", PAGES["faq"], f'<link rel="canonical" href="{SITE}/b">'),
        b=page_html("Faq", PAGES["faq"]),
    )
    harness = make_worker(site=site)
    body = add_site(tenant_a)
    source_id = body["source"]["id"]
    crawl_once(harness)

    assert set(documents(source_id)) == {f"{SITE}/", f"{SITE}/b"}
    rows = pages(source_id)
    assert rows[f"{SITE}/b"].status == "indexed" and rows[f"{SITE}/b"].document_url == f"{SITE}/b"
    alias = rows[f"{SITE}/a"]
    assert alias.status == "duplicate" and alias.duplicate_of == f"{SITE}/b"
    assert alias.document_url is None
    assert sql("SELECT count(*) AS n FROM chunks")[0].n == 2  # home + /b, nothing for /a


# --- removals when nothing (else) can be indexed --------------------------------------------


def indexed_northstar(tenant: Tenant) -> tuple[FakeSite, WorkerHarness, str]:
    site = northstar()
    harness = make_worker(site=site)
    body = add_site(tenant)
    crawl_once(harness)
    assert len(documents(body["source"]["id"])) == 5
    return site, harness, body["source"]["id"]


def test_gone_pages_are_removed_even_when_no_page_is_indexable(tenant_a: Tenant) -> None:
    site, harness, source_id = indexed_northstar(tenant_a)
    site.add(f"{SITE}/", "gone", status=404)  # start page gone: no links to follow
    site.add(f"{SITE}/faq", "gone", status=410)  # still listed in the sitemap
    second = recrawl(tenant_a, source_id)
    harness.run_all()

    failed = job(second["id"])
    assert failed.status == "failed" and failed.attempts == 1  # permanent: no retry
    docs = documents(source_id)
    assert f"{SITE}/" not in docs and f"{SITE}/faq" not in docs
    # Pages this crawl couldn't reach keep their documents.
    assert {f"{SITE}/returns", f"{SITE}/shipping", f"{SITE}/warranty"} <= set(docs)
    assert not any("Gift cards never expire" in t for t in chunk_texts(source_id))


def test_mixed_removals_and_transient_failures_with_nothing_indexable(tenant_a: Tenant) -> None:
    site, harness, source_id = indexed_northstar(tenant_a)
    before = chunk_ids(source_id)
    noindex = '<meta name="robots" content="noindex">'
    site.add(
        f"{SITE}/",
        page_html(
            "Welcome",
            noindex + "Home",
            '<a href="/returns">r</a><a href="/shipping">s</a><a href="/warranty">w</a>',
        ),
    )
    site.add(f"{SITE}/returns", "gone", status=410)  # gone
    site.add(f"{SITE}/shipping", "busy", status=503)  # transient
    site.add(f"{SITE}/warranty", page_html("Warranty", noindex + PAGES["warranty"]))
    site.add(  # newly disallowed: /faq (still in the sitemap)
        f"{SITE}/robots.txt",
        f"User-agent: *\nDisallow: /faq\nSitemap: {SITE}/sitemap.xml\n",
        content_type="text/plain",
    )
    second = recrawl(tenant_a, source_id)
    harness.run_all()

    waiting = job(second["id"])
    assert waiting.status == "queued" and waiting.attempts == 1  # retried for /shipping
    docs = documents(source_id)
    assert set(docs) == {f"{SITE}/shipping"}  # only the transient failure kept its index
    assert chunk_ids(source_id)[f"{SITE}/shipping"] == before[f"{SITE}/shipping"]
    rows = pages(source_id)
    assert rows[f"{SITE}/"].reason == "noindex"
    assert rows[f"{SITE}/returns"].reason == "not_found"
    assert rows[f"{SITE}/warranty"].reason == "noindex"
    assert rows[f"{SITE}/faq"].reason == "robots"
    assert rows[f"{SITE}/shipping"].reason == "server_error"
    assert rows[f"{SITE}/shipping"].document_url == f"{SITE}/shipping"


def test_transient_failures_alone_keep_the_whole_index(tenant_a: Tenant) -> None:
    site, harness, source_id = indexed_northstar(tenant_a)
    before = chunk_ids(source_id)
    for url in list(site.pages):
        if not url.endswith((".txt", ".xml")):
            site.add(url, "maintenance", status=503)
    second = recrawl(tenant_a, source_id)
    harness.run_all()
    assert job(second["id"]).status == "queued"  # retried later
    assert chunk_ids(source_id) == before


def test_pages_newly_disallowed_are_removed_even_if_not_reached(tenant_a: Tenant) -> None:
    site, harness, source_id = indexed_northstar(tenant_a)
    site.add(
        f"{SITE}/robots.txt",
        f"User-agent: *\nDisallow: /warranty\nSitemap: {SITE}/sitemap.xml\n",
        content_type="text/plain",
    )
    relink_home(site, "returns", "shipping")  # /warranty isn't even linked any more
    requests_before = len(site.urls)
    second = recrawl(tenant_a, source_id)
    crawl_once(harness)
    assert job(second["id"]).status == "succeeded"
    assert f"{SITE}/warranty" not in documents(source_id)
    row = pages(source_id)[f"{SITE}/warranty"]
    assert row.reason == "robots" and row.document_url is None
    assert f"{SITE}/warranty" not in site.urls[requests_before:]


@pytest.mark.parametrize(
    ("rule", "kept"),
    [
        ("Disallow: /", set()),  # the whole site is withdrawn
        ("Disallow: /$", {"returns", "shipping", "warranty", "faq"}),  # only the start page
    ],
    ids=["whole-site", "start-page-only"],
)
def test_a_start_url_newly_disallowed_by_robots(
    tenant_a: Tenant, rule: str, kept: set[str]
) -> None:
    site, harness, source_id = indexed_northstar(tenant_a)
    site.add(f"{SITE}/robots.txt", f"User-agent: *\n{rule}\n", content_type="text/plain")
    requests_before = len(site.urls)
    second = recrawl(tenant_a, source_id)
    harness.run_all()

    failed = job(second["id"])
    assert failed.status == "failed" and "does not allow" in failed.last_error
    assert site.urls[requests_before:] == [f"{SITE}/robots.txt"]  # nothing else fetched
    assert set(documents(source_id)) == {f"{SITE}/{name}" for name in kept}
    assert pages(source_id)[f"{SITE}/"].reason == "robots"


# --- failures -------------------------------------------------------------------------------


def test_a_failed_page_update_keeps_the_previous_index(tenant_a: Tenant) -> None:
    site = northstar()
    harness = make_worker(site=site)
    body = add_site(tenant_a)
    source_id = body["source"]["id"]
    crawl_once(harness)
    good = chunk_ids(source_id)
    good_text = documents(source_id)[f"{SITE}/returns"].text_content

    new_text = "A completely rewritten returns policy: every item may be returned within 90 days."
    site.add(f"{SITE}/returns", page_html("Returns", new_text))
    harness.provider.failures.extend([ProviderUnavailable("down")] * 10)
    second = recrawl(tenant_a, source_id)
    for _ in range(3):
        harness.run_all()
        make_due()

    failed = job(second["id"])
    assert failed.status == "failed" and "Gave up after 3 attempts" in failed.last_error
    assert chunk_ids(source_id) == good
    assert documents(source_id)[f"{SITE}/returns"].text_content == good_text
    status = sql("SELECT status, error_message FROM sources WHERE id = :s", s=source_id)[0]
    assert "previously indexed version is still available" in status.error_message


def test_some_failed_pages_do_not_fail_the_crawl(tenant_a: Tenant) -> None:
    site = northstar()
    site.add(f"{SITE}/shipping", "oops", status=500)
    site.add(f"{SITE}/warranty", page_html("Warranty", "Short."))  # too little text
    harness = make_worker(site=site)
    body = add_site(tenant_a)
    crawl_once(harness)

    done = job(body["job"]["id"])
    assert done.status == "succeeded"
    assert done.result["pages_failed"] == 2 and done.result["pages_indexed"] == 3
    rows = pages(body["source"]["id"])
    assert rows[f"{SITE}/shipping"].reason == "server_error"
    assert rows[f"{SITE}/warranty"].reason == "too_little_text"
    failures = tenant_a.client.get(
        f"/api/v1/sources/{body['source']['id']}/pages", params={"status": "failed"}
    ).json()
    assert {p["url"] for p in failures} == {f"{SITE}/shipping", f"{SITE}/warranty"}
    assert all(p["message"] for p in failures)
    detail = tenant_a.client.get(f"/api/v1/sources/{body['source']['id']}").json()
    assert detail["status"] == "ready"
    assert detail["error_message"].startswith("2 of 5 pages could not be indexed")


def test_a_site_that_is_down_is_retried_later(tenant_a: Tenant) -> None:
    site = FakeSite()
    site.add(f"{SITE}/", "maintenance", status=503)
    harness = make_worker(site=site)
    body = add_site(tenant_a)
    harness.run_all()
    waiting = job(body["job"]["id"])
    assert waiting.status == "queued" and waiting.attempts == 1
    assert "No pages could be indexed" in waiting.last_error
    assert waiting.next_run_at > datetime.now(UTC) + timedelta(seconds=20)


def test_a_missing_start_page_fails_once_without_retries(tenant_a: Tenant) -> None:
    harness = make_worker(site=FakeSite())  # everything 404
    body = add_site(tenant_a)
    harness.run_all()
    failed = job(body["job"]["id"])
    assert failed.status == "failed" and failed.attempts == 1
    assert "no longer exists (HTTP 404)" in failed.last_error


def test_unreachable_robots_txt_crawls_nothing_and_retries(tenant_a: Tenant) -> None:
    site = northstar()
    site.add(f"{SITE}/robots.txt", "down", status=503)
    harness = make_worker(site=site)
    body = add_site(tenant_a)
    harness.run_all()
    waiting = job(body["job"]["id"])
    assert waiting.status == "queued" and "complete disallow" in waiting.last_error
    assert site.urls == [f"{SITE}/robots.txt"]
    assert documents(body["source"]["id"]) == {}


def test_a_host_resolving_to_a_private_address_is_never_contacted(tenant_a: Tenant) -> None:
    """Public when the URL was added, private when the worker crawls (DNS rebinding)."""
    site = northstar()
    harness = make_worker(site=site, resolver=lambda host, port: ["10.0.0.12"])
    body = add_site(tenant_a)
    harness.run_all()
    failed = job(body["job"]["id"])
    assert failed.status == "failed" and failed.attempts == 1
    assert "non-public address" in failed.last_error
    assert site.urls == []


def test_the_daily_budget_defers_the_crawl_and_keeps_finished_pages(tenant_a: Tenant) -> None:
    harness = make_worker(site=northstar(), env={"DAILY_EMBEDDING_TEXT_LIMIT": "2"})
    body = add_site(tenant_a)
    harness.run_all()
    deferred = job(body["job"]["id"])
    assert deferred.status == "queued" and deferred.attempts == 0
    assert "daily embedding limit" in deferred.last_error
    assert len(documents(body["source"]["id"])) == 2  # finished pages stay indexed
    assert harness.provider.texts_embedded == 2


# --- the queue: crash recovery and fencing ---------------------------------------------------


def test_a_crawl_abandoned_by_a_crashed_worker_is_recovered(tenant_a: Tenant) -> None:
    harness = make_worker(site=northstar())
    body = add_site(tenant_a)
    crashed = claim_next_job(harness.worker._session_factory, "worker-that-died")
    assert crashed is not None
    sql_exec("UPDATE ingestion_jobs SET locked_at = now() - interval '10 minutes'")
    assert recover_stale_jobs(harness.worker._session_factory, lease_seconds=120) == 1
    make_due()  # recovery uses the host clock; the database container's may lag slightly
    crawl_once(harness)
    done = job(body["job"]["id"])
    assert done.status == "succeeded" and done.attempts == 2, done.last_error
    assert len(documents(body["source"]["id"])) == 5


def test_a_worker_that_lost_its_lease_publishes_nothing(tenant_a: Tenant) -> None:
    site = northstar()
    original = site.handler

    def takeover(request: httpx.Request) -> httpx.Response:
        if str(request.url) == f"{SITE}/returns":
            # Meanwhile the lease lapsed and another worker re-claimed the job.
            sql_exec("UPDATE ingestion_jobs SET lock_token = gen_random_uuid()")
        return original(request)

    site.handler = takeover  # type: ignore[method-assign]
    harness = make_worker(site=site)
    body = add_site(tenant_a)
    harness.run_all()
    assert documents(body["source"]["id"]) == {}
    assert pages(body["source"]["id"]) == {}
    assert job(body["job"]["id"]).status == "running"  # still the new owner's
    # Its next fenced progress write failed, so it stopped crawling at once.
    assert site.urls[-1] == f"{SITE}/returns"


# --- isolation and deletion -------------------------------------------------------------------


def test_website_data_is_isolated_per_workspace(tenant_a: Tenant, tenant_b: Tenant) -> None:
    harness = make_worker(site=northstar())
    body = add_site(tenant_a)
    crawl_once(harness)
    source_id = body["source"]["id"]
    for table in ("documents", "chunks", "crawl_pages"):
        owners = sql(f"SELECT DISTINCT workspace_id FROM {table}")  # noqa: S608
        assert [row.workspace_id for row in owners] == [tenant_a.workspace_id]
    for path in (
        f"/sources/{source_id}",
        f"/sources/{source_id}/pages",
        f"/jobs/{body['job']['id']}",
    ):
        assert tenant_b.client.get("/api/v1" + path).status_code == 404
    assert tenant_b.client.post(f"/api/v1/sources/{source_id}/reindex").status_code == 404
    assert tenant_b.client.delete(f"/api/v1/sources/{source_id}").status_code == 404
    assert tenant_b.client.get("/api/v1/sources").json() == []
    # The same site added by B is B's own source, crawled and stored separately.
    other = add_site(tenant_b)
    assert other["source"]["id"] != source_id


def test_deleting_a_website_removes_pages_chunks_and_cache(tenant_a: Tenant) -> None:
    harness = make_worker(site=northstar())
    body = add_site(tenant_a)
    crawl_once(harness)
    response = tenant_a.client.delete(f"/api/v1/sources/{body['source']['id']}")
    assert response.status_code == 202
    harness.run_all()
    for table in ("sources", "documents", "chunks", "crawl_pages", "embedding_cache"):
        assert sql(f"SELECT count(*) AS n FROM {table}")[0].n == 0, table  # noqa: S608
