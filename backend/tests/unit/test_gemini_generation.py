"""GeminiGenerationProvider against a scripted SDK client (no network)."""

import base64
import logging

import httpx
import pytest
from google import genai
from google.genai import types

from app.providers.errors import IncompleteGeneration
from app.providers.gemini import GeminiCalls, GeminiGenerationProvider
from app.providers.health import ProviderHealth
from app.providers.retry import RetryPolicy
from tests.fakes.gemini import generation_response, make_harness

PROMPT = "PRIVATE-PROMPT-TEXT: summarise the returns policy"
SYSTEM = "PRIVATE-SYSTEM-INSTRUCTION"


def test_successful_generation_returns_text_usage_latency_and_attempts() -> None:
    h = make_harness()
    h.client.models.generate_script.append(
        generation_response("Returns are accepted within 30 days.", usage=(40, 8, 5, 53))
    )
    result = h.generation.generate(PROMPT, system_instruction=SYSTEM)

    assert result.text == "Returns are accepted within 30 days."
    assert result.finish_reason == "STOP"
    assert result.model == "gemini-3.5-flash-lite"
    assert (result.usage.prompt_tokens, result.usage.output_tokens) == (40, 8)
    assert (result.usage.thinking_tokens, result.usage.total_tokens) == (5, 53)
    assert isinstance(result.latency_ms, int) and result.latency_ms >= 0
    assert result.attempts == 1
    assert h.health.snapshot().last_success_at is not None


def test_request_uses_configured_model_thinking_level_and_token_limit() -> None:
    h = make_harness(chat_model="configured-chat-model")
    h.client.models.generate_script.append(generation_response())
    h.generation.generate(PROMPT, system_instruction=SYSTEM)

    (call,) = h.calls
    assert call.method == "generate_content"
    assert call.model == "configured-chat-model"
    assert call.contents == PROMPT
    config: types.GenerateContentConfig = call.config
    assert config.system_instruction == SYSTEM
    assert config.max_output_tokens == 1200
    assert config.thinking_config is not None
    assert config.thinking_config.thinking_level == types.ThinkingLevel.LOW
    assert config.thinking_config.thinking_budget is None  # deprecated for Gemini 3


def test_request_has_no_paid_tools_or_deprecated_sampling_parameters() -> None:
    h = make_harness()
    h.client.models.generate_script.append(generation_response())
    h.generation.generate(PROMPT)
    config: types.GenerateContentConfig = h.calls[0].config
    assert config.tools is None  # no Google Search grounding / File Search
    assert (config.temperature, config.top_p, config.top_k) == (None, None, None)
    assert config.candidate_count is None
    assert config.automatic_function_calling is not None
    assert config.automatic_function_calling.disable is True  # no hidden extra calls


def test_per_call_token_limit_override() -> None:
    h = make_harness()
    h.client.models.generate_script.append(generation_response())
    h.generation.generate(PROMPT, max_output_tokens=300)
    assert h.calls[0].config.max_output_tokens == 300


def test_thought_parts_are_not_part_of_the_answer() -> None:
    h = make_harness()
    h.client.models.generate_script.append(
        generation_response("Final answer.", thought="internal reasoning")
    )
    assert h.generation.generate(PROMPT).text == "Final answer."


@pytest.mark.parametrize(
    "finish_reason",
    [
        types.FinishReason.MAX_TOKENS,
        types.FinishReason.SAFETY,
        types.FinishReason.RECITATION,
        types.FinishReason.OTHER,
        types.FinishReason.FINISH_REASON_UNSPECIFIED,
    ],
)
def test_non_stop_finish_reason_is_incomplete_even_with_partial_text(
    finish_reason: types.FinishReason,
) -> None:
    h = make_harness()
    h.client.models.generate_script.append(
        generation_response(
            "Returns are accepted wi", finish_reason=finish_reason, usage=(40, 1190, 10, 1240)
        )
    )
    with pytest.raises(IncompleteGeneration) as excinfo:
        h.generation.generate(PROMPT)
    error = excinfo.value
    assert error.finish_reason == finish_reason.name
    assert error.usage.output_tokens == 1190  # kept for observability
    assert "Returns are accepted wi" not in str(error)  # partial text is never surfaced


