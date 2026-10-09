"""Full production ASGI stack + real Turso rollback; conflict is injected.

The DB-retry middleware replays only safe methods (GET/HEAD/OPTIONS): a
handler that already produced out-of-band effects (enqueue, downloader RPC,
SSE) must never be replayed — see ``app/middleware/db_retry.py``. The POST
case below pins the new contract: no replay, the transaction rolls back, and
the client receives the error (it can retry the operation itself). The GET
case keeps the body-preservation + rollback coverage for replayable methods.
"""

import asyncio
from typing import Annotated

import httpx
import pytest
from fastapi import Depends
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import DatabaseError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import get_db
from app.main import app
from app.models.movie import Movie


class ReplayBody(BaseModel):
    title: str
    padding: str


def _conflict():
    return DatabaseError("synthetic after-write conflict", {}, Exception("Write-write conflict"))


async def _run_probe(db_session_factory, monkeypatch, method, size,
                     raise_app_exceptions=True):
    monkeypatch.setattr("app.database.async_session_factory", db_session_factory)
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "api_key", "synthetic-replay-key")
    monkeypatch.setattr("app.database._backoff_delay", lambda _: 0)
    attempts = []

    async def endpoint(body: ReplayBody, db: Annotated[AsyncSession, Depends(get_db)]):
        attempts.append((body.title, len(body.padding)))
        db.add(Movie(title_cn=body.title))
        await db.flush()
        if len(attempts) == 1:
            raise _conflict()
        # get_db handles the final commit; the failed attempt must rollback.
        return {"success": True, "data": {"padding": body.padding}, "error": None, "meta": {}}

    route = APIRoute("/api/v1/replay-production-probe", endpoint, methods=[method])
    app.router.routes.insert(0, route)
    app.middleware_stack = None
    try:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
        async with httpx.AsyncClient(transport=transport, base_url="http://synthetic") as client:
            response = await asyncio.wait_for(client.request(method, route.path, headers={
                "X-API-Key": "synthetic-replay-key", "Accept-Encoding": "gzip",
            }, json={"title": "Synthetic rollback", "padding": "x" * size}), 10)
    finally:
        app.router.routes.remove(route)
        app.middleware_stack = None
    return response, attempts


@pytest.mark.parametrize("size", [0, 1024 * 1024 + 23])
async def test_production_stack_replays_body_and_rolls_back(db_session_factory, monkeypatch, size):
    """GET (safe method): the request IS replayed with its body intact and the
    failed attempt's writes are rolled back."""
    response, attempts = await _run_probe(db_session_factory, monkeypatch, "GET", size)
    assert response.status_code == 200
    assert response.json()["data"]["padding"] == "x" * size
    assert attempts == [("Synthetic rollback", size)] * 2
    if size:
        assert response.headers["content-encoding"] == "gzip"
    async with db_session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(Movie).where(Movie.title_cn == "Synthetic rollback")) == 1


@pytest.mark.parametrize("size", [0, 1024 * 1024 + 23])
async def test_production_stack_never_replays_post(db_session_factory, monkeypatch, size):
    """POST: no replay — a single attempt, the error reaches the client as a
    500, and the rolled-back attempt leaves no row behind."""
    response, attempts = await _run_probe(
        db_session_factory, monkeypatch, "POST", size, raise_app_exceptions=False,
    )
    assert response.status_code == 500
    assert attempts == [("Synthetic rollback", size)]
    async with db_session_factory() as db:
        assert await db.scalar(select(func.count()).select_from(Movie).where(Movie.title_cn == "Synthetic rollback")) == 0
