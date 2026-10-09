"""Replay an uncommitted HTTP request after a transient database conflict."""

import asyncio
import struct
import tempfile

import anyio
from sqlalchemy.exc import DatabaseError
from starlette.types import ASGIApp, Receive, Scope, Send

_FRAME = struct.Struct("!Q?")

# Methods whose replay cannot multiply side effects. GET/HEAD/OPTIONS are
# safe by HTTP definition: handlers do not commit writes or trigger
# out-of-band work for them, so re-running the whole request after a
# rolled-back transaction is indistinguishable from a single attempt.
#
# Everything else is deliberately NOT replayed — including PUT/DELETE, which
# are idempotent per HTTP semantics but in this app can enqueue background
# jobs (e.g. PUT /resources/{id}/associations enqueues a targeted agent run)
# or issue downloader RPCs (e.g. DELETE task → Transmission). For those
# handlers a DatabaseError mid-request means the DB transaction was rolled
# back but any out-of-band effect (enqueue, RPC, SSE event) already
# happened and cannot be undone; replaying would duplicate it. The client
# receives the error and can retry the operation itself.
_REPLAYABLE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class DatabaseRetryMiddleware:
    """Preserve request chunks; never restart a response already sent.

    Large uploads spill to a private temporary file rather than growing memory
    without bound. Reading remains demand-driven, including streaming uploads.

    Only safe HTTP methods (``_REPLAYABLE_METHODS``) are replayed; other
    methods pass through untouched — no body spooling, no retry.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in _REPLAYABLE_METHODS:
            await self.app(scope, receive, send)
            return
        from app.database import _MAX_DB_RETRIES, _backoff_delay, _is_retryable_lock_error

        with tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b") as spool:
            saved = 0
            disconnected = False

            def store(message):
                body = message.get("body", b"")
                spool.write(_FRAME.pack(len(body), message.get("more_body", False)))
                spool.write(body)

            def restore():
                length, more = _FRAME.unpack(spool.read(_FRAME.size))
                return {"type": "http.request", "body": spool.read(length), "more_body": more}

            for attempt in range(_MAX_DB_RETRIES):
                await anyio.to_thread.run_sync(spool.seek, 0)
                replayed = 0
                response_started = False

                async def replay_receive():
                    nonlocal saved, replayed, disconnected
                    if replayed < saved:
                        replayed += 1
                        return await anyio.to_thread.run_sync(restore)
                    message = await receive()
                    if message["type"] == "http.request":
                        await anyio.to_thread.run_sync(store, message)
                        saved += 1
                        replayed += 1
                    elif message["type"] == "http.disconnect":
                        disconnected = True
                    return message

                async def tracked_send(message):
                    nonlocal response_started
                    if message["type"] == "http.response.start":
                        response_started = True
                    await send(message)

                try:
                    await self.app(scope, replay_receive, tracked_send)
                    return
                except DatabaseError as exc:
                    if (response_started or disconnected or not _is_retryable_lock_error(exc)
                            or attempt == _MAX_DB_RETRIES - 1):
                        raise
                    await asyncio.sleep(_backoff_delay(attempt))
            raise AssertionError("unreachable")
