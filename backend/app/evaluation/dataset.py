"""The versioned evaluation dataset (SPEC.md §13.1, ADR 0014).

A dataset is a JSON file in `datasets/`: questions about the Northstar demo
documents, each answerable one naming its expected sources by **source key**
(the demo file name). Keys are stable across re-indexing; database ids are
not. The file also records each demo file's SHA-256, so a workspace source
counts as a dataset source only if both its name and its content match.

Every reference fact quotes its evidence, and every unanswerable question
lists terms the corpus must not contain; `corpus_problems` checks both against
the demo files (a unit test runs it on `sample_data/demo_store`). Expected
answers are for human review, never string-matched against model output.
"""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

DATASETS_DIR = Path(__file__).resolve().parent / "datasets"
DEFAULT_DATASET = DATASETS_DIR / "northstar-demo-v1.json"

MIN_QUESTIONS = 20
MIN_ANSWERABLE = 15
MIN_UNANSWERABLE = 5

ANSWERABLE_CATEGORIES = (
    "direct",
    "paraphrase",
    "exact_term",
    "negative_policy",  # a documented "no" is an answer, not an abstention
    "multi_source",
    "follow_up",
)
UNANSWERABLE_CATEGORIES = ("out_of_scope", "absent_detail")
CATEGORIES = ANSWERABLE_CATEGORIES + UNANSWERABLE_CATEGORIES
Category = Literal[
    "direct",
    "paraphrase",
    "exact_term",
    "negative_policy",
    "multi_source",
    "follow_up",
    "out_of_scope",
    "absent_detail",
]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


