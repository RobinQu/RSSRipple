"""Tests for RequestIdMiddleware: generation, passthrough, sanitization, contextvar."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient

from app.middleware.request_id import RequestIdMiddleware, get_request_id


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(request: Request):
        return {
            "state": getattr(request.state, "request_id", None),
            "ctx": get_request_id(),
        }

    app.add_middleware(RequestIdMiddleware)
    return app


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=_app()), base_url="http://test") as ac:
        yield ac


class TestRequestId:
    async def test_generates_uuid_when_absent(self, client):
        res = await client.get("/whoami")
        rid = res.headers["x-request-id"]
        assert len(rid) == 32  # uuid4 hex
        body = res.json()
        assert body["state"] == rid
        assert body["ctx"] == rid

    async def test_unique_per_request(self, client):
        a = await client.get("/whoami")
        b = await client.get("/whoami")
        assert a.headers["x-request-id"] != b.headers["x-request-id"]

    async def test_valid_inbound_header_is_echoed(self, client):
        res = await client.get("/whoami", headers={"X-Request-ID": "trace-abc.123:Z"})
        assert res.headers["x-request-id"] == "trace-abc.123:Z"
        assert res.json()["ctx"] == "trace-abc.123:Z"

    async def test_unsafe_inbound_header_is_replaced(self, client):
        res = await client.get("/whoami", headers={"X-Request-ID": "bad value\r\ninjected"})
        rid = res.headers["x-request-id"]
        assert rid != "bad value\r\ninjected"
        assert len(rid) == 32

    async def test_overlong_inbound_header_is_replaced(self, client):
        res = await client.get("/whoami", headers={"X-Request-ID": "a" * 200})
        rid = res.headers["x-request-id"]
        assert rid != "a" * 200
        assert len(rid) == 32

    async def test_contextvar_cleared_outside_request(self):
        assert get_request_id() is None
