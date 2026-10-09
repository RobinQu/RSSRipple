"""Security response headers middleware.

Adds baseline hardening headers to every HTTP response (including auth 401
rejections and 500 error bodies, since it sits outside the auth/retry layers):

- ``X-Content-Type-Options: nosniff``
- ``X-Frame-Options: DENY`` (mirrored by CSP ``frame-ancestors 'none'``)
- ``Referrer-Policy: no-referrer``
- ``Content-Security-Policy`` — two profiles:
  - docs/schema paths (``/docs``, ``/redoc``, ``/openapi.json``) get a relaxed
    policy allowing the Swagger/ReDoc CDN assets and their inline bootstrap
    script, otherwise the API browser breaks;
  - everything else (SPA shell, hashed assets, API JSON) gets a self-only
    policy. JSON responses are inert under CSP, so the strict value is safe
    for the whole API.
- ``Strict-Transport-Security`` only when ``SECURITY_HSTS_ENABLED`` is truthy.
  HSTS is meaningless (and harmful to plain-HTTP LAN access) unless TLS is
  terminated at a reverse proxy, so it is opt-in. The flag is read from the
  environment directly — keep it out of ``app.config.Settings`` until that
  module's owner merges it.

TrustedHostMiddleware is deliberately NOT installed: RSSRipple is a
self-hosted single-admin app routinely reached by bare LAN IPs/ports, where
host-header allowlisting only causes false rejections while adding little
against DNS rebinding (the API is already behind cookie/API-key auth with
origin checks). Revisit if multi-tenant exposure becomes a goal.
"""

from __future__ import annotations

import os

from starlette.types import ASGIApp, Message, Receive, Scope, Send

_DOCS_PREFIXES = ("/docs", "/redoc")
_DOCS_PATHS = ("/openapi.json",)

_BASE_CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; font-src 'self' data:; connect-src 'self'; "
    "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
)
_DOCS_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
)

_HSTS_VALUE = b"max-age=31536000; includeSubDomains"


def _hsts_enabled() -> bool:
    return os.environ.get("SECURITY_HSTS_ENABLED", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def _csp_for(path: str) -> str:
    if path in _DOCS_PATHS or any(
        path == p or path.startswith(p + "/") for p in _DOCS_PREFIXES
    ):
        return _DOCS_CSP
    return _BASE_CSP


class SecurityHeadersMiddleware:
    """Pure ASGI middleware appending security headers to all responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        static_headers = [
            (b"x-content-type-options", b"nosniff"),
            (b"x-frame-options", b"DENY"),
            (b"referrer-policy", b"no-referrer"),
            (b"content-security-policy", _csp_for(path).encode("latin-1")),
        ]
        if _hsts_enabled():
            static_headers.append((b"strict-transport-security", _HSTS_VALUE))

        async def send_with_security_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.extend(static_headers)
            await send(message)

        await self.app(scope, receive, send_with_security_headers)
