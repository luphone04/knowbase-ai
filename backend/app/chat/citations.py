"""Citation validation (SPEC.md §10.2) and abstention detection (§10.3).

The server numbered the excerpts before calling Gemini, so it knows every valid
id. In the model's answer:

- a citation group such as [S1], [S2, S4], or [s3] keeps only ids that were
  given; a group with none left is removed, with the space before it;
- a URL is kept only if it appears verbatim in an excerpt the model was given;
  any other is replaced with "[link removed]" (file sources have no URLs, so
  the model has nothing to link to);
- the cited ids come back in order of first use, without repeats.

An answer is grounded only when at least one valid citation is left; the caller
abstains otherwise.
"""

import re
from collections.abc import Collection
from dataclasses import dataclass

from app.chat.prompt import ABSTENTION

# A bracket holding only citation ids and separators: [S1], [S1, S2], [S1; S3], [S1-S3].
_GROUP = re.compile(
    r"(?P<space>[ \t]*)\[(?P<body>\s*S\d+(?:\s*(?:,|;|&|-|–|and)?\s*S\d+)*\s*)\]",
    re.IGNORECASE,
)
_ID = re.compile(r"S(\d+)", re.IGNORECASE)
_URL = re.compile(r"https?://[^\s<>\[\]()\"']+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?"
LINK_REMOVED = "[link removed]"


@dataclass(frozen=True)
class CheckedAnswer:
    text: str
    cited_ids: list[str]  # valid ids in order of first use
    invalid_ids: list[str]  # ids the model made up, in order of first use
    removed_urls: int


def _normalized(text: str) -> str:
    return " ".join(text.replace("’", "'").casefold().split())


_ABSTENTION_MARKERS = (
    _normalized(ABSTENTION).removesuffix(" to answer that."),
    "could not find enough information in this knowledge base",
)


def is_abstention(text: str) -> bool:
    """True when the answer says the knowledge base doesn't hold enough information."""
    normalized = _normalized(text)
    return any(marker in normalized for marker in _ABSTENTION_MARKERS)


def check_answer(text: str, *, valid_ids: Collection[str], excerpt_text: str) -> CheckedAnswer:
    valid = {i.upper() for i in valid_ids}
    cited: list[str] = []
    invalid: list[str] = []

    def rewrite_group(match: re.Match[str]) -> str:
        kept = []
        for number in _ID.findall(match["body"]):
            citation_id = f"S{int(number)}"
            if citation_id in valid:
                if citation_id not in kept:
                    kept.append(citation_id)
                if citation_id not in cited:
                    cited.append(citation_id)
            elif citation_id not in invalid:
                invalid.append(citation_id)
        return f"{match['space']}[{', '.join(kept)}]" if kept else ""

    text = _GROUP.sub(rewrite_group, text)

    removed = 0

    def check_url(match: re.Match[str]) -> str:
        nonlocal removed
        url = match.group(0)
        stripped = url.rstrip(_TRAILING_PUNCTUATION)
        if stripped in excerpt_text:
            return url
        removed += 1
        return LINK_REMOVED + url[len(stripped) :]

    text = _URL.sub(check_url, text)
    return CheckedAnswer(
        text=text.strip(), cited_ids=cited, invalid_ids=invalid, removed_urls=removed
    )
