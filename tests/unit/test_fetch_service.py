"""Tests for fetch_channel_resources pipeline."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.agent import Agent
from app.models.channel import Channel
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.services import fetch_service as fs
from app.services import task_queue as tq_mod


def _uuid():
    return str(uuid.uuid4())


def _mock_feed(entries):
    feed = MagicMock()
    feed.bozo = False
    feed.entries = entries
    return feed


def _entry(guid, title, link=None, enclosures=None, description=None, published=None):
    """Create a feedparser-like entry object supporting .keys(), .get(), [key],
    attribute access, plus the enclosures/published_parsed fields used by
    fetch_service / rss_parser helpers."""
    base = {
        "title": title,
        "link": link or f"https://example.com/{guid}",
        "id": guid,
        "description": description or "",
    }
    enclosures = enclosures or []
    published_parsed = published or (2024, 1, 1, 0, 0, 0, 0, 0, 0)

    class Entry(SimpleNamespace):
        def keys(self):
            return list(base.keys())

        def get(self, key, default=None):
            if key in base:
                return base[key]
            return default

        def __getitem__(self, key):
            return base[key]

    return Entry(
        id=guid,
        title=title,
        link=link or f"https://example.com/{guid}",
        enclosures=enclosures,
        description=description or "",
        published_parsed=published_parsed,
    )


@pytest.fixture
async def channel(db_session):
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    ch = Channel(
        id=_uuid(), name="ch", type="rss_feed", url="https://example.com/rss",
        metadata_source="none", status="active",
        title_extraction_method="none",
    )
    db_session.add(ch)
    await db_session.commit()
    cur = await db_session.execute(
        select(Channel).where(Channel.id == ch.id).options(
            selectinload(Channel.agents),
            selectinload(Channel.file_resources),
            selectinload(Channel.raw_title_mappings),
        )
    )
    return cur.scalar_one()


@pytest.fixture
async def downloader(db_session):
    dl = DownloaderInstance(
        id=_uuid(), name="dl", type="transmission",
        url="http://127.0.0.1:9091/transmission/rpc", download_dir="/tmp",
    )
    db_session.add(dl)
    await db_session.flush()
    return dl


@pytest.fixture
def fake_queue(monkeypatch):
    q = MagicMock()
    q.enqueue = AsyncMock(return_value={"job_id": "j"})
    import app.services.task_queue as tq_mod_real
    monkeypatch.setattr(tq_mod_real, "task_queue", q)
    return q


class TestFetchChannelResources:
    async def test_feed_fetch_failure_marks_channel_error(self, db_session, channel, fake_queue):
        with patch(
            "app.services.fetch_service._parse_feed_sync",
            side_effect=Exception("network error"),
        ):
            res = await fs.fetch_channel_resources(channel, db_session)
        assert res["new_count"] == 0
        assert channel.status == "error"
        assert channel.last_fetch_status == "failed"
        assert "network error" in (channel.last_fetch_error or "")

    async def test_bozo_feed_without_entries_marks_error(self, db_session, channel, fake_queue):
        feed = MagicMock()
        feed.bozo = True
        feed.entries = []
        feed.bozo_exception = Exception("bad xml")
        with patch("app.services.fetch_service._parse_feed_sync", return_value=feed):
            res = await fs.fetch_channel_resources(channel, db_session)
        assert res["new_count"] == 0
        assert channel.last_fetch_status == "failed"

    async def test_new_entries_create_resources(self, db_session, channel, fake_queue):
        entries = [
            _entry("g1", "[Group] Show - 01 [1080p]", enclosures=[
                {"url": "magnet:?xt=urn:btih:aaa", "type": "application/x-bittorrent"}
            ]),
        ]
        feed = _mock_feed(entries)
        with patch("app.services.fetch_service._parse_feed_sync", return_value=feed), \
             patch("app.services.fetch_service.fetch_and_link_metadata", new_callable=AsyncMock):
            res = await fs.fetch_channel_resources(channel, db_session)
        assert res["new_count"] == 1
        from sqlalchemy import func, select
        count = (await db_session.execute(
            select(func.count()).select_from(FileResource).where(FileResource.channel_id == channel.id)
        )).scalar_one()
        assert count == 1

    async def test_existing_guid_skipped(self, db_session, channel, fake_queue):
        existing = FileResource(
            id=_uuid(), channel_id=channel.id, guid="g1",
            title_raw="old", torrent_url="magnet:?xt=urn:btih:old",
        )
        db_session.add(existing)
        await db_session.flush()
        entries = [
            _entry("g1", "[Group] Show - 01 [1080p]", enclosures=[
                {"url": "magnet:?xt=urn:btih:aaa"}
            ]),
            _entry("g2", "[Group] Show - 02 [1080p]", enclosures=[
                {"url": "magnet:?xt=urn:btih:bbb"}
            ]),
        ]
        feed = _mock_feed(entries)
        with patch("app.services.fetch_service._parse_feed_sync", return_value=feed), \
             patch("app.services.fetch_service.fetch_and_link_metadata", new_callable=AsyncMock):
            res = await fs.fetch_channel_resources(channel, db_session)
        assert res["new_count"] == 1

    async def test_active_agents_enqueued(self, db_session, channel, downloader, monkeypatch):
        q = MagicMock()
        q.enqueue = AsyncMock(return_value={"job_id": "j"})
        import app.services.task_queue as tq_mod_real
        monkeypatch.setattr(tq_mod_real, "task_queue", q)

        agent = Agent(
            id=_uuid(), name="a", channel_id=channel.id,
            downloader_id=downloader.id, status="active",
            scope_channel_wide=True,
        )
        db_session.add(agent)
        inactive = Agent(
            id=_uuid(), name="paused", channel_id=channel.id,
            downloader_id=downloader.id, status="paused",
            scope_channel_wide=True,
        )
        db_session.add(inactive)
        await db_session.commit()
        # Eager-load agents collection onto the channel instance
        from sqlalchemy.orm import selectinload
        await db_session.refresh(channel, attribute_names=["agents", "file_resources"])

        entries = [_entry("g1", "[G] S - 01", enclosures=[
            {"url": "magnet:?xt=urn:btih:aaa"}
        ])]
        feed = _mock_feed(entries)
        with patch("app.services.fetch_service._parse_feed_sync", return_value=feed), \
             patch("app.services.fetch_service.fetch_and_link_metadata", new_callable=AsyncMock):
            res = await fs.fetch_channel_resources(channel, db_session)
        assert q.enqueue.await_count == 1
        args = q.enqueue.await_args.args
        assert args[0] == "run_agent"
        assert args[2]["agent_id"] == agent.id

    async def test_no_download_url_entry_skipped(self, db_session, channel, fake_queue):
        e = _entry("g1", "title", link="https://example.com/page", enclosures=[])
        feed = _mock_feed([e])
        with patch("app.services.fetch_service._parse_feed_sync", return_value=feed), \
             patch("app.services.fetch_service.fetch_and_link_metadata", new_callable=AsyncMock):
            res = await fs.fetch_channel_resources(channel, db_session)
        assert res["new_count"] == 0

    async def test_empty_feed(self, db_session, channel, fake_queue):
        feed = _mock_feed([])
        with patch("app.services.fetch_service._parse_feed_sync", return_value=feed):
            res = await fs.fetch_channel_resources(channel, db_session)
        assert res["new_count"] == 0
        assert channel.last_fetch_status == "success"
