"""Run untrusted-document parsing in a child process with time and memory limits.

Parsers (pypdf, python-docx) run third-party code over attacker-controlled
bytes. In-process, a pathological file could hold the only worker for hours
or exhaust its memory (audit H2/H3). Here each parse runs in a separate
process:

- **Time:** the parent waits at most `timeout` seconds, then *kills* the child,
  so a stuck parser stops consuming CPU instead of being merely abandoned.
- **Memory:** the child caps its own address space (RLIMIT_AS; enforced on
  Linux, i.e. in the containers; macOS ignores it).
- **Start method:** `forkserver`. The worker is multi-threaded (heartbeat), and
  forking a threaded process is unsafe. The fork server is a small,
  single-threaded process that preloads the parsers once, so each parse
  starts in milliseconds.

The child sends back the parsed document or a classified failure; no
exception object crosses the process boundary.
"""

import contextlib
import logging
import multiprocessing
import resource
from collections.abc import Callable
from multiprocessing.connection import Connection
from typing import Any

from app.ingestion.parsers import ParsedDocument, ParseLimits, UnsupportedDocument, parse_document

logger = logging.getLogger(__name__)

_CONTEXT = multiprocessing.get_context("forkserver")
_CONTEXT.set_forkserver_preload(["app.ingestion.parsers"])

ParseFunction = Callable[[str, bytes, ParseLimits], ParsedDocument]


class ParseTimeout(Exception):
    def __init__(self, seconds: float) -> None:
        self.message = (
            f"Reading the file took longer than {seconds:g} s and was stopped "
            "(PARSE_TIMEOUT_SECONDS). The file may be malformed or too complex."
        )
        super().__init__(self.message)


class ParseResourceError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _child(
    conn: Connection,
    target: ParseFunction,
    file_type: str,
    data: bytes,
    limits: ParseLimits,
    memory_bytes: int | None,
) -> None:
    try:
        if memory_bytes:
            # Not supported on every platform (macOS); the time limit still applies.
            with contextlib.suppress(ValueError, OSError):
                resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        conn.send(("ok", target(file_type, data, limits)))
    except UnsupportedDocument as exc:
        conn.send(("unsupported", exc.message))
    except MemoryError:
        conn.send(("memory", None))
    except BaseException:  # never let a parser crash the child silently
        conn.send(("error", None))
    finally:
        conn.close()


def parse_in_subprocess(
    file_type: str,
    data: bytes,
    limits: ParseLimits,
    *,
    timeout: float,
    memory_mb: int | None,
    target: ParseFunction = parse_document,
) -> ParsedDocument:
    """`target(file_type, data, limits)` in a child process; raise on timeout or failure."""
    receiver, sender = _CONTEXT.Pipe(duplex=False)
    process = _CONTEXT.Process(
        target=_child,
        args=(
            sender,
            target,
            file_type,
            data,
            limits,
            memory_mb * 1024 * 1024 if memory_mb else None,
        ),
        daemon=True,
    )
    process.start()
    sender.close()
    try:
        if not receiver.poll(timeout):
            logger.warning(
                "Parsing %s timed out after %.0fs; killing pid %s", file_type, timeout, process.pid
            )
            raise ParseTimeout(timeout)
        try:
            status, payload = receiver.recv()
        except EOFError as exc:  # the child died without answering (OOM kill, crash)
            raise ParseResourceError(
                "Reading the file stopped unexpectedly, possibly because it needed more "
                "memory than allowed (PARSE_MEMORY_LIMIT_MB)."
            ) from exc
    finally:
        if process.is_alive():
            process.kill()
        process.join(5)
        receiver.close()
    return _result(status, payload)


def _result(status: str, payload: Any) -> ParsedDocument:
    if status == "ok":
        assert isinstance(payload, ParsedDocument)
        return payload
    if status == "unsupported":
        raise UnsupportedDocument(str(payload))
    if status == "memory":
        raise ParseResourceError(
            "Reading the file used too much memory and was stopped (PARSE_MEMORY_LIMIT_MB)."
        )
    raise UnsupportedDocument("The file could not be read; it may be damaged.")
