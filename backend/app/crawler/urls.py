"""URL validation and normalisation (SPEC.md §7.3).

`normalize_url` gives every spelling of the same address one canonical form,
so the crawler's visited set and the document keys deduplicate reliably:

- scheme and host lower-cased, IDNA host encoded, trailing host dot dropped;
- default ports removed; dot segments (`/a/../b`) resolved;
- percent-encoding normalised: unreserved characters decoded, hex upper-cased,
  characters that must be escaped are escaped; `%2F` stays `%2F` (it is a
  different path from `/`);
- known tracking parameters removed (utm_*, gclid, fbclid, ...); the rest are
  kept, sorted by name (values and repeated keys keep their order);
- the fragment removed.

Two things are deliberately *not* merged, because they can be different pages:
letter case in the path or query, and `/a` vs `/a/` (see `visit_key`, which
the crawler uses so it fetches only one of those).

Validation here rejects anything but http/https, credentials in the URL, and
invalid host names. IP literals in legacy notations (`127.1`, `0x7f000001`)
are rewritten to dotted form, so the destination checks in
app.crawler.network see the real address.
"""

import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import parse_qsl, quote, urljoin, urlsplit, urlunsplit

ALLOWED_SCHEMES = ("http", "https")
DEFAULT_PORTS = {"http": 80, "https": 443}
MAX_URL_LENGTH = 2048
MAX_QUERY_PARAMETERS = 3  # more than this after tracking removal: a query-heavy URL

TRACKING_PREFIXES = ("utm_",)
TRACKING_PARAMETERS = frozenset(
    {
        "gclid",
        "gclsrc",
        "dclid",
        "gbraid",
        "wbraid",
        "fbclid",
        "msclkid",
        "yclid",
        "twclid",
        "ttclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "_ga",
        "_gl",
        "mkt_tok",
        "oly_anon_id",
        "oly_enc_id",
        "vero_id",
        "srsltid",
        "phpsessid",
        "jsessionid",
        "sessionid",
        "sid",
    }
)

# Never real public web hosts: loopback names, mDNS, and private-use suffixes.
BLOCKED_HOST_NAMES = frozenset({"localhost", "localhost.localdomain"})
BLOCKED_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
    ".intranet",
    ".lan",
    ".home.arpa",
    ".arpa",
)

# Path segments of sign-in, account, and checkout pages (SPEC.md §7.3).
AUTH_SEGMENTS = frozenset(
    {
        "login",
        "log-in",
        "signin",
        "sign-in",
        "signup",
        "sign-up",
        "register",
        "logout",
        "log-out",
        "signout",
        "sign-out",
        "auth",
        "oauth",
        "sso",
        "account",
        "accounts",
        "my-account",
        "password",
        "reset-password",
        "forgot-password",
        "wp-login.php",
        "wp-admin",
        "admin",
        "cart",
        "checkout",
        "basket",
    }
)

# Extensions that are never HTML pages; skipped without a request.
NON_HTML_EXTENSIONS = frozenset(
    [
        "pdf",
        "doc",
        "docx",
        "xls",
        "xlsx",
        "ppt",
        "pptx",
        "odt",
        "ods",
        "csv",
        "txt",
        "rtf",
        "epub",
        "jpg",
        "jpeg",
        "png",
        "gif",
        "webp",
        "svg",
        "ico",
        "bmp",
        "tif",
        "tiff",
        "avif",
        "heic",
        "mp3",
        "mp4",
        "m4a",
        "m4v",
        "mov",
        "avi",
        "wmv",
        "webm",
        "ogg",
        "ogv",
        "wav",
        "flac",
        "mkv",
        "zip",
        "gz",
        "tgz",
        "tar",
        "rar",
        "7z",
        "bz2",
        "xz",
        "dmg",
        "exe",
        "msi",
        "apk",
        "iso",
        "bin",
        "css",
        "js",
        "mjs",
        "json",
        "xml",
        "rss",
        "atom",
        "map",
        "woff",
        "woff2",
        "ttf",
        "otf",
        "eot",
    ]
)

_LABEL = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)$")
_PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
# Characters left as they are in a path (RFC 3986 pchar, plus "/" and "%").
_PATH_SAFE = "/:@!$&'()*+,;=-._~%"
_QUERY_SAFE = "/:@!$'()*,;-._~"


class InvalidUrl(ValueError):
    """The URL can't be crawled. `message` is safe to show the user."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Origin:
    scheme: str
    host: str
    port: int

    def __str__(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        if DEFAULT_PORTS[self.scheme] == self.port:
            return f"{self.scheme}://{host}"
        return f"{self.scheme}://{host}:{self.port}"


def ip_literal(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """The address if `host` is an IP in any notation getaddrinfo would accept."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    # inet_aton accepts the legacy forms ("127.1", "0x7f.1", "2130706433") that
    # resolvers turn into addresses without any DNS lookup.
    if re.fullmatch(r"[0-9a-fA-Fx.]+", host) and not host.endswith("."):
        try:
            return ipaddress.IPv4Address(socket.inet_aton(host))
        except OSError:
            return None
    return None


