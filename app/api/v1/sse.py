"""Shared Server-Sent Events framing helpers.

Channels and resources expose streaming endpoints with the same wire contract:
ordinary progress frames use the default ``message`` event, failures must use
the ``error`` event name (docs/design/error-handling.md), and every payload is
JSON-encoded through :func:`api_json`.
"""

import json

from fastapi.responses import StreamingResponse

from app.schemas.common import api_json

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Connection": "keep-alive",
    "X-Accel-Buffering": "no",
}


def _frame(payload: dict, *, event: str | None, ensure_ascii: bool) -> str:
    body = json.dumps(api_json(payload), ensure_ascii=ensure_ascii)
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {body}\n\n"


def sse_data(payload: dict, *, ensure_ascii: bool = True) -> str:
    return _frame(payload, event=None, ensure_ascii=ensure_ascii)


def sse_error(payload: dict, *, ensure_ascii: bool = True) -> str:
    return _frame(payload, event="error", ensure_ascii=ensure_ascii)


def sse_response(gen) -> StreamingResponse:
    return StreamingResponse(gen, media_type="text/event-stream", headers=SSE_HEADERS)
