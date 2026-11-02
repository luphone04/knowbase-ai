"""Minimal live Gemini check through the application's own providers.

    docker compose exec api python -m app.providers.smoke

Exactly three logical requests: one generation, one document embedding (two
texts in one call), and one query embedding. Transient 429/5xx errors may add
bounded retries; the attempt count is printed. Output is results and
measurements only, never the key, the vectors, or the generated text.

Exit codes tell failures apart:
  0 pass · 1 implementation or response bug · 2 not configured
  3 rate limit or quota · 4 provider temporarily unavailable · 5 key/model/project rejected
"""

import math
import sys
from collections.abc import Callable
from typing import TextIO

from app.config import get_settings
from app.core.logging import configure_logging
from app.providers.errors import (
    IncompleteGeneration,
    ProviderConfigurationError,
    ProviderError,
    ProviderNotConfigured,
    ProviderQuotaExhausted,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from app.providers.factory import build_embedding_provider, build_generation_provider
from app.providers.interfaces import EmbeddingProvider, GenerationProvider

DOCUMENTS = [
    "Northstar Outfitters accepts returns of unused laptops within 30 days of delivery.",
    "Standard shipping within the country takes three to five business days.",
]
QUERY = "Can I return a laptop I bought 20 days ago?"

EXIT_CODES: list[tuple[type[ProviderError], int, str]] = [
    (ProviderNotConfigured, 2, "NOT CONFIGURED"),
    (ProviderRateLimited, 3, "RATE LIMITED (temporary free-tier limit)"),
    (ProviderQuotaExhausted, 3, "DAILY QUOTA EXHAUSTED (resets at midnight Pacific)"),
    (ProviderTimeout, 4, "PROVIDER TIMED OUT (slow right now; retry later or raise the timeout)"),
    (ProviderUnavailable, 4, "PROVIDER TEMPORARILY UNAVAILABLE"),
    (ProviderConfigurationError, 5, "CONFIGURATION REJECTED (key, model, or project)"),
]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _failure(exc: ProviderError) -> tuple[int, str]:
    if isinstance(exc, IncompleteGeneration):
        return 1, f"[FAIL] generation incomplete: finish_reason={exc.finish_reason}"
    for error_type, code, label in EXIT_CODES:
        if isinstance(exc, error_type):
            return (
                code,
                f"[{label}] {exc.message} (http={exc.status_code}, attempts={exc.attempts})",
            )
    return 1, f"[FAIL] {exc.category}: {exc.message} (http={exc.status_code})"


def run_smoke(generation: GenerationProvider, embedding: EmbeddingProvider, out: TextIO) -> int:
    """Run all three steps independently, so one failure doesn't hide the others.

    The exit code is that of the first failing step, or 1 if a check failed.
    """

    def line(text: str) -> None:
        print(text, file=out)

    def check(label: str, ok: bool, detail: str = "") -> bool:
        line(f"[{'PASS' if ok else 'FAIL'}] {label}{f' - {detail}' if detail else ''}")
        return ok

    checks_ok = True
    failures: list[int] = []

    def provider_failed(exc: ProviderError) -> None:
        code, message = _failure(exc)
        failures.append(code)
        line(message)

    line(f"1/3 generation ({generation.model})")
    try:
        result = generation.generate(
            "Reply with exactly one word: ready",
            system_instruction="You are a connectivity check. Answer with one word.",
        )
        checks_ok &= check("finish reason STOP", result.finish_reason == "STOP")
        checks_ok &= check("non-empty text", bool(result.text.strip()), f"{len(result.text)} chars")
        u = result.usage
        line(
            f"       latency_ms={result.latency_ms} attempts={result.attempts} "
            f"tokens: prompt={u.prompt_tokens} output={u.output_tokens} "
            f"thinking={u.thinking_tokens} total={u.total_tokens}"
        )
    except ProviderError as exc:
        provider_failed(exc)

    line(f"2/3 document embeddings ({embedding.model}, {len(DOCUMENTS)} texts, one request)")
    docs = None
    try:
        docs = embedding.embed_documents(DOCUMENTS)
        checks_ok &= check("task type RETRIEVAL_DOCUMENT", docs.task_type == "RETRIEVAL_DOCUMENT")
        checks_ok &= check("one embedding per input", len(docs.vectors) == len(DOCUMENTS))
        checks_ok &= check("one request", docs.requests == 1, f"attempts={docs.attempts}")
        for i, vector in enumerate(docs.vectors):
            norm = math.sqrt(_dot(vector, vector))
            checks_ok &= check(
                f"doc[{i}] {len(vector)} dims, unit length",
                len(vector) == 768 and abs(norm - 1) < 1e-6,
                f"norm={norm:.9f} latency_ms={docs.latency_ms}",
            )
    except ProviderError as exc:
        provider_failed(exc)

    line(f"3/3 query embedding ({embedding.model})")
    query = None
    try:
        query = embedding.embed_query(QUERY)
        checks_ok &= check("task type RETRIEVAL_QUERY", query.task_type == "RETRIEVAL_QUERY")
        q = query.vectors[0]
        q_norm = math.sqrt(_dot(q, q))
        checks_ok &= check(
            "768 dims, unit length",
            len(q) == 768 and abs(q_norm - 1) < 1e-6,
            f"norm={q_norm:.9f} latency_ms={query.latency_ms}",
        )
    except ProviderError as exc:
        provider_failed(exc)

    if docs is not None and query is not None:
        # Sanity check of retrieval semantics: the return-policy text should match best.
        returns, shipping = (_dot(query.vectors[0], d) for d in docs.vectors)
        checks_ok &= check(
            "query is closer to the returns text than the shipping text",
            returns > shipping,
            f"cosine returns={returns:.3f} shipping={shipping:.3f}",
        )

    if failures:
        line(f"RESULT: FAIL ({len(failures)} of 3 steps failed at the provider)")
        return failures[0]
    line("RESULT: PASS" if checks_ok else "RESULT: FAIL")
    return 0 if checks_ok else 1


def main(
    build: Callable[[], tuple[GenerationProvider, EmbeddingProvider]] | None = None,
    out: TextIO = sys.stdout,
) -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.secret_values)
    try:
        if build is not None:
            generation, embedding = build()
        else:
            generation = build_generation_provider(settings)
            embedding = build_embedding_provider(settings)
    except ProviderNotConfigured as exc:
        print(f"[NOT CONFIGURED] {exc.message}", file=out)
        print("Live verification: PENDING (nothing was sent to Gemini).", file=out)
        return 2
    return run_smoke(generation, embedding, out)


if __name__ == "__main__":
    sys.exit(main())
