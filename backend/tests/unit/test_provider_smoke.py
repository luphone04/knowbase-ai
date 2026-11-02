"""The live smoke CLI's logic and exit codes, driven by the fake client (no network)."""

import io
import re

import pytest
from google.genai import types

from app.providers import smoke
from tests.fakes.gemini import (
    Harness,
    api_error,
    daily_quota_error,
    embedding_response,
    generation_response,
    invalid_key_error,
    make_harness,
    raw_vector,
    unavailable_error,
)


def semantic_embeddings(contents: list[str]) -> types.EmbedContentResponse:
    """Query vectors near the returns text, so the smoke test's sanity check can pass."""
    vectors = []
    for text in contents:
        if "laptop I bought" in text:  # the query
            vectors.append(
                [a + 0.1 * b for a, b in zip(raw_vector("returns"), raw_vector("q"), strict=True)]
            )
        elif "returns" in text:
            vectors.append(raw_vector("returns"))
        else:
            vectors.append(raw_vector("shipping"))
    return embedding_response(vectors)


def run(**script: list[object]) -> tuple[int, str, Harness]:
    h = make_harness(max_retries=1)
    h.client.models.generate_script.extend(script.get("generate", [generation_response("ready")]))
    h.client.models.default_embed = semantic_embeddings
    h.client.models.embed_script.extend(script.get("embed", []))
    out = io.StringIO()
    code = smoke.run_smoke(h.generation, h.embedding, out)
    return code, out.getvalue(), h


def test_passing_run_makes_exactly_three_requests() -> None:
    code, output, h = run()
    assert code == 0
    assert "RESULT: PASS" in output
    assert [c.method for c in h.calls] == ["generate_content", "embed_content", "embed_content"]
    assert [c.config.task_type for c in h.calls[1:]] == ["RETRIEVAL_DOCUMENT", "RETRIEVAL_QUERY"]


def test_output_never_includes_vectors_or_generated_text() -> None:
    _, output, _ = run(generate=[generation_response("SECRET-GENERATED-TEXT")])
    assert "SECRET-GENERATED-TEXT" not in output
    assert not re.search(r"(-?0\.\d+,\s*){5,}", output)  # no raw vector dumps


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (daily_quota_error(), 3),
        (unavailable_error(), 4),
        (invalid_key_error(), 5),
        (api_error(404, "NOT_FOUND"), 5),
        (api_error(400, "INVALID_ARGUMENT"), 1),
    ],
    ids=["daily-quota", "unavailable", "bad-key", "unknown-model", "bad-request"],
)
def test_failures_map_to_distinct_exit_codes(error: Exception, code: int) -> None:
    exit_code, output, _ = run(generate=[error, error])
    assert exit_code == code
    assert "RESULT: PASS" not in output


def test_incomplete_generation_is_a_failure() -> None:
    code, output, _ = run(
        generate=[generation_response("partial", finish_reason=types.FinishReason.MAX_TOKENS)]
    )
    assert code == 1 and "MAX_TOKENS" in output


def test_without_a_key_nothing_is_sent_and_verification_stays_pending() -> None:
    out = io.StringIO()
    assert smoke.main(out=out) == 2  # tests run with GEMINI_API_KEY=""
    assert "PENDING" in out.getvalue()
