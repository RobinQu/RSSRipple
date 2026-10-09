"""Request-id middleware: every HTTP request gets a correlation id.

An inbound ``X-Request-ID`` header is honored when it is short and free of
control/whitespace characters (anything else is replaced — the header is
attacker-controlled and would otherwise end up in log lines). The id is
exposed three ways:

- ``request.state.request_id`` for route handlers / exception handlers,
- :func:`get_request_id` (contextvar) for code without a Request object,
- an ``X-Request-ID`` response header so clients can correlate.

The contextvar is set in the same async context that runs the rest of the
middleware stack and the route handler, so it stays visible inside the global
exception handlers and the 500 error log (see ``app.main``).
"""

from __future__ import annotations

import contextvars
import re
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

request_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)

_HEADER = b"x-request-id"
_MAX_LEN = 128
_SAFE_VALUE = re.compile(rb"^[\w:.-]+$")


def get_request_id() -> str | None:
    """Current request's correlation id, or None outside a request context."""
    return request_id_ctx.get()


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers", []):
        if name.lower() == _HEADER:
            candidate = value.strip()
            if 0 < len(candidate) <= _MAX_LEN and _SAFE_VALUE.match(candidate):
                return candidate.decode("latin-1")
            return None
    return None


class RequestIdMiddleware:
    """Pure ASGI middleware assigning/propagating ``X-Request-ID``."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid.uuid4().hex
        token = request_id_ctx.set(request_id)
        scope.setdefault("state", {})["request_id"] = request_id

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((_HEADER, request_id.encode("latin-1")))
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            request_id_ctx.reset(token)
