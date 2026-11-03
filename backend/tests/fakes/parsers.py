"""Parser stand-ins for isolation tests. Module-level, so a child process can import them."""

import time

from app.ingestion.parsers import ParsedDocument, ParseLimits


def hang(file_type: str, data: bytes, limits: ParseLimits) -> ParsedDocument:
    """A parser stuck in a loop: only killing the process stops it."""
    while True:
        time.sleep(0.05)


def crash(file_type: str, data: bytes, limits: ParseLimits) -> ParsedDocument:
    raise RuntimeError("parser bug")


def allocate(file_type: str, data: bytes, limits: ParseLimits) -> ParsedDocument:
    """Tries to hold far more memory than the cap allows."""
    hoard = [bytearray(64 * 1024 * 1024) for _ in range(64)]  # 4 GB
    raise AssertionError(f"allocated {len(hoard)} blocks without hitting the cap")