def normalize_host(raw_host: str) -> str:
    host = raw_host.strip().rstrip(".").lower()
    if not host:
        raise InvalidUrl("The URL has no host name.")
    literal = ip_literal(host)
    if literal is not None:
        return str(literal)
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise InvalidUrl("The host name is not valid.") from exc
    labels = ascii_host.split(".")
    if len(ascii_host) > 253 or not all(_LABEL.match(label) for label in labels):
        raise InvalidUrl("The host name is not valid.")
    return ascii_host


def _normalize_percent(component: str, safe: str) -> str:
    def fix(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else f"%{match.group(1).upper()}"

    # A "%" not followed by two hex digits is escaped itself.
    component = re.sub(r"%(?![0-9A-Fa-f]{2})", "%25", component)
    return quote(_PERCENT.sub(fix, component), safe=safe)


def _remove_dot_segments(path: str) -> str:
    """RFC 3986 §5.2.4."""
    output: list[str] = []
    for segment in path.split("/"):
        if segment == "..":
            if len(output) > 1:
                output.pop()
        elif segment != ".":
            output.append(segment)
    result = "/".join(output)
    if path.endswith(("/.", "/..")):
        result += "/"
    return result if result.startswith("/") else "/" + result


def _is_tracking(name: str) -> bool:
    lowered = name.lower()
    return lowered in TRACKING_PARAMETERS or lowered.startswith(TRACKING_PREFIXES)


def _normalize_query(query: str) -> str:
    # surrogateescape round-trips bytes that aren't UTF-8 instead of replacing them.
    pairs = [
        (name, value)
        for name, value in parse_qsl(query, keep_blank_values=True, errors="surrogateescape")
        if not _is_tracking(name)
    ]
    pairs.sort(key=lambda pair: pair[0])  # stable: repeated keys keep their order
    return "&".join(
        f"{_quote_query(name)}={_quote_query(value)}" if value else _quote_query(name)
        for name, value in pairs
    )


def _quote_query(part: str) -> str:
    return quote(part, safe=_QUERY_SAFE, errors="surrogateescape")


def normalize_url(raw: str, base: str | None = None) -> str:
    """Absolute, canonical http(s) URL, or InvalidUrl. Doesn't check the host's IPs."""
    raw = (raw or "").strip()
    if not raw:
        raise InvalidUrl("The URL is empty.")
    if len(raw) > MAX_URL_LENGTH:
        raise InvalidUrl(f"The URL is longer than {MAX_URL_LENGTH} characters.")
    if base is not None:
        raw = urljoin(base, raw)
    try:
        parts = urlsplit(raw)
        port = parts.port
    except ValueError as exc:
        raise InvalidUrl("The URL is not valid.") from exc
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise InvalidUrl("Only http:// and https:// URLs can be crawled.")
    if "@" in parts.netloc:
        raise InvalidUrl("URLs with a user name or password can't be crawled.")
    if parts.hostname is None:
        raise InvalidUrl("The URL has no host name.")
    host = normalize_host(parts.hostname)
    if port is not None and not 0 < port <= 65535:
        raise InvalidUrl("The URL's port is not valid.")
    netloc = f"[{host}]" if ":" in host else host
    if port is not None and port != DEFAULT_PORTS[scheme]:
        netloc = f"{netloc}:{port}"
    path = _remove_dot_segments(_normalize_percent(parts.path or "/", _PATH_SAFE))
    url = urlunsplit((scheme, netloc, path, _normalize_query(parts.query), ""))
    if len(url) > MAX_URL_LENGTH:
        raise InvalidUrl(f"The URL is longer than {MAX_URL_LENGTH} characters.")
    return url


def origin_of(url: str) -> Origin:
    """The (scheme, host, port) of an already normalised URL."""
    parts = urlsplit(url)
    scheme = parts.scheme
    host = parts.hostname or ""
    return Origin(scheme, host, parts.port or DEFAULT_PORTS[scheme])


def visit_key(url: str) -> str:
    """Key for "already crawled": `/a` and `/a/` count as one page (the first one seen)."""
    parts = urlsplit(url)
    path = parts.path
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))


def path_and_query(url: str) -> str:
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def skip_reason(url: str) -> tuple[str, str] | None:
    """Why a same-origin URL shouldn't be requested at all, or None."""
    parts = urlsplit(url)
    segments = [s.lower() for s in parts.path.split("/") if s]
    if any(segment in AUTH_SEGMENTS for segment in segments):
        return ("auth_page", "Sign-in, account, and checkout pages are not crawled.")
    if segments and "." in segments[-1]:
        extension = segments[-1].rsplit(".", 1)[-1]
        if extension in NON_HTML_EXTENSIONS:
            return ("not_html", f"Links to .{extension} files are not crawled.")
    if parts.query and len(parse_qsl(parts.query, keep_blank_values=True)) > (MAX_QUERY_PARAMETERS):
        return ("query_heavy", "URLs with many query parameters are not crawled.")
    return None
