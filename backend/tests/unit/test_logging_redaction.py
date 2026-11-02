import io
import logging
from collections.abc import Iterator

import pytest

from app.config import get_settings
from app.core.logging import REDACTED, configure_logging

SECRET = "test-only-secret-key-not-real-0123456789"


@pytest.fixture
def captured_root() -> Iterator[io.StringIO]:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    stream = io.StringIO()
    configure_logging("INFO", [SECRET, "short"], stream=stream)
    yield stream
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


def test_secret_in_message_is_redacted(captured_root: io.StringIO) -> None:
    logging.getLogger("app.test").warning("calling with key=%s", SECRET)
    output = captured_root.getvalue()
    assert SECRET not in output and REDACTED in output


def test_secret_in_traceback_is_redacted(captured_root: io.StringIO) -> None:
    try:
        raise RuntimeError(f"request failed for key {SECRET}")
    except RuntimeError:
        logging.getLogger("app.test").exception("third-party failure")
    output = captured_root.getvalue()
    assert "RuntimeError" in output
    assert SECRET not in output


def test_third_party_loggers_are_redacted_too(captured_root: io.StringIO) -> None:
    logging.getLogger("google_genai._api_client").warning("header x-goog-api-key: %s", SECRET)
    assert SECRET not in captured_root.getvalue()


def test_settings_list_every_secret_for_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    get_settings.cache_clear()
    try:
        secrets = get_settings().secret_values
        assert SECRET in secrets
        assert get_settings().jwt_secret.get_secret_value() in secrets
        assert get_settings().app_secret_key.get_secret_value() in secrets
    finally:
        monkeypatch.setenv("GEMINI_API_KEY", "")
        get_settings.cache_clear()


def test_empty_key_is_not_a_redaction_pattern(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "")
    get_settings.cache_clear()
    try:
        assert "" not in get_settings().secret_values
    finally:
        get_settings.cache_clear()
