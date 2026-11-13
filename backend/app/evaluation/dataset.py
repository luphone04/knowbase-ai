"""The versioned evaluation datasets (SPEC.md §13.1, ADR 0014).

Two datasets ship in `datasets/`, each with its own format and validation:

- **Northstar** (`northstar-demo`, the default for every workspace): described below.
- **Asia Cement** (`asia-cement-realworld`): a real-world PDF benchmark with
  passage-level evidence (source key + PDF page + quotation) and a fixed
  development / held-out split. `parse_asia_cement` validates its own format;
  Northstar's rules are unchanged. Runs use the development split only. The file
  holds the sealed held-out split, so it is **private**: kept on disk, never in
  Git (.gitignore, scripts/check_repo_hygiene.py). Where it isn't installed, a
  workspace assigned to it gets a clear "unavailable" error, never a crash.
- **Asia Cement development split** (`asia-cement-realworld-dev`): the public file,
  the 16 development questions of `asia-cement-realworld` 1.0.0 copied verbatim
  (its own dataset name, so stored runs of the full benchmark keep their identity).
- **Asia Cement follow-ups** (`asia-cement-followups`, Experiment 006): a separately
  versioned, development-only extension in the same format, over the same four
  PDFs. Every question is a follow-up with a `follow_up_type`; a question may list
  reviewer-only `corroborating_evidence`, which is stored for review and never
  scored (only `evidence` is). The parent benchmark and its held-out split are
  unchanged.

Which dataset a workspace evaluates is chosen on the server from its id
(`EVALUATION_WORKSPACE_DATASETS`), never from the request.

A Northstar dataset is a JSON file in `datasets/`: questions about the Northstar demo
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
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

DATASETS_DIR = Path(__file__).resolve().parent / "datasets"
DEFAULT_DATASET = DATASETS_DIR / "northstar-demo-v1.json"
ASIA_CEMENT_DATASET = DATASETS_DIR / "asia-cement-realworld-v1.json"
ASIA_CEMENT_DEV_DATASET = DATASETS_DIR / "asia-cement-realworld-dev-v1.json"
ASIA_FOLLOWUPS_DATASET = DATASETS_DIR / "asia-cement-followups-v1.json"

NORTHSTAR_ID = "northstar-demo"
ASIA_CEMENT_ID = "asia-cement-realworld"
ASIA_CEMENT_DEV_ID = "asia-cement-realworld-dev"
ASIA_FOLLOWUPS_ID = "asia-cement-followups"
DatasetId = Literal[
    "northstar-demo", "asia-cement-realworld", "asia-cement-realworld-dev", "asia-cement-followups"
]

FollowUpType = Literal["self_contained", "pronoun", "context_dependent", "topic_change"]

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


class DatasetUnavailable(DatasetError):
    """A private dataset file (the full Asia Cement benchmark) isn't installed here."""


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
    page: int | None = None  # 1-based PDF page (passage datasets)
    section: str | None = None


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
    split: str | None = None  # "dev" or "heldout" (Asia Cement); None: no split
    required_facts: tuple[str, ...] = ()
    follow_up_type: str | None = None  # Asia Cement follow-ups only
    # Reviewer-only supporting passages: stored for review, never mapped or scored.
    corroborating_evidence: tuple[Evidence, ...] = ()

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
    # Source key -> the original file name a workspace document must have. None: the key
    # is the file name (Northstar).
    source_files: Mapping[str, str] | None = None
    # How retrieval relevance is judged: "source" (the expected document) or "passage"
    # (a chunk holding the expected evidence quotation; app/evaluation/passages.py).
    relevance: Literal["source", "passage"] = "source"
    # The split a run evaluates (None: every question). Other splits are never run.
    run_split: str | None = None

    @property
    def answerable(self) -> int:
        return sum(q.answerable for q in self.questions)

    @property
    def unanswerable(self) -> int:
        return len(self.questions) - self.answerable

    def file_name(self, key: str) -> str:
        return self.source_files[key] if self.source_files else key

    @property
    def run_questions(self) -> tuple[DatasetQuestion, ...]:
        """The questions a run evaluates: the run split's, or all of them."""
        if self.run_split is None:
            return self.questions
        return tuple(q for q in self.questions if q.split == self.run_split)

    @property
    def excluded_questions(self) -> int:
        """Questions never run automatically (the held-out split)."""
        return len(self.questions) - len(self.run_questions)


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


# --- Asia Cement real-world benchmark ----------------------------------------------------

ASIA_RUN_SPLIT = "dev"  # the held-out split is never run automatically
_FILE_SHA256 = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class _AsiaFormat:
    """What differs between the Asia Cement benchmark and its follow-up extension."""

    splits: tuple[str, ...]  # the splits it uses; each must hold questions
    ids: re.Pattern[str]
    follow_ups_only: bool  # every question is a follow-up with a follow_up_type


