"""The retrieval configurations an evaluation run compares (ADR 0014).

`vector` and `hybrid` are the two production modes. The other three change only
hybrid mode's full-text list and exist only here: they are experiments, and
production keeps `simple` all-words matching (ADR 0009) whatever they score.
Answers are evaluated for production modes only.
"""

from dataclasses import dataclass
from typing import Literal

from app.retrieval.service import PRODUCTION_LEXICAL, LexicalVariant, RetrievalMode

AnswerMode = Literal["vector", "hybrid"]
ANSWER_MODES: tuple[AnswerMode, ...] = ("vector", "hybrid")


@dataclass(frozen=True)
class RetrievalVariant:
    key: str
    label: str
    mode: RetrievalMode
    lexical: LexicalVariant
    experimental: bool


VARIANTS: tuple[RetrievalVariant, ...] = (
    RetrievalVariant("vector", "Vector only", "vector", PRODUCTION_LEXICAL, False),
    RetrievalVariant(
        "hybrid", "Hybrid: keyword AND, simple (production)", "hybrid", PRODUCTION_LEXICAL, False
    ),
    RetrievalVariant(
        "hybrid_or", "Hybrid: keyword OR, simple", "hybrid", LexicalVariant("simple", "any"), True
    ),
    RetrievalVariant(
        "hybrid_english",
        "Hybrid: keyword AND, english",
        "hybrid",
        LexicalVariant("english", "all"),
        True,
    ),
    RetrievalVariant(
        "hybrid_english_or",
        "Hybrid: keyword OR, english",
        "hybrid",
        LexicalVariant("english", "any"),
        True,
    ),
)
BY_KEY = {variant.key: variant for variant in VARIANTS}
