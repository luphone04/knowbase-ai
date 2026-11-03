"""The BFS crawler: ordering, scope, limits, politeness, robots.txt, and sitemaps.

Every test runs against a FakeSite with a FakeClock: no network and no real
waiting, and delays are asserted exactly.
"""

import pytest

from app.crawler.crawl import (
    CrawlBlocked,
    Crawler,
    CrawlLimits,
    CrawlReport,
    PageResult,
    RobotsUnreachable,
)
from app.crawler.urls import origin_of
from app.ingestion.parsers import Block
from app.ingestion.website import repeated_blocks
from tests.fakes.websites import SITE, FakeClock, FakeSite, make_fetcher

ORIGIN = origin_of(SITE + "/")
NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


def links(*paths: str) -> str:
    return "".join(f'<a href="{p}">{p}</a>' for p in paths)


def crawl(
    site: FakeSite,
    *,
    max_pages: int = 20,
    max_depth: int = 2,
    delay: float = 1.0,
    duration: float = 300.0,
    max_bytes: int = 5 * 1024 * 1024,
    start: str = SITE + "/",
) -> tuple[CrawlReport, FakeClock]:
    clock = site.clock or FakeClock()
    site.clock = clock
    crawler = Crawler(
        fetcher=make_fetcher(site, ORIGIN, clock=clock),
        limits=CrawlLimits(
            max_pages=max_pages,
            max_depth=max_depth,
            delay_seconds=delay,
            timeout_seconds=10,
            max_duration_seconds=duration,
            max_bytes=max_bytes,
        ),
        product_token="KnowbaseBot",
        clock=clock,
        sleep=clock.sleep,
    )
    return crawler.crawl(start), clock


def page_urls(report: CrawlReport) -> list[str]:
    return [p.url for p in report.pages if p.status != "skipped" or p.page is not None]


def by_url(report: CrawlReport) -> dict[str, PageResult]:
    return {p.url: p for p in report.pages}


def small_site() -> FakeSite:
    """/ -> about, products, contact; products -> shoes, bags; shoes -> laces (depth 3)."""
    site = FakeSite()
    site.html(f"{SITE}/", "Home", "<main><p>Welcome</p>" + links("/about", "/products", "/contact"))
    site.html(f"{SITE}/about", "About", "<p>About us</p>")
    site.html(
        f"{SITE}/products", "Products", "<p>All</p>" + links("/products/shoes", "/products/bags")
    )
    site.html(f"{SITE}/contact", "Contact", "<p>Call us</p>")
    site.html(f"{SITE}/products/shoes", "Shoes", "<p>Shoes</p>" + links("/products/shoes/laces"))
    site.html(f"{SITE}/products/bags", "Bags", "<p>Bags</p>")
    site.html(f"{SITE}/products/shoes/laces", "Laces", "<p>Laces</p>")
    return site


def test_breadth_first_order_and_depth_limit() -> None:
    site = small_site()
    report, _ = crawl(site, max_depth=2)
    assert page_urls(report) == [
        f"{SITE}/",
        f"{SITE}/about",
        f"{SITE}/products",
        f"{SITE}/contact",
        f"{SITE}/products/shoes",
        f"{SITE}/products/bags",
    ]
    assert [p.depth for p in report.pages] == [0, 1, 1, 1, 2, 2]
    assert f"{SITE}/products/shoes/laces" not in site.urls  # depth 3: never requested
    assert report.robots == "missing"  # robots.txt 404: no restrictions


def test_depth_zero_fetches_only_the_start_page_and_skips_sitemaps() -> None:
    site = small_site()
    report, _ = crawl(site, max_depth=0)
    assert page_urls(report) == [f"{SITE}/"]
    assert site.urls == [f"{SITE}/robots.txt", f"{SITE}/"]


def test_page_limit_counts_requests_and_reports_the_stop() -> None:
    site = small_site()
    report, _ = crawl(site, max_pages=3)
    assert page_urls(report) == [f"{SITE}/", f"{SITE}/about", f"{SITE}/products"]
    assert report.stopped_reason == "page_limit"
    assert report.counts["requested"] == 3


def test_external_links_are_never_requested() -> None:
    site = FakeSite()
    site.html(
        f"{SITE}/",
        "Home",
        links(
            "https://evil.example.net/",
            "http://shop.example.com/",  # other scheme
            "https://shop.example.com:8443/",  # other port
            "https://sub.shop.example.com/",
            "http://169.254.169.254/latest/meta-data/",
            "/inside",
        ),
    )
    site.html(f"{SITE}/inside", "Inside", "<p>in</p>")
    report, _ = crawl(site)
    assert set(site.urls) <= {
        f"{SITE}/robots.txt",
        f"{SITE}/sitemap.xml",
        f"{SITE}/",
        f"{SITE}/inside",
    }
    assert report.counts["external_links"] == 5


