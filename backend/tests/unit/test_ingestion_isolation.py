"""Untrusted parsing runs in a killable child process (audit H2/H3)."""

import multiprocessing
import sys
import time

import pytest

from app.ingestion.isolation import ParseResourceError, ParseTimeout, parse_in_subprocess
from app.ingestion.parsers import ParseLimits, UnsupportedDocument
from tests.fakes.parsers import allocate, crash, hang


def test_a_normal_document_parses_in_the_child() -> None:
    parsed = parse_in_subprocess(
        "md", b"# Returns\n\nThirty days.", ParseLimits(), timeout=30, memory_mb=1024
    )
    assert [(b.kind, b.text) for b in parsed.blocks] == [
        ("heading", "Returns"),
        ("paragraph", "Thirty days."),
    ]


def test_parser_errors_and_limits_come_back_as_clear_messages() -> None:
    with pytest.raises(UnsupportedDocument, match="more than 10,000 characters"):
        parse_in_subprocess(
            "txt", b"x" * 20_000, ParseLimits(max_chars=10_000), timeout=30, memory_mb=1024
        )
    with pytest.raises(UnsupportedDocument, match="could not be read"):
        parse_in_subprocess("txt", b"x", ParseLimits(), timeout=30, memory_mb=1024, target=crash)


def test_a_hung_parser_is_killed_at_the_timeout() -> None:
    started = time.perf_counter()
    with pytest.raises(ParseTimeout, match="longer than 1 s"):
        parse_in_subprocess("txt", b"x", ParseLimits(), timeout=1, memory_mb=1024, target=hang)
    assert time.perf_counter() - started < 5
    # Killed, not abandoned: no child process keeps running.
    assert multiprocessing.active_children() == []


@pytest.mark.skipif(sys.platform != "linux", reason="RLIMIT_AS is enforced on Linux only")
def test_the_memory_cap_stops_a_parser_that_allocates_too_much() -> None:
    with pytest.raises((ParseResourceError, UnsupportedDocument)):
        parse_in_subprocess("txt", b"x", ParseLimits(), timeout=30, memory_mb=512, target=allocate)
