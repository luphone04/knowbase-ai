"""CSRF defence: state-changing requests must come from an allowed origin.

The auth cookie is SameSite=Lax, which stops cross-site requests from carrying
it. But every localhost port is the same "site", so any page served from
another local port could still send it. Checking the Origin header closes that
gap.

- Unsafe methods (POST, PUT, PATCH, DELETE) with an Origin header must match
  the frontend origin exactly, or the API's own origin (for /docs).
- Without an Origin header, Sec-Fetch-Site (sent by modern browsers) must not
  say the request is cross-site or same-site. Requests with neither header come
  from non-browser clients such as curl, which carry no ambient browser cookies.
- "Origin: null" (sandboxed frames, file://) is rejected.
"""

from collections.abc import Iterable

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.errors import error_response

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})


class OriginCheckMiddleware:
    def __init__(self, app: ASGIApp, allowed_origins: Iterable[str]) -> None:
        self.app = app
        self.allowed_origins = frozenset(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope["method"] in UNSAFE_METHODS:
            headers = Headers(scope=scope)
            if not self._is_allowed(scope, headers):
                response = error_response(
                    403, "origin_not_allowed", "Cross-origin request blocked."
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _is_allowed(self, scope: Scope, headers: Headers) -> bool:
        origin = headers.get("origin")
        if origin is not None:
            origin = origin.lower()
            own_origin = f"{scope['scheme']}://{headers.get('host', '')}".lower()
            return origin in self.allowed_origins or origin == own_origin
        fetch_site = headers.get("sec-fetch-site")
        return fetch_site is None or fetch_site in _ALLOWED_FETCH_SITES
