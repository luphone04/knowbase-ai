"""The bounded, polite, same-origin crawl (SPEC.md §7.3). Fetching only; no database.

    robots.txt ─ sitemaps (depth 1) ─ BFS from the start URL, level by level

- **robots.txt first** (RFC 9309 §2.3.1): 2xx is parsed; 4xx means no
  restrictions; 429, 5xx, a timeout, or a network error means *complete
  disallow*, so the crawl stops and the job retries later. A robots.txt
  that redirects to another origin is treated as unreachable too.
- **Sitemaps:** `Sitemap:` lines from robots.txt (same origin only) plus
  `/sitemap.xml`. Index files are followed, up to 5 sitemap files and 500
  URLs. Their pages join the BFS at depth 1, after the start page. They go
  through exactly the same checks as links (origin, robots, skip rules).
- **BFS:** a FIFO frontier, so every page at depth d is fetched before any at
  d + 1. The visited set uses normalised URLs (`/a` and `/a/` count once), and
  canonical URLs and redirect targets are marked as seen too. A page's links
  are queued only while its depth is below the limit.
- **Never requested:** other origins, robots-disallowed paths, sign-in,
  account, and checkout pages, non-HTML file extensions, query-heavy URLs.
  Forms are never submitted (only GET).
- **Bounds:** `max_pages` page requests, `max_depth`, a total deadline, a
  per-request timeout, and a byte limit. Requests to a host are at least
  `delay` seconds apart, or the robots.txt `Crawl-delay` if that's longer.
- **Stops early, keeping what it fetched:** on a 429 or a 503 with
  Retry-After (the site asked us to slow down), when a destination is blocked
  (the host now resolves to a private address), at the deadline, or at the
  page limit.
"""

import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.crawler.extract import ExtractedPage, extract_page, robots_directives
from app.crawler.fetcher import HTML_TYPES, XML_TYPES, FetchError, FetchResponse, SafeFetcher
from app.crawler.robots import MAX_ROBOTS_BYTES, RobotsRules, parse_robots
from app.crawler.sitemap import MAX_SITEMAP_URLS, SitemapError, parse_sitemap
from app.crawler.urls import (
    InvalidUrl,
    normalize_url,
    origin_of,
    path_and_query,
    skip_reason,
    visit_key,
)
from app.ingestion.parsers import UnsupportedDocument

logger = logging.getLogger(__name__)

MAX_SITEMAP_FILES = 5
MAX_RECORDED_SKIPS = 50  # skipped URLs kept for display; all are counted
ROBOTS_FETCH_LIMIT = 4 * MAX_ROBOTS_BYTES


