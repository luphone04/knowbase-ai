"""HTML extraction: main content, headings, boilerplate removal, links, metadata."""

import pytest

from app.crawler.extract import extract_page
from app.ingestion.parsers import Block, UnsupportedDocument

URL = "https://shop.example.com/policies/returns"

PAGE = """<!doctype html>
<html lang="en">
<head>
  <title> Returns policy | Northstar Outfitters </title>
  <meta name="description" content="How returns work.">
  <link rel="canonical" href="/returns">
  <style>body { color: red }</style>
  <script>var tracking = "do not index me";</script>
</head>
<body>
  <header><a href="/">Northstar</a> <nav><a href="/shipping">Shipping</a></nav></header>
  <div class="cookie-banner">We use cookies. <button>Accept</button></div>
  <nav aria-label="breadcrumbs"><a href="/policies">Policies</a></nav>
  <main>
    <h1>Returns policy</h1>
    <p>Unused items can be returned within <strong>30 days</strong> of delivery.</p>
    <h2>Laptops</h2>
    <p>Laptops can be returned within 14 days
       if the seal is intact.</p>
    <ul><li>Keep the receipt.</li><li>Use the <a href="/rma">RMA form</a>.</li></ul>
    <table><tr><th>Item</th><th>Window</th></tr><tr><td>Laptop</td><td>14 days</td></tr></table>
    <form action="/subscribe"><input name="email"><p>Subscribe to our newsletter</p></form>
    <div hidden>Hidden promo text</div>
    <noscript>Enable JavaScript</noscript>
  </main>
  <aside>Related: <a href="https://partner.example.net/deal">partner deal</a></aside>
  <footer>© Northstar Outfitters. <a href="/privacy">Privacy</a></footer>
</body></html>"""


def texts(blocks: list[Block]) -> list[str]:
    return [block.text for block in blocks]


def test_main_content_headings_and_paragraphs() -> None:
    page = extract_page(PAGE.encode(), url=URL)
    assert page.title == "Returns policy | Northstar Outfitters"
    assert page.blocks[0] == Block("heading", "Returns policy", level=1)
    assert Block("heading", "Laptops", level=2) in page.blocks
    joined = "\n".join(texts(page.blocks))
    assert "Unused items can be returned within 30 days of delivery." in joined
    assert "Laptops can be returned within 14 days if the seal is intact." in joined
    assert "- Keep the receipt." in joined and "- Use the RMA form." in joined
    assert "Item | Window\nLaptop | 14 days" in joined
    assert page.metadata == {"language": "en", "description": "How returns work."}


@pytest.mark.parametrize(
    "boilerplate",
    [
        "do not index me",  # script
        "color: red",  # style
        "Shipping",  # nav
        "We use cookies",  # cookie banner
        "Subscribe to our newsletter",  # form
        "Hidden promo text",  # hidden
        "Enable JavaScript",  # noscript
        "partner deal",  # aside
        "Privacy",  # footer
        "Northstar Outfitters.",
    ],
)
def test_boilerplate_is_removed(boilerplate: str) -> None:
    page = extract_page(PAGE.encode(), url=URL)
    assert boilerplate not in "\n".join(texts(page.blocks))


def test_links_are_collected_from_the_whole_page_before_cleaning() -> None:
    page = extract_page(PAGE.encode(), url=URL)
    assert "https://shop.example.com/shipping" in page.links  # from the nav
    assert "https://shop.example.com/privacy" in page.links  # from the footer
    assert "https://shop.example.com/rma" in page.links
    assert "https://partner.example.net/deal" in page.links  # scoped later by the crawler


def test_canonical_url_is_resolved() -> None:
    assert extract_page(PAGE.encode(), url=URL).canonical_url == "https://shop.example.com/returns"


def test_article_is_used_when_there_is_no_main() -> None:
    html = b"""<html><body><div class="sidebar">Sidebar text everywhere</div>
    <article><header><h1>Warranty</h1><p class="byline">By support</p></header>
    <p>Two years on all tents.</p></article></body></html>"""
    page = extract_page(html, url=URL)
    assert texts(page.blocks)[0] == "Warranty"  # the article's own header heading is kept
    assert "Two years on all tents." in texts(page.blocks)
    assert "Sidebar text everywhere" not in texts(page.blocks)


def test_body_is_used_without_main_or_article_and_site_header_goes() -> None:
    html = b"""<html><body><header><h1>Site name</h1></header>
    <div>Text directly in a div, with <em>inline</em> markup.</div>
    <section><h3>FAQ</h3><div><div>Nested answer.</div></div></section></body></html>"""
    page = extract_page(html, url=URL)
    assert texts(page.blocks) == [
        "Text directly in a div, with inline markup.",
        "FAQ",
        "Nested answer.",
    ]


def test_title_falls_back_to_og_title_then_first_heading() -> None:
    og = b'<html><head><meta property="og:title" content="OG title"></head><body><p>x</p></body>'
    assert extract_page(og, url=URL).title == "OG title"
    heading = b"<html><body><main><h2>Shipping</h2><p>Ships in 2 days.</p></main></body></html>"
    assert extract_page(heading, url=URL).title == "Shipping"


def test_robots_meta_tags_and_nofollow_links() -> None:
    html = b"""<html><head><meta name="robots" content="noindex, nofollow"></head>
    <body><p>secret</p><a href="/a" rel="nofollow">a</a></body></html>"""
    page = extract_page(html, url=URL)
    assert page.noindex and page.nofollow
    assert page.links == []
    assert extract_page(b'<meta name="robots" content="none"><p>x</p>', url=URL).noindex


def test_non_http_links_and_fragments_are_ignored() -> None:
    html = b"""<body><a href="mailto:a@b.c">m</a><a href="javascript:void(0)">j</a>
    <a href="#top">t</a><a href="tel:123">t</a><a href="/ok">ok</a></body>"""
    assert extract_page(html, url=URL).links == ["https://shop.example.com/ok"]


def test_base_href_is_honoured_for_links() -> None:
    html = b'<head><base href="https://shop.example.com/docs/"></head><body><a href="faq">f</a>'
    assert extract_page(html, url=URL).links == ["https://shop.example.com/docs/faq"]


def test_charset_from_the_header_is_used() -> None:
    page = extract_page("<p>Café crème</p>".encode("latin-1"), url=URL, charset="latin-1")
    assert texts(page.blocks) == ["Café crème"]


def test_scripts_are_never_executed_or_kept() -> None:
    html = b"<body><p>Real text</p><script>document.write('<p>Injected</p>')</script></body>"
    assert texts(extract_page(html, url=URL).blocks) == ["Real text"]


def test_an_empty_page_has_no_blocks() -> None:
    assert extract_page(b"<html><body><nav>menu</nav></body></html>", url=URL).blocks == []


def test_pathologically_deep_html_is_handled() -> None:
    html = ("<div>" * 5000 + "deep" + "</div>" * 5000).encode()
    try:
        page = extract_page(html, url=URL)
    except UnsupportedDocument as exc:
        assert "nested too deeply" in exc.message
    else:
        assert "deep" in " ".join(texts(page.blocks))