def test_duplicate_and_equivalent_links_are_fetched_once() -> None:
    site = FakeSite()
    site.html(
        f"{SITE}/",
        "Home",
        links(
            "/faq",
            "/faq/",
            "/faq#shipping",
            "/FAQ/../faq",
            "/faq?utm_source=mail",
            "faq",
            "https://SHOP.example.com:443/faq",
        ),
    )
    site.html(f"{SITE}/faq", "FAQ", "<p>answers</p>")
    report, _ = crawl(site)
    assert site.urls.count(f"{SITE}/faq") == 1
    assert report.counts["duplicate_links"] == 6


def test_robots_disallow_is_respected_for_links_sitemaps_and_redirects() -> None:
    site = small_site()
    site.add(
        f"{SITE}/robots.txt", "User-agent: *\nDisallow: /products\n", content_type="text/plain"
    )
    site.add(
        f"{SITE}/sitemap.xml",
        f"<urlset {NS}><url><loc>{SITE}/products/bags</loc></url></urlset>",
        content_type="application/xml",
    )
    site.add(f"{SITE}/contact", status=301, headers={"location": "/products/contact"})
    report, _ = crawl(site)
    requested = set(site.urls)
    assert not any(u.startswith(f"{SITE}/products") for u in requested)
    pages = by_url(report)
    assert pages[f"{SITE}/products"].reason == "robots"
    assert pages[f"{SITE}/products/bags"].reason == "robots"  # from the sitemap
    assert pages[f"{SITE}/contact"].reason == "robots"  # redirected into a disallowed path
    assert report.robots == "parsed"


def test_start_url_disallowed_by_robots_is_refused() -> None:
    site = small_site()
    site.add(
        f"{SITE}/robots.txt", "User-agent: KnowbaseBot\nDisallow: /\n", content_type="text/plain"
    )
    with pytest.raises(CrawlBlocked, match="robots.txt"):
        crawl(site)
    assert site.urls == [f"{SITE}/robots.txt"]


@pytest.mark.parametrize("status", [500, 502, 503, 429])
def test_unreachable_robots_txt_means_nothing_is_crawled(status: int) -> None:
    site = small_site()
    site.add(f"{SITE}/robots.txt", "error", status=status)
    with pytest.raises(RobotsUnreachable, match="complete disallow"):
        crawl(site)
    assert site.urls == [f"{SITE}/robots.txt"]


@pytest.mark.parametrize("status", [401, 403, 404, 410])
def test_robots_txt_4xx_means_no_restrictions(status: int) -> None:
    site = small_site()
    site.add(f"{SITE}/robots.txt", "nope", status=status)
    report, _ = crawl(site)
    assert report.robots in ("missing", "unavailable")
    assert f"{SITE}/products" in site.urls


def test_robots_txt_redirecting_to_another_origin_blocks_the_crawl() -> None:
    site = small_site()
    site.add(
        f"{SITE}/robots.txt", status=301, headers={"location": "https://www.example.com/robots.txt"}
    )
    with pytest.raises(CrawlBlocked, match="redirects to"):
        crawl(site)


def test_sitemap_pages_join_at_depth_one_after_the_start_page() -> None:
    site = small_site()
    site.add(
        f"{SITE}/robots.txt",
        f"User-agent: *\nDisallow:\nSitemap: {SITE}/sitemap_index.xml\n"
        "Sitemap: https://cdn.example.net/sitemap.xml\n",
        content_type="text/plain",
    )
    site.add(
        f"{SITE}/sitemap_index.xml",
        f"<sitemapindex {NS}><sitemap><loc>{SITE}/pages.xml</loc></sitemap>"
        "<sitemap><loc>https://elsewhere.example.org/x.xml</loc></sitemap></sitemapindex>",
        content_type="application/xml",
    )
    site.add(
        f"{SITE}/pages.xml",
        f"<urlset {NS}><url><loc>{SITE}/products/shoes/laces</loc></url>"
        f"<url><loc>{SITE}/about</loc></url>"
        "<url><loc>https://elsewhere.example.org/p</loc></url></urlset>",
        content_type="text/xml",
    )
    report, _ = crawl(site)
    order = page_urls(report)
    assert order[:3] == [f"{SITE}/", f"{SITE}/products/shoes/laces", f"{SITE}/about"]
    assert by_url(report)[f"{SITE}/products/shoes/laces"].discovered_via == "sitemap"
    assert "https://elsewhere.example.org/x.xml" not in site.urls
    statuses = {s["url"]: s["status"] for s in report.sitemaps}
    assert statuses["https://cdn.example.net/sitemap.xml"] == "ignored_other_site"
    assert statuses[f"{SITE}/pages.xml"] == "parsed"


