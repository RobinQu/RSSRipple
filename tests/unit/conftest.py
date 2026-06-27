"""Shared fixtures for unit tests that need a database session.

Uses a file-backed aiosqlite database in a temporary directory per test
function, ensuring each test has isolated state. Also installs a clean
global engine/session_factory in ``app.database`` so that application
code that imports the module-global factory uses the test database.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Patch long sleeps in retry/backoff code paths to run immediately in tests.
# Keep sleep(0) intact because asyncio primitives rely on it for scheduling.
_real_sleep = asyncio.sleep
async def _fast_sleep(delay: float, *a, **kw):
    if delay and delay >= 1:
        return
    return await _real_sleep(delay, *a, **kw)
asyncio.sleep = _fast_sleep  # type: ignore[assignment]

# Use SQLite for all unit tests; override DATABASE_URL before app imports it.
_TMP_DB_DIR: tempfile.TemporaryDirectory[str] | None = None
_TMP_DB_PATH: Path | None = None


def _fresh_db_url() -> str:
    global _TMP_DB_DIR, _TMP_DB_PATH
    _TMP_DB_DIR = tempfile.TemporaryDirectory(prefix="rssripple-tests-")
    _TMP_DB_PATH = Path(_TMP_DB_DIR.name) / "test.db"
    return f"sqlite+aiosqlite:///{_TMP_DB_PATH}"


@pytest.fixture(scope="session")
def event_loop_policy():
    """Use default event loop policy; pytest-asyncio 0.24+ requires a fixture
    when ``scope='session'`` is used for event loops."""
    return asyncio.DefaultEventLoopPolicy()


# Import Base/models after setting DATABASE_URL.
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"

from app.database import Base, enable_sqlite_fk  # noqa: E402
import app.models  # noqa: E402, F401  (register models)


@pytest_asyncio.fixture
async def db_engine():
    """Provide a fresh aiosqlite engine per test with all tables created."""
    url = _fresh_db_url()
    engine = create_async_engine(url, echo=False)
    enable_sqlite_fk(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    try:
        yield engine
    finally:
        await engine.dispose()
        if _TMP_DB_DIR is not None:
            _TMP_DB_DIR.cleanup()


@pytest_asyncio.fixture
async def db_session(db_engine):
    """Provide an async SQLAlchemy session bound to the test engine."""
    factory = async_sessionmaker(
        db_engine, class_=AsyncSession, expire_on_commit=False
    )
    async with factory() as session:
        yield session
        await session.rollback()


# ---------------------------------------------------------------------------
# Sample model factories
# ---------------------------------------------------------------------------


def _uuid() -> str:
    import uuid

    return str(uuid.uuid4())


@pytest_asyncio.fixture
async def sample_channel(db_session: AsyncSession):
    """Create and persist a minimal active Channel."""
    from sqlalchemy.orm import selectinload
    from app.models.channel import Channel

    ch = Channel(
        id=_uuid(),
        name="Test Channel",
        type="rss_feed",
        url="https://example.com/rss",
        fetch_interval=1800,
        status="active",
        metadata_source="none",
        title_extraction_method="none",
    )
    db_session.add(ch)
    await db_session.commit()
    # Refetch with relationships eager-loaded so downstream code that walks
    # .agents / .file_resources doesn't trigger implicit lazy IO under async.
    cur = await db_session.execute(
        __import__("sqlalchemy").select(Channel)
        .where(Channel.id == ch.id)
        .options(selectinload(Channel.agents), selectinload(Channel.file_resources),
                 selectinload(Channel.raw_title_mappings))
    )
    return cur.scalar_one()


@pytest_asyncio.fixture
async def sample_downloader(db_session: AsyncSession):
    """Create and persist a minimal DownloaderInstance."""
    from app.models.downloader import DownloaderInstance

    dl = DownloaderInstance(
        id=_uuid(),
        name="Test Downloader",
        type="transmission",
        url="http://127.0.0.1:9091/transmission/rpc",
        download_dir="/tmp/downloads",
        status="disconnected",
    )
    db_session.add(dl)
    await db_session.flush()
    return dl


@pytest_asyncio.fixture
def make_resource():
    """Factory returning a simple FileResource-like SimpleNamespace (unsaved)
    that can be passed to pure-Python services like filter_engine."""

    def _make(**overrides: Any):
        defaults: dict[str, Any] = dict(
            id=_uuid(),
            channel_id="ch",
            guid="guid-1",
            title_raw="[Group] Show - 01 [1080p HEVC]",
            title_cn=None,
            title_en=None,
            search_title="Show",
            subtitle_group="Group",
            episode=1,
            season=1,
            resolution="1080p",
            source="WebRip",
            video_codec="HEVC",
            audio_codec="AAC",
            subtitle_type="CHS",
            container="MKV",
            file_size=1_000_000_000,
            torrent_url="magnet:?xt=urn:btih:abc",
            detail_url=None,
            published_at=None,
            parsed_at=None,
            series_id=None,
            movie_id=None,
            metadata_matched_at=None,
        )
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    return _make


@pytest_asyncio.fixture
async def sample_series(db_session: AsyncSession):
    from app.models.series import TVSeries

    s = TVSeries(
        id=_uuid(),
        title_cn="测试剧集",
        title_en="Test Series",
        original_title="Test Series",
        aliases=["别名"],
        external_id="tt-test-series",
        external_source="manual",
        content_type="tv",
    )
    db_session.add(s)
    await db_session.flush()
    return s


@pytest_asyncio.fixture
async def sample_movie(db_session: AsyncSession):
    from app.models.movie import Movie

    m = Movie(
        id=_uuid(),
        title_cn="测试电影",
        title_en="Test Movie",
        original_title="Test Movie",
        external_id="tt-test-movie",
        external_source="manual",
        content_type="movie",
    )
    db_session.add(m)
    await db_session.flush()
    return m


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_transmission():
    """Patch TransmissionWrapper.add_torrent to avoid real RPC calls."""
    from unittest.mock import patch

    add_torrent = AsyncMock(return_value={"torrent_id": 42, "name": "x", "hash": "h"})
    list_torrents = AsyncMock(return_value=[])
    test_conn = AsyncMock(return_value=(True, "Transmission 3.00"))
    pause_torrent = AsyncMock(return_value=True)
    resume_torrent = AsyncMock(return_value=True)
    remove_torrent = AsyncMock(return_value=True)
    with patch(
        "app.clients.transmission.TransmissionWrapper.add_torrent", add_torrent
    ), patch(
        "app.clients.transmission.TransmissionWrapper.list_torrents", list_torrents
    ), patch(
        "app.clients.transmission.TransmissionWrapper.test_connection", test_conn
    ), patch(
        "app.clients.transmission.TransmissionWrapper.pause_torrent", pause_torrent
    ), patch(
        "app.clients.transmission.TransmissionWrapper.resume_torrent", resume_torrent
    ), patch(
        "app.clients.transmission.TransmissionWrapper.remove_torrent", remove_torrent
    ):
        yield SimpleNamespace(
            add_torrent=add_torrent,
            list_torrents=list_torrents,
            test_connection=test_conn,
            pause_torrent=pause_torrent,
            resume_torrent=resume_torrent,
            remove_torrent=remove_torrent,
        )
