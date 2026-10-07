"""Full production ASGI stack + real Turso rollback; conflict is injected."""

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


@pytest.mark.parametrize("size", [0, 1024 * 1024 + 23])
async def test_production_stack_replays_body_and_rolls_back(db_session_factory, monkeypatch, size):
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
            raise DatabaseError("synthetic after-write conflict", {}, Exception("Write-write conflict"))
        # get_db handles the final commit; the failed attempt must rollback.
        return {"success": True, "data": {"padding": body.padding}, "error": None, "meta": {}}

    route = APIRoute("/api/v1/replay-production-probe", endpoint, methods=["POST"])
    app.router.routes.insert(0, route)
    app.middleware_stack = None
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic") as client:
            response = await asyncio.wait_for(client.post(route.path, headers={
                "X-API-Key": "synthetic-replay-key", "Accept-Encoding": "gzip",
            }, json={"title": "Synthetic rollback", "padding": "x" * size}), 10)
        assert response.status_code == 200
        assert response.json()["data"]["padding"] == "x" * size
        assert attempts == [("Synthetic rollback", size)] * 2
        if size:
            assert response.headers["content-encoding"] == "gzip"
        async with db_session_factory() as db:
            assert await db.scalar(select(func.count()).select_from(Movie).where(Movie.title_cn == "Synthetic rollback")) == 1
    finally:
        app.router.routes.remove(route)
        app.middleware_stack = None