_ASIA_FORMATS = {
    ASIA_CEMENT_ID: _AsiaFormat(("dev", "heldout"), re.compile(r"^AC-\d{3}$"), False),
    # The public development split of the benchmark above.
    ASIA_CEMENT_DEV_ID: _AsiaFormat(("dev",), re.compile(r"^AC-\d{3}$"), False),
    # Development only: AC-017/AC-019 (verbatim copies) plus the new FU- questions.
    ASIA_FOLLOWUPS_ID: _AsiaFormat(("dev",), re.compile(r"^(AC|FU)-\d{3}$"), True),
}
ASIA_SPLITS = _ASIA_FORMATS[ASIA_CEMENT_ID].splits


class _AsiaDocumentIn(_Strict):
    key: str
    filename: str = Field(min_length=1, max_length=255)
    title: str
    version_or_date: str
    pdf_pages: int = Field(ge=1)
    sha256: str
    bytes: int = Field(ge=1)


class _AsiaEvidenceIn(_Strict):
    source_key: str
    pdf_page: int = Field(ge=1)
    section: str | None = None
    quote: str = Field(min_length=10)


class _AsiaQuestionIn(_Strict):
    id: str = Field(pattern=r"^(AC|FU)-\d{3}$")
    category: Category
    split: Literal["dev", "heldout"]
    question: str = Field(min_length=3, max_length=500)
    previous_question: str | None = Field(default=None, max_length=500)
    expected_behavior: Literal["answer", "abstain"]
    reference_answer: str
    required_facts: list[str]
    expected_source_keys: list[str]
    evidence: list[_AsiaEvidenceIn]
    notes: str | None = None
    # The follow-up extension only (optional, so the parent file parses unchanged).
    follow_up_type: FollowUpType | None = None
    corroborating_evidence: list[_AsiaEvidenceIn] = Field(default_factory=list)
    origin: str | None = None  # where a copied question comes from


class _AsiaDatasetIn(_Strict):
    name: Literal["asia-cement-realworld", "asia-cement-realworld-dev", "asia-cement-followups"]
    version: str = Field(min_length=1, max_length=20)
    created_date: str
    status: str
    purpose: str
    compatibility: str
    source_of_truth: str
    documents: list[_AsiaDocumentIn]
    questions: list[_AsiaQuestionIn]
    scoring_guidance: dict[str, str]
    extends: dict[str, str] | None = None
    follow_up_types: dict[str, str] | None = None


def _asia_problems(dataset: _AsiaDatasetIn) -> list[str]:
    problems = []
    form = _ASIA_FORMATS[dataset.name]
    keys = [d.key for d in dataset.documents]
    if not keys:
        problems.append("the benchmark names no documents")
    if len(set(keys)) != len(keys):
        problems.append("document keys repeat")
    if len({d.filename for d in dataset.documents}) != len(keys):
        problems.append("document file names repeat")
    for document in dataset.documents:
        if not _KEY.match(document.key):
            problems.append(f"document {document.key!r}: keys are short lower-case identifiers")
        if not _FILE_SHA256.match(document.sha256):
            problems.append(f"document {document.key!r} needs a lower-case hex SHA-256")
    known = set(keys)
    seen: set[str] = set()
    for q in dataset.questions:
        where = f"question {q.id}"
        if not form.ids.match(q.id):
            problems.append(f"{where}: not a question id {dataset.name} uses")
        if q.id in seen:
            problems.append(f"{where}: duplicate id")
        seen.add(q.id)
        if q.split not in form.splits:
            problems.append(f"{where}: {dataset.name} has no {q.split} split")
        cited = {e.source_key for e in q.evidence}
        corroborating = {e.source_key for e in q.corroborating_evidence}
        for key in sorted((set(q.expected_source_keys) | cited | corroborating) - known):
            problems.append(f"{where}: unknown source key {key!r}")
        if len(set(q.expected_source_keys)) != len(q.expected_source_keys):
            problems.append(f"{where}: repeated expected source")
        # A follow-up is a follow_up question, or (extension) an unanswerable one with a type.
        follow_up = q.category == "follow_up" or q.follow_up_type is not None
        if (q.previous_question is not None) != follow_up:
            problems.append(f"{where}: only follow_up questions have a previous_question")
        if form.follow_ups_only and (q.previous_question is None or q.follow_up_type is None):
            problems.append(f"{where}: every question here is a follow-up with a follow_up_type")
        answerable = q.category in ANSWERABLE_CATEGORIES
        if answerable != (q.expected_behavior == "answer"):
            problems.append(f"{where}: category {q.category} contradicts {q.expected_behavior}")
        if answerable:
            if not q.expected_source_keys or not q.evidence:
                problems.append(f"{where}: an answerable question needs sources and evidence")
            if not cited <= set(q.expected_source_keys):
                problems.append(f"{where}: evidence from a source that isn't expected")
        elif q.expected_source_keys or q.evidence or q.corroborating_evidence:
            problems.append(f"{where}: an abstention question has no sources or evidence")
        mandatory = {_normalized(e.quote) for e in q.evidence}
        if any(_normalized(e.quote) in mandatory for e in q.corroborating_evidence):
            problems.append(f"{where}: corroborating evidence repeats a scored quotation")
    for split in form.splits:
        if not any(q.split == split for q in dataset.questions):
            problems.append(f"the {split} split is empty")
    return problems