class DatasetError(ValueError):
    """The dataset file is malformed or breaks a rule below."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class _EvidenceIn(_Strict):
    source_key: str
    quote: str = Field(min_length=10)


class _QuestionIn(_Strict):
    key: str
    category: Category
    question: str = Field(min_length=3, max_length=500)
    previous_question: str | None = Field(default=None, max_length=500)
    expected_source_keys: list[str] = Field(default_factory=list)
    expected_answer: str | None = None
    evidence: list[_EvidenceIn] = Field(default_factory=list)
    absent_terms: list[str] = Field(default_factory=list)
    notes: str | None = None


class _CorpusIn(_Strict):
    directory: str
    sources: dict[str, str]


class _DatasetIn(_Strict):
    name: str = Field(min_length=1, max_length=100)
    version: str = Field(min_length=1, max_length=20)
    description: str
    corpus: _CorpusIn
    questions: list[_QuestionIn]


@dataclass(frozen=True)
class Evidence:
    source_key: str
    quote: str


@dataclass(frozen=True)
class DatasetQuestion:
    key: str
    position: int  # 1-based order in the file
    category: str
    question: str
    previous_question: str | None
    expected_source_keys: tuple[str, ...]
    expected_answer: str | None
    evidence: tuple[Evidence, ...]
    absent_terms: tuple[str, ...]
    notes: str | None

    @property
    def answerable(self) -> bool:
        return self.category in ANSWERABLE_CATEGORIES


@dataclass(frozen=True)
class Dataset:
    name: str
    version: str
    description: str
    corpus_directory: str
    source_hashes: Mapping[str, str]  # source key -> SHA-256 of the demo file
    questions: tuple[DatasetQuestion, ...]
    sha256: str  # of the file's bytes: any edit changes it

    @property
    def answerable(self) -> int:
        return sum(q.answerable for q in self.questions)

    @property
    def unanswerable(self) -> int:
        return len(self.questions) - self.answerable


def parse_dataset(raw: bytes) -> Dataset:
    try:
        parsed = _DatasetIn.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as exc:
        raise DatasetError(f"The dataset file is not valid: {exc}") from exc
    problems = _rule_problems(parsed)
    if problems:
        raise DatasetError("The dataset breaks its rules: " + "; ".join(problems))
    return Dataset(
        name=parsed.name,
        version=parsed.version,
        description=parsed.description,
        corpus_directory=parsed.corpus.directory,
        source_hashes=dict(parsed.corpus.sources),
        questions=tuple(
            DatasetQuestion(
                key=q.key,
                position=position,
                category=q.category,
                question=q.question,
                previous_question=q.previous_question,
                expected_source_keys=tuple(q.expected_source_keys),
                expected_answer=q.expected_answer,
                evidence=tuple(Evidence(e.source_key, e.quote) for e in q.evidence),
                absent_terms=tuple(q.absent_terms),
                notes=q.notes,
            )
            for position, q in enumerate(parsed.questions, start=1)
        ),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def _rule_problems(dataset: _DatasetIn) -> list[str]:
    problems = []
    sources = dataset.corpus.sources
    if not sources:
        problems.append("the corpus names no sources")
    for key, digest in sources.items():
        if key != key.lower() or "/" in key or not key.strip():
            problems.append(f"source key {key!r} must be a lower-case file name")
        if not _SHA256.match(digest):
            problems.append(f"source {key!r} needs a lower-case hex SHA-256")
    seen: set[str] = set()
    for q in dataset.questions:
        where = f"question {q.key!r}"
        if not _KEY.match(q.key):
            problems.append(f"{where}: keys are short lower-case identifiers")
        if q.key in seen:
            problems.append(f"{where}: duplicate key")
        seen.add(q.key)
        unknown = [k for k in q.expected_source_keys + [e.source_key for e in q.evidence]]
        for key in sorted({k for k in unknown if k not in sources}):
            problems.append(f"{where}: unknown source key {key!r}")
        if len(set(q.expected_source_keys)) != len(q.expected_source_keys):
            problems.append(f"{where}: repeated expected source")
        if (q.previous_question is not None) != (q.category == "follow_up"):
            problems.append(f"{where}: only follow_up questions have a previous_question")
        if q.category in ANSWERABLE_CATEGORIES:
            if not q.expected_source_keys:
                problems.append(f"{where}: an answerable question needs expected sources")
            if not q.expected_answer or not q.evidence:
                problems.append(f"{where}: an answerable question needs an answer and evidence")
            cited = {e.source_key for e in q.evidence}
            if not cited <= set(q.expected_source_keys):
                problems.append(f"{where}: evidence from a source that isn't expected")
            if q.absent_terms:
                problems.append(f"{where}: absent_terms are for unanswerable questions")
        else:
            if q.expected_source_keys or q.evidence or q.expected_answer:
                problems.append(f"{where}: an unanswerable question has no sources or answer")
            if not q.absent_terms:
                problems.append(f"{where}: an unanswerable question lists its absent terms")
    answerable = sum(q.category in ANSWERABLE_CATEGORIES for q in dataset.questions)
    unanswerable = len(dataset.questions) - answerable
    if len(dataset.questions) < MIN_QUESTIONS:
        problems.append(f"at least {MIN_QUESTIONS} questions are needed")
    if answerable < MIN_ANSWERABLE:
        problems.append(f"at least {MIN_ANSWERABLE} answerable questions are needed")
    if unanswerable < MIN_UNANSWERABLE:
        problems.append(f"at least {MIN_UNANSWERABLE} unanswerable questions are needed")
    return problems


def _normalized(text: str) -> str:
    return " ".join(text.split())


def corpus_problems(dataset: Dataset, files: Mapping[str, bytes]) -> list[str]:
    """Check the dataset against the demo files' bytes (source key -> content).

    Each file's hash must match; each evidence quote must appear in its file
    (whitespace normalized); no absent term may appear in any file (whole
    words, case-insensitive).
    """
    problems = []
    for key, digest in dataset.source_hashes.items():
        if key not in files:
            problems.append(f"{key}: missing from the corpus")
        elif hashlib.sha256(files[key]).hexdigest() != digest:
            problems.append(f"{key}: content differs from the recorded SHA-256")
    texts = {key: _normalized(content.decode("utf-8")) for key, content in files.items()}
    everything = "\n".join(texts.values())
    for q in dataset.questions:
        for evidence in q.evidence:
            if _normalized(evidence.quote) not in texts.get(evidence.source_key, ""):
                problems.append(f"{q.key}: evidence not found in {evidence.source_key}")
        for term in q.absent_terms:
            if re.search(rf"\b{re.escape(term)}\b", everything, re.IGNORECASE):
                problems.append(f"{q.key}: absent term {term!r} appears in the corpus")
    return problems


@lru_cache
def load_dataset(path: Path = DEFAULT_DATASET) -> Dataset:
    return parse_dataset(path.read_bytes())
