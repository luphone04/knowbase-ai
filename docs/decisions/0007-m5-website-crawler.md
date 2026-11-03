# 0007 — Milestone 5 website crawler and incremental indexing

- Status: Accepted (within SPEC.md §2.5, §6, §7.3, §7.6, §14; no architecture change)
- Date: 3 November 2026

## Flow

```
POST /sources/websites ── validate URL + DNS (public only) ── source + job ── 202
                                                                │ (PostgreSQL row)
worker: claim ── robots.txt ── sitemaps ── BFS fetch (guarded client, 1 req/s/host)
   ── drop repeated blocks ── for each page: index_document (the M4 path)
          unchanged? ── yes ── keep chunks, record the check (0 embeddings)
                 └─ no ── chunk ── embed (cache → budget → Gemini) ── atomic swap
   ── one fenced transaction: page records, removals, source ready, job result
```

The API never fetches a page. Crawling happens only in the worker, which
reuses the M4 queue, fencing, heartbeat, retries, embedding cache, daily
budget, chunker, and publishing transaction. There is no second pipeline.

## SSRF prevention (the priority)

**Policy.** A destination must be a public address: `is_global`, and not
loopback, private, link-local (169.254.169.254 metadata), multicast,
reserved, unspecified, or site-local. IPv6 addresses that embed IPv4
(IPv4-mapped, 6to4, Teredo, NAT64) are judged by the embedded address too.
Host names that can only be local are refused before DNS: `localhost`,
`*.localhost`, `.local`, `.internal` (including `host.docker.internal`),
`.home.arpa`, and single-label names (Docker services such as `db` or `api`).
Legacy IPv4 spellings (`127.1`, `0x7f000001`, `2130706433`) are converted to
dotted form first, so they can't hide the real address.

**Three checkpoints:**
1. **Submission (API):** normalise the URL, refuse non-http(s) schemes and
   credentials, resolve the host, and require *every* DNS answer to be
   public. A clear 422 (`invalid_url`, `blocked_url`, `unresolvable_host`).
2. **Every request and redirect hop (fetcher):** the same resolve-and-check,
   for an early, readable error.
3. **The connection itself (the guarantee):** `GuardedTransport` is httpx's
   own transport with its httpcore connection pool on `GuardedNetworkBackend`.
   `connect_tcp` resolves the name itself, validates every answer, and opens
   the socket to *one of exactly those IP literals*. There is no second
   lookup between the check and the connection, so DNS rebinding has nothing
   to exploit. The URL keeps the host name, so the Host header, TLS SNI, and
   certificate verification still use the name.

Also: environment proxies are ignored (a proxy would connect somewhere
unchecked), Unix sockets are refused, and only GET is ever sent (no forms,
no credentials, no cookies). Tests prove all of this against a real HTTP
server on 127.0.0.1. Nothing reaches it, whether by IP, by a name resolving
to it, through a proxy setting, or through a redirect to the metadata address.

**Redirects** are never followed automatically. Each `Location` is
normalised and must keep the crawl's origin (scheme, host, port). The robots
rules and skip rules are applied to it, and it gets a fresh destination
check. At most 5 hops. A cross-origin redirect is reported (for example,
http to https: "add the site using the address it redirects to").

**Response limits:** 10 s per request, covering the whole response, so a
slow drip doesn't reset it. 5 MB of HTML, counted *after* decompression,
with output capped on each decompression step: a 50 KB gzip bomb is refused
with memory staying near the limit (tested). Only gzip, deflate, or identity
encodings. HTML content types only, checked before the body is read.

For any public deployment, add a domain allowlist and network-level egress
controls (SPEC.md §7.3). This is a local prototype, not an open URL fetcher:
crawling needs an account, is rate-limited per workspace
(`DAILY_CRAWL_REQUEST_LIMIT`, 20 per day), and the API never returns raw HTML.

## robots.txt (RFC 9309)

- Fetched first, with `CRAWL_USER_AGENT`; its first word (`KnowbaseBot`) is
  the product token. Rules are cached for the crawl.
- Our group wins over `*`; groups for the same agent merge. The longest
  matching pattern wins, `Allow` wins a tie, and `*` and `$` are supported.
  Paths are percent-normalised the same way as patterns. A longer
  `Crawl-delay` than ours is honoured, up to 60 s.
- **Unavailable vs unreachable:** 4xx (missing) means no restrictions;
  5xx, 429, a timeout, or a network error mean *complete disallow*, so
  nothing is crawled and the job retries with backoff. A robots.txt that
  redirects to another origin is treated the same way, as a permanent error.
- **Start URL newly disallowed:** nothing is crawled and the job fails
  permanently. Earlier pages the new rules forbid are removed (all of them for
  `Disallow: /`); pages still allowed keep their documents.
