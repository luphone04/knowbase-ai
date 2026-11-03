"""HTML -> title, links, and readable blocks (SPEC.md §7.3, §7.4). No LLM, no JavaScript.

The output reuses the M4 `Block` type (heading / paragraph / table), so web
pages go through exactly the same cleaner, hasher, and chunker as files.

1. **Links** are collected from the whole page first (navigation menus are
   how a crawler finds pages), skipping `rel="nofollow"` and non-http schemes.
2. **Main content:** `<main>` (or `role="main"`), else a single `<article>`,
   else `<body>`.
3. **Removed:** script, style, noscript, template, nav, footer, form and its
   controls, aside, iframes and media, hidden elements, navigation landmarks,
   and cookie/consent banners. `<header>` goes too, except a header inside
   the main content that holds the page's heading.
4. **Blocks:** h1-h6 become headings with their level; paragraphs, list
   items ("- "), quotes, and text in plain `<div>`s become paragraphs;
   tables become "cell | cell" rows.

Crawled HTML is only ever *read*: nothing is executed or rendered, and only
plain text is stored.
"""

import re
import warnings
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag
from bs4.element import (
    CData,
    Comment,
    Declaration,
    Doctype,
    NavigableString,
    ProcessingInstruction,
)

from app.ingestion.parsers import Block, UnsupportedDocument

_REMOVE = [
    "script",
    "style",
    "noscript",
    "template",
    "nav",
    "footer",
    "form",
    "button",
    "select",
    "input",
    "textarea",
    "label",
    "aside",
    "iframe",
    "frame",
    "frameset",
    "object",
    "embed",
    "svg",
    "canvas",
    "video",
    "audio",
    "picture",
    "map",
    "dialog",
    "menu",
]
_HIDDEN_ROLES = frozenset(
    {"navigation", "banner", "contentinfo", "search", "dialog", "alertdialog"}
)
_BANNER = re.compile(r"(^|[-_])(cookie|cookies|consent|gdpr)([-_]|$)", re.IGNORECASE)
_HIDDEN_STYLE = re.compile(r"display\s*:\s*none|visibility\s*:\s*hidden", re.IGNORECASE)
_HEADING = re.compile(r"^h([1-6])$")
_BLOCK_TAGS = frozenset(
    [
        "address",
        "article",
        "blockquote",
        "body",
        "center",
        "dd",
        "details",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "header",
        "hgroup",
        "hr",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "summary",
        "ul",
    ]
)
_SPACE = re.compile(r"\s+")
_SKIP_STRINGS = (Comment, CData, Declaration, Doctype, ProcessingInstruction)
_MAX_TITLE = 200


@dataclass
class ExtractedPage:
    title: str | None
    canonical_url: str | None  # absolute but not yet validated
    blocks: list[Block]
    links: list[str]  # absolute but not yet validated or scoped
    noindex: bool = False
    nofollow: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


def _text(node: Tag) -> str:
    return _SPACE.sub(" ", node.get_text(" ", strip=True)).strip()


def _attr(tag: Tag, name: str) -> str:
    value = tag.get(name)
    if isinstance(value, list):
        return " ".join(value)
    return value or ""


def robots_directives(value: str) -> set[str]:
    """Tokens of a robots meta tag or X-Robots-Tag value ("noindex, nofollow")."""
    tokens = {token.strip().lower() for token in re.split(r"[,\s]+", value) if token.strip()}
    if "none" in tokens:
        tokens |= {"noindex", "nofollow"}
    return tokens


def _is_hidden(tag: Tag) -> bool:
    if tag.has_attr("hidden") or _attr(tag, "aria-hidden").lower() == "true":
        return True
    if _attr(tag, "role").lower() in _HIDDEN_ROLES:
        return True
    if _HIDDEN_STYLE.search(_attr(tag, "style")):
        return True
    names = (_attr(tag, "id") + " " + _attr(tag, "class")).split()
    return any(_BANNER.search(name) for name in names)


class _Blocks:
    """Walks the content tree and emits blocks in reading order."""

    def __init__(self) -> None:
        self.blocks: list[Block] = []
        self._buffer: list[str] = []
        self._prefix = ""

    def flush(self) -> None:
        text = _SPACE.sub(" ", "".join(self._buffer)).strip()
        self._buffer = []
        if text:
            self.blocks.append(Block("paragraph", self._prefix + text))
        self._prefix = ""

    def walk(self, node: Tag) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                if not isinstance(child, _SKIP_STRINGS):
                    self._buffer.append(str(child))
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name.lower()
            heading = _HEADING.match(name)
            if heading:
                self.flush()
                text = _text(child)
                if text:
                    self.blocks.append(Block("heading", text, level=int(heading.group(1))))
            elif name == "li":
                self.flush()
                self._prefix = "- "
                self.walk(child)
                self.flush()
            elif name == "table":
                self.flush()
                rows = []
                for row in child.find_all("tr"):
                    cells = [_text(cell) for cell in row.find_all(["td", "th"])]
                    cells = [cell for cell in cells if cell]
                    if cells:
                        rows.append(" | ".join(cells))
                if rows:
                    self.blocks.append(Block("table", "\n".join(rows)))
            elif name == "br":
                self._buffer.append(" ")
            elif name in _BLOCK_TAGS:
                prefix = self._prefix if name not in ("ul", "ol") else ""
                self.flush()
                self._prefix = prefix
                self.walk(child)
                self.flush()
            else:
                self.walk(child)  # inline: its text joins the current paragraph


