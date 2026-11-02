"""Reject oversized request bodies before they are read (413).

FastAPI parses multipart uploads before the endpoint runs, so a size check in
the endpoint alone would come too late: the whole body would already be on
disk. This middleware refuses a declared Content-Length over the limit, and
counts streamed bytes when no length is declared (chunked uploads).
"""

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import error_response


class _TooLarge(Exception):
    pass


class BodySizeLimitMiddleware:
    def __init__(
        self, app: ASGIApp, *, default_limit: int, path_limits: dict[str, int] | None = None
    ) -> None:
        self.app = app
        self.default_limit = default_limit
        self.path_limits = path_limits or {}

    def _limit_for(self, path: str) -> int:
        for prefix, limit in self.path_limits.items():
            if path.startswith(prefix):
                return limit
        return self.default_limit

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self._limit_for(scope["path"])
        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._reject(scope, receive, send, limit)
            return

        received = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _TooLarge()
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except _TooLarge:
            if not response_started:
                await self._reject(scope, receive, send, limit)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        response = error_response(
            413,
            "payload_too_large",
            f"The request body is larger than the {limit // (1024 * 1024) or 1} MB limit.",
        )
        await response(scope, receive, send)
