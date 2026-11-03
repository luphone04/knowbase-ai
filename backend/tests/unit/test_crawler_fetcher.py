"""SafeFetcher: explicit redirects, origin lock, size, encoding, type, and time limits."""

import gzip
import tracemalloc
import zlib
from collections.abc import Iterator

import httpx
import pytest

from app.crawler.fetcher import HTML_TYPES, FetchError
from app.crawler.urls import origin_of
from tests.fakes.websites import SITE, FakeClock, FakeSite, make_fetcher

ORIGIN = origin_of(SITE + "/")
MB = 1024 * 1024


def _get(site: FakeSite, url: str, **kwargs: object) -> object:
    fetcher = make_fetcher(site, ORIGIN, clock=kwargs.pop("clock", None))  # type: ignore[arg-type]
    return fetcher.get(url, accept=HTML_TYPES, max_bytes=kwargs.pop("max_bytes", MB), **kwargs)  # type: ignore[arg-type]


def test_same_origin_redirects_are_followed_and_checked_each_hop() -> None:
    site = FakeSite()
    site.add(f"{SITE}/old", status=301, headers={"location": "/older"})
    site.add(f"{SITE}/older", status=308, headers={"location": f"{SITE}/new#frag"})
    site.html(f"{SITE}/new", "New", "<p>hello</p>")
    hops: list[str] = []
    fetcher = make_fetcher(site, ORIGIN)
    response = fetcher.get(
        f"{SITE}/old", accept=HTML_TYPES, max_bytes=MB, before_request=hops.append
    )
    assert response.status == 200
    assert response.final_url == f"{SITE}/new"
    assert response.redirects == (f"{SITE}/older", f"{SITE}/new")
    assert hops == [f"{SITE}/old", f"{SITE}/older", f"{SITE}/new"]  # throttled every hop


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example.net/",  # another host
        "http://shop.example.com/",  # another scheme
        "https://shop.example.com:8443/",  # another port
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata
        "http://127.0.0.1:8000/api/v1/sources",  # local API
        "http://localhost/",
        "http://db:5432/",  # Docker service
    ],
)
def test_cross_origin_redirects_are_refused_without_a_request(location: str) -> None:
    site = FakeSite()
    site.add(f"{SITE}/go", status=302, headers={"location": location})
    with pytest.raises(FetchError) as excinfo:
        _get(site, f"{SITE}/go")
    assert excinfo.value.reason == "cross_origin_redirect"
    assert site.urls == [f"{SITE}/go"]  # the target was never requested


def test_redirect_to_a_host_now_resolving_privately_is_refused() -> None:
    """Public on the first hop, private on the next (DNS rebinding across a redirect)."""
    site = FakeSite()
    site.add(f"{SITE}/a", status=302, headers={"location": "/b"})
    site.html(f"{SITE}/b", "B", "<p>b</p>")
    answers = iter([["93.184.216.34"], ["10.0.0.8"]])
    fetcher = make_fetcher(site, ORIGIN, resolver=lambda h, p: next(answers))
    with pytest.raises(FetchError) as excinfo:
        fetcher.get(f"{SITE}/a", accept=HTML_TYPES, max_bytes=MB)
    assert excinfo.value.reason == "blocked_destination"
    assert site.urls == [f"{SITE}/a"]


def test_redirect_checks_can_refuse_a_target() -> None:
    site = FakeSite()
    site.add(f"{SITE}/a", status=302, headers={"location": "/private/x"})
    fetcher = make_fetcher(site, ORIGIN)
    with pytest.raises(FetchError) as excinfo:
        fetcher.get(
            f"{SITE}/a",
            accept=HTML_TYPES,
            max_bytes=MB,
            allow_redirect=lambda url: ("robots", "no") if "/private/" in url else None,
        )
    assert excinfo.value.reason == "robots"
    assert site.urls == [f"{SITE}/a"]


def test_redirect_loops_stop() -> None:
    site = FakeSite()
    site.add(f"{SITE}/a", status=302, headers={"location": "/b"})
    site.add(f"{SITE}/b", status=302, headers={"location": "/a"})
    with pytest.raises(FetchError) as excinfo:
        _get(site, f"{SITE}/a")
    assert excinfo.value.reason == "too_many_redirects"
    assert len(site.urls) == 6


def test_a_url_on_another_origin_is_refused_up_front() -> None:
    site = FakeSite()
    with pytest.raises(FetchError) as excinfo:
        _get(site, "https://other.example.org/")
    assert excinfo.value.reason == "cross_origin"
    assert site.urls == []


def test_non_html_responses_are_refused_before_reading() -> None:
    site = FakeSite()
    site.add(f"{SITE}/file", body=b"%PDF-1.7 ...", content_type="application/pdf")
    site.add(f"{SITE}/untyped", body=b"<p>x</p>", content_type=None)
    for path in ("/file", "/untyped"):
        with pytest.raises(FetchError) as excinfo:
            _get(site, SITE + path)
        assert excinfo.value.reason == "not_html"


def test_xhtml_is_accepted() -> None:
    site = FakeSite()
    site.add(f"{SITE}/x", body=b"<html/>", content_type="application/xhtml+xml")
    assert _get(site, f"{SITE}/x").status == 200  # type: ignore[attr-defined]