def _evidence(items: list[_AsiaEvidenceIn]) -> tuple[Evidence, ...]:
    return tuple(Evidence(e.source_key, e.quote, page=e.pdf_page, section=e.section) for e in items)


def parse_asia_cement(raw: bytes, *, name: str | None = None) -> Dataset:
    """An Asia Cement benchmark file (or its follow-up extension), validated against its
    own format and rules. `name`, if given, is the dataset the file must be."""
    try:
        parsed = _AsiaDatasetIn.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as exc:
        raise DatasetError(f"The Asia Cement benchmark is not valid: {exc}") from exc
    if name is not None and parsed.name != name:
        raise DatasetError(f"Expected the {name} benchmark, found {parsed.name}")
    problems = _asia_problems(parsed)
    if problems:
        raise DatasetError("The Asia Cement benchmark breaks its rules: " + "; ".join(problems))
    return Dataset(
        name=parsed.name,
        version=parsed.version,
        description=parsed.purpose,
        corpus_directory="asia-cement (user-uploaded PDFs)",
        source_hashes={d.key: d.sha256 for d in parsed.documents},
        source_files={d.key: d.filename for d in parsed.documents},
        relevance="passage",
        run_split=ASIA_RUN_SPLIT,
        questions=tuple(
            DatasetQuestion(
                key=q.id,
                position=position,
                category=q.category,
                question=q.question,
                previous_question=q.previous_question,
                expected_source_keys=tuple(q.expected_source_keys),
                expected_answer=q.reference_answer,
                evidence=_evidence(q.evidence),
                absent_terms=(),
                notes=q.notes,
                split=q.split,
                required_facts=tuple(q.required_facts),
                follow_up_type=q.follow_up_type,
                corroborating_evidence=_evidence(q.corroborating_evidence),
            )
            for position, q in enumerate(parsed.questions, start=1)
        ),
        sha256=hashlib.sha256(raw).hexdigest(),
    )


@lru_cache
def load_asia_cement(path: Path = ASIA_CEMENT_DATASET) -> Dataset:
    """The full benchmark (private: not in the repository, so possibly not installed)."""
    if not path.is_file():
        raise DatasetUnavailable(
            "The full Asia Cement benchmark (asia-cement-realworld) is private and isn't "
            "installed here. Use asia-cement-realworld-dev, its public development split."
        )
    return parse_asia_cement(path.read_bytes(), name=ASIA_CEMENT_ID)


@lru_cache
def load_asia_cement_dev(path: Path = ASIA_CEMENT_DEV_DATASET) -> Dataset:
    return parse_asia_cement(path.read_bytes(), name=ASIA_CEMENT_DEV_ID)


@lru_cache
def load_asia_followups(path: Path = ASIA_FOLLOWUPS_DATASET) -> Dataset:
    return parse_asia_cement(path.read_bytes(), name=ASIA_FOLLOWUPS_ID)


# --- which dataset a workspace evaluates -------------------------------------------------

DATASETS: dict[str, Callable[[], Dataset]] = {
    NORTHSTAR_ID: load_dataset,
    ASIA_CEMENT_ID: load_asia_cement,
    ASIA_CEMENT_DEV_ID: load_asia_cement_dev,
    ASIA_FOLLOWUPS_ID: load_asia_followups,
}


def dataset_id_for(workspace_id: uuid.UUID, assignments: Mapping[uuid.UUID, str]) -> str:
    """The workspace's dataset id: its configured one, or Northstar."""
    return assignments.get(workspace_id, NORTHSTAR_ID)


def dataset_for(workspace_id: uuid.UUID, assignments: Mapping[uuid.UUID, str]) -> Dataset:
    """The dataset this workspace evaluates (server-side; never chosen by the request)."""
    return DATASETS[dataset_id_for(workspace_id, assignments)]()
