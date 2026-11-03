"""sitemap.xml parsing: urlsets, indexes, and hostile or broken XML."""

import pytest

from app.crawler.sitemap import SitemapError, parse_sitemap

NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'


def test_urlset_locations_are_read_in_order() -> None:
    body = f"""<?xml version="1.0" encoding="UTF-8"?>
    <urlset {NS}>
      <url><loc> https://shop.example.com/ </loc><lastmod>2026-10-01</lastmod></url>
      <url><loc>https://shop.example.com/returns</loc></url>
      <url><priority>0.5</priority></url>
    </urlset>""".encode()
    sitemap = parse_sitemap(body)
    assert sitemap.kind == "urlset"
    assert sitemap.locations == ["https://shop.example.com/", "https://shop.example.com/returns"]


def test_sitemap_index_lists_child_sitemaps() -> None:
    body = f"""<sitemapindex {NS}>
      <sitemap><loc>https://shop.example.com/sitemap-pages.xml</loc></sitemap>
      <sitemap><loc>https://shop.example.com/sitemap-blog.xml</loc></sitemap>
    </sitemapindex>""".encode()
    sitemap = parse_sitemap(body)
    assert sitemap.kind == "index"
    assert len(sitemap.locations) == 2


def test_namespace_free_sitemaps_work_too() -> None:
    body = b"<urlset><url><loc>https://shop.example.com/a</loc></url></urlset>"
    assert parse_sitemap(body).locations == ["https://shop.example.com/a"]


def test_the_number_of_urls_is_capped() -> None:
    entries = "".join(f"<url><loc>https://shop.example.com/{i}</loc></url>" for i in range(50))
    assert len(parse_sitemap(f"<urlset>{entries}</urlset>".encode(), max_urls=10).locations) == 10


@pytest.mark.parametrize(
    "body",
    [
        b"<urlset><url><loc>https://x</loc></url>",  # truncated
        b"not xml at all",
        b"",
        b"<html><body>Page not found</body></html>",  # soft 404
        b"<rss><channel/></rss>",
    ],
)
def test_malformed_or_foreign_documents_are_rejected(body: bytes) -> None:
    with pytest.raises(SitemapError):
        parse_sitemap(body)


def test_entity_expansion_is_refused_before_parsing() -> None:
    billion_laughs = b"""<?xml version="1.0"?>
    <!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;">]>
    <urlset><url><loc>&lol2;</loc></url></urlset>"""
    with pytest.raises(SitemapError, match="DTD"):
        parse_sitemap(billion_laughs)


def test_external_entities_are_refused() -> None:
    xxe = b"""<?xml version="1.0"?>
    <!DOCTYPE urlset [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
    <urlset><url><loc>&xxe;</loc></url></urlset>"""
    with pytest.raises(SitemapError):
        parse_sitemap(xxe)
