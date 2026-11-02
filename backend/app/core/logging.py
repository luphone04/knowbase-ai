import logging
from collections.abc import Iterable
from typing import TextIO

REDACTED = "[REDACTED]"
_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


class RedactingFormatter(logging.Formatter):
    """Masks known secret values anywhere in a formatted record, tracebacks included.

    This is defence in depth: code should never log secrets in the first place.
    The formatter catches anything that slips through, including from
    third-party libraries such as the Gemini SDK.
    """

    def __init__(self, fmt: str, secrets: Iterable[str]) -> None:
        super().__init__(fmt)
        # Longest first, so a secret that contains another is fully masked.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text


def configure_logging(
    level: str, secrets: Iterable[str] = (), *, stream: TextIO | None = None
) -> None:
    logging.basicConfig(level=level, format=_FORMAT, force=True, stream=stream)
    formatter = RedactingFormatter(_FORMAT, secrets)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)
