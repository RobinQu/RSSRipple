"""Branch coverage for app.services.fetch_service.

Covers the retry-eligibility matrix, source-change reset, the per-resource
metadata task's failure/fallback branches (magnet enqueue failure, agent
failure title fallback, franchise invariant guard, poster caching, rollback),
backfill force/limit paths, stale-episode reconciliation, and the fetch loop's
feed-failure / field-mapping-failure / no-download-URL / compilation /
agent-enqueue branches. Feeds are fake ``_parse_feed_sync`` results; anything
that would touch the network (torrent caching, posters, the metadata agent)
is patched at its module boundary.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import app.services.fetch_service as fs
from app.models.agent import Agent
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.series import TVSeries
from app.services.runtime_config import reset_to_env_defaults
from app.utils.time import utcnow


@pytest.fixture(autouse=True)
def _isolate_shared_module_state():
    """Reset process-global state this file's code paths touch, both before and
    after each test, so the file stays green regardless of what earlier test
    files left behind (and leaks nothing itself) in combined directory runs.

    - ``runtime_config._overrides`` is a process-wide map populated from the
      ``app_settings`` table; a leftover LLM/source override changes the
      behavior of the metadata call paths these tests exercise. (The root
      conftest resets it too, but this file should not depend on run scope.)
    - ``fetch_service._WORK_METADATA_LOCKS`` holds ``asyncio.Lock`` objects
      keyed by normalized title; the dict outlives any single test's event
      loop, and a lock created — or left held — under another loop must never
      be reused here.
    - ``metadata_search_agent`` keeps process-lifetime caches (result cache,
      TMDB genre map, image base); clear them so cache state from sibling
      files cannot leak into a fetch/metadata path.
    """
    from app.services import metadata_search_agent as msa

    reset_to_env_defaults()
    fs._WORK_METADATA_LOCKS.clear()
    msa._cache.clear()
    msa._TMDB_GENRE_MAP = None
    msa._tmdb_image_base.cache_clear()
    yield
    msa._tmdb_image_base.cache_clear()
    msa._TMDB_GENRE_MAP = None
    msa._cache.clear()
    fs._WORK_METADATA_LOCKS.clear()
    reset_to_env_defaults()


def _uuid() -> str:
    return str(uuid.uuid4())


class _Entry(dict):
    """feedparser-style entry: dict access + attribute access."""

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key) from None


async def _make_channel(db_session, **overrides) -> Channel:
    defaults = dict(
        id=_uuid(), name="ch", type="rss_feed", url="https://example.com/rss",
        fetch_interval=1800, status="active",
        field_mapping={"list_locator": {"source": "entries"},
                       "field_mappings": {"torrent_url": {"source": "link"}}},
        metadata_agent_enabled=False,
    )
    defaults.update(overrides)
    ch = Channel(**defaults)
    db_session.add(ch)
    await db_session.commit()
    return await _reload_channel(db_session, ch.id)


async def _reload_channel(db_session, channel_id: str) -> Channel:
    cur = await db_session.execute(
        select(Channel).where(Channel.id == channel_id)
        .options(selectinload(Channel.agents), selectinload(Channel.file_resources),
                 selectinload(Channel.raw_title_mappings))
        .execution_options(populate_existing=True)
    )
    return cur.scalar_one()


def _make_resource(channel_id: str, **overrides) -> FileResource:
    defaults = dict(
        id=_uuid(), channel_id=channel_id, guid=_uuid(),
        title_raw="[G] Show - 01 [1080p]",
        torrent_url="https://x.example/a.torrent",
        search_title="Show",
    )
    defaults.update(overrides)
    return FileResource(**defaults)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestSimpleTitleClean:
    def test_empty_returns_none(self):
        assert fs._simple_title_clean("") is None
        assert fs._simple_title_clean(None) is None

    def test_strips_group_alt_episode_and_season(self):
        assert fs._simple_title_clean("[VCB-Studio] 孤独摇滚！ / Bocchi the Rock! - 05 [1080p]") == (
            "孤独摇滚！"
        )

    def test_falls_back_to_raw_when_everything_stripped(self):
        assert fs._simple_title_clean("[1080p]") == "[1080p]"


class TestRetryEligibility:
    def _res(self, **kw):
        defaults = dict(
            metadata_attempts=0, metadata_failure_type=None,
            last_metadata_attempt_at=None,
        )
        defaults.update(kw)
        return SimpleNamespace(**defaults)

    def test_never_tried_is_eligible(self):
        assert fs._is_retry_eligible(self._res(), utcnow()) is True

    def test_non_work_never_retries(self):
        assert fs._is_retry_eligible(
            self._res(metadata_attempts=1, metadata_failure_type="non_work"), utcnow()
        ) is False

    def test_missing_timestamp_is_eligible(self):
        assert fs._is_retry_eligible(self._res(metadata_attempts=2), utcnow()) is True

    def test_transient_backoff(self):
        now = utcnow()
        recent = self._res(metadata_attempts=1, metadata_failure_type="transient",
                           last_metadata_attempt_at=now - timedelta(minutes=30))
        assert fs._is_retry_eligible(recent, now) is False
        old = self._res(metadata_attempts=1, metadata_failure_type="transient",
                        last_metadata_attempt_at=now - timedelta(hours=2))
        assert fs._is_retry_eligible(old, now) is True
        # Backoff is capped at TRANSIENT_BACKOFF_MAX_HOURS.
        many = self._res(metadata_attempts=10, metadata_failure_type="transient",
                         last_metadata_attempt_at=now - timedelta(hours=30))
        assert fs._is_retry_eligible(many, now) is True

    def test_not_found_retry_window(self):
        now = utcnow()
        recent = self._res(metadata_attempts=1, metadata_failure_type="not_found",
                           last_metadata_attempt_at=now - timedelta(days=2))
        assert fs._is_retry_eligible(recent, now) is False
        old = self._res(metadata_attempts=1, metadata_failure_type="not_found",
                        last_metadata_attempt_at=now - timedelta(days=8))
        assert fs._is_retry_eligible(old, now) is True

    def test_unknown_failure_type_retries(self):
        now = utcnow()
        res = self._res(metadata_attempts=3, metadata_failure_type="weird",
                        last_metadata_attempt_at=now)
        assert fs._is_retry_eligible(res, now) is True


class TestLinkedEnrichmentEligibility:
    def test_unlinked_is_not_eligible(self):
        res = SimpleNamespace(series_id=None, movie_id=None, audio_work_id=None,
                              collection_id=None)
        assert fs._is_linked_enrichment_eligible(res, utcnow()) is False

    def test_missing_channel_relation_is_not_eligible(self):
        res = SimpleNamespace(series_id="s1", movie_id=None, audio_work_id=None,
                              collection_id=None, channel=None)
        assert fs._is_linked_enrichment_eligible(res, utcnow()) is False


# ---------------------------------------------------------------------------
# reset_channel_metadata_for_source_change
# ---------------------------------------------------------------------------


async def test_reset_channel_metadata_for_source_change(db_session):
    channel = await _make_channel(db_session)
    series = TVSeries(id=_uuid(), title_cn="剧集", content_type="tv")
    db_session.add(series)
    nf = _make_resource(channel.id, metadata_failure_type="not_found",
                        metadata_attempts=2, last_metadata_attempt_at=utcnow())
    tr = _make_resource(channel.id, metadata_failure_type="transient",
                        metadata_attempts=1, last_metadata_attempt_at=utcnow())
    nw = _make_resource(channel.id, metadata_failure_type="non_work",
                        metadata_attempts=1, last_metadata_attempt_at=utcnow())
    linked = _make_resource(channel.id, metadata_failure_type="not_found",
                            metadata_attempts=2, series_id=series.id)
    channel_b = Channel(
        id=_uuid(), name="ch2", type="rss_feed", url="https://example.com/rss2",
        fetch_interval=1800, status="active", field_mapping={},
        metadata_agent_enabled=False,
    )
    db_session.add(channel_b)
    await db_session.flush()
    other_channel = _make_resource(channel_b.id, metadata_failure_type="not_found")
    db_session.add_all([nf, tr, nw, linked, other_channel])
    await db_session.commit()

    reset = await fs.reset_channel_metadata_for_source_change(db_session, channel.id)
    assert reset == 2
    assert nf.metadata_failure_type is None and nf.metadata_attempts == 0
    assert tr.metadata_failure_type is None and tr.last_metadata_attempt_at is None
    # non_work and linked resources are untouched.
    assert nw.metadata_failure_type == "non_work"
    assert linked.metadata_failure_type == "not_found"


# ---------------------------------------------------------------------------
# _process_resource_metadata task branches
# ---------------------------------------------------------------------------


@pytest.fixture
def _patch_torrent_and_franchise(monkeypatch):
    """Neutralize the network-facing stages of _process_resource_metadata."""
    monkeypatch.setattr(
        "app.services.torrent_inspect.ensure_torrent_cached", AsyncMock()
    )
    monkeypatch.setattr(
        "app.services.torrent_inspect.maybe_inspect_torrent", AsyncMock()
    )
    monkeypatch.setattr(
        "app.services.franchise_service.enforce_franchise_resource_invariant",
        lambda resource: False,
    )
    monkeypatch.setattr(
        "app.services.batch_content_analysis.bind_single_work_assignments", AsyncMock()
    )


async def test_process_metadata_missing_rows_returns(db_session, _patch_torrent_and_franchise):
    import asyncio

    sem = asyncio.Semaphore(1)
    # Neither resource nor channel exists: early return, no error.
    await fs._process_resource_metadata(_uuid(), _uuid(), sem)


async def test_process_metadata_magnet_enqueue_failure_is_swallowed(
    db_session, monkeypatch, _patch_torrent_and_franchise
):
    import asyncio

    channel = await _make_channel(db_session)
    res = _make_resource(channel.id, torrent_url="magnet:?xt=urn:btih:" + "ab" * 20)
    db_session.add(res)
    await db_session.commit()

    async def _enqueue_boom(rid):
        raise RuntimeError("queue down")

    monkeypatch.setattr("app.services.magnet_resolve.enqueue_resolution", _enqueue_boom)
    # metadata_agent_enabled=False → fetch_and_link_metadata path; patch it.
    monkeypatch.setattr(fs, "fetch_and_link_metadata", AsyncMock())
    await fs._process_resource_metadata(res.id, channel.id, asyncio.Semaphore(1))
    fs.fetch_and_link_metadata.assert_awaited_once()


async def test_process_metadata_agent_failure_falls_back_to_simple_title(
    db_session, monkeypatch, _patch_torrent_and_franchise
):
    import asyncio

    channel = await _make_channel(db_session, metadata_agent_enabled=True)
    res = _make_resource(channel.id, search_title=None)
    db_session.add(res)
    await db_session.commit()

    async def _process_boom(*a, **kw):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(
        "app.services.metadata_agent.get_agent",
        lambda: SimpleNamespace(process=_process_boom),
    )
    await fs._process_resource_metadata(res.id, channel.id, asyncio.Semaphore(1))
    await db_session.refresh(res)
    # The minimal title cleanup filled search_title so the resource is not
    # left with a raw release title.
    assert res.search_title == "Show"


async def test_process_metadata_franchise_invariant_guard_logs(
    db_session, monkeypatch, caplog, _patch_torrent_and_franchise
):
    import asyncio
    import logging

    channel = await _make_channel(db_session)
    res = _make_resource(channel.id)
    db_session.add(res)
    await db_session.commit()
    monkeypatch.setattr(fs, "fetch_and_link_metadata", AsyncMock())
    monkeypatch.setattr(
        "app.services.franchise_service.enforce_franchise_resource_invariant",
        lambda resource: True,
    )
    with caplog.at_level(logging.INFO):
        await fs._process_resource_metadata(res.id, channel.id, asyncio.Semaphore(1))
    assert "cleared flat work FK" in caplog.text


async def test_process_metadata_caches_series_poster(
    db_session, monkeypatch, _patch_torrent_and_franchise
):
    import asyncio

    channel = await _make_channel(db_session)
    series = TVSeries(id=_uuid(), title_cn="剧集", content_type="tv",
                      poster_url="https://img.example/p.jpg",
                      number_of_episodes=12, season_number=1)
    db_session.add(series)
    res = _make_resource(channel.id, series_id=series.id, is_batch=False, episode=1)
    db_session.add(res)
    await db_session.commit()
    monkeypatch.setattr(fs, "fetch_and_link_metadata", AsyncMock())
    monkeypatch.setattr(
        "app.services.metadata_service.download_and_cache_poster",
        AsyncMock(return_value="/posters/local.jpg"),
    )
    await fs._process_resource_metadata(res.id, channel.id, asyncio.Semaphore(1))
    await db_session.refresh(series)
    assert series.poster_url == "/posters/local.jpg"


async def test_process_metadata_caches_movie_poster(
    db_session, monkeypatch, _patch_torrent_and_franchise
):
    import asyncio

    from app.models.movie import Movie

    channel = await _make_channel(db_session)
    movie = Movie(id=_uuid(), title_cn="电影", content_type="movie",
                  poster_url="https://img.example/m.jpg")
    db_session.add(movie)
    res = _make_resource(channel.id, movie_id=movie.id, series_id=None,
                         episode=None, season=None)
    db_session.add(res)
    await db_session.commit()
    monkeypatch.setattr(fs, "fetch_and_link_metadata", AsyncMock())
    monkeypatch.setattr(
        "app.services.metadata_service.download_and_cache_poster",
        AsyncMock(return_value="/posters/movie.jpg"),
    )
    await fs._process_resource_metadata(res.id, channel.id, asyncio.Semaphore(1))
    await db_session.refresh(movie)
    assert movie.poster_url == "/posters/movie.jpg"


async def test_process_metadata_rollback_failure_is_swallowed(
    db_session, monkeypatch, caplog
):
    import asyncio
    import logging

    class _BadSession:
        async def execute(self, *a, **kw):
            raise RuntimeError("db exploded")

        async def rollback(self):
            raise RuntimeError("rollback also exploded")

    class _BadFactory:
        async def __aenter__(self):
            return _BadSession()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("app.database.async_session_factory", lambda: _BadFactory())
    with caplog.at_level(logging.WARNING):
        # Must not raise despite both the body and the rollback failing.
        await fs._process_resource_metadata(_uuid(), _uuid(), asyncio.Semaphore(1))
    assert "db exploded" in caplog.text


# ---------------------------------------------------------------------------
# Backfill paths
# ---------------------------------------------------------------------------


async def test_backfill_force_bypasses_cooldowns(db_session, monkeypatch):
    channel = await _make_channel(db_session)
    # not_found one minute ago: normally in cooldown, force reprocesses it.
    res = _make_resource(channel.id, metadata_failure_type="not_found",
                         metadata_attempts=1,
                         last_metadata_attempt_at=utcnow() - timedelta(minutes=1))
    db_session.add(res)
    await db_session.commit()
    proc = AsyncMock()
    monkeypatch.setattr(fs, "_process_resource_metadata", proc)

    import asyncio

    count = await fs._backfill_unmatched_resources(
        channel, db_session, asyncio.Semaphore(1), force=True
    )
    assert count == 1
    proc.assert_awaited_once()


async def test_backfill_respects_cooldowns(db_session, monkeypatch):
    channel = await _make_channel(db_session)
    res = _make_resource(channel.id, metadata_failure_type="not_found",
                         metadata_attempts=1,
                         last_metadata_attempt_at=utcnow() - timedelta(minutes=1))
    db_session.add(res)
    await db_session.commit()
    proc = AsyncMock()
    monkeypatch.setattr(fs, "_process_resource_metadata", proc)

    import asyncio

    count = await fs._backfill_unmatched_resources(
        channel, db_session, asyncio.Semaphore(1), force=False
    )
    assert count == 0
    proc.assert_not_awaited()


async def test_global_backfill_no_candidates(db_session):
    assert await fs.backfill_unmatched_resources_global(db_session) == 0


async def test_global_backfill_limit_caps_processing(db_session, monkeypatch):
    channel = await _make_channel(db_session, metadata_agent_enabled=True)
    for _ in range(3):
        db_session.add(_make_resource(channel.id))
    await db_session.commit()
    proc = AsyncMock()
    monkeypatch.setattr(fs, "_process_resource_metadata", proc)
    count = await fs.backfill_unmatched_resources_global(db_session, limit=1)
    assert count == 1
    assert proc.await_count == 1


# ---------------------------------------------------------------------------
# reconcile_stale_raw_episodes
# ---------------------------------------------------------------------------


async def test_reconcile_stale_history_path(db_session, monkeypatch):
    channel = await _make_channel(db_session)
    series = TVSeries(id=_uuid(), title_cn="剧集", content_type="tv",
                      number_of_episodes=12, season_number=1)
    db_session.add(series)
    res = _make_resource(channel.id, series_id=series.id, is_batch=False,
                         episode=3, season=1, absolute_episode=3,
                         episode_confidence="raw")
    db_session.add(res)
    await db_session.commit()

    monkeypatch.setattr(
        "app.services.episode_history.apply_episode_history_reconcile",
        AsyncMock(return_value=True),
    )
    changed = await fs.reconcile_stale_raw_episodes(db_session, return_resource_ids=True)
    assert changed == [res.id]


async def test_reconcile_stale_legacy_arithmetic_path(db_session, monkeypatch):
    channel = await _make_channel(db_session)
    # Legacy unsplit row: per-season counts live in the inert ``seasons`` column.
    series = TVSeries(
        id=_uuid(), title_cn="剧集", content_type="tv",
        seasons=[{"season_number": 1, "episode_count": 12},
                 {"season_number": 2, "episode_count": 12}],
    )
    db_session.add(series)
    # Absolute episode 15 on a 12-episode season 2 → S2E3.
    res = _make_resource(channel.id, series_id=series.id, is_batch=False,
                         episode=15, season=2, episode_confidence="raw")
    db_session.add(res)
    await db_session.commit()

    monkeypatch.setattr(
        "app.services.episode_history.apply_episode_history_reconcile",
        AsyncMock(return_value=False),
    )
    changed = await fs.reconcile_stale_raw_episodes(db_session)
    assert changed == 1
    await db_session.refresh(res)
    assert res.episode_confidence == "reconciled"
    assert res.episode == 3
    assert res.absolute_episode == 15


# ---------------------------------------------------------------------------
# fetch_channel_resources loop branches
# ---------------------------------------------------------------------------


def _feed(entries, bozo=False):
    return SimpleNamespace(bozo=bozo, entries=entries)


async def test_fetch_feed_failure_marks_channel_error(db_session, monkeypatch):
    def _boom(url):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(fs, "_parse_feed_sync", _boom)
    backfill = AsyncMock(return_value=0)
    monkeypatch.setattr(fs, "_backfill_unmatched_resources", backfill)
    channel = await _make_channel(db_session)
    result = await fs.fetch_channel_resources(channel, db_session)
    assert result["status"] == "error"
    assert "connection reset" in result["error"]
    await db_session.refresh(channel)
    assert channel.last_fetch_status == "failed"
    assert channel.status == "error"
    # The backfill phase still ran despite the feed outage.
    backfill.assert_awaited_once()


async def test_fetch_entry_without_download_url_is_skipped(db_session, monkeypatch):
    # No field mapping and a non-torrent link: nothing downloadable → skipped.
    entries = [_Entry(id="g1", title="[G] Show - 01", link="https://x.example/detail-page")]
    monkeypatch.setattr(fs, "_parse_feed_sync", lambda url: _feed(entries))
    monkeypatch.setattr(fs, "_backfill_unmatched_resources", AsyncMock(return_value=0))
    channel = await _make_channel(db_session, field_mapping={})
    result = await fs.fetch_channel_resources(channel, db_session)
    assert result["status"] == "unchanged"
    assert result["new_count"] == 0
    assert result["total"] == 1


async def test_fetch_field_mapping_failure_still_creates_resource(db_session, monkeypatch):
    entries = [_Entry(
        id="g1", title="[G] Show - 01 [1080p]",
        link="https://x.example/detail",
        enclosures=[{"url": "https://x.example/a.torrent", "type": "application/x-bittorrent"}],
    )]
    monkeypatch.setattr(fs, "_parse_feed_sync", lambda url: _feed(entries))

    def _parse_boom(entry_dict, field_mapping, description):
        raise RuntimeError("bad mapping")

    monkeypatch.setattr(fs, "parse_entry", _parse_boom)
    monkeypatch.setattr(fs, "_process_resource_metadata", AsyncMock())
    monkeypatch.setattr(fs, "_backfill_unmatched_resources", AsyncMock(return_value=0))
    channel = await _make_channel(db_session)
    result = await fs.fetch_channel_resources(channel, db_session)
    assert result["new_count"] == 1
    res = (await db_session.execute(
        select(FileResource).where(FileResource.channel_id == channel.id)
    )).scalar_one()
    # The enclosure URL won over the field-mapped (failed) one.
    assert res.torrent_url == "https://x.example/a.torrent"


async def test_fetch_subtitle_groups_backfill_legacy_scalar(db_session, monkeypatch):
    entries = [_Entry(
        id="g1", title="[G] Show - 01 [1080p]",
        enclosures=[{"url": "https://x.example/a.torrent", "type": "application/x-bittorrent"}],
    )]
    monkeypatch.setattr(fs, "_parse_feed_sync", lambda url: _feed(entries))
    monkeypatch.setattr(fs, "parse_entry",
                        lambda entry_dict, fm, desc: {"subtitle_groups": ["G1", "G2"]})
    monkeypatch.setattr(fs, "_process_resource_metadata", AsyncMock())
    monkeypatch.setattr(fs, "_backfill_unmatched_resources", AsyncMock(return_value=0))
    channel = await _make_channel(db_session)
    result = await fs.fetch_channel_resources(channel, db_session)
    assert result["new_count"] == 1
    res = (await db_session.execute(
        select(FileResource).where(FileResource.channel_id == channel.id)
    )).scalar_one()
    assert res.subtitle_group  # legacy scalar derived from the plural field
    assert res.subtitle_groups


async def test_fetch_compilation_title_marks_batch(db_session, monkeypatch):
    entries = [_Entry(
        id="g1", title="[整理搬运] 某作品 完全版",
        enclosures=[{"url": "https://x.example/a.torrent", "type": "application/x-bittorrent"}],
    )]
    monkeypatch.setattr(fs, "_parse_feed_sync", lambda url: _feed(entries))
    monkeypatch.setattr(
        "app.services.resource_parser.extract_compilation_work_title",
        lambda title: "某作品",
    )
    monkeypatch.setattr(fs, "_process_resource_metadata", AsyncMock())
    monkeypatch.setattr(fs, "_backfill_unmatched_resources", AsyncMock(return_value=0))
    channel = await _make_channel(db_session)
    result = await fs.fetch_channel_resources(channel, db_session)
    assert result["new_count"] == 1
    res = (await db_session.execute(
        select(FileResource).where(FileResource.channel_id == channel.id)
    )).scalar_one()
    assert res.is_batch is True
    assert res.batch_scope == "season"
    assert res.search_title == "某作品"


async def test_fetch_backfill_failure_does_not_fail_fetch(db_session, monkeypatch):
    entries = [_Entry(
        id="g1", title="[G] Show - 01",
        enclosures=[{"url": "https://x.example/a.torrent", "type": "application/x-bittorrent"}],
    )]
    monkeypatch.setattr(fs, "_parse_feed_sync", lambda url: _feed(entries))
    monkeypatch.setattr(fs, "_process_resource_metadata", AsyncMock())

    async def _backfill_boom(*a, **kw):
        raise RuntimeError("backfill exploded")

    monkeypatch.setattr(fs, "_backfill_unmatched_resources", _backfill_boom)
    channel = await _make_channel(db_session)
    result = await fs.fetch_channel_resources(channel, db_session)
    assert result["status"] == "success"
    assert result["backfilled_count"] == 0


async def _make_downloader(db_session):
    from app.models.downloader import DownloaderInstance

    dl = DownloaderInstance(
        id=_uuid(), name="dl", type="transmission",
        url="http://127.0.0.1:9091/transmission/rpc",
        download_dir="/downloads/rssripple",
    )
    db_session.add(dl)
    await db_session.flush()
    return dl


async def test_fetch_enqueues_active_agents(db_session, monkeypatch):
    channel = await _make_channel(db_session)
    downloader = await _make_downloader(db_session)
    ok_agent = Agent(id=_uuid(), name="ok", channel_id=channel.id,
                     downloader_id=downloader.id, status="active")
    paused_agent = Agent(id=_uuid(), name="paused", channel_id=channel.id,
                         downloader_id=downloader.id, status="paused")
    db_session.add_all([ok_agent, paused_agent])
    await db_session.commit()
    channel = await _reload_channel(db_session, channel.id)

    entries = [_Entry(
        id="g1", title="[G] Show - 01",
        enclosures=[{"url": "https://x.example/a.torrent", "type": "application/x-bittorrent"}],
    )]
    monkeypatch.setattr(fs, "_parse_feed_sync", lambda url: _feed(entries))
    monkeypatch.setattr(fs, "_process_resource_metadata", AsyncMock())
    monkeypatch.setattr(fs, "_backfill_unmatched_resources", AsyncMock(return_value=0))

    calls: list[dict] = []

    async def _enqueue(job_type, key, payload):
        calls.append({"job_type": job_type, "key": key, "payload": payload})

    monkeypatch.setattr("app.services.task_queue.task_queue",
                        SimpleNamespace(enqueue=_enqueue))
    result = await fs.fetch_channel_resources(channel, db_session)
    assert result["status"] == "success"
    # Only the active agent gets a run; the paused one is skipped.
    assert calls == [{
        "job_type": "run_agent", "key": f"agent:{ok_agent.id}",
        "payload": {"agent_id": ok_agent.id},
    }]


async def test_fetch_agent_enqueue_failure_is_logged(db_session, monkeypatch, caplog):
    import logging

    channel = await _make_channel(db_session)
    downloader = await _make_downloader(db_session)
    agent = Agent(id=_uuid(), name="a", channel_id=channel.id,
                  downloader_id=downloader.id, status="active")
    db_session.add(agent)
    await db_session.commit()
    channel = await _reload_channel(db_session, channel.id)

    monkeypatch.setattr(fs, "_parse_feed_sync", lambda url: _feed([]))
    monkeypatch.setattr(fs, "_backfill_unmatched_resources", AsyncMock(return_value=0))

    async def _enqueue_boom(*a, **kw):
        raise RuntimeError("queue full")

    monkeypatch.setattr("app.services.task_queue.task_queue",
                        SimpleNamespace(enqueue=_enqueue_boom))
    with caplog.at_level(logging.WARNING):
        result = await fs.fetch_channel_resources(channel, db_session)
    assert result["status"] == "unchanged"
    assert "Failed to enqueue run_agent" in caplog.text


class TestLinkedEnrichmentEligible:
    def _linked(self, **kw):
        defaults = dict(
            series_id="s1", movie_id=None, audio_work_id=None, collection_id=None,
            is_batch=False, batch_scope=None, episode=None, season=None,
            search_title="Show", work_links=[],
            channel=SimpleNamespace(required_metadata_fields=["episode"]),
            last_metadata_attempt_at=None,
        )
        defaults.update(kw)
        return SimpleNamespace(**defaults)

    def test_incomplete_contract_and_never_attempted_is_eligible(self):
        assert fs._is_linked_enrichment_eligible(self._linked(), utcnow()) is True

    def test_recent_attempt_is_not_eligible(self):
        res = self._linked(last_metadata_attempt_at=utcnow() - timedelta(minutes=5))
        assert fs._is_linked_enrichment_eligible(res, utcnow()) is False

    def test_complete_contract_is_not_eligible(self):
        import datetime as _dt

        res = self._linked(
            episode=1,
            series=SimpleNamespace(start_date=_dt.date(2024, 1, 1), is_anime=True),
            channel=SimpleNamespace(required_metadata_fields=["episode"]),
        )
        assert fs._is_linked_enrichment_eligible(res, utcnow()) is False


async def test_process_metadata_link_failure_is_swallowed(
    db_session, monkeypatch, _patch_torrent_and_franchise
):
    import asyncio

    channel = await _make_channel(db_session)
    res = _make_resource(channel.id)
    db_session.add(res)
    await db_session.commit()

    async def _link_boom(*a, **kw):
        raise RuntimeError("metadata source down")

    monkeypatch.setattr(fs, "fetch_and_link_metadata", _link_boom)
    # Must not raise: the failure is logged and the task moves on.
    await fs._process_resource_metadata(res.id, channel.id, asyncio.Semaphore(1))


async def test_backfill_exclude_ids_skips_new_resources(db_session, monkeypatch):
    channel = await _make_channel(db_session)
    r1 = _make_resource(channel.id)
    r2 = _make_resource(channel.id)
    db_session.add_all([r1, r2])
    await db_session.commit()
    proc = AsyncMock()
    monkeypatch.setattr(fs, "_process_resource_metadata", proc)

    import asyncio

    count = await fs._backfill_unmatched_resources(
        channel, db_session, asyncio.Semaphore(1), force=True, exclude_ids={r1.id}
    )
    assert count == 1
    assert proc.await_args.args[0] == r2.id


async def test_backfill_scan_cap_breaks(db_session, monkeypatch):
    channel = await _make_channel(db_session)
    for _ in range(fs.MAX_BACKFILL_PER_FETCH + 1):
        db_session.add(_make_resource(channel.id))
    await db_session.commit()
    proc = AsyncMock()
    monkeypatch.setattr(fs, "_process_resource_metadata", proc)

    import asyncio

    count = await fs._backfill_unmatched_resources(
        channel, db_session, asyncio.Semaphore(1), force=False
    )
    assert count == fs.MAX_BACKFILL_PER_FETCH


async def test_reconcile_stale_no_candidates_returns_zero(db_session):
    assert await fs.reconcile_stale_raw_episodes(db_session) == 0
    assert await fs.reconcile_stale_raw_episodes(db_session, return_resource_ids=True) == []


async def test_reconcile_stale_skips_non_raw_and_in_tolerance(db_session, monkeypatch):
    channel = await _make_channel(db_session)
    series = TVSeries(id=_uuid(), title_cn="剧集", content_type="tv",
                      number_of_episodes=12, season_number=1)
    db_session.add(series)
    # Already-reconciled row the history pass declines: must be skipped by the
    # legacy arithmetic (only raw/NULL values are reconsidered).
    rec = _make_resource(channel.id, series_id=series.id, is_batch=False,
                         episode=25, season=1, episode_confidence="reconciled")
    # Raw but within tolerance of the season count: nothing to do.
    ok = _make_resource(channel.id, series_id=series.id, is_batch=False,
                        episode=5, season=1, episode_confidence="raw")
    db_session.add_all([rec, ok])
    await db_session.commit()
    monkeypatch.setattr(
        "app.services.episode_history.apply_episode_history_reconcile",
        AsyncMock(return_value=False),
    )
    changed = await fs.reconcile_stale_raw_episodes(db_session)
    assert changed == 0


async def test_fetch_bozo_feed_without_entries_is_error(db_session, monkeypatch):
    feed = SimpleNamespace(bozo=True, entries=[], bozo_exception=RuntimeError("malformed XML"))
    monkeypatch.setattr(fs, "_parse_feed_sync", lambda url: feed)
    monkeypatch.setattr(fs, "_backfill_unmatched_resources", AsyncMock(return_value=0))
    channel = await _make_channel(db_session)
    result = await fs.fetch_channel_resources(channel, db_session)
    assert result["status"] == "error"
    assert "malformed XML" in result["error"]


async def test_fetch_entry_with_empty_guid_is_skipped(db_session, monkeypatch):
    entries = [_Entry(id="", title="", link="")]
    monkeypatch.setattr(fs, "_parse_feed_sync", lambda url: _feed(entries))
    monkeypatch.setattr(fs, "_backfill_unmatched_resources", AsyncMock(return_value=0))
    channel = await _make_channel(db_session)
    result = await fs.fetch_channel_resources(channel, db_session)
    assert result["new_count"] == 0
    assert result["total"] == 1


async def test_fetch_subtitle_groups_without_scalar_uses_legacy_join(db_session, monkeypatch):
    # No bracket subtitle group in the title, so normalize_parsed_fields does
    # not pre-fill subtitle_group and the plural field drives the legacy join.
    entries = [_Entry(
        id="g1", title="Show - 01",
        enclosures=[{"url": "https://x.example/a.torrent", "type": "application/x-bittorrent"}],
    )]
    monkeypatch.setattr(fs, "_parse_feed_sync", lambda url: _feed(entries))
    monkeypatch.setattr(fs, "parse_entry",
                        lambda entry_dict, fm, desc: {"subtitle_groups": ["G1", "G2"]})
    monkeypatch.setattr(fs, "_process_resource_metadata", AsyncMock())
    monkeypatch.setattr(fs, "_backfill_unmatched_resources", AsyncMock(return_value=0))
    channel = await _make_channel(db_session)
    result = await fs.fetch_channel_resources(channel, db_session)
    assert result["new_count"] == 1
    res = (await db_session.execute(
        select(FileResource).where(FileResource.channel_id == channel.id)
    )).scalar_one()
    assert res.subtitle_group == "G1&G2"
    # resolve_subtitle_groups keeps an unrecognized joined name as one group.
    assert res.subtitle_groups == ["G1&G2"]


async def test_fetch_batch_title_sets_episode_range(db_session, monkeypatch):
    entries = [_Entry(
        id="g1", title="[G] Show S01 01-12 [1080p]",
        enclosures=[{"url": "https://x.example/a.torrent", "type": "application/x-bittorrent"}],
    )]
    monkeypatch.setattr(fs, "_parse_feed_sync", lambda url: _feed(entries))
    monkeypatch.setattr(fs, "_process_resource_metadata", AsyncMock())
    monkeypatch.setattr(fs, "_backfill_unmatched_resources", AsyncMock(return_value=0))
    channel = await _make_channel(db_session, field_mapping={})
    result = await fs.fetch_channel_resources(channel, db_session)
    assert result["new_count"] == 1
    res = (await db_session.execute(
        select(FileResource).where(FileResource.channel_id == channel.id)
    )).scalar_one()
    assert res.is_batch is True
    assert res.batch_scope == "season"
    assert res.episode is None  # batches never carry a stray single episode
    assert res.episode_start == 1
    assert res.episode_end == 12


async def test_fetch_double_labeled_episode_records_absolute(db_session, monkeypatch):
    entries = [_Entry(
        id="g1", title="[G] Show - 13(85) [1080p]",
        enclosures=[{"url": "https://x.example/a.torrent", "type": "application/x-bittorrent"}],
    )]
    monkeypatch.setattr(fs, "_parse_feed_sync", lambda url: _feed(entries))
    monkeypatch.setattr(fs, "_process_resource_metadata", AsyncMock())
    monkeypatch.setattr(fs, "_backfill_unmatched_resources", AsyncMock(return_value=0))
    channel = await _make_channel(db_session, field_mapping={})
    result = await fs.fetch_channel_resources(channel, db_session)
    assert result["new_count"] == 1
    res = (await db_session.execute(
        select(FileResource).where(FileResource.channel_id == channel.id)
    )).scalar_one()
    assert res.episode == 13
    assert res.absolute_episode == 85
    assert res.episode_confidence == "reconciled"
