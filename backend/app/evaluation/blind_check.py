"""Blind held-out duplicate check (Experiment 006).

Before a new development dataset is frozen, it must not duplicate the sealed held-out
questions of asia-cement-realworld. This check compares them without revealing them:
the result holds counts and a pass/fail only, never a held-out question, quotation,
page, section or id, and never which candidate matched.

Pre-registered rules (fixed before the check was first run):

- A **duplicate question**: a candidate question or previous question that is the same
  text as a held-out question or previous question once normalized (case, whitespace,
  quote and dash forms; `passages.normalize`), or is substantially identical to one
  (`difflib` similarity >= 0.9 on the normalized text).
- A **duplicate quotation**: a candidate's mandatory evidence quotation that is the same
  as a held-out evidence quotation once normalized, the same with all whitespace
  removed, or substantially identical (similarity >= 0.9).
- Sharing a topic, a document, a page or a section is not a duplicate, and nothing
  short of the rules above counts. Corroborating evidence isn't scored, so it isn't
  compared.

Questions are never tuned against this check: it is run once per dataset version, and
its only output is the report below.
"""

from dataclasses import dataclass
from difflib import SequenceMatcher

from app.evaluation.dataset import Dataset
from app.evaluation.passages import compact, normalize

SIMILARITY = 0.9
HELD_OUT = "heldout"


@dataclass(frozen=True)
class DuplicateReport:
    """Counts only: nothing here identifies a held-out question or its evidence."""

    candidate_questions: int
    held_out_questions: int
    duplicate_questions: int  # candidate (previous) questions matching a held-out one
    duplicate_quotations: int  # candidate quotations matching a held-out quotation

    @property
    def passed(self) -> bool:
        return self.duplicate_questions == 0 and self.duplicate_quotations == 0


def _same(a: str, b: str) -> bool:
    left, right = normalize(a), normalize(b)
    if left == right or compact(a) == compact(b):
        return True
    return SequenceMatcher(None, left, right, autojunk=False).ratio() >= SIMILARITY


def heldout_duplicates(candidate: Dataset, protected: Dataset) -> DuplicateReport:
    """Compare `candidate`'s questions with `protected`'s held-out split, blind."""
    held_out = [q for q in protected.questions if q.split == HELD_OUT]
    held_texts = [t for q in held_out for t in (q.question, q.previous_question) if t]
    held_quotes = [e.quote for q in held_out for e in q.evidence]
    questions = sum(
        any(_same(text, other) for other in held_texts)
        for q in candidate.questions
        for text in (q.question, q.previous_question)
        if text
    )
    quotations = sum(
        any(_same(e.quote, other) for other in held_quotes)
        for q in candidate.questions
        for e in q.evidence
    )
    return DuplicateReport(
        candidate_questions=len(candidate.questions),
        held_out_questions=len(held_out),
        duplicate_questions=questions,
        duplicate_quotations=quotations,
    )