- The rules apply to links, sitemap URLs, sitemap files, and redirect
  targets. Page-level `<meta name="robots">` and `X-Robots-Tag` (unprefixed,
  or prefixed for our token) are honoured: `noindex` means not indexed,
  `nofollow` means links aren't followed.
- Written in-house (about 160 lines, 16 test functions) because `urllib.robotparser`
  applies the first matching rule, not the longest, and has no wildcards.

## Sitemaps

`Sitemap:` lines from robots.txt (same origin only) plus `/sitemap.xml`.
Index files are followed, up to 5 sitemap files and 500 URLs. Their pages
join the BFS at depth 1, right after the start page, and pass through the
same origin, robots, and skip checks as links. A sitemap's own redirects are
checked the same way before the target is requested: same origin, the
destination check, robots.txt, and the skip rules (except the file-extension
rule, since sitemaps are `.xml`). A disallowed target is never fetched. A sitemap with a DOCTYPE is
refused before parsing, which rules out entity expansion and external
entities. A malformed or missing sitemap is recorded and the crawl continues
from links. Gzipped sitemaps are skipped (not supported).

## Breadth-first crawl

A FIFO frontier processes depth by depth, never past `max_depth`, and stops
at `max_pages` page requests or `CRAWL_MAX_DURATION_SECONDS` (300). Requests
to a host are at least `CRAWL_DELAY_SECONDS` apart, measured from start to
start. URLs are never requested if they are:
- on another origin;
- disallowed by robots.txt;
- sign-in, account, cart, or checkout paths;
- non-HTML file extensions;
- carrying more than 3 query parameters (after tracking parameters are
  removed).

Skipped URLs are recorded, with a reason. The crawl stops early, keeping what
it fetched, on a 429 (or a 503 with Retry-After), or when the host starts
resolving to a non-public address.

## URL normalisation

The scheme and host are lower-cased, the host IDNA-encoded and its trailing
dot dropped. Default ports are removed and dot segments resolved. Percent
escapes of unreserved characters are decoded, the rest upper-cased, and
unsafe characters escaped. `%2F` stays distinct from `/`. Tracking parameters
(`utm_*`, `gclid`, `fbclid`, `msclkid`, `_ga`, session ids, ...) are removed,
the remaining parameters sorted by name, and the fragment dropped.

Not merged, because they can be different pages: path and query letter case,
and http vs https. `/a` and `/a/` share one *visit key*, so only the first
spelling seen is fetched. Canonical URLs and redirect targets are marked as
seen too.

## HTML extraction (BeautifulSoup, no LLM)

- **Links** are collected from the whole page before anything is removed.
  `rel=nofollow` is respected; `<base href>` is honoured.
- **Content** comes from `<main>` (or `role=main`), else a single
  `<article>`, else `<body>`.
- **Removed:** script, style, noscript, template, nav, footer, header (except
  a heading inside the main content), form and its controls, aside, iframes
  and media, `hidden` / `aria-hidden` / `display:none` elements, navigation
  landmarks, and cookie or consent banners.
- **Output** is M4 `Block`s: h1–h6 headings with level; paragraphs, list
  items, and div text; tables as `cell | cell` rows.
- **Metadata:** title (`<title>`, then `og:title`, then the first heading),
  canonical URL, language, description, HTTP status, fetch time.
- **Site-level boilerplate:** a block on at least half the pages (and at
  least 3) is dropped from every page.
- Pages under `MIN_DOCUMENT_CHARS` after cleaning fail with "too little
  readable text". Crawled HTML is never executed, rendered, or returned.

## Incremental indexing

- **One page = one document,** keyed by `documents.canonical_url`. The key is
  the page's canonical URL when it's on the same site, otherwise the URL it
  was fetched from. Keys are assigned for the whole crawl at once
  (`assign_document_keys`), deterministically:
  1. A canonical URL that is another fetched page with *different* content is
     ignored, so a canonical tag can't take over a real page.
  2. Among pages wanting the same key, the page fetched *at* that URL owns
     it, otherwise the first in BFS order. A member with different content
     falls back to its own URL.
  3. Identical text under different keys is indexed once; the others become
     `duplicate`.
  4. A duplicate never points at its own URL: if it was fetched there, it
     owns the document instead.
  So `/a` declaring `/b` canonical, with different content from the real
  `/b`, gives two documents in either order.
- **Unchanged:** same text hash, title, chunker, and model. No chunking and no
  embedding; the chunks are untouched (same ids). `fetched_at` and the page
  record are updated. Tested live: an unchanged re-crawl made 0 Gemini calls.
- **Changed:** chunks rebuilt, and unchanged chunk texts reuse cached
  vectors. The swap is the M4 single fenced transaction, and the page's crawl
  record is written in the *same* transaction, so the two can't disagree.
  A title-only change re-chunks with 0 new embeddings.