def test_malformed_or_missing_sitemaps_fall_back_to_links() -> None:
    site = small_site()
    site.add(f"{SITE}/sitemap.xml", "<urlset><url><loc>broken", content_type="application/xml")
    report, _ = crawl(site)
    assert report.sitemaps[0]["status"] == "invalid"
    assert f"{SITE}/products" in page_urls(report)
    site2 = small_site()
    report2, _ = crawl(site2)
    assert report2.sitemaps[0]["status"] == "missing"


def test_requests_to_the_host_are_spaced_by_the_delay() -> None:
    site = small_site()
    _, _clock = crawl(site, delay=1.0)
    times = [t for t, _ in site.requests]
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert gaps and min(gaps) >= 1.0


def test_a_longer_robots_crawl_delay_wins() -> None:
    site = small_site()
    site.add(f"{SITE}/robots.txt", "User-agent: *\nCrawl-delay: 4\n", content_type="text/plain")
    report, _ = crawl(site, delay=1.0)
    times = [t for t, _ in site.requests]
    assert min(b - a for a, b in zip(times, times[1:], strict=False)) >= 4.0
    assert report.crawl_delay == 4.0


def test_the_total_deadline_stops_the_crawl() -> None:
    site = small_site()
    site.request_seconds = 2.0
    report, clock = crawl(site, delay=1.0, duration=8.0)
    assert report.stopped_reason == "deadline"
    assert clock.now - 1000.0 <= 8.0 + 10.0  # never starts a request past the deadline
    assert len(page_urls(report)) < 6


def test_failed_pages_are_reported_and_crawling_continues() -> None:
    site = small_site()
    site.add(f"{SITE}/about", "boom", status=500)
    site.add(f"{SITE}/contact", b"%PDF-1.4", content_type="application/pdf")
    site.add(f"{SITE}/products/bags", "<p>" + "x" * 2000 + "</p>")
    del site.pages[f"{SITE}/products/shoes"]  # 404
    report, _ = crawl(site, max_bytes=1000)
    pages = by_url(report)
    assert pages[f"{SITE}/about"].reason == "server_error"
    assert pages[f"{SITE}/contact"].reason == "not_html"
    assert pages[f"{SITE}/products/bags"].reason == "too_large"
    assert pages[f"{SITE}/products/shoes"].reason == "not_found"
    assert pages[f"{SITE}/products"].status == "fetched"


def test_a_429_stops_the_crawl_politely() -> None:
    site = small_site()
    site.add(f"{SITE}/about", "slow down", status=429, headers={"retry-after": "60"})
    report, _ = crawl(site)
    assert report.stopped_reason == "rate_limited"
    assert site.urls[-1] == f"{SITE}/about"


def test_sign_in_pages_and_files_are_never_requested() -> None:
    site = FakeSite()
    site.html(
        f"{SITE}/",
        "Home",
        links("/login", "/account/orders", "/manual.pdf", "/s?a=1&b=2&c=3&d=4", "/ok"),
    )
    site.html(f"{SITE}/ok", "OK", "<p>fine</p>")
    report, _ = crawl(site)
    reasons = {p.url: p.reason for p in report.pages if p.status == "skipped"}
    assert reasons == {
        f"{SITE}/login": "auth_page",
        f"{SITE}/account/orders": "auth_page",
        f"{SITE}/manual.pdf": "not_html",
        f"{SITE}/s?a=1&b=2&c=3&d=4": "query_heavy",
    }
    assert not any(u in site.urls for u in reasons)


def test_a_sign_in_start_url_is_refused() -> None:
    with pytest.raises(CrawlBlocked):
        crawl(small_site(), start=f"{SITE}/login")


def test_noindex_pages_are_skipped_but_their_links_followed() -> None:
    site = FakeSite()
    site.html(f"{SITE}/", "Home", '<meta name="robots" content="noindex">' + links("/a"))
    site.html(f"{SITE}/a", "A", "<p>a</p>", headers={"x-robots-tag": "knowbasebot: nofollow"})
    site.html(f"{SITE}/b", "B", "<p>b</p>")
    report, _ = crawl(site)
    pages = by_url(report)
    assert pages[f"{SITE}/"].status == "skipped" and pages[f"{SITE}/"].reason == "noindex"
    assert pages[f"{SITE}/a"].status == "fetched"


def test_x_robots_tag_for_other_bots_is_ignored() -> None:
    site = FakeSite()
    site.html(f"{SITE}/", "Home", "<p>x</p>", headers={"x-robots-tag": "googlebot: noindex"})
    report, _ = crawl(site)
    assert report.pages[0].status == "fetched"


