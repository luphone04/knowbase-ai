"""robots.txt rules, per RFC 9309.

Written here rather than using `urllib.robotparser`, which applies the first
matching rule instead of the most specific one and doesn't support the `*`
and `$` wildcards. Both matter: `Disallow: /` with `Allow: /docs` must allow
`/docs/x`.

- **Groups:** the group(s) naming our product token (case-insensitive) apply;
  otherwise the `*` group(s); otherwise everything is allowed. Groups for the
  same agent are merged.
- **Matching:** the longest matching pattern wins, and `Allow` wins a tie.
  `*` matches any characters and a trailing `$` anchors the end. Paths and
  patterns are compared after the same percent-encoding normalisation.
  `/robots.txt` itself is always allowed.
- `Crawl-delay` (not in the RFC, but widely used) is read from the chosen
  group. `Sitemap` lines are global.
- Only the first 500 KiB are parsed (the RFC's minimum).

What happens when robots.txt can't be fetched is decided by the crawler
(app.crawler.crawl): 4xx means no restrictions; 5xx, 429, or a network error
means complete disallow.
"""

import re
from dataclasses import dataclass, field
from urllib.parse import quote

MAX_ROBOTS_BYTES = 500 * 1024
MAX_CRAWL_DELAY_SECONDS = 60.0

_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_SAFE = "/?=&;:@!$'()*+,%-._~"


@dataclass(frozen=True)
class _Rule:
    allow: bool
    pattern: str
    regex: re.Pattern[str]


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[_Rule] = field(default_factory=list)
    crawl_delay: float | None = None


def _normalize(value: str) -> str:
    """Decode escaped unreserved characters, upper-case other escapes, escape the rest."""

    def fix(match: re.Match[str]) -> str:
        char = chr(int(match.group(1), 16))
        return char if char in _UNRESERVED else f"%{match.group(1).upper()}"

    return quote(re.sub(r"%([0-9A-Fa-f]{2})", fix, value), safe=_SAFE + "$")


def _compile(pattern: str) -> re.Pattern[str]:
    anchored = pattern.endswith("$")
    body = pattern[:-1] if anchored else pattern
    regex = ".*".join(re.escape(part) for part in body.split("*"))
    return re.compile(regex + ("$" if anchored else ""))


@dataclass(frozen=True)
class RobotsRules:
    """The rules that apply to this crawler, plus the file's sitemap URLs."""

    rules: tuple[_Rule, ...] = ()
    crawl_delay: float | None = None
    sitemaps: tuple[str, ...] = ()
    disallow_all: bool = False

    @classmethod
    def allow_everything(cls) -> "RobotsRules":
        return cls()

    @classmethod
    def deny_everything(cls) -> "RobotsRules":
        return cls(disallow_all=True)

    def allows(self, path_and_query: str) -> bool:
        if path_and_query == "/robots.txt":
            return True
        if self.disallow_all:
            return False
        target = _normalize(path_and_query)
        best: _Rule | None = None
        for rule in self.rules:
            if rule.regex.match(target) is None:
                continue
            if (
                best is None
                or len(rule.pattern) > len(best.pattern)
                or (len(rule.pattern) == len(best.pattern) and rule.allow and not best.allow)
            ):
                best = rule
        return best is None or best.allow


def parse_robots(content: bytes | str, product_token: str) -> RobotsRules:
    if isinstance(content, bytes):
        content = content[:MAX_ROBOTS_BYTES].decode("utf-8", errors="replace")
    else:
        content = content[:MAX_ROBOTS_BYTES]
    token = product_token.lower()
    groups: list[_Group] = []
    sitemaps: list[str] = []
    current: _Group | None = None
    in_agent_lines = False

    for raw_line in content.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if key == "sitemap":
            if value:
                sitemaps.append(value)
            continue
        if key in ("user-agent", "useragent"):
            if current is None or not in_agent_lines:
                current = _Group()
                groups.append(current)
            current.agents.append(value.lower())
            in_agent_lines = True
            continue
        in_agent_lines = False
        if current is None:
            continue  # a rule before any user-agent line belongs to no group
        if key in ("allow", "disallow"):
            if not value:
                continue  # "Disallow:" with no path restricts nothing
            if not value.startswith(("/", "*")):
                value = "/" + value
            pattern = _normalize(value)
            current.rules.append(_Rule(key == "allow", pattern, _compile(pattern)))
        elif key == "crawl-delay":
            try:
                delay = float(value)
            except ValueError:
                continue
            if delay >= 0:
                current.crawl_delay = min(delay, MAX_CRAWL_DELAY_SECONDS)

    def names(agent: str) -> str:
        return agent.split("/", 1)[0].strip()

    chosen = [g for g in groups if any(names(a) == token for a in g.agents)]
    if not chosen:
        chosen = [g for g in groups if "*" in g.agents]
    rules = tuple(rule for group in chosen for rule in group.rules)
    delays = [g.crawl_delay for g in chosen if g.crawl_delay is not None]
    return RobotsRules(
        rules=rules, crawl_delay=max(delays) if delays else None, sitemaps=tuple(sitemaps)
    )