def test_missing_finish_reason_is_incomplete() -> None:
    h = make_harness()
    h.client.models.generate_script.append(generation_response("text", finish_reason=None))
    with pytest.raises(IncompleteGeneration, match="UNSPECIFIED"):
        h.generation.generate(PROMPT)


@pytest.mark.parametrize("text", [None, "", "   \n "])
def test_empty_answer_with_stop_is_incomplete(text: str | None) -> None:
    h = make_harness()
    h.client.models.generate_script.append(generation_response(text))
    with pytest.raises(IncompleteGeneration) as excinfo:
        h.generation.generate(PROMPT)
    assert excinfo.value.finish_reason == "EMPTY_RESPONSE"


def test_only_thoughts_and_no_answer_is_incomplete() -> None:
    h = make_harness()
    h.client.models.generate_script.append(generation_response(None, thought="thinking..."))
    with pytest.raises(IncompleteGeneration, match="EMPTY_RESPONSE"):
        h.generation.generate(PROMPT)


def test_no_candidates_is_incomplete() -> None:
    h = make_harness()
    h.client.models.generate_script.append(generation_response(no_candidates=True))
    with pytest.raises(IncompleteGeneration) as excinfo:
        h.generation.generate(PROMPT)
    assert excinfo.value.finish_reason == "NO_CANDIDATES"


def test_blocked_prompt_is_incomplete() -> None:
    h = make_harness()
    h.client.models.generate_script.append(
        generation_response(no_candidates=True, block_reason=types.BlockedReason.SAFETY)
    )
    with pytest.raises(IncompleteGeneration) as excinfo:
        h.generation.generate(PROMPT)
    assert excinfo.value.finish_reason == "BLOCKED_SAFETY"


def test_missing_usage_metadata_is_tolerated() -> None:
    h = make_harness()
    h.client.models.generate_script.append(generation_response(usage=None))
    usage = h.generation.generate(PROMPT).usage
    assert usage.prompt_tokens is None and usage.total_tokens is None


@pytest.mark.parametrize("prompt", ["", "   "])
def test_empty_prompt_is_rejected_without_an_api_call(prompt: str) -> None:
    h = make_harness()
    with pytest.raises(ValueError):
        h.generation.generate(prompt)
    assert h.calls == []


def test_logs_have_usage_and_latency_but_never_the_prompt(caplog: pytest.LogCaptureFixture) -> None:
    h = make_harness()
    h.client.models.generate_script.append(
        generation_response("Answer text here", usage=(9, 3, 0, 12))
    )
    with caplog.at_level(logging.DEBUG):
        h.generation.generate(PROMPT, system_instruction=SYSTEM)
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert "model=gemini-3.5-flash-lite" in logged and "latency_ms=" in logged
    assert "total_tokens=12" in logged
    for private in ("PRIVATE-PROMPT-TEXT", "PRIVATE-SYSTEM-INSTRUCTION", "Answer text here"):
        assert private not in logged


def test_one_generate_call_is_exactly_one_http_request() -> None:
    """The real SDK over a mock transport: no hidden continuation requests."""
    requests: list[httpx.Request] = []

    def gemini(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        candidate: dict[str, object] = {
            "content": {"role": "model", "parts": [{"text": "part "}]},
            "finishReason": "STOP",
        }
        if len(requests) < 4:  # bounded, so a continuing client ends instead of looping
            candidate["finishReason"] = "CONTINUATION"
            candidate["continuationToken"] = base64.b64encode(b"resume-here").decode()
        return httpx.Response(200, json={"candidates": [candidate]})

    client = genai.Client(
        api_key="test-gemini-key-not-real",
        vertexai=False,
        http_options=types.HttpOptions(
            httpx_client=httpx.Client(transport=httpx.MockTransport(gemini)),
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    )
    provider = GeminiGenerationProvider(
        client,
        model="gemini-3.5-flash-lite",
        thinking_level="low",
        max_output_tokens=1200,
        calls=GeminiCalls(RetryPolicy(max_retries=0), health=ProviderHealth()),
    )
    with pytest.raises(IncompleteGeneration) as excinfo:
        provider.generate(PROMPT)
    assert excinfo.value.finish_reason == "CONTINUATION"
    assert len(requests) == 1
