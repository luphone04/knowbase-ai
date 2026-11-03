"""Follow-up questions without an extra Gemini call (SPEC.md §9.5).

"And the warranty?" on its own retrieves poorly. When the new question is
short, or refers back ("it", "that", "what about", ...), the retrieval query
becomes the previous user question plus the current one. Only one previous
question is used, so history never grows without bound. The answer prompt
(M7) keeps the original question.
"""

import re

SHORT_QUESTION_WORDS = 10
MAX_QUERY_CHARS = 2000
_FOLLOW_UP = re.compile(
    r"\b(it|its|it's|that|this|those|these|they|them|their|there|"
    r"what about|how about|and if|same|also|instead)\b",
    re.IGNORECASE,
)


def is_follow_up(question: str) -> bool:
    words = question.split()
    return len(words) < SHORT_QUESTION_WORDS or bool(_FOLLOW_UP.search(question))


def contextualize(question: str, previous_question: str | None) -> str:
    """The text to retrieve with: the question, or previous + question for a follow-up."""
    current = " ".join(question.split())
    previous = " ".join((previous_question or "").split())
    if not previous or not is_follow_up(current):
        return current[:MAX_QUERY_CHARS]
    return f"{previous} {current}"[-MAX_QUERY_CHARS:]
