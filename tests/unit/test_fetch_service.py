"""Tests for fetch_channel_resources pipeline."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.agent import Agent
from app.models.channel import Channel
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.services import fetch_service as fs


def _uuid():
    return str(uuid.uuid4())


TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"title_cn": {"source": "title"}},
}


def _mock_feed(entries):
    feed = MagicMock()
    feed.bozo = False
    feed.entries = entries
    return feed


def _entry(guid, title, link=None, enclosures=None, description=None, published=None):
    """Create a feedparser-like entry object supporting .keys(), .get(), [key]
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
        field_mapping=TEST_FIELD_MAPPING,
        metadata_agent_enabled=False, status="active",
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
        url="http://127.0.0.1:9091/transmission/rpc",
        download_dir="/downloads/rssripple",
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
            side_effect=Exception("network error")
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
            ])
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

    async def test_pre_parser_fills_batch_and_subtitle_langs(self, db_session, channel, fake_queue):
        """Batch flag and subtitle language tags land on the row before the
        LLM path runs — even with the metadata agent stubbed out."""
        entries = [
            _entry("gb", "Show S01E01~13 1080p [简繁内封字幕]", enclosures=[
                {"url": "magnet:?xt=urn:btih:bbb"},
            ]),
        ]
        feed = _mock_feed(entries)
        with patch("app.services.fetch_service._parse_feed_sync", return_value=feed), \
             patch("app.services.fetch_service.fetch_and_link_metadata", new_callable=AsyncMock):
            await fs.fetch_channel_resources(channel, db_session)

        from sqlalchemy import select
        row = (await db_session.execute(
            select(FileResource).where(FileResource.guid == "gb")
        )).scalar_one()
        assert row.is_batch is True
        assert row.episode_start == 1
        assert row.episode_end == 13
        assert row.subtitle_langs == ["zh-CN", "zh-TW"]

    async def test_existing_guid_skipped(self, db_session, channel, fake_queue):
        existing = FileResource(
            id=_uuid(), channel_id=channel.id, guid="g1",
            title_raw="old", torrent_url="magnet:?xt=urn:btih:old",
        )
        db_session.add(existing)
        await db_session.flush()
        entries = [
            _entry("g1", "[Group] Show - 01 [1080p]", enclosures=[
                {"url": "magnet:?xt=urn:btih:aaa"},
            ]),
            _entry("g2", "[Group] Show - 02 [1080p]", enclosures=[
                {"url": "magnet:?xt=urn:btih:bbb"},
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
        await db_session.refresh(channel, attribute_names=["agents", "file_resources"])

        entries = [_entry("g1", "[G] S - 01", enclosures=[
            {"url": "magnet:?xt=urn:btih:aaa"}
        ])]
        feed = _mock_feed(entries)
        with patch("app.services.fetch_service._parse_feed_sync", return_value=feed), \
             patch("app.services.fetch_service.fetch_and_link_metadata", new_callable=AsyncMock):
            await fs.fetch_channel_resources(channel, db_session)
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

    # ------------------------------------------------------------------
    # Field mapping parsing failure (lines 101-103)
    # ------------------------------------------------------------------
    async def test_field_mapping_parse_failure_falls_back_to_empty(self, db_session, channel, fake_queue):
        """When parse_entry raises, parsed should fall back to {} and the
        entry is still processed using auto-extracted torrent URL."""
        entries = [
            _entry("g1", "[Group] Show - 01 [1080p]", enclosures=[
                {"url": "magnet:?xt=urn:btih:aaa", "type": "application/x-bittorrent"}
            ])
        ]
        feed = _mock_feed(entries)
        with patch("app.services.fetch_service._parse_feed_sync", return_value=feed), \
             patch("app.services.fetch_service.parse_entry", side_effect=Exception("mapping boom")), \
             patch("app.services.fetch_service.fetch_and_link_metadata", new_callable=AsyncMock):
            res = await fs.fetch_channel_resources(channel, db_session)
        # Entry still created because torrent_url comes from enclosure fallback
        assert res["new_count"] == 1

    # ------------------------------------------------------------------
    # Metadata agent exception handling
    # ------------------------------------------------------------------
    async def test_metadata_agent_exception_uses_fallback(self, db_session, channel, fake_queue):
        """When metadata agent raises, search_title falls back to _simple_title_clean."""
        channel.metadata_agent_enabled = True
        entries = [
            _entry("g1", "[Group] Show - 01 [1080p]", enclosures=[
                {"url": "magnet:?xt=urn:btih:aaa"}
            ])
        ]
        feed = _mock_feed(entries)
        with patch("app.services.fetch_service._parse_feed_sync", return_value=feed), \
             patch("app.services.metadata_agent.UnifiedMetadataAgent.process",
                   new_callable=AsyncMock, side_effect=Exception("llm timeout")):
            res = await fs.fetch_channel_resources(channel, db_session)
        assert res["new_count"] == 1
        from sqlalchemy import select as sa_select
        row = (await db_session.execute(
            sa_select(FileResource).where(FileResource.channel_id == channel.id)
        )).scalar_one()
        # _simple_title_clean should produce a search_title from the raw title
        assert row.search_title is not None

    async def test_metadata_agent_disabled_uses_local_match(self, db_session, channel, fake_queue):
        """When metadata_agent_enabled=False, only local DB match runs."""
        channel.metadata_agent_enabled = False
        entries = [
            _entry("g1", "[Group] Show - 01 [1080p]", enclosures=[
                {"url": "magnet:?xt=urn:btih:aaa"}
            ])
        ]
        feed = _mock_feed(entries)
        with patch("app.services.fetch_service._parse_feed_sync", return_value=feed), \
             patch("app.services.fetch_service.fetch_and_link_metadata", new_callable=AsyncMock):
            res = await fs.fetch_channel_resources(channel, db_session)
        assert res["new_count"] == 1

    # ------------------------------------------------------------------
    # Metadata linking exception handling
    # ------------------------------------------------------------------
    async def test_metadata_linking_exception_swallowed(self, db_session, channel, fake_queue):
        """When fetch_and_link_metadata raises, the resource is still created."""
        entries = [
            _entry("g1", "[Group] Show - 01 [1080p]", enclosures=[
                {"url": "magnet:?xt=urn:btih:aaa"}
            ])
        ]
        feed = _mock_feed(entries)
        with patch("app.services.fetch_service._parse_feed_sync", return_value=feed), \
             patch("app.services.fetch_service.fetch_and_link_metadata",
                   new_callable=AsyncMock, side_effect=Exception("metadata boom")):
            res = await fs.fetch_channel_resources(channel, db_session)
        # Resource still created despite metadata linking failure
        assert res["new_count"] == 1
        assert channel.last_fetch_status == "success"

    # ------------------------------------------------------------------
    # Poster download for linked series (lines 160-167)
    # ------------------------------------------------------------------
    async def test_poster_download_for_series_with_http_url(self, db_session, channel, fake_queue):
        """When resource is linked to a series with http poster_url, poster is downloaded."""
        from app.models.series import TVSeries

        series = TVSeries(
            id=_uuid(), title_cn="Test", title_en="Test",
            poster_url="https://example.com/poster.jpg"
        )
        db_session.add(series)
        await db_session.flush()

        async def _link_metadata(db, resource, ch):
            resource.series_id = series.id

        entries = [
            _entry("g1", "[Group] Show - 01 [1080p]", enclosures=[
                {"url": "magnet:?xt=urn:btih:aaa"}
            ])
        ]
        feed = _mock_feed(entries)
        with patch("app.services.fetch_service._parse_feed_sync", return_value=feed), \
             patch("app.services.fetch_service.fetch_and_link_metadata",
                   new_callable=AsyncMock, side_effect=_link_metadata), \
             patch("app.services.metadata_service.download_and_cache_poster",
                   new_callable=AsyncMock, return_value="/posters/abc123.jpg") as mock_dl:
            res = await fs.fetch_channel_resources(channel, db_session)

        assert res["new_count"] == 1
        mock_dl.assert_awaited_once_with("https://example.com/poster.jpg")
        # Verify poster_url was updated to local path
        await db_session.refresh(series)
        assert series.poster_url == "/posters/abc123.jpg"

    async def test_poster_download_series_local_url_skipped(self, db_session, channel, fake_queue):
        """When series poster_url is already local (/posters/...), no download is attempted."""
        from app.models.series import TVSeries

        series = TVSeries(
            id=_uuid(), title_cn="Test", title_en="Test",
            poster_url="/posters/already_local.jpg"
        )
        db_session.add(series)
        await db_session.flush()

        async def _link_metadata(db, resource, ch):
            resource.series_id = series.id

        entries = [
            _entry("g1", "[Group] Show - 01 [1080p]", enclosures=[
                {"url": "magnet:?xt=urn:btih:aaa"}
            ])
        ]
        feed = _mock_feed(entries)
        with patch("app.services.fetch_service._parse_feed_sync", return_value=feed), \
             patch("app.services.fetch_service.fetch_and_link_metadata",
                   new_callable=AsyncMock, side_effect=_link_metadata), \
             patch("app.services.metadata_service.download_and_cache_poster",
                   new_callable=AsyncMock) as mock_dl:
            res = await fs.fetch_channel_resources(channel, db_session)

        assert res["new_count"] == 1
        mock_dl.assert_not_awaited()

    async def test_poster_download_series_returns_none(self, db_session, channel, fake_queue):
        """When download_and_cache_poster returns None, poster_url is not updated."""
        from app.models.series import TVSeries

        original_url = "https://example.com/poster.jpg"
        series = TVSeries(
            id=_uuid(), title_cn="Test", title_en="Test",
            poster_url=original_url
        )
        db_session.add(series)
        await db_session.flush()

        async def _link_metadata(db, resource, ch):
            resource.series_id = series.id

        entries = [
            _entry("g1", "[Group] Show - 01 [1080p]", enclosures=[
                {"url": "magnet:?xt=urn:btih:aaa"}
            ])
        ]
        feed = _mock_feed(entries)
        with patch("app.services.fetch_service._parse_feed_sync", return_value=feed), \
             patch("app.services.fetch_service.fetch_and_link_metadata",
                   new_callable=AsyncMock, side_effect=_link_metadata), \
             patch("app.services.metadata_service.download_and_cache_poster",
                   new_callable=AsyncMock, return_value=None):
            res = await fs.fetch_channel_resources(channel, db_session)

        assert res["new_count"] == 1
        # poster_url should remain the original http URL
        await db_session.refresh(series)
        assert series.poster_url == original_url

    async def test_poster_download_series_no_poster_url(self, db_session, channel, fake_queue):
        """When series has no poster_url (None), no download is attempted."""
        from app.models.series import TVSeries

        series = TVSeries(
            id=_uuid(), title_cn="Test", title_en="Test",
            poster_url=None
        )
        db_session.add(series)
        await db_session.flush()

        async def _link_metadata(db, resource, ch):
            resource.series_id = series.id

        entries = [
            _entry("g1", "[Group] Show - 01 [1080p]", enclosures=[
                {"url": "magnet:?xt=urn:btih:aaa"}
            ])
        ]
        feed = _mock_feed(entries)
        with patch("app.services.fetch_service._parse_feed_sync", return_value=feed), \
             patch("app.services.fetch_service.fetch_and_link_metadata",
                   new_callable=AsyncMock, side_effect=_link_metadata), \
             patch("app.services.metadata_service.download_and_cache_poster",
                   new_callable=AsyncMock) as mock_dl:
            res = await fs.fetch_channel_resources(channel, db_session)

        assert res["new_count"] == 1
        mock_dl.assert_not_awaited()

    # ------------------------------------------------------------------
    # Poster download for linked movie (lines 168-175)
    # ------------------------------------------------------------------
    async def test_poster_download_for_movie_with_http_url(self, db_session, channel, fake_queue):
        """When resource is linked to a movie with http poster_url, poster is downloaded."""
        from app.models.movie import Movie

        movie = Movie(
            id=_uuid(), title_cn="Test Movie", title_en="Test Movie",
            poster_url="https://example.com/movie_poster.jpg"
        )
        db_session.add(movie)
        await db_session.flush()

        async def _link_metadata(db, resource, ch):
            resource.movie_id = movie.id

        entries = [
            _entry("g1", "[Group] Movie Title [1080p]", enclosures=[
                {"url": "magnet:?xt=urn:btih:aaa"}
            ])
        ]
        feed = _mock_feed(entries)
        with patch("app.services.fetch_service._parse_feed_sync", return_value=feed), \
             patch("app.services.fetch_service.fetch_and_link_metadata",
                   new_callable=AsyncMock, side_effect=_link_metadata), \
             patch("app.services.metadata_service.download_and_cache_poster",
                   new_callable=AsyncMock, return_value="/posters/movie123.jpg") as mock_dl:
            res = await fs.fetch_channel_resources(channel, db_session)

        assert res["new_count"] == 1
        mock_dl.assert_awaited_once_with("https://example.com/movie_poster.jpg")
        await db_session.refresh(movie)
        assert movie.poster_url == "/posters/movie123.jpg"

    async def test_poster_download_movie_local_url_skipped(self, db_session, channel, fake_queue):
        """When movie poster_url is already local, no download is attempted."""
        from app.models.movie import Movie

        movie = Movie(
            id=_uuid(), title_cn="Test Movie", title_en="Test Movie",
            poster_url="/posters/local_movie.jpg"
        )
        db_session.add(movie)
        await db_session.flush()

        async def _link_metadata(db, resource, ch):
            resource.movie_id = movie.id

        entries = [
            _entry("g1", "[Group] Movie Title [1080p]", enclosures=[
                {"url": "magnet:?xt=urn:btih:aaa"}
            ])
        ]
        feed = _mock_feed(entries)
        with patch("app.services.fetch_service._parse_feed_sync", return_value=feed), \
             patch("app.services.fetch_service.fetch_and_link_metadata",
                   new_callable=AsyncMock, side_effect=_link_metadata), \
             patch("app.services.metadata_service.download_and_cache_poster",
                   new_callable=AsyncMock) as mock_dl:
            res = await fs.fetch_channel_resources(channel, db_session)

        assert res["new_count"] == 1
        mock_dl.assert_not_awaited()

    async def test_poster_download_movie_returns_none(self, db_session, channel, fake_queue):
        """When download_and_cache_poster returns None for movie, poster_url unchanged."""
        from app.models.movie import Movie

        original_url = "https://example.com/movie_poster.jpg"
        movie = Movie(
            id=_uuid(), title_cn="Test Movie", title_en="Test Movie",
            poster_url=original_url
        )
        db_session.add(movie)
        await db_session.flush()

        async def _link_metadata(db, resource, ch):
            resource.movie_id = movie.id

        entries = [
            _entry("g1", "[Group] Movie Title [1080p]", enclosures=[
                {"url": "magnet:?xt=urn:btih:aaa"}
            ])
        ]
        feed = _mock_feed(entries)
        with patch("app.services.fetch_service._parse_feed_sync", return_value=feed), \
             patch("app.services.fetch_service.fetch_and_link_metadata",
                   new_callable=AsyncMock, side_effect=_link_metadata), \
             patch("app.services.metadata_service.download_and_cache_poster",
                   new_callable=AsyncMock, return_value=None):
            res = await fs.fetch_channel_resources(channel, db_session)

        assert res["new_count"] == 1
        await db_session.refresh(movie)
        assert movie.poster_url == original_url

    # ------------------------------------------------------------------
    # Task queue enqueue failure (lines 200-201)
    # ------------------------------------------------------------------
    async def test_enqueue_failure_logged_but_does_not_raise(self, db_session, channel, downloader):
        """When task_queue.enqueue raises, the error is logged but fetch still succeeds."""
        q = MagicMock()
        q.enqueue = AsyncMock(side_effect=Exception("queue full"))
        import app.services.task_queue as tq_mod_real
        # Patch at the module level where it's imported inside the function
        with patch.object(tq_mod_real, "task_queue", q):
            agent = Agent(
                id=_uuid(), name="a", channel_id=channel.id,
                downloader_id=downloader.id, status="active",
                scope_channel_wide=True,
            )
            db_session.add(agent)
            await db_session.commit()
            await db_session.refresh(channel, attribute_names=["agents", "file_resources"])

            entries = [_entry("g1", "[G] S - 01", enclosures=[
                {"url": "magnet:?xt=urn:btih:aaa"}
            ])]
            feed = _mock_feed(entries)
            with patch("app.services.fetch_service._parse_feed_sync", return_value=feed), \
                 patch("app.services.fetch_service.fetch_and_link_metadata", new_callable=AsyncMock):
                # Should NOT raise despite enqueue failure
                res = await fs.fetch_channel_resources(channel, db_session)

        assert res["new_count"] == 1
        assert channel.last_fetch_status == "success"


# ---------------------------------------------------------------------------
# Metadata backfill: retry-eligibility + fetch-time re-processing of existing
# unmatched resources.
# ---------------------------------------------------------------------------


def _res(**over):
    from types import SimpleNamespace
    base = dict(
        metadata_attempts=0, metadata_failure_type=None,
        last_metadata_attempt_at=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_is_retry_eligible_never_tried():
    from app.services.fetch_service import _is_retry_eligible
    from app.utils.time import utcnow
    assert _is_retry_eligible(_res(), utcnow()) is True


def test_is_retry_eligible_non_work_never():
    from datetime import timedelta

    from app.services.fetch_service import _is_retry_eligible
    from app.utils.time import utcnow
    now = utcnow()
    r = _res(metadata_attempts=3, metadata_failure_type="non_work",
             last_metadata_attempt_at=now - timedelta(days=400))
    assert _is_retry_eligible(r, now) is False


def test_is_retry_eligible_transient_backoff():
    from datetime import timedelta

    from app.services.fetch_service import _is_retry_eligible
    from app.utils.time import utcnow
    now = utcnow()
    # attempts=1 → 1h backoff; 30min ago → not yet
    r = _res(metadata_attempts=1, metadata_failure_type="transient",
             last_metadata_attempt_at=now - timedelta(minutes=30))
    assert _is_retry_eligible(r, now) is False
    # 2h ago → eligible
    r2 = _res(metadata_attempts=1, metadata_failure_type="transient",
              last_metadata_attempt_at=now - timedelta(hours=2))
    assert _is_retry_eligible(r2, now) is True


def test_is_retry_eligible_not_found_ttl():
    from datetime import timedelta

    from app.services.fetch_service import NOT_FOUND_RETRY_DAYS, _is_retry_eligible
    from app.utils.time import utcnow
    now = utcnow()
    r = _res(metadata_attempts=1, metadata_failure_type="not_found",
             last_metadata_attempt_at=now - timedelta(days=NOT_FOUND_RETRY_DAYS - 1))
    assert _is_retry_eligible(r, now) is False
    r2 = _res(metadata_attempts=1, metadata_failure_type="not_found",
              last_metadata_attempt_at=now - timedelta(days=NOT_FOUND_RETRY_DAYS + 1))
    assert _is_retry_eligible(r2, now) is True


class TestMetadataBackfill:
    async def test_backfill_caps_and_records_attempts(self, db_session, channel, fake_queue):
        """40 never-tried unmatched resources → exactly MAX_BACKFILL_PER_FETCH
        re-processed, each stamped with a not_found attempt."""
        from sqlalchemy import select
        for i in range(40):
            db_session.add(FileResource(
                id=_uuid(), channel_id=channel.id, guid=f"g{i}",
                title_raw=f"[G] Show{i} - 01 [1080p]",
                torrent_url=f"magnet:?xt=urn:btih:{i}",
            ))
        await db_session.commit()

        feed = _mock_feed([])  # empty feed: no new entries, backfill still runs
        with patch("app.services.fetch_service._parse_feed_sync", return_value=feed):
            res = await fs.fetch_channel_resources(channel, db_session)

        assert res["backfilled_count"] == 30
        rows = (await db_session.execute(
            select(FileResource).where(FileResource.channel_id == channel.id)
        )).scalars().all()
        attempted = [r for r in rows if r.metadata_attempts == 1]
        untouched = [r for r in rows if r.metadata_attempts == 0]
        assert len(attempted) == 30
        assert len(untouched) == 10
        assert all(r.metadata_failure_type == "not_found" for r in attempted)
        assert all(r.last_metadata_attempt_at is not None for r in attempted)

    async def test_backfill_skips_non_work(self, db_session, channel, fake_queue):
        """A correctly-identified non-work resource is never auto-retried."""
        from datetime import timedelta

        from sqlalchemy import select

        from app.utils.time import utcnow
        db_session.add(FileResource(
            id=_uuid(), channel_id=channel.id, guid="nw",
            title_raw="[ASMR] something", torrent_url="magnet:?xt=urn:btih:nw",
            metadata_attempts=1, metadata_failure_type="non_work",
            last_metadata_attempt_at=utcnow() - timedelta(days=400),
        ))
        await db_session.commit()

        feed = _mock_feed([])
        with patch("app.services.fetch_service._parse_feed_sync", return_value=feed):
            res = await fs.fetch_channel_resources(channel, db_session)

        assert res["backfilled_count"] == 0
        row = (await db_session.execute(
            select(FileResource).where(FileResource.guid == "nw")
        )).scalar_one()
        assert row.metadata_attempts == 1  # untouched


async def test_backfill_runs_even_when_feed_fetch_fails(db_session, channel, fake_queue):
    """A feed outage must not block repair: the backfill still re-runs
    retry-eligible unmatched resources even though the RSS fetch failed."""
    from sqlalchemy import select
    for i in range(5):
        db_session.add(FileResource(
            id=_uuid(), channel_id=channel.id, guid=f"g{i}",
            title_raw=f"[G] Show{i} - 01 [1080p]",
            torrent_url=f"magnet:?xt=urn:btih:{i}",
        ))
    await db_session.commit()

    with patch("app.services.fetch_service._parse_feed_sync",
               side_effect=Exception("network error")):
        res = await fs.fetch_channel_resources(channel, db_session)

    assert res["status"] == "error"
    assert res["new_count"] == 0
    assert res["backfilled_count"] == 5  # backfill ran despite the feed failure
    assert channel.last_fetch_status == "failed"
    rows = (await db_session.execute(
        select(FileResource).where(FileResource.channel_id == channel.id)
    )).scalars().all()
    assert all(r.metadata_attempts == 1 for r in rows)


# ---------------------------------------------------------------------------
# Standalone global metadata backfill (scheduler-driven, cross-channel)
# ---------------------------------------------------------------------------


def test_not_found_retry_days_shortened_to_seven():
    """not_found TTL lowered 14d -> 7d so LLM finalization misses (agent
    identified the work but finalized found=false) get another chance sooner
    without hammering genuinely-nonexistent works too hard."""
    from app.services.fetch_service import NOT_FOUND_RETRY_DAYS
    assert NOT_FOUND_RETRY_DAYS == 7


async def _agent_channel(db_session, *, name):
    ch = Channel(
        id=_uuid(), name=name, type="rss_feed", url=f"https://{name}.example/rss",
        field_mapping=TEST_FIELD_MAPPING, metadata_agent_enabled=True, status="active",
    )
    db_session.add(ch)
    await db_session.flush()
    return ch


def _stamp_process():
    """An async stand-in for UnifiedMetadataAgent.process that records a
    not_found attempt on the resource."""
    from app.utils.time import utcnow

    async def _process(resource, channel, db, force_refresh=False):
        resource.metadata_attempts = int(getattr(resource, "metadata_attempts", 0) or 0) + 1
        resource.metadata_failure_type = "not_found"
        resource.last_metadata_attempt_at = utcnow()
    return _process


async def test_global_backfill_processes_agent_enabled_channels_only(db_session):
    """Global backfill re-runs unmatched resources across ALL agent-enabled
    channels and skips agent-disabled channels."""
    from unittest.mock import MagicMock, patch

    ch_on = await _agent_channel(db_session, name="on")
    ch_off = Channel(
        id=_uuid(), name="off", type="rss_feed", url="https://off.example/rss",
        field_mapping=TEST_FIELD_MAPPING, metadata_agent_enabled=False, status="active",
    )
    db_session.add(ch_off)
    for ch, n in ((ch_on, 3), (ch_off, 2)):
        for i in range(n):
            db_session.add(FileResource(
                id=_uuid(), channel_id=ch.id, guid=f"{ch.name}-{i}",
                title_raw=f"[G] {ch.name}{i} - 01", torrent_url=f"magnet:?xt=urn:btih:{ch.name}{i}",
            ))
    await db_session.commit()

    seen: list[str] = []
    async def _process(resource, channel, db, force_refresh=False):
        await _stamp_process()(resource, channel, db, force_refresh=force_refresh)
        seen.append(resource.channel_id)

    mock_agent = MagicMock()
    mock_agent.process = _process
    with patch("app.services.metadata_agent.get_agent", return_value=mock_agent):
        count = await fs.backfill_unmatched_resources_global(db_session, limit=10)

    assert count == 3  # only the 3 agent-enabled resources
    assert all(cid == ch_on.id for cid in seen)
    from sqlalchemy import select
    off_rows = (await db_session.execute(
        select(FileResource).where(FileResource.channel_id == ch_off.id)
    )).scalars().all()
    assert all(r.metadata_attempts == 0 for r in off_rows)


async def test_global_backfill_respects_per_run_limit(db_session):
    """The per-run limit caps how many resources are re-processed in one tick."""
    from unittest.mock import MagicMock, patch

    ch = await _agent_channel(db_session, name="lim")
    for i in range(10):
        db_session.add(FileResource(
            id=_uuid(), channel_id=ch.id, guid=f"lim-{i}",
            title_raw=f"[G] lim{i} - 01", torrent_url=f"magnet:?xt=urn:btih:lim{i}",
        ))
    await db_session.commit()

    mock_agent = MagicMock()
    mock_agent.process = _stamp_process()
    with patch("app.services.metadata_agent.get_agent", return_value=mock_agent):
        count = await fs.backfill_unmatched_resources_global(db_session, limit=4)

    assert count == 4


async def test_global_backfill_skips_non_work_and_respects_cooldown(db_session):
    """non_work resources are never retried; a recently-attempted transient is
    still within backoff and is skipped - only the never-tried resource runs."""
    from datetime import timedelta
    from unittest.mock import MagicMock, patch

    from app.utils.time import utcnow

    ch = await _agent_channel(db_session, name="elig")
    now = utcnow()
    db_session.add(FileResource(
        id=_uuid(), channel_id=ch.id, guid="nw",
        title_raw="[ASMR] x", torrent_url="magnet:?xt=urn:btih:nw",
        metadata_attempts=2, metadata_failure_type="non_work",
        last_metadata_attempt_at=now - timedelta(days=400),
    ))
    db_session.add(FileResource(
        id=_uuid(), channel_id=ch.id, guid="recent",
        title_raw="[G] recent - 01", torrent_url="magnet:?xt=urn:btih:recent",
        metadata_attempts=1, metadata_failure_type="transient",
        last_metadata_attempt_at=now - timedelta(minutes=10),  # 1h backoff not elapsed
    ))
    db_session.add(FileResource(
        id=_uuid(), channel_id=ch.id, guid="ok",
        title_raw="[G] ok - 01", torrent_url="magnet:?xt=urn:btih:ok",
        # never tried -> eligible
    ))
    await db_session.commit()

    mock_agent = MagicMock()
    mock_agent.process = _stamp_process()
    with patch("app.services.metadata_agent.get_agent", return_value=mock_agent):
        count = await fs.backfill_unmatched_resources_global(db_session, limit=10)

    assert count == 1  # only the never-tried "ok" resource


async def test_reset_channel_metadata_for_source_change(db_session, channel, fake_queue):
    """Switching a channel's source resets its not_found/transient unmatched
    resources so the backfill reprocesses them under the new source."""
    from datetime import timedelta

    from sqlalchemy import select

    # not_found (in cooldown), transient (in cooldown), already-matched, non_work
    from app.models.series import TVSeries
    from app.utils.time import utcnow
    series = TVSeries(id=_uuid(), title_cn="Matched")
    db_session.add(series)
    await db_session.flush()
    db_session.add(FileResource(
        id=_uuid(), channel_id=channel.id, guid="nf",
        title_raw="[G] nf", torrent_url="magnet:?xt=urn:btih:nf",
        metadata_attempts=1, metadata_failure_type="not_found",
        last_metadata_attempt_at=utcnow() - timedelta(days=1),
    ))
    db_session.add(FileResource(
        id=_uuid(), channel_id=channel.id, guid="tr",
        title_raw="[G] tr", torrent_url="magnet:?xt=urn:btih:tr",
        metadata_attempts=2, metadata_failure_type="transient",
        last_metadata_attempt_at=utcnow() - timedelta(minutes=10),
    ))
    db_session.add(FileResource(
        id=_uuid(), channel_id=channel.id, guid="mt",
        title_raw="[G] mt", torrent_url="magnet:?xt=urn:btih:mt",
        series_id=series.id, metadata_attempts=1, metadata_failure_type=None,
    ))
    db_session.add(FileResource(
        id=_uuid(), channel_id=channel.id, guid="nw",
        title_raw="[ASMR] nw", torrent_url="magnet:?xt=urn:btih:nw",
        metadata_attempts=1, metadata_failure_type="non_work",
        last_metadata_attempt_at=utcnow() - timedelta(days=400),
    ))
    await db_session.commit()

    reset = await fs.reset_channel_metadata_for_source_change(db_session, channel.id)
    await db_session.commit()

    assert reset == 2  # only not_found + transient (matched + non_work untouched)
    rows = {r.guid: r for r in (await db_session.execute(
        select(FileResource).where(FileResource.channel_id == channel.id)
    )).scalars().all()}
    assert rows["nf"].metadata_failure_type is None
    assert rows["nf"].metadata_attempts == 0
    assert rows["nf"].last_metadata_attempt_at is None
    assert rows["tr"].metadata_failure_type is None
    assert rows["mt"].metadata_failure_type is None  # untouched (matched)
    assert rows["nw"].metadata_failure_type == "non_work"  # untouched
