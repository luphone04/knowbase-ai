"""sitemap.xml parsing (sitemaps.org protocol): `<urlset>` and `<sitemapindex>`.

Sitemaps are untrusted XML. A document with a DOCTYPE is refused before
parsing: without a DTD there are no entity definitions, so entity expansion
("billion laughs") and external entities are impossible. The standard
library's ElementTree never fetches external resources anyway. Only `<loc>`
values are read, at most `max_urls` of them. Every URL is validated and
scoped by the crawler, exactly like a link found in a page.
"""

import re
from dataclasses import dataclass
from typing import Literal
from xml.etree import ElementTree

MAX_SITEMAP_URLS = 500
_DTD = re.compile(rb"<!(DOCTYPE|ENTITY)", re.IGNORECASE)


class SitemapError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class Sitemap:
    kind: Literal["urlset", "index"]
    locations: list[str]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def parse_sitemap(body: bytes, *, max_urls: int = MAX_SITEMAP_URLS) -> Sitemap:
    if _DTD.search(body):
        raise SitemapError("The sitemap declares a DTD, which is not accepted.")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        raise SitemapError("The sitemap is not valid XML.") from exc
    kind_name = _local(root.tag)
    if kind_name not in ("urlset", "sitemapindex"):
        raise SitemapError("The file is not a sitemap (no <urlset> or <sitemapindex>).")
    entry = "url" if kind_name == "urlset" else "sitemap"
    locations: list[str] = []
    for child in root:
        if _local(child.tag) != entry:
            continue
        for item in child:
            if _local(item.tag) == "loc" and item.text and item.text.strip():
                locations.append(item.text.strip())
                break
        if len(locations) >= max_urls:
            break
    return Sitemap("urlset" if kind_name == "urlset" else "index", locations)