def test_redirect_to_an_already_seen_page_is_a_duplicate() -> None:
    site = FakeSite()
    site.html(f"{SITE}/", "Home", links("/a", "/old-a"))
    site.html(f"{SITE}/a", "A", "<p>a</p>")
    site.add(f"{SITE}/old-a", status=301, headers={"location": "/a"})
    report, _ = crawl(site)
    assert site.urls.count(f"{SITE}/a") == 1
    assert by_url(report)[f"{SITE}/old-a"].reason == "duplicate"


def test_canonical_urls_are_recorded_and_not_fetched_again() -> None:
    site = FakeSite()
    site.html(f"{SITE}/", "Home", links("/p?id=1&ref=nav"))
    site.html(
        f"{SITE}/p?id=1&ref=nav",
        "P",
        f'<link rel="canonical" href="{SITE}/p?id=1"><p>product</p>' + links("/p?id=1"),
    )
    report, _ = crawl(site)
    assert by_url(report)[f"{SITE}/p?id=1&ref=nav"].canonical_url == f"{SITE}/p?id=1"
    assert f"{SITE}/p?id=1" not in site.urls


def test_a_cross_site_canonical_is_ignored() -> None:
    site = FakeSite()
    site.html(f"{SITE}/", "Home", '<link rel="canonical" href="https://evil.example.net/"><p>x</p>')
    report, _ = crawl(site)
    assert report.pages[0].canonical_url is None


def test_a_host_that_turns_private_mid_crawl_stops_it() -> None:
    site = small_site()
    calls = {"n": 0}

    def resolver(_host: str, _port: int) -> list[str]:
        calls["n"] += 1
        return ["93.184.216.34"] if calls["n"] <= 3 else ["10.0.0.1"]

    clock = FakeClock()
    site.clock = clock
    crawler = Crawler(
        fetcher=make_fetcher(site, ORIGIN, clock=clock, resolver=resolver),
        limits=CrawlLimits(20, 2, 1.0, 10, 300, 5 * 1024 * 1024),
        product_token="KnowbaseBot",
        clock=clock,
        sleep=clock.sleep,
    )
    report = crawler.crawl(SITE + "/")
    assert report.stopped_reason == "blocked_destination"
    assert report.pages[-1].reason == "blocked_destination"


def test_repeated_blocks_are_site_boilerplate() -> None:
    footer = Block("paragraph", "Free shipping over $50. Join our newsletter!")
    pages = [[Block("paragraph", f"Unique page text {i}"), footer] for i in range(4)]
    assert repeated_blocks(pages) == {footer.text}
    assert repeated_blocks(pages[:2]) == set()  # too few pages to tell


def test_sitemap_redirects_obey_robots_and_skip_rules() -> None:
    site = small_site()
    site.add(
        f"{SITE}/robots.txt",
        f"User-agent: *\nDisallow: /private/\nSitemap: {SITE}/a.xml\nSitemap: {SITE}/b.xml\n",
        content_type="text/plain",
    )
    site.add(f"{SITE}/sitemap.xml", status=301, headers={"location": "/private/sitemap.xml"})
    site.add(f"{SITE}/a.xml", status=302, headers={"location": "/login/sitemap.xml"})
    site.add(f"{SITE}/b.xml", status=302, headers={"location": "/maps/pages.xml"})
    site.add(
        f"{SITE}/maps/pages.xml",
        f"<urlset {NS}><url><loc>{SITE}/about</loc></url></urlset>",
        content_type="application/xml",
    )
    site.add(f"{SITE}/private/sitemap.xml", "<urlset/>", content_type="application/xml")
    site.add(f"{SITE}/login/sitemap.xml", "<urlset/>", content_type="application/xml")
    report, _ = crawl(site)

    assert f"{SITE}/private/sitemap.xml" not in site.urls  # disallowed target never requested
    assert f"{SITE}/login/sitemap.xml" not in site.urls
    assert f"{SITE}/maps/pages.xml" in site.urls  # an allowed .xml target is followed
    failed = {s["url"]: s["message"] for s in report.sitemaps if s["status"] == "failed"}
    assert "robots.txt" in failed[f"{SITE}/sitemap.xml"]
    assert f"{SITE}/a.xml" in failed
    assert by_url(report)[f"{SITE}/about"].discovered_via == "sitemap"


def test_sitemap_redirects_cannot_leave_the_site() -> None:
    site = small_site()
    site.add(
        f"{SITE}/sitemap.xml", status=301, headers={"location": "http://169.254.169.254/x.xml"}
    )
    report, _ = crawl(site)
    assert report.sitemaps[0]["status"] == "failed"
    assert not any("169.254" in url for url in site.urls)