- **Each page commits on its own.** A crash or a budget deferral keeps the
  pages already done; the retry finds them unchanged and costs nothing.
- **A failed update never destroys the last good index.** Fetch or extraction
  failures keep the previous document. An embedding failure aborts the job
  before that page's swap.
- **Ownership** (review fix, migration `0005`). `crawl_pages.document_url`
  is the document a page *owns*, the one it published. A duplicate owns
  nothing; it points at the other page's document through `duplicate_of`.
  A page only ever gives up a document it owns. The document is deleted only
  after this crawl's page rows are written, and only if no page owns it any
  more. So:
  - a duplicate alias that disappears or becomes unique never removes the
    original, even when this crawl didn't reach the original;
  - a document two pages own survives while either still owns it.
- **Removed from the index:** a page's own document goes when the page:
  - returns 404/410;
  - is now robots-disallowed (also for pages the crawl didn't reach: every
    known page is checked against the current rules);
  - is noindex;
  - now redirects to another crawled page;
  - became a duplicate of another page;
  - moved to a different key.
  Pages that weren't reached for other reasons (limits, no longer linked)
  keep their documents.
- **Removals happen even when no page is indexable this time.** Confirmed
  removals commit first, then the job fails (all permanent) or retries (any
  transient failure). Pages that failed transiently keep their documents.

## Database (migrations `0004_crawler`, `0005_crawl_page_duplicates`)

The `crawl_pages` table holds, per URL and source:
- the URL, final URL after redirects, the document the page owns
  (`document_url`), and for duplicates the document with the same content
  (`duplicate_of`, added in `0005`, which moves existing duplicate links there);
- depth and how it was discovered (`start`, `sitemap`, `link`);
- the status (`indexed`, `unchanged`, `duplicate`, `skipped`, `failed`) with a
  reason code and message;
- the HTTP status, content type, title, and content hash;
- the latest job id, and the first-seen, fetched, indexed, and changed times.

It has a composite `(workspace_id, source_id)` foreign key to sources, a
unique `(source_id, url)`, and a `(workspace_id, source_id, status)` index.
Website pages reuse `documents` (unique `(source_id, canonical_url)` already
existed) and `chunks`. Crawl progress and totals live in `ingestion_jobs.result`.

## Failure handling

| Situation | Outcome |
|---|---|
| A page: 404, 401/403, 5xx, timeout, not HTML, too large, too little text | Recorded on the page; crawl continues; previous version kept (404 removes it) |
| No page indexable; any failure transient | Confirmed removals commit; job retried with backoff (30 s, 60 s), up to 3 attempts |
| No page indexable; all permanent | Confirmed removals commit; job fails once with a clear message |
| Start URL newly disallowed by robots.txt | Nothing crawled; pages the rules forbid are removed; job fails once |
| Host blocked | Job fails once with a clear message |
| robots.txt 5xx/429/unreachable | Nothing crawled; retried |
| Daily embedding budget or Gemini daily quota | Deferred without using an attempt; finished pages kept |
| Gemini temporary error | Job retried; already-published pages are unchanged next time |
| Worker crash | Lease lapses, job recovered (M4); re-crawl is idempotent |
| Stale worker | Fenced progress write fails, so it stops at once; it can't publish either |

## API

- `POST /sources/websites` `{url, max_pages?, max_depth?}` returns 202 with
  the source and job. The same URL again returns 200 with the existing source.
- `GET /sources/{id}/pages?status=failed&limit=&offset=` gives per-page
  results, with `in_latest_crawl`.
- `POST /sources/{id}/reindex` re-crawls; 409 `crawl_in_progress` while one is
  queued or running.
- `GET /sources/{id}` now includes `source_url`, `settings`, documents with
  `canonical_url` and `fetched_at`, and `page_counts`.
- `GET /jobs/{id}` shows live progress, then totals.
- `DELETE /sources/{id}` removes pages, chunks, crawl records, and orphaned
  cached vectors.

## Known limitations

- **No JavaScript rendering:** pages that build their content in the browser
  yield little or no text, and fail as "too little readable text".
- **Boilerplate removal is heuristic.** Sites without semantic tags (`nav`,
  `footer`, `main`) keep some chrome, such as a "Login" link text or a
  copyright line, until a crawl has at least 3 pages to compare.
- **Single origin only:** `example.com` and `www.example.com`, or http and
  https, are different sites. A site that redirects everything elsewhere must
  be added at the address it redirects to.
- **No gzipped sitemaps, no PDF or other files linked from a site,** no
  conditional requests (ETag / If-Modified-Since): every crawl re-fetches,
  but unchanged pages cost no embeddings.
- **The per-host delay is per worker process.** Several workers crawling the
  same host would each keep their own 1 s spacing.
- **The time limit covers fetching only:** embedding time after the crawl
  isn't counted.
- Up to 50 skipped URLs are recorded per crawl; all are counted.
