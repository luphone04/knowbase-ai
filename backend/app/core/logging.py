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

    def __init__(
        self, fmt: str, secrets: Iterable[str], *, inner: logging.Formatter | None = None
    ) -> None:
        super().__init__(fmt)
        # Longest first, so a secret that contains another is fully masked.
        self._secrets = sorted({s for s in secrets if s}, key=len, reverse=True)
        self._inner = inner  # keep another formatter's layout (uvicorn's), then mask

    def format(self, record: logging.LogRecord) -> str:
        text = self._inner.format(record) if self._inner else super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text


def configure_logging(
    level: str, secrets: Iterable[str] = (), *, stream: TextIO | None = None
) -> None:
    secrets = list(secrets)
    logging.basicConfig(level=level, format=_FORMAT, force=True, stream=stream)
    formatter = RedactingFormatter(_FORMAT, secrets)
    for handler in logging.getLogger().handlers:
        handler.setFormatter(formatter)
    # Loggers with their own handlers (uvicorn's, which log unhandled-exception
    # tracebacks) never reach the root handlers: mask theirs as well.
    for logger in list(logging.Logger.manager.loggerDict.values()):
        if not isinstance(logger, logging.Logger):
            continue
        for handler in logger.handlers:
            if not isinstance(handler.formatter, RedactingFormatter):
                handler.setFormatter(RedactingFormatter(_FORMAT, secrets, inner=handler.formatter))
