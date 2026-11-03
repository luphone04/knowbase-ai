"""URL normalisation, validation, deduplication keys, and skip rules."""

import pytest

from app.crawler.urls import (
    InvalidUrl,
    Origin,
    normalize_url,
    origin_of,
    skip_reason,
    visit_key,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://example.com", "http://example.com/"),
        ("https://example.com/", "https://example.com/"),
        ("HTTPS://Example.COM/About", "https://example.com/About"),  # path case kept
        ("https://example.com:443/a", "https://example.com/a"),
        ("http://example.com:80/a", "http://example.com/a"),
        ("https://example.com:8443/a", "https://example.com:8443/a"),
        ("https://example.com./a", "https://example.com/a"),
        ("https://example.com/a/./b/../c", "https://example.com/a/c"),
        ("https://example.com/../../a", "https://example.com/a"),
        ("https://example.com/a#section-2", "https://example.com/a"),
        ("https://example.com/%7Euser/%61bc", "https://example.com/~user/abc"),
        ("https://example.com/a%2fb", "https://example.com/a%2Fb"),  # %2F is not "/"
        ("https://example.com/a b", "https://example.com/a%20b"),
        ("https://example.com/café", "https://example.com/caf%C3%A9"),
        ("https://example.com/100%", "https://example.com/100%25"),
        ("https://bücher.example/", "https://xn--bcher-kva.example/"),
        ("https://example.com/?utm_source=x&utm_medium=y", "https://example.com/"),
        ("https://example.com/p?id=7&gclid=abc&fbclid=z", "https://example.com/p?id=7"),
        ("https://example.com/p?b=2&a=1", "https://example.com/p?a=1&b=2"),
        ("https://example.com/p?tag=x&tag=a", "https://example.com/p?tag=x&tag=a"),
        ("https://example.com/p?q=a+b", "https://example.com/p?q=a%20b"),
        ("https://example.com/p?flag", "https://example.com/p?flag"),
        ("https://[2001:db8::1]:8080/", "https://[2001:db8::1]:8080/"),
        ("http://0x7f.1/", "http://127.0.0.1/"),  # legacy IPv4 forms are made explicit
        ("http://2130706433/", "http://127.0.0.1/"),
    ],
)
def test_normalize_url(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_relative_links_resolve_against_the_page() -> None:
    page = "https://example.com/docs/guide/intro"
    assert normalize_url("setup", base=page) == "https://example.com/docs/guide/setup"
    assert normalize_url("../faq#top", base=page) == "https://example.com/docs/faq"
    assert normalize_url("/returns", base=page) == "https://example.com/returns"
    assert normalize_url("//cdn.example.net/x", base=page) == "https://cdn.example.net/x"
    assert normalize_url("?page=2", base=page) == "https://example.com/docs/guide/intro?page=2"


def test_equivalent_spellings_normalise_to_one_url() -> None:
    spellings = [
        "https://EXAMPLE.com/returns",
        "https://example.com:443/returns",
        "https://example.com/./returns#policy",
        "https://example.com/returns?utm_campaign=spring",
        "https://example.com/%72eturns",
    ]
    assert {normalize_url(s) for s in spellings} == {"https://example.com/returns"}


def test_different_pages_are_not_merged() -> None:
    # Path case, meaningful query values, and http vs https stay distinct.
    assert normalize_url("https://example.com/Returns") != normalize_url(
        "https://example.com/returns"
    )
    assert normalize_url("https://example.com/p?id=1") != normalize_url(
        "https://example.com/p?id=2"
    )
    assert normalize_url("http://example.com/") != normalize_url("https://example.com/")


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://example.com/file",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "gopher://example.com/",
        "data:text/html,hi",
        "mailto:a@example.com",
        "example.com/no-scheme",
        "https://user:secret@example.com/",
        "https://user@example.com/",
        "https://@example.com/",
        "https:///path-only",
        "https://exa mple.com/",
        "https://-bad-.example.com/",
        "https://under_score.example.com/",
        "https://example.com:99999/",
        "https://example.com:abc/",
        "",
        "   ",
        "https://example.com/" + "a" * 2100,
    ],
)
def test_invalid_urls_are_rejected(raw: str) -> None:
    with pytest.raises(InvalidUrl):
        normalize_url(raw)


def test_credentials_get_a_clear_message() -> None:
    with pytest.raises(InvalidUrl, match="user name or password"):
        normalize_url("https://admin:hunter2@example.com/")


def test_origin_is_scheme_host_and_port() -> None:
    assert origin_of("https://example.com/a") == Origin("https", "example.com", 443)
    assert origin_of("http://example.com:8080/") == Origin("http", "example.com", 8080)
    assert origin_of("https://example.com/") != origin_of("http://example.com/")
    assert origin_of("https://example.com/") != origin_of("https://www.example.com/")
    assert origin_of("https://example.com/") != origin_of("https://example.com:8443/")
    assert str(Origin("https", "example.com", 443)) == "https://example.com"
    assert str(Origin("http", "example.com", 8080)) == "http://example.com:8080"


def test_visit_key_treats_trailing_slash_variants_as_one_page() -> None:
    assert visit_key("https://example.com/about/") == visit_key("https://example.com/about")
    assert visit_key("https://example.com/") == "https://example.com/"
    assert visit_key("https://example.com/a?x=1") != visit_key("https://example.com/a")


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://example.com/login", "auth_page"),
        ("https://example.com/account/orders", "auth_page"),
        ("https://example.com/wp-login.php", "auth_page"),
        ("https://example.com/checkout", "auth_page"),
        ("https://example.com/files/manual.pdf", "not_html"),
        ("https://example.com/img/logo.PNG", "not_html"),
        ("https://example.com/s?a=1&b=2&c=3&d=4", "query_heavy"),
    ],
)
def test_urls_that_are_never_requested(url: str, reason: str) -> None:
    result = skip_reason(url)
    assert result is not None and result[0] == reason


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/",
        "https://example.com/blog/how-to-register-a-domain",
        "https://example.com/docs/v1.2/intro",
        "https://example.com/p?id=1&page=2",
        "https://example.com/page.html",
    ],
)
def test_ordinary_pages_are_not_skipped(url: str) -> None:
    assert skip_reason(url) is None
