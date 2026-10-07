"""Reject browser state changes from untrusted origins before any side effect."""

import httpx
from starlette.datastructures import URL, Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import settings


def _origin(value: str, *, referer: bool = False) -> tuple[str, str, int] | None:
    try:
        url = httpx.URL(value)
        if (url.scheme not in {"http", "https"} or not url.host or url.userinfo
                or any(ord(c) < 32 or ord(c) == 127 for c in value)
                or (not referer and (url.path != "/" or url.query or url.fragment))):
            return None
        return url.scheme, url.raw_host.decode("ascii"), url.port or (443 if url.scheme == "https" else 80)
    except (httpx.InvalidURL, ValueError):
        return None


class BrowserOriginMiddleware:
    """CORS controls reads; this independently protects unsafe API requests.

    Missing browser provenance remains valid for existing programmatic clients.
    Explicit untrusted provenance is rejected even when API-key headers exist:
    merely supplying a header must not bypass cookie-authenticated protection.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (scope["type"] != "http" or scope["method"] in {"GET", "HEAD", "OPTIONS"}
                or not scope.get("path", "").startswith("/api/v1/")):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        allowed = {_origin(str(URL(scope=scope)), referer=True)}
        allowed.update(_origin(value) for value in settings.cors_allowed_origins)
        allowed.discard(None)
        origins = headers.getlist("origin")
        if origins:
            permitted = len(origins) == 1 and _origin(origins[0]) in allowed
        elif referers := headers.getlist("referer"):
            permitted = len(referers) == 1 and _origin(referers[0], referer=True) in allowed
        else:
            permitted = headers.get("sec-fetch-site", "").lower() not in {"cross-site", "same-site"}
        if not permitted:
            response = JSONResponse(status_code=403, content={
                "success": False, "data": None,
                "error": {"code": "FORBIDDEN", "message": "request origin is not allowed"}, "meta": {},
            })
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
