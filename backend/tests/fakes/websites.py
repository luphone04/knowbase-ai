"""A deterministic fake website for crawler tests: no network, no real DNS, no sleeping.

`FakeSite` serves canned responses through `httpx.MockTransport` and logs every
request. `FakeClock` stands in for time.monotonic/time.sleep, so delays and
deadlines are asserted exactly instead of waited for. The fake resolver maps
test host names to a public address (nothing ever connects to it).
"""

from collections.abc import Callable
from dataclasses import dataclass, field

import httpx

from app.crawler.fetcher import SafeFetcher
from app.crawler.urls import Origin

PUBLIC_IP = "93.184.216.34"
SITE = "https://shop.example.com"


@dataclass
class FakeClock:
    now: float = 1000.0
    sleeps: list[float] = field(default_factory=list)

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


@dataclass
class Reply:
    status: int = 200
    body: bytes | str = b""
    headers: dict[str, str] = field(default_factory=dict)
    content_type: str | None = "text/html; charset=utf-8"


@dataclass
class FakeSite:
    """URL -> Reply. Unknown URLs get 404. `requests` lists (time, url) in order."""

    pages: dict[str, Reply] = field(default_factory=dict)
    clock: FakeClock | None = None
    request_seconds: float = 0.0  # simulated latency per request
    requests: list[tuple[float, str]] = field(default_factory=list)
    seen_headers: list[httpx.Headers] = field(default_factory=list)

    def add(self, url: str, body: bytes | str = b"", **kwargs: object) -> None:
        self.pages[url] = Reply(body=body, **kwargs)  # type: ignore[arg-type]

    def html(self, url: str, title: str, body: str, **kwargs: object) -> None:
        self.add(
            url, f"<html><head><title>{title}</title></head><body>{body}</body></html>", **kwargs
        )

    @property
    def urls(self) -> list[str]:
        return [url for _, url in self.requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        now = self.clock() if self.clock else 0.0
        self.requests.append((now, str(request.url)))
        self.seen_headers.append(request.headers)
        if self.clock is not None and self.request_seconds:
            self.clock.now += self.request_seconds
        reply = self.pages.get(str(request.url))
        if reply is None:
            return httpx.Response(
                404, headers={"content-type": "text/plain"}, stream=httpx.ByteStream(b"not found")
            )
        headers = dict(reply.headers)
        if reply.content_type is not None:
            headers.setdefault("content-type", reply.content_type)
        body = reply.body.encode() if isinstance(reply.body, str) else reply.body
        # A stream, as from a real server: the fetcher reads raw (still encoded) bytes.
        return httpx.Response(reply.status, headers=headers, stream=httpx.ByteStream(body))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def public_resolver(_host: str, _port: int) -> list[str]:
    return [PUBLIC_IP]


def make_fetcher(
    site: FakeSite,
    origin: Origin,
    *,
    resolver: Callable[[str, int], list[str]] = public_resolver,
    clock: FakeClock | None = None,
    timeout: float = 10.0,
) -> SafeFetcher:
    return SafeFetcher(
        origin=origin,
        client=httpx.Client(transport=site.transport(), follow_redirects=False),
        user_agent="KnowbaseBot/0.1 (+test)",
        timeout=timeout,
        resolver=resolver,
        clock=clock or FakeClock(),
    )
