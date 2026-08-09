"""Authentication middleware.

Gate rules:

- ``settings.auth_enabled`` is False → everything passes.
- ``/api/v1/auth/*`` → always open (login/status endpoints).
- ``/api/v1/*`` and ``/posters/*`` → require a valid API key (env bootstrap
  key or a ``api_keys`` row, presented via ``Authorization: Bearer`` or
  ``X-API-Key``) OR a valid session cookie (issued by ``/api/v1/auth/otp``).
- Everything else (SPA index, ``/assets``) → open.
"""

from __future__ import annotations

import hmac
import json
import logging

from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import settings
from app.services.auth_service import (
    AUTH_COOKIE_NAME,
    check_api_key,
    get_or_create_cookie_secret,
    validate_cookie,
)

logger = logging.getLogger(__name__)

_OPEN_PREFIXES = ("/api/v1/auth/",)
_PROTECTED_PREFIXES = ("/api/v1/", "/posters/")

_UNAUTHORIZED_BODY = {
    "success": False,
    "data": None,
    "error": {"code": "UNAUTHORIZED", "message": "authentication required"},
    "meta": {},
}


class AuthMiddleware:
    """Pure ASGI middleware enforcing cookie / API-key auth on API paths."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "")
        if not settings.auth_enabled or self._is_open(path):
            await self.app(scope, receive, send)
            return

        if not await self._is_authenticated(scope):
            await self._reject(scope, receive, send)
            return

        await self.app(scope, receive, send)

    @staticmethod
    def _is_open(path: str) -> bool:
        if any(path.startswith(p) for p in _OPEN_PREFIXES):
            return True
        return not any(path.startswith(p) for p in _PROTECTED_PREFIXES)

    async def _is_authenticated(self, scope: Scope) -> bool:
        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in scope.get("headers", [])
        }

        presented_key: str | None = None
        auth_header = headers.get("authorization", "")
        if auth_header.lower().startswith("bearer "):
            presented_key = auth_header[7:].strip()
        if not presented_key:
            x_key = headers.get("x-api-key", "").strip()
            presented_key = x_key or None

        cookie_value: str | None = None
        cookie_header = headers.get("cookie", "")
        for part in cookie_header.split(";"):
            name, _, value = part.strip().partition("=")
            if name == AUTH_COOKIE_NAME:
                cookie_value = value
                break

        # Static env bootstrap key — checked without touching the DB.
        if presented_key and settings.api_key:
            if hmac.compare_digest(settings.api_key, presented_key):
                return True

        if not presented_key and not cookie_value:
            return False

        # Remaining checks need the DB (cookie secret + hashed key lookup).
        from app.database import async_session_factory

        try:
            async with async_session_factory() as session:
                if cookie_value:
                    secret = await get_or_create_cookie_secret(session)
                    if validate_cookie(cookie_value, secret):
                        return True
                if presented_key:
                    return await check_api_key(session, presented_key)
        except Exception as e:  # noqa: BLE001 — never 500 on auth plumbing
            logger.warning("auth check failed (%s); treating as unauthenticated", e)
        return False

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        body = json.dumps(_UNAUTHORIZED_BODY).encode()
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        })
        await send({"type": "http.response.body", "body": body})
