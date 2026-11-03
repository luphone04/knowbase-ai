"""One bounded, same-origin HTTP GET with explicit redirect handling.

- **Every URL, every hop:** the host is checked and resolved (public
  addresses only) before the request. The connection itself is checked again
  in app.crawler.network, so DNS rebinding between the two can't succeed.
- **Redirects** are never followed automatically. Each `Location` is
  normalised, must stay on the crawl's origin (scheme, host, port), and is
  offered to `allow_redirect` (robots.txt, already-crawled). At most 5 hops.
- **Limits:** a declared `Content-Length` over the limit is refused before
  reading; otherwise bytes are counted while streaming, *after*
  decompression, so a small gzip bomb can't expand past the limit in memory.
  Only gzip/deflate/identity encodings are accepted. The whole response must
  arrive within the request timeout (a slow drip of bytes doesn't reset it).
- **Content types** are checked from the header before the body is read.
"""

import time
import zlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import httpx

from app.crawler.network import (
    AddressPolicy,
    BlockedDestination,
    DnsFailure,
    Resolver,
    is_public_address,
    resolve_public,
    system_resolver,
)
from app.crawler.urls import InvalidUrl, Origin, normalize_url, origin_of

MAX_REDIRECTS = 5
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
HTML_TYPES = ("text/html", "application/xhtml+xml")
XML_TYPES = ("application/xml", "text/xml", "application/rss+xml", "application/atom+xml")