def _content_root(soup: BeautifulSoup) -> Tag:
    main = soup.find("main") or soup.find(None, {"role": "main"})
    if isinstance(main, Tag):
        return main
    articles = soup.find_all("article")
    if len(articles) == 1 and isinstance(articles[0], Tag):
        return articles[0]
    body = soup.find("body")
    return body if isinstance(body, Tag) else soup


def _strip_boilerplate(root: Tag) -> None:
    keep_headers = root.name in ("main", "article") or _attr(root, "role") == "main"
    doomed: list[Tag] = []
    for tag in root.find_all(True):
        if not isinstance(tag, Tag):
            continue
        name = tag.name.lower()
        if (
            name in _REMOVE
            or _is_hidden(tag)
            or name == "header"
            and not (keep_headers and tag.find(["h1", "h2", "h3", "h4", "h5", "h6"]))
        ):
            doomed.append(tag)
    for tag in doomed:
        tag.decompose()


def extract_page(html: bytes, *, url: str, charset: str | None = None) -> ExtractedPage:
    """Parse untrusted HTML. Raises UnsupportedDocument if it can't be read."""
    try:
        with warnings.catch_warnings():
            # bs4 warns on odd input (e.g. text that looks like a filename); not our concern.
            warnings.simplefilter("ignore")
            soup = BeautifulSoup(html, "html.parser", from_encoding=charset)
    except Exception as exc:  # html.parser is lenient; anything else is unreadable input
        raise UnsupportedDocument("The page's HTML could not be parsed.") from exc

    base = url
    base_tag = soup.find("base", href=True)
    if isinstance(base_tag, Tag):
        candidate = urljoin(url, _attr(base_tag, "href").strip())
        if candidate.startswith(("http://", "https://")):
            base = candidate

    directives: set[str] = set()
    for meta in soup.find_all("meta"):
        if isinstance(meta, Tag) and _attr(meta, "name").lower() in ("robots", "knowbasebot"):
            directives |= robots_directives(_attr(meta, "content"))

    links: list[str] = []
    for anchor in soup.find_all("a", href=True):
        if not isinstance(anchor, Tag):
            continue
        if "nofollow" in _attr(anchor, "rel").lower().split():
            continue
        href = _attr(anchor, "href").strip()
        if not href or href.startswith("#"):
            continue
        absolute = urljoin(base, href)
        if absolute.startswith(("http://", "https://")):
            links.append(absolute)

    canonical = None
    for link in soup.find_all("link", href=True):
        if isinstance(link, Tag) and "canonical" in _attr(link, "rel").lower().split():
            canonical = urljoin(base, _attr(link, "href").strip())
            break

    title = None
    if soup.title is not None:
        title = _SPACE.sub(" ", soup.title.get_text(" ", strip=True)).strip() or None
    if title is None:
        og = soup.find("meta", attrs={"property": "og:title"})
        if isinstance(og, Tag):
            title = _SPACE.sub(" ", _attr(og, "content")).strip() or None

    metadata: dict[str, Any] = {}
    html_tag = soup.find("html")
    if isinstance(html_tag, Tag) and _attr(html_tag, "lang"):
        metadata["language"] = _attr(html_tag, "lang")[:20]
    description = soup.find("meta", attrs={"name": "description"})
    if isinstance(description, Tag) and _attr(description, "content").strip():
        metadata["description"] = _SPACE.sub(" ", _attr(description, "content")).strip()[:300]

    root = _content_root(soup)
    _strip_boilerplate(root)
    walker = _Blocks()
    try:
        walker.walk(root)
    except RecursionError as exc:
        raise UnsupportedDocument("The page's HTML is nested too deeply to read.") from exc
    walker.flush()
    if title is None:
        first_heading = next((b for b in walker.blocks if b.kind == "heading"), None)
        title = first_heading.text if first_heading else None

    return ExtractedPage(
        title=title[:_MAX_TITLE] if title else None,
        canonical_url=canonical,
        blocks=walker.blocks,
        links=links,
        noindex="noindex" in directives,
        nofollow="nofollow" in directives,
        metadata=metadata,
    )