def test_declared_oversized_responses_are_refused() -> None:
    site = FakeSite()
    site.add(f"{SITE}/big", body=b"x" * 100, headers={"content-length": str(6 * MB)})
    with pytest.raises(FetchError) as excinfo:
        _get(site, f"{SITE}/big", max_bytes=5 * MB)
    assert excinfo.value.reason == "too_large"


def test_undeclared_oversized_responses_stop_while_streaming() -> None:
    def stream() -> Iterator[bytes]:
        for _ in range(100):
            yield b"<p>" + b"x" * 65536 + b"</p>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=stream())

    from app.crawler.fetcher import SafeFetcher
    from tests.fakes.websites import public_resolver

    fetcher = SafeFetcher(
        origin=ORIGIN,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        user_agent="t",
        timeout=10,
        resolver=public_resolver,
        clock=FakeClock(),
    )
    with pytest.raises(FetchError) as excinfo:
        fetcher.get(f"{SITE}/stream", accept=HTML_TYPES, max_bytes=MB)
    assert excinfo.value.reason == "too_large"


def test_a_gzip_bomb_cannot_expand_past_the_limit() -> None:
    bomb = gzip.compress(b"\0" * (50 * MB))  # about 50 KB on the wire
    assert len(bomb) < 100 * 1024
    site = FakeSite()
    site.add(f"{SITE}/bomb", body=bomb, headers={"content-encoding": "gzip"})
    tracemalloc.start()
    try:
        with pytest.raises(FetchError) as excinfo:
            _get(site, f"{SITE}/bomb", max_bytes=MB)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert excinfo.value.reason == "too_large"
    # Refused while decompressing: memory stays near the limit, never the 50 MB payload.
    assert peak < 8 * MB


def test_gzip_and_deflate_bodies_are_decoded() -> None:
    html = b"<html><body><p>compressed page</p></body></html>"
    site = FakeSite()
    site.add(f"{SITE}/gz", body=gzip.compress(html), headers={"content-encoding": "gzip"})
    site.add(f"{SITE}/df", body=zlib.compress(html), headers={"content-encoding": "deflate"})
    assert _get(site, f"{SITE}/gz").body == html  # type: ignore[attr-defined]
    assert _get(site, f"{SITE}/df").body == html  # type: ignore[attr-defined]


def test_unsupported_or_corrupt_encodings_are_refused() -> None:
    site = FakeSite()
    site.add(f"{SITE}/br", body=b"\x1b\x00", headers={"content-encoding": "br"})
    site.add(f"{SITE}/bad", body=b"not gzip at all", headers={"content-encoding": "gzip"})
    with pytest.raises(FetchError) as excinfo:
        _get(site, f"{SITE}/br")
    assert excinfo.value.reason == "unsupported_encoding"
    with pytest.raises(FetchError) as excinfo:
        _get(site, f"{SITE}/bad")
    assert excinfo.value.reason == "invalid_encoding"


def test_a_slow_drip_response_hits_the_request_deadline() -> None:
    clock = FakeClock()

    def drip() -> Iterator[bytes]:
        for _ in range(20):
            clock.now += 1.0  # one second per chunk
            yield b"<p>x</p>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=drip())

    from app.crawler.fetcher import SafeFetcher
    from tests.fakes.websites import public_resolver

    fetcher = SafeFetcher(
        origin=ORIGIN,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        user_agent="t",
        timeout=5,
        resolver=public_resolver,
        clock=clock,
    )
    with pytest.raises(FetchError) as excinfo:
        fetcher.get(f"{SITE}/slow", accept=HTML_TYPES, max_bytes=MB)
    assert excinfo.value.reason == "timeout"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectTimeout("slow"), "timeout"),
        (httpx.ConnectError("refused"), "network_error"),
        (httpx.RemoteProtocolError("garbage"), "network_error"),
    ],
)
def test_transport_errors_become_fetch_errors(error: Exception, reason: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    from app.crawler.fetcher import SafeFetcher
    from tests.fakes.websites import public_resolver

    fetcher = SafeFetcher(
        origin=ORIGIN,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        user_agent="t",
        timeout=5,
        resolver=public_resolver,
        clock=FakeClock(),
    )
    with pytest.raises(FetchError) as excinfo:
        fetcher.get(f"{SITE}/", accept=HTML_TYPES, max_bytes=MB)
    assert excinfo.value.reason == reason


def test_error_statuses_return_without_a_body() -> None:
    site = FakeSite()
    site.add(f"{SITE}/gone", body=b"<p>gone</p>", status=410)
    response = _get(site, f"{SITE}/gone")
    assert response.status == 410 and response.body == b""  # type: ignore[attr-defined]


def test_requests_identify_the_crawler() -> None:
    site = FakeSite()
    site.html(f"{SITE}/", "Home", "<p>hi</p>")
    _get(site, f"{SITE}/")
    headers = site.seen_headers[0]
    assert headers["user-agent"].startswith("KnowbaseBot/")
    assert headers["accept-encoding"] == "gzip, deflate"
    assert "cookie" not in headers and "authorization" not in headers