class FetchError(Exception):
    """A request that produced no usable response. `reason` is a stable code."""

    def __init__(self, reason: str, message: str, *, final_url: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.final_url = final_url


@dataclass(frozen=True)
class FetchResponse:
    url: str
    final_url: str
    status: int
    content_type: str | None  # media type only, lower-case
    charset: str | None
    body: bytes  # empty unless 2xx and an accepted type
    headers: dict[str, str] = field(default_factory=dict)
    redirects: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Redirect:
    location: str  # normalised, absolute


# (reason, message) to refuse a redirect target, or None to allow it.
RedirectCheck = Callable[[str], tuple[str, str] | None]
# Called right before every request (including redirect hops), e.g. to throttle.
BeforeRequest = Callable[[str], None]


def _media_type(header: str | None) -> tuple[str | None, str | None]:
    if not header:
        return None, None
    media, _, params = header.partition(";")
    charset = None
    for param in params.split(";"):
        name, _, value = param.strip().partition("=")
        if name.lower() == "charset" and value:
            charset = value.strip().strip('"').strip("'") or None
    return media.strip().lower() or None, charset


class _Decoder:
    """Content-Encoding decoding with an output cap enforced per step."""

    def __init__(self, encoding: str | None, limit: int, url: str) -> None:
        encodings = [e.strip().lower() for e in (encoding or "").split(",") if e.strip()]
        encodings = [e for e in encodings if e != "identity"]
        if len(encodings) > 1 or (encodings and encodings[0] not in ("gzip", "x-gzip", "deflate")):
            raise FetchError(
                "unsupported_encoding", f"Unsupported content encoding: {encoding}.", final_url=url
            )
        # wbits 47 = auto-detect a gzip or zlib header.
        self._zlib = zlib.decompressobj(47) if encodings else None
        self._limit = limit
        self._url = url
        self.size = 0

    def _check(self, size: int) -> None:
        if size > self._limit:
            raise FetchError(
                "too_large",
                f"The response is larger than the {self._limit // (1024 * 1024) or 1} MB limit.",
                final_url=self._url,
            )

    def feed(self, chunk: bytes) -> bytes:
        if self._zlib is None:
            self.size += len(chunk)
            self._check(self.size)
            return chunk
        out = bytearray()
        data = chunk
        try:
            while data:
                piece = self._zlib.decompress(data, self._limit + 1 - self.size)
                self.size += len(piece)
                self._check(self.size)
                out += piece
                data = self._zlib.unconsumed_tail
        except zlib.error as exc:
            raise FetchError("invalid_encoding", "The response could not be decompressed.") from exc
        return bytes(out)

    def finish(self) -> bytes:
        if self._zlib is None:
            return b""
        try:
            tail = self._zlib.flush()
        except zlib.error as exc:
            raise FetchError("invalid_encoding", "The response could not be decompressed.") from exc
        self.size += len(tail)
        self._check(self.size)
        return tail


class SafeFetcher:
    """GETs on one origin, through the guarded client only."""

    def __init__(
        self,
        *,
        origin: Origin,
        client: httpx.Client,
        user_agent: str,
        timeout: float,
        resolver: Resolver = system_resolver,
        policy: AddressPolicy = is_public_address,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.origin = origin
        self._client = client
        self._user_agent = user_agent
        self._timeout = timeout
        self._resolver = resolver
        self._policy = policy
        self._clock = clock

    def close(self) -> None:
        self._client.close()

    def get(
        self,
        url: str,
        *,
        accept: tuple[str, ...] | None,
        max_bytes: int,
        allow_redirect: RedirectCheck = lambda _url: None,
        before_request: BeforeRequest = lambda _url: None,
        timeout: float | None = None,
    ) -> FetchResponse:
        """Fetch `url`. `accept`: media types whose body is read (None: any type)."""
        timeout = min(self._timeout, timeout) if timeout is not None else self._timeout
        current = url
        redirects: list[str] = []
        if origin_of(url) != self.origin:
            raise FetchError("cross_origin", "The URL is not on the crawled site.", final_url=url)
        for _hop in range(MAX_REDIRECTS + 1):
            self._preflight(current)
            before_request(current)
            response = self._request(current, accept=accept, max_bytes=max_bytes, timeout=timeout)
            if isinstance(response, _Redirect):
                target = response.location
                if origin_of(target) != self.origin:
                    raise FetchError(
                        "cross_origin_redirect",
                        f"Redirected to a different site ({target}), which is not crawled.",
                        final_url=target,
                    )
                refusal = allow_redirect(target)
                if refusal is not None:
                    raise FetchError(refusal[0], refusal[1], final_url=target)
                redirects.append(target)
                current = target
                continue
            return FetchResponse(
                url=url,
                final_url=current,
                status=response.status,
                content_type=response.content_type,
                charset=response.charset,
                body=response.body,
                headers=response.headers,
                redirects=tuple(redirects),
            )
        raise FetchError("too_many_redirects", f"More than {MAX_REDIRECTS} redirects.")

    def _preflight(self, url: str) -> None:
        """Clear, early errors for blocked or unresolvable hosts (the connection re-checks)."""
        try:
            resolve_public(
                self.origin.host, self.origin.port, resolver=self._resolver, policy=self._policy
            )
        except BlockedDestination as exc:
            raise FetchError("blocked_destination", exc.message, final_url=url) from exc
        except DnsFailure as exc:
            raise FetchError("dns_failure", exc.message, final_url=url) from exc

    def _request(
        self, url: str, *, accept: tuple[str, ...] | None, max_bytes: int, timeout: float
    ) -> "FetchResponse | _Redirect":
        headers = {
            "User-Agent": self._user_agent,
            "Accept": ", ".join(accept) + ", */*;q=0.1" if accept else "*/*",
            "Accept-Encoding": "gzip, deflate",
        }
        started = self._clock()
        try:
            with self._client.stream("GET", url, headers=headers, timeout=timeout) as response:
                if response.status_code in REDIRECT_STATUSES and "location" in response.headers:
                    try:
                        return _Redirect(normalize_url(response.headers["location"], base=url))
                    except InvalidUrl as exc:
                        raise FetchError(
                            "invalid_redirect", "The site redirected to an invalid URL."
                        ) from exc
                media, charset = _media_type(response.headers.get("content-type"))
                kept = {
                    name: response.headers[name]
                    for name in ("x-robots-tag", "retry-after")
                    if name in response.headers
                }
                body = b""
                ok = 200 <= response.status_code < 300
                if ok and accept is not None and media not in accept:
                    raise FetchError(
                        "not_html" if accept == HTML_TYPES else "unsupported_content_type",
                        f"The response is {media or 'of an unknown type'}, "
                        f"not {' or '.join(accept)}.",
                        final_url=url,
                    )
                if ok:
                    declared = response.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        raise FetchError(
                            "too_large",
                            f"The response is larger than the "
                            f"{max_bytes // (1024 * 1024) or 1} MB limit.",
                            final_url=url,
                        )
                    body = self._read(
                        response, max_bytes=max_bytes, started=started, timeout=timeout, url=url
                    )
                return FetchResponse(
                    url=url,
                    final_url=url,
                    status=response.status_code,
                    content_type=media,
                    charset=charset,
                    body=body,
                    headers=kept,
                )
        except BlockedDestination as exc:  # raised by the guarded connection itself
            raise FetchError("blocked_destination", exc.message, final_url=url) from exc
        except DnsFailure as exc:
            raise FetchError("dns_failure", exc.message, final_url=url) from exc
        except httpx.TimeoutException as exc:
            raise FetchError(
                "timeout", f"The request timed out after {timeout:g} s.", final_url=url
            ) from exc
        except httpx.TransportError as exc:
            raise FetchError(
                "network_error", "The site could not be reached.", final_url=url
            ) from exc

    def _read(
        self,
        response: httpx.Response,
        *,
        max_bytes: int,
        started: float,
        timeout: float,
        url: str,
    ) -> bytes:
        decoder = _Decoder(response.headers.get("content-encoding"), max_bytes, url)
        chunks: list[bytes] = []
        raw: Iterator[bytes] = response.iter_raw()
        for chunk in raw:
            chunks.append(decoder.feed(chunk))
            if self._clock() - started > timeout:
                raise FetchError(
                    "timeout", f"The response took longer than {timeout:g} s.", final_url=url
                )
        chunks.append(decoder.finish())
        return b"".join(chunks)
