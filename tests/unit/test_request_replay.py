"""ASGI retry contracts with explicitly injected transaction failures."""

import asyncio

import pytest
from sqlalchemy.exc import DatabaseError

from app.middleware.db_retry import DatabaseRetryMiddleware


def conflict():
    return DatabaseError("synthetic", {}, Exception("Write-write conflict"))


@pytest.mark.parametrize("chunks", [[b""], [b'{"a":', b'1}'], [b"x" * (1024 * 1024 + 17), b"end"]])
@pytest.mark.parametrize("fail_after_chunks", [0, 1])
async def test_replay_keeps_body_bytes_and_remaining_stream(monkeypatch, chunks, fail_after_chunks):
    monkeypatch.setattr("app.database._backoff_delay", lambda _: 0)
    incoming = [{"type": "http.request", "body": body, "more_body": i < len(chunks)-1}
                for i, body in enumerate(chunks)]
    delivered = []
    attempts = []

    async def receive():
        assert incoming, "must replay consumed chunks instead of requesting them twice"
        return incoming.pop(0)

    async def send(message):
        delivered.append(message)

    async def application(scope, receive, send):
        attempt = len(attempts)
        body = []
        attempts.append(body)
        if attempt == 0 and fail_after_chunks == 0:
            raise conflict()
        while True:
            message = await receive()
            body.append(message["body"])
            if attempt == 0 and len(body) == fail_after_chunks:
                raise conflict()
            if not message["more_body"]:
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    await DatabaseRetryMiddleware(application)({"type": "http"}, receive, send)
    assert len(attempts) == 2
    assert attempts[1] == chunks
    assert len(delivered) == 2
    assert incoming == []


@pytest.mark.parametrize("mode", ["started", "disconnected", "nonretryable", "cancelled", "exhausted"])
async def test_no_unsafe_replay(monkeypatch, mode):
    monkeypatch.setattr("app.database._backoff_delay", lambda _: 0)
    calls = 0
    sent = []
    error = asyncio.CancelledError() if mode == "cancelled" else (
        DatabaseError("synthetic", {}, Exception("not a lock error")) if mode == "nonretryable" else conflict()
    )

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    async def application(scope, receive, send):
        nonlocal calls
        calls += 1
        if mode == "started":
            await send({"type": "http.response.start", "status": 200, "headers": []})
        if mode == "disconnected":
            await receive()
        raise error

    with pytest.raises(type(error)) as raised:
        await DatabaseRetryMiddleware(application)({"type": "http"}, receive, send)
    assert raised.value is error
    assert calls == (5 if mode == "exhausted" else 1)
    assert len(sent) == (1 if mode == "started" else 0)


async def test_lifespan_is_forwarded_without_http_retry():
    seen = []

    async def application(scope, receive, send):
        seen.append(scope["type"])

    await DatabaseRetryMiddleware(application)({"type": "lifespan"}, None, None)
    assert seen == ["lifespan"]


@pytest.mark.parametrize("outcome", ["complete", "cancel", "error"])
async def test_spilled_request_file_is_closed_on_every_exit(monkeypatch, outcome):
    import errno
    import os
    import tempfile

    original_spool = tempfile.SpooledTemporaryFile
    spools = []
    descriptors = []
    ready = asyncio.Event()
    hold = asyncio.Event()
    delivered = []

    def record_spool(*args, **kwargs):
        spool = original_spool(*args, **kwargs)
        spools.append(spool)
        return spool

    monkeypatch.setattr("app.middleware.db_retry.tempfile.SpooledTemporaryFile", record_spool)

    async def receive():
        return {"type": "http.request", "body": b"synthetic" * 150000, "more_body": False}

    async def send(message):
        delivered.append(message)

    async def application(scope, receive, send):
        message = await receive()
        assert len(message["body"]) > 1024 * 1024
        # name becomes a real descriptor after automatic rollover. Do not call
        # fileno() here, since doing so would force rollover even for small data.
        descriptor = spools[0].name
        assert isinstance(descriptor, int)
        assert os.fstat(descriptor).st_size > 1024 * 1024
        descriptors.append(descriptor)
        ready.set()
        if outcome == "cancel":
            await hold.wait()
        if outcome == "error":
            raise ValueError("Synthetic handler failure after reading upload")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    task = asyncio.create_task(DatabaseRetryMiddleware(application)({"type": "http"}, receive, send))
    try:
        await asyncio.wait_for(ready.wait(), 3)
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        elif outcome == "error":
            with pytest.raises(ValueError):
                await task
        else:
            await task
        assert len(spools) == 1
        assert spools[0].closed
        with pytest.raises(OSError) as raised:
            os.fstat(descriptors[0])
        assert raised.value.errno == errno.EBADF
        assert len(delivered) == (2 if outcome == "complete" else 0)
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
