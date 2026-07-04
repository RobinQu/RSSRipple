"""Shared test fixtures for API tests.

Sets up a dedicated FastAPI test application (without running the lifespan
or the real scheduler/task queue) backed by a file-based aiosqlite
database created per-test, and provides an :class:`httpx.AsyncClient`
pointed at that app.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Patch long sleeps in retry/backoff code paths.
_real_sleep = asyncio.sleep
async def _fast_sleep(delay: float, *a, **kw):
    if delay and delay >= 1:
        return
    return await _real_sleep(delay, *a, **kw)
asyncio.sleep = _fast_sleep  # type: ignore[assignment]

# Ensure a default DATABASE_URL before app imports.
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from fastapi import FastAPI  # noqa: E402
from fastapi.exceptions import RequestValidationError  # noqa: E402
from starlette.exceptions import HTTPException as StarletteHTTPException  # noqa: E402

from app.database import Base, enable_sqlite_fk, get_db  # noqa: E402
import app.models  # noqa: E402, F401
from app.main import (  # noqa: E402
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)

# Import routers directly (avoid pulling in lifespan)
from app.api.v1 import (  # noqa: E402
    agents,
    channels,
    dashboard,
    decisions,
    downloaders,
    movies,
    resources,
    series,
    tasks,
)


_TMP_DB_DIR: tempfile.TemporaryDirectory[str] | None = None
_TMP_DB_PATH: Path | None = None


def _fresh_db_url() -> str:
    global _TMP_DB_DIR, _TMP_DB_PATH
    _TMP_DB_DIR = tempfile.TemporaryDirectory(prefix="rssripple-apitests-")
    _TMP_DB_PATH = Path(_TMP_DB_DIR.name) / "test.db"
    return f"sqlite+aiosqlite:///{_TMP_DB_PATH}"


def _build_test_app(session_factory: async_sessionmaker) -> FastAPI:
    """Build a stripped-down FastAPI app that mirrors production exception
    handlers and routers but skips the lifespan (scheduler/queue init)."""
    test_app = FastAPI()
    test_app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    test_app.add_exception_handler(RequestValidationError, validation_exception_handler)
    test_app.add_exception_handler(Exception, unhandled_exception_handler)

    # Mount a no-op static files directory for /posters
    from fastapi.staticfiles import StaticFiles

    poster_dir = Path(tempfile.mkdtemp(prefix="rssripple-posters-"))
    test_app.mount("/posters", StaticFiles(directory=str(poster_dir)), name="poster-cache")

    for router in (
        dashboard.router,
        channels.router,
        agents.router,
        downloaders.router,
        tasks.router,
        decisions.router,
        resources.router,
        series.router,
        movies.router,
    ):
        test_app.include_router(router, prefix="/api/v1")

    # Override DB dependency
    async def _override_get_db():
        async with session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    test_app.dependency_overrides[get_db] = _override_get_db
    return test_app


@pytest_asyncio.fixture
async def db_engine():
    url = _fresh_db_url()
    engine = create_async_engine(url, echo=False)
    enable_sqlite_fk(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        from app.services.fts import ensure_fts_tables
        await ensure_fts_tables(conn)
    try:
        yield engine
    finally:
        await engine.dispose()
        if _TMP_DB_DIR is not None:
            _TMP_DB_DIR.cleanup()


@pytest_asyncio.fixture
async def db_session_factory(db_engine):
    return async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def db_session(db_session_factory):
    async with db_session_factory() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def client(db_session_factory, monkeypatch):
    """Async HTTP client pointed at a test FastAPI instance."""
    # Install a no-op in-memory task queue so API code that imports task_queue
    # doesn't attempt to use a real backend.
    from app.services import task_queue as tq_mod

    fake_queue = MagicMock()
    fake_queue.enqueue = AsyncMock(return_value={
        "job_id": "job-test",
        "job_type": "test",
        "key": "test",
        "status": "queued",
    })
    fake_queue.status = AsyncMock(return_value={"status": "done", "result": {}})
    fake_queue.start = AsyncMock()
    fake_queue.stop = AsyncMock()
    monkeypatch.setattr(tq_mod, "task_queue", fake_queue)

    # Submission guard: issue() returns a fixed token; consume() returns True.
    from app.services import submission_guard as sg_mod

    class _FakeGuard:
        async def issue(self) -> str:
            return "test-token"

        async def consume(self, token: str) -> bool:
            return True

    monkeypatch.setattr(sg_mod, "submission_guard", _FakeGuard())

    # Scheduler helpers: no-ops.
    import app.services.scheduler as sch_mod

    monkeypatch.setattr(sch_mod, "reschedule_channel", lambda ch: None)
    monkeypatch.setattr(sch_mod, "unschedule_channel", lambda cid: None)

    test_app = _build_test_app(db_session_factory)
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Helpers for creating fixtures via the API or directly in the DB
# ---------------------------------------------------------------------------


def _uuid() -> str:
    import uuid

    return str(uuid.uuid4())


TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"torrent_url": {"source": "link"}},
}


@pytest_asyncio.fixture
async def sample_channel(db_session: AsyncSession):
    from app.models.channel import Channel

    ch = Channel(
        id=_uuid(),
        name="Test Channel",
        type="rss_feed",
        url="https://example.com/rss",
        fetch_interval=1800,
        status="active",
        field_mapping=TEST_FIELD_MAPPING,
        metadata_agent_enabled=False,
    )
    db_session.add(ch)
    await db_session.flush()
    await db_session.commit()
    return ch


@pytest_asyncio.fixture
async def sample_downloader(db_session):
    from app.models.downloader import DownloaderInstance

    dl = DownloaderInstance(
        id=_uuid(),
        name="TestDL",
        type="transmission",
        url="http://127.0.0.1:9091/transmission/rpc",
        download_dir="/downloads/rssripple",
        status="disconnected",
    )
    db_session.add(dl)
    await db_session.commit()
    return dl


@pytest_asyncio.fixture
async def sample_series(db_session: AsyncSession):
    from app.models.series import TVSeries

    s = TVSeries(
        id=_uuid(),
        title_cn="测试剧集",
        title_en="Test Series",
        original_title="Test Series",
        content_type="tv",
    )
    db_session.add(s)
    await db_session.flush()
    await db_session.commit()
    return s


@pytest_asyncio.fixture
async def sample_movie(db_session: AsyncSession):
    from app.models.movie import Movie

    m = Movie(
        id=_uuid(),
        title_cn="测试电影",
        title_en="Test Movie",
        original_title="Test Movie",
        content_type="movie",
    )
    db_session.add(m)
    await db_session.flush()
    await db_session.commit()
    return m


@pytest.fixture
def api_mocks(monkeypatch):
    """Provide a namespace of common external-call mocks used across API tests."""
    from app.clients import rss_parser as rss_mod
    from app.api.v1 import channels as channels_mod
    from app.services import feed_analyzer as fa_mod

    validate = AsyncMock(return_value=(True, "valid", 5, 5)),
    get_entries = AsyncMock(return_value=[{"title": "[G] T - 01 [1080p]"}]),
    analyze = AsyncMock(return_value={
        "field_mapping": {
            "list_locator": {"source": "entries"},
            "field_mappings": {"subtitle_group": {"source": "title"}},
        },
        "sample_results": [],
        "confidence": "high",
    })

    monkeypatch.setattr(rss_mod, "validate_rss_url", validate)
    monkeypatch.setattr(rss_mod, "get_raw_entries", get_entries)
    monkeypatch.setattr(channels_mod, "analyze_feed", analyze)

    async def _fake_call_llm(*a, **kw):
        return '{"list_locator": {"source": "entries"}, "field_mappings": {}}'

    monkeypatch.setattr(fa_mod, "call_llm", _fake_call_llm)

    return SimpleNamespace(validate=validate, get_entries=get_entries, analyze=analyze)


@pytest.fixture
def mock_transmission(monkeypatch):
    """Patch TransmissionWrapper.* methods (returns AsyncMock defaults)."""
    from unittest.mock import patch

    add_t = AsyncMock(return_value={"torrent_id": 42, "name": "x", "hash": "h"})
    list_t = AsyncMock(return_value=[])
    test_t = AsyncMock(return_value=(True, "Transmission 3.00"))
    free_t = AsyncMock(return_value=1024 * 1024 * 1024)
    pause_t = AsyncMock(return_value=True)
    resume_t = AsyncMock(return_value=True)
    remove_t = AsyncMock(return_value=True)
    with patch("app.clients.transmission.TransmissionWrapper.add_torrent", add_t), patch(
        "app.clients.transmission.TransmissionWrapper.list_torrents", list_t
    ), patch(
        "app.clients.transmission.TransmissionWrapper.test_connection", test_t
    ), patch(
        "app.clients.transmission.TransmissionWrapper.free_space", free_t
    ), patch(
        "app.clients.transmission.TransmissionWrapper.pause_torrent", pause_t
    ), patch(
        "app.clients.transmission.TransmissionWrapper.resume_torrent", resume_t
    ), patch(
        "app.clients.transmission.TransmissionWrapper.remove_torrent", remove_t
    ):
        yield SimpleNamespace(
            add_torrent=add_t,
            list_torrents=list_t,
            test_connection=test_t,
            free_space=free_t,
            pause_torrent=pause_t,
            resume_torrent=resume_t,
            remove_torrent=remove_t,
        )