class CrawlBlocked(Exception):
    """The crawl can't start for a reason retrying won't fix. `message` is user-safe."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class StartDisallowed(CrawlBlocked):
    """robots.txt now forbids the start URL. `rules` lets the caller apply them to
    pages indexed earlier (they're removed if disallowed too)."""

    def __init__(self, message: str, rules: RobotsRules) -> None:
        super().__init__(message)
        self.rules = rules


class RobotsUnreachable(Exception):
    """robots.txt couldn't be read (5xx, 429, network): RFC 9309 says assume disallow."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class CrawlLimits:
    max_pages: int
    max_depth: int
    delay_seconds: float
    timeout_seconds: float
    max_duration_seconds: float
    max_bytes: int


@dataclass
class PageResult:
    url: str
    depth: int
    discovered_via: str  # start | sitemap | link
    status: str  # fetched | skipped | failed
    reason: str | None = None
    message: str | None = None
    http_status: int | None = None
    final_url: str | None = None
    content_type: str | None = None
    canonical_url: str | None = None  # same-origin and normalised, else None
    fetched_at: datetime | None = None
    page: ExtractedPage | None = None


@dataclass
class CrawlReport:
    start_url: str
    robots: str  # parsed | missing | unavailable
    crawl_delay: float
    pages: list[PageResult] = field(default_factory=list)
    sitemaps: list[dict[str, Any]] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    stopped_reason: str | None = None
    duration_ms: int = 0
    # The robots.txt rules this crawl obeyed (also applied to pages it didn't reach).
    rules: RobotsRules = field(default_factory=RobotsRules.allow_everything)


class HostThrottle:
    """At least `delay` seconds between the starts of two requests to one host."""

    def __init__(
        self, delay: float, clock: Callable[[], float], sleep: Callable[[float], None]
    ) -> None:
        self.delay = delay
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}

    def next_allowed(self, host: str) -> float:
        last = self._last.get(host)
        return self._clock() if last is None else max(self._clock(), last + self.delay)

    def wait(self, host: str) -> None:
        pause = self.next_allowed(host) - self._clock()
        if pause > 0:
            self._sleep(pause)
        self._last[host] = self._clock()


def _x_robots(value: str, token: str) -> set[str]:
    """X-Robots-Tag directives for us: unprefixed ones, or ones naming our token."""
    directives: set[str] = set()
    for part in value.split(","):
        name, sep, rest = part.partition(":")
        if sep and name.strip().lower() == token.lower():
            directives |= robots_directives(rest)
        elif not sep:
            directives |= robots_directives(part)
    return directives


class Crawler:
    def __init__(
        self,
        *,
        fetcher: SafeFetcher,
        limits: CrawlLimits,
        product_token: str,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        on_progress: Callable[[dict[str, Any]], None] = lambda _progress: None,
    ) -> None:
        self._fetcher = fetcher
        self._origin = fetcher.origin
        self._limits = limits
        self._token = product_token
        self._clock = clock
        self._throttle = HostThrottle(limits.delay_seconds, clock, sleep)
        self._on_progress = on_progress
        self._deadline = 0.0
        self._robots = RobotsRules.allow_everything()
        self._frontier: deque[tuple[str, int, str]] = deque()
        self._seen: set[str] = set()
        self._report: CrawlReport | None = None
        self._counts = {
            "discovered": 0,
            "requested": 0,
            "fetched": 0,
            "failed": 0,
            "skipped": 0,
            "external_links": 0,
            "duplicate_links": 0,
        }

    # --- entry point ---------------------------------------------------------------------

    def crawl(self, start_url: str) -> CrawlReport:
        started = self._clock()
        self._deadline = started + self._limits.max_duration_seconds
        if origin_of(start_url) != self._origin:
            raise CrawlBlocked("The start URL is not on the crawled site.")
        start_skip = skip_reason(start_url)
        if start_skip is not None:
            raise CrawlBlocked(start_skip[1])

        robots_status = self._load_robots()
        if not self._robots.allows(path_and_query(start_url)):
            raise StartDisallowed(
                "robots.txt does not allow this crawler to fetch the start URL.", self._robots
            )
        delay = max(self._limits.delay_seconds, self._robots.crawl_delay or 0.0)
        self._throttle.delay = delay
        report = CrawlReport(
            start_url=start_url, robots=robots_status, crawl_delay=delay, rules=self._robots
        )
        self._report = report

        self._enqueue(start_url, 0, "start")
        if self._limits.max_depth >= 1:
            for url in self._sitemap_urls():
                self._enqueue(url, 1, "sitemap")

        while self._frontier:
            if self._counts["requested"] >= self._limits.max_pages:
                report.stopped_reason = "page_limit"
                break
            if self._throttle.next_allowed(self._origin.host) >= self._deadline:
                report.stopped_reason = "deadline"
                break
            url, depth, via = self._frontier.popleft()
            result = self._fetch_page(url, depth, via)
            report.pages.append(result)
            self._counts["fetched" if result.page is not None else "failed"] += 1
            if result.status == "skipped":  # fetched but noindex
                self._counts["skipped"] += 1
            page = result.page
            if page is not None and not page.nofollow and depth < self._limits.max_depth:
                for link in page.links:
                    self._enqueue(link, depth + 1, "link")
            self._on_progress({"phase": "fetching", **self._counts})
            if result.reason == "rate_limited":
                report.stopped_reason = "rate_limited"
                break
            if result.reason == "blocked_destination":
                report.stopped_reason = "blocked_destination"
                break

        report.counts = dict(self._counts)
        report.duration_ms = round((self._clock() - started) * 1000)
        logger.info(
            "Crawl finished origin=%s requested=%d fetched=%d failed=%d skipped=%d stopped=%s",
            self._origin,
            self._counts["requested"],
            self._counts["fetched"],
            self._counts["failed"],
            self._counts["skipped"],
            report.stopped_reason,
        )
        return report

    # --- discovery ------------------------------------------------------------------------

    def _record_skip(self, url: str, depth: int, via: str, reason: str, message: str) -> None:
        self._counts["skipped"] += 1
        report = self._report
        recorded = sum(1 for p in report.pages if p.status == "skipped") if report else 0
        if report is not None and recorded < MAX_RECORDED_SKIPS:
            report.pages.append(PageResult(url, depth, via, "skipped", reason, message))

    def _enqueue(self, raw: str, depth: int, via: str) -> None:
        try:
            url = normalize_url(raw)
        except InvalidUrl:
            return
        if origin_of(url) != self._origin:
            self._counts["external_links"] += 1
            return
        key = visit_key(url)
        if key in self._seen:
            self._counts["duplicate_links"] += 1
            return
        self._seen.add(key)
        self._counts["discovered"] += 1
        if not self._robots.allows(path_and_query(url)):
            self._record_skip(url, depth, via, "robots", "Disallowed by robots.txt.")
            return
        skip = skip_reason(url)
        if skip is not None:
            self._record_skip(url, depth, via, *skip)
            return
        self._frontier.append((url, depth, via))

    def _get(self, url: str, **kwargs: Any) -> FetchResponse:
        remaining = max(0.001, self._deadline - self._clock())
        return self._fetcher.get(
            url,
            before_request=lambda _target: self._throttle.wait(self._origin.host),
            timeout=remaining,
            **kwargs,
        )

    def _load_robots(self) -> str:
        url = f"{self._origin}/robots.txt"
        try:
            response = self._get(url, accept=None, max_bytes=ROBOTS_FETCH_LIMIT)
        except FetchError as exc:
            if exc.reason == "blocked_destination":
                raise CrawlBlocked(exc.message) from exc
            if exc.reason == "cross_origin_redirect":
                raise CrawlBlocked(
                    f"{exc.message} Add the site using the address it redirects to."
                ) from exc
            raise RobotsUnreachable(
                f"robots.txt could not be read ({exc.message}), so nothing is crawled "
                "(RFC 9309: an unreachable robots.txt means complete disallow)."
            ) from exc
        status = response.status
        if 200 <= status < 300:
            self._robots = parse_robots(response.body, self._token)
            return "parsed"
        if status == 429 or status >= 500:
            raise RobotsUnreachable(
                f"robots.txt returned HTTP {status}, so nothing is crawled "
                "(RFC 9309: an unreachable robots.txt means complete disallow)."
            )
        # 4xx: "unavailable", which RFC 9309 treats as no restrictions.
        self._robots = RobotsRules.allow_everything()
        return "missing" if status in (404, 410) else "unavailable"

    def _sitemap_urls(self) -> list[str]:
        assert self._report is not None
        sitemaps = self._report.sitemaps
        queue: deque[str] = deque()
        queued: set[str] = set()
        for raw in [*self._robots.sitemaps, f"{self._origin}/sitemap.xml"]:
            try:
                url = normalize_url(raw)
            except InvalidUrl:
                sitemaps.append({"url": raw[:300], "status": "invalid"})
                continue
            if origin_of(url) != self._origin:
                sitemaps.append({"url": url, "status": "ignored_other_site"})
                continue
            if url not in queued:
                queued.add(url)
                queue.append(url)

        pages: list[str] = []
        fetched = 0
        while queue and fetched < MAX_SITEMAP_FILES and len(pages) < MAX_SITEMAP_URLS:
            if self._throttle.next_allowed(self._origin.host) >= self._deadline:
                break
            url = queue.popleft()
            if not self._robots.allows(path_and_query(url)):
                sitemaps.append({"url": url, "status": "disallowed"})
                continue
            fetched += 1
            try:
                response = self._get(
                    url,
                    accept=XML_TYPES,
                    max_bytes=self._limits.max_bytes,
                    allow_redirect=self._sitemap_redirect_check,
                )
            except FetchError as exc:
                sitemaps.append({"url": url, "status": "failed", "message": exc.message})
                if exc.reason == "blocked_destination":
                    break
                continue
            if response.status != 200:
                sitemaps.append({"url": url, "status": "missing", "http_status": response.status})
                continue
            try:
                sitemap = parse_sitemap(response.body, max_urls=MAX_SITEMAP_URLS - len(pages))
            except SitemapError as exc:
                sitemaps.append({"url": url, "status": "invalid", "message": exc.message})
                continue
            sitemaps.append(
                {
                    "url": url,
                    "status": "parsed",
                    "kind": sitemap.kind,
                    "urls": len(sitemap.locations),
                }
            )
            if sitemap.kind == "urlset":
                pages.extend(sitemap.locations)
                continue
            for location in sitemap.locations:
                try:
                    child = normalize_url(location)
                except InvalidUrl:
                    continue
                if origin_of(child) == self._origin and child not in queued:
                    queued.add(child)
                    queue.append(child)
        return pages

    def _sitemap_redirect_check(self, target: str) -> tuple[str, str] | None:
        """A sitemap may redirect only to a URL we would fetch directly.

        The fetcher has already normalised the target, kept it on this origin,
        and will check its destination address; this adds robots.txt and the
        skip rules (except the file-extension rule: sitemaps are .xml files).
        """
        if not self._robots.allows(path_and_query(target)):
            return ("robots", "The sitemap redirects to a URL disallowed by robots.txt.")
        skip = skip_reason(target)
        if skip is not None and skip[0] != "not_html":
            return skip
        return None

    # --- one page -------------------------------------------------------------------------

    def _redirect_check(self, source: str, target: str) -> tuple[str, str] | None:
        target_key = visit_key(target)
        if target_key != visit_key(source) and target_key in self._seen:
            # Discovered separately (fetched already, or waiting in the frontier).
            return ("duplicate", f"Redirects to {target}, which is crawled separately.")
        if not self._robots.allows(path_and_query(target)):
            return ("robots", "Redirects to a page disallowed by robots.txt.")
        skip = skip_reason(target)
        if skip is not None:
            return skip
        return None

    def _fetch_page(self, url: str, depth: int, via: str) -> PageResult:
        self._counts["requested"] += 1
        fetched_at = datetime.now(UTC)
        try:
            response = self._get(
                url,
                accept=HTML_TYPES,
                max_bytes=self._limits.max_bytes,
                allow_redirect=lambda target: self._redirect_check(url, target),
            )
        except FetchError as exc:
            return PageResult(
                url,
                depth,
                via,
                "failed",
                exc.reason,
                exc.message,
                final_url=exc.final_url,
                fetched_at=fetched_at,
            )

        final = response.final_url
        if final != url:
            final_key = visit_key(final)
            if final_key in self._seen and visit_key(url) != final_key:
                # Another discovered URL already covers the redirect target.
                return PageResult(
                    url,
                    depth,
                    via,
                    "failed",
                    "duplicate",
                    f"Redirects to {final}, which is crawled separately.",
                    http_status=response.status,
                    final_url=final,
                    fetched_at=fetched_at,
                )
            self._seen.add(final_key)
        result = PageResult(
            url,
            depth,
            via,
            "failed",
            http_status=response.status,
            final_url=final,
            content_type=response.content_type,
            fetched_at=fetched_at,
        )
        status = response.status
        if not 200 <= status < 300:
            result.reason, result.message = self._http_failure(status, response)
            return result

        try:
            page = extract_page(response.body, url=final, charset=response.charset)
        except UnsupportedDocument as exc:
            result.reason, result.message = "invalid_html", exc.message
            return result
        header = _x_robots(response.headers.get("x-robots-tag", ""), self._token)
        page.noindex = page.noindex or "noindex" in header
        page.nofollow = page.nofollow or "nofollow" in header
        if page.canonical_url:
            try:
                canonical = normalize_url(page.canonical_url)
            except InvalidUrl:
                canonical = None
            if canonical is not None and origin_of(canonical) == self._origin:
                result.canonical_url = canonical
                self._seen.add(visit_key(canonical))
        result.page = page
        if page.noindex:
            result.status, result.reason = "skipped", "noindex"
            result.message = "The page asks not to be indexed (robots noindex)."
        else:
            result.status = "fetched"
        return result

    @staticmethod
    def _http_failure(status: int, response: FetchResponse) -> tuple[str, str]:
        if status in (404, 410):
            return "not_found", f"The page no longer exists (HTTP {status})."
        if status in (401, 403):
            return "access_denied", f"The page requires sign-in or is forbidden (HTTP {status})."
        if status == 429 or (status == 503 and "retry-after" in response.headers):
            return "rate_limited", f"The site asked the crawler to slow down (HTTP {status})."
        if status >= 500:
            return "server_error", f"The site returned a server error (HTTP {status})."
        if 300 <= status < 400:
            return "http_error", f"Unfollowable redirect (HTTP {status} without a location)."
        return "http_error", f"The site returned HTTP {status}."
