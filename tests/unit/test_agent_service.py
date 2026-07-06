"""Tests for agent_service.process_resources dispatch/dedup/conflict logic."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import func, select

from app.models.agent import Agent
from app.models.agent_work import AgentWork
from app.models.channel import Channel
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.models.series import TVSeries
from app.services.agent_service import (
    RuleSet,
    _generate_llm_pick,
    _parse_llm_pick,
    _resource_matches_rules,
    compute_rule_diff,
    create_pending_decision,
    dispatch_download,
    process_resources,
    score_and_pick,
)
from app.utils.download_paths import DownloadPathError


def _uuid() -> str:
    return str(uuid.uuid4())


TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"torrent_url": {"source": "link"}},
}


@pytest.fixture
async def channel(db_session):
    ch = Channel(
        id=_uuid(), name="ch", type="rss_feed", url="https://example.com/rss",
        field_mapping=TEST_FIELD_MAPPING, metadata_agent_enabled=False, status="active",
    )
    db_session.add(ch)
    await db_session.flush()
    return ch


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
async def series(db_session):
    s = TVSeries(id=_uuid(), title_cn="剧集A", title_en="Series A", content_type="tv")
    db_session.add(s)
    await db_session.flush()
    return s


@pytest.fixture
async def series_b(db_session):
    s = TVSeries(id=_uuid(), title_cn="剧集B", title_en="Series B", content_type="tv")
    db_session.add(s)
    await db_session.flush()
    return s


@pytest.fixture
async def movie(db_session):
    m = Movie(id=_uuid(), title_cn="电影A", title_en="Movie A", content_type="movie")
    db_session.add(m)
    await db_session.flush()
    return m


def _make_resource(channel_id: str, **overrides) -> FileResource:
    base = dict(
        id=_uuid(), channel_id=channel_id, guid=_uuid(),
        title_raw="[G] Title - 01 [1080p]",
        torrent_url="magnet:?xt=urn:btih:abc",
        resolution="1080p", subtitle_group="G", container="MKV",
        video_codec="HEVC", audio_codec="AAC",
        search_title="Title",
        episode=1, season=1, file_size=1_000_000_000,
        parsed_at=datetime.now(UTC),
    )
    base.update(overrides)
    return FileResource(**base)


# ---------------------------------------------------------------------------
# dispatch_download
# ---------------------------------------------------------------------------


class TestDispatchDownload:
    async def test_success_sets_downloading(self, db_session, channel, downloader):
        agent = Agent(
            id=_uuid(), name="a", channel_id=channel.id,
            downloader_id=downloader.id, status="active",
            download_subdir="Anime/2026",
            scope_channel_wide=True, conflict_resolution="ask",
        )
        db_session.add(agent)
        await db_session.flush()
        res = _make_resource(channel.id, series_id=None, movie_id=None)
        db_session.add(res)
        await db_session.flush()

        # Patch TransmissionWrapper.add_torrent directly (it is an async method).
        with patch(
            "app.clients.transmission.TransmissionWrapper.add_torrent",
            new_callable=AsyncMock,
            return_value={"torrent_id": 7, "name": "x", "hash": "h"},
        ) as add_torrent:
            task = await dispatch_download(agent, res, db_session)

        assert task.status == "downloading"
        assert task.transmission_torrent_id == 7
        assert task.agent_id == agent.id
        assert task.download_dir == "/downloads/rssripple/Anime/2026"
        add_torrent.assert_awaited_once_with(
            res.torrent_url,
            download_dir="/downloads/rssripple/Anime/2026",
        )

    async def test_failure_sets_error(self, db_session, channel, downloader):
        agent = Agent(
            id=_uuid(), name="a", channel_id=channel.id,
            downloader_id=downloader.id, status="active",
            scope_channel_wide=True, conflict_resolution="ask",
        )
        db_session.add(agent)
        await db_session.flush()
        res = _make_resource(channel.id)
        db_session.add(res)
        await db_session.flush()

        async def _raise(*a, **kw):
            raise RuntimeError("connection refused")

        client_instance = MagicMock()
        client_instance.add_torrent = MagicMock(side_effect=RuntimeError("connection refused"))
        # Patch wrapper.add_torrent (an async method) via the class method
        with patch(
            "app.clients.transmission.TransmissionWrapper.add_torrent",
            new_callable=AsyncMock,
            side_effect=RuntimeError("connection refused"),
        ):
            task = await dispatch_download(agent, res, db_session)
        assert task.status == "error"
        assert "connection refused" in task.error_message

    async def test_missing_downloader_record_sets_error(self, db_session, channel, downloader, monkeypatch):
        agent = Agent(
            id=_uuid(), name="a", channel_id=channel.id,
            downloader_id=downloader.id, status="active",
            scope_channel_wide=True, conflict_resolution="ask",
        )
        db_session.add(agent)
        await db_session.flush()
        res = _make_resource(channel.id)
        db_session.add(res)
        await db_session.flush()
        monkeypatch.setattr(db_session, "get", AsyncMock(return_value=None))
        task = await dispatch_download(agent, res, db_session)
        assert task.status == "error"
        assert "not found" in task.error_message

    async def test_no_downloader_id_sets_error(self, db_session, channel, downloader, monkeypatch):
        """When DB lookup of the downloader returns None, task is created with error status."""
        agent = Agent(
            id=_uuid(), name="a", channel_id=channel.id,
            downloader_id=downloader.id, status="active",
            scope_channel_wide=True, conflict_resolution="ask",
        )
        db_session.add(agent)
        await db_session.flush()
        res = _make_resource(channel.id, series_id=None, movie_id=None)
        db_session.add(res)
        await db_session.flush()
        # Mock db.get to return None for DownloaderInstance lookup
        monkeypatch.setattr(db_session, "get", AsyncMock(return_value=None))
        task = await dispatch_download(agent, res, db_session)
        assert task.status == "error"
        assert "not found" in task.error_message

    async def test_download_path_error_sets_error(self, db_session, channel, downloader):
        """When resolve_download_dir raises DownloadPathError, task gets error status."""
        agent = Agent(
            id=_uuid(), name="a", channel_id=channel.id,
            downloader_id=downloader.id, status="active",
            download_subdir="valid/subdir",
            scope_channel_wide=True, conflict_resolution="ask",
        )
        db_session.add(agent)
        await db_session.flush()
        res = _make_resource(channel.id)
        db_session.add(res)
        await db_session.flush()

        with patch(
            "app.services.agent_service.resolve_download_dir",
            side_effect=DownloadPathError("download_subdir escapes downloader download_dir"),
        ):
            task = await dispatch_download(agent, res, db_session)

        assert task.status == "error"
        assert "escapes" in task.error_message
        # download_dir falls back to the downloader root directory
        assert task.download_dir == downloader.download_dir


# ---------------------------------------------------------------------------
# _generate_llm_pick
# ---------------------------------------------------------------------------


class TestGenerateLlmPick:
    async def test_returns_none_when_llm_disabled(self, db_session, channel, downloader):
        """When agent.llm_enabled is False, returns (None, None) immediately."""
        agent = Agent(
            id=_uuid(), name="a", channel_id=channel.id,
            downloader_id=downloader.id, scope_channel_wide=True,
            llm_enabled=False,
        )
        db_session.add(agent)
        await db_session.flush()
        res = _make_resource(channel.id)
        result = await _generate_llm_pick(agent, [res], ("series", "x", 1))
        assert result == (None, None)

    async def test_returns_none_when_no_api_key(self, db_session, channel, downloader, monkeypatch):
        """When settings.llm_api_key is empty, returns (None, None)."""
        agent = Agent(
            id=_uuid(), name="a", channel_id=channel.id,
            downloader_id=downloader.id, scope_channel_wide=True,
            llm_enabled=True,
        )
        db_session.add(agent)
        await db_session.flush()
        res = _make_resource(channel.id)

        from app.config import settings
        monkeypatch.setattr(settings, "llm_api_key", "")
        result = await _generate_llm_pick(agent, [res], ("series", "x", 1))
        assert result == (None, None)

    async def test_returns_none_when_llm_call_fails(self, db_session, channel, downloader, monkeypatch):
        """When call_llm raises an exception, returns (None, None) gracefully."""
        agent = Agent(
            id=_uuid(), name="a", channel_id=channel.id,
            downloader_id=downloader.id, scope_channel_wide=True,
            llm_enabled=True,
        )
        db_session.add(agent)
        await db_session.flush()
        res = _make_resource(channel.id)

        from app.config import settings
        monkeypatch.setattr(settings, "llm_api_key", "test-key-123")

        with patch(
            "app.services.feed_analyzer.call_llm",
            new_callable=AsyncMock,
            side_effect=RuntimeError("LLM API timeout"),
        ):
            result = await _generate_llm_pick(agent, [res], ("series", "x", 1))
        assert result == (None, None)

    async def test_returns_pick_and_reason_on_success(self, db_session, channel, downloader, monkeypatch):
        """When call_llm returns JSON, returns (picked_id, reason)."""
        agent = Agent(
            id=_uuid(), name="a", channel_id=channel.id,
            downloader_id=downloader.id, scope_channel_wide=True,
            llm_enabled=True,
        )
        db_session.add(agent)
        await db_session.flush()
        r1 = _make_resource(channel.id, episode=1)
        r2 = _make_resource(channel.id, episode=2)
        db_session.add_all([r1, r2])
        await db_session.flush()

        from app.config import settings
        monkeypatch.setattr(settings, "llm_api_key", "test-key-123")

        with patch(
            "app.services.feed_analyzer.call_llm",
            new_callable=AsyncMock,
            return_value='{"pick": 2, "reason": "higher resolution"}',
        ):
            picked_id, reason = await _generate_llm_pick(agent, [r1, r2], ("series", "x", 1))
        assert picked_id == r2.id
        assert reason == "higher resolution"


def test_parse_llm_pick_json():
    assert _parse_llm_pick('{"pick": 1, "reason": "best"}', 2) == (1, "best")


def test_parse_llm_pick_markdown_wrapped():
    assert _parse_llm_pick('```json\n{"pick": 2, "reason": "ok"}\n```', 2) == (2, "ok")


def test_parse_llm_pick_leading_number_fallback():
    """When no JSON, a leading 'pick N' still parses."""
    assert _parse_llm_pick("Pick 2 because higher res", 2)[0] == 2


def test_parse_llm_pick_out_of_range_returns_none_pick():
    assert _parse_llm_pick('{"pick": 5, "reason": "x"}', 2)[0] is None


def test_parse_llm_pick_garbage():
    assert _parse_llm_pick("no idea", 2) == (None, "no idea")


# ---------------------------------------------------------------------------
# score_and_pick
# ---------------------------------------------------------------------------


def test_score_and_pick_prefers_higher_resolution(channel, downloader):
    r1 = _make_resource(channel.id, resolution="1080p", file_size=500,
                        published_at=datetime(2024, 1, 1, tzinfo=UTC))
    r2 = _make_resource(channel.id, resolution="2160p", file_size=100,
                        published_at=datetime(2023, 1, 1, tzinfo=UTC))
    agent = Agent(id=_uuid(), name="a", channel_id=channel.id,
                  downloader_id=downloader.id, scope_channel_wide=True,
                  conflict_resolution="auto")
    assert score_and_pick([r1, r2], None, agent).id == r2.id


# ---------------------------------------------------------------------------
# process_resources
# ---------------------------------------------------------------------------


class TestProcessResources:
    async def _make_agent(
        self, db_session, channel, downloader, *,
        scope_channel_wide=False, conflict_resolution="ask",
        filter_config=None, works=None,
    ):
        agent = Agent(
            id=_uuid(), name="agent", channel_id=channel.id,
            downloader_id=downloader.id, status="active",
            scope_channel_wide=scope_channel_wide,
            conflict_resolution=conflict_resolution,
            filter_config=filter_config,
        )
        db_session.add(agent)
        await db_session.flush()
        if works:
            for w in works:
                db_session.add(AgentWork(agent_id=agent.id, **w))
            await db_session.flush()
        await db_session.refresh(agent)
        return agent

    @pytest.fixture(autouse=True)
    def patch_transmission(self):
        # Patch the low-level transmission_rpc.Client inside the wrapper so we
        # avoid real RPC calls regardless of how the wrapper is imported.
        client_cls = MagicMock()
        client_instance = MagicMock()
        client_instance.add_torrent = MagicMock(
            return_value=SimpleNamespace(id=1, name="x", hashString="h")
        )
        client_cls.return_value = client_instance
        with patch("transmission_rpc.Client", client_cls):
            yield client_instance

    async def test_resource_without_metadata_goes_to_suggestions(
        self, db_session, channel, downloader, series
    ):
        agent = await self._make_agent(
            db_session, channel, downloader,
            scope_channel_wide=True,
        )
        res = _make_resource(channel.id, series_id=None, movie_id=None)
        db_session.add(res)
        await db_session.flush()
        result = await process_resources(agent, [res], db_session)
        assert result.unrecognized == 1
        assert result.dispatched == 0
        assert len(result.suggestions) >= 1

    async def test_resource_not_matching_work_skipped(
        self, db_session, channel, downloader, series, series_b
    ):
        """Agent subscribes to series; resource for series_b is skipped."""
        agent = await self._make_agent(
            db_session, channel, downloader,
            works=[{"content_type": "tv", "series_id": series.id,
                    "enable_episode_dedup": True}],
        )
        res = _make_resource(channel.id, series_id=series_b.id, episode=1)
        db_session.add(res)
        await db_session.flush()
        result = await process_resources(agent, [res], db_session)
        assert result.total_resources == 1
        assert result.matched == 0
        assert result.dispatched == 0

    async def test_filter_match_and_fail(
        self, db_session, channel, downloader, series
    ):
        agent = await self._make_agent(
            db_session, channel, downloader,
            scope_channel_wide=True,
            filter_config={"combinator": "and", "conditions": [
                {"field": "resolution", "operator": "eq", "value": "2160p"},
            ]},
        )
        ok = _make_resource(channel.id, series_id=series.id,
                            episode=1, resolution="2160p")
        bad = _make_resource(channel.id, series_id=series.id,
                             episode=2, resolution="720p")
        db_session.add_all([ok, bad])
        await db_session.flush()
        result = await process_resources(agent, [ok, bad], db_session)
        assert result.matched == 1
        assert result.filter_failed == 1
        assert result.dispatched == 1

    async def test_tv_episode_dedup(
        self, db_session, channel, downloader, series
    ):
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True
        )
        r1 = _make_resource(channel.id, series_id=series.id,
                            episode=3, guid=_uuid())
        r2 = _make_resource(channel.id, series_id=series.id,
                            episode=3, guid=_uuid())
        db_session.add_all([r1, r2])
        await db_session.flush()
        # Seed an existing completed task for r1's episode
        task = DownloadTask(
            id=_uuid(), agent_id=agent.id, file_resource_id=r1.id,
            downloader_id=downloader.id, download_dir="/downloads/rssripple",
            status="completed",
        )
        db_session.add(task)
        await db_session.flush()
        # Note: process_resources dedupes via existing tasks at query time,
        # but only if r1/r2 are in the same run. For two fresh resources of
        # same ep, they both go to candidates and create a pending decision.
        # Let's test that: with the existing task, neither will dispatch.
        result = await process_resources(agent, [r2], db_session)
        assert result.duplicates_skipped == 1
        assert result.dispatched == 0

    async def test_movie_dedup(
        self, db_session, channel, downloader, movie
    ):
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True
        )
        r1 = _make_resource(channel.id, movie_id=movie.id,
                            episode=None, season=None, guid=_uuid())
        db_session.add(r1)
        await db_session.flush()
        task = DownloadTask(
            id=_uuid(), agent_id=agent.id, file_resource_id=r1.id,
            downloader_id=downloader.id, download_dir="/downloads/rssripple",
            status="completed",
        )
        db_session.add(task)
        await db_session.flush()
        r2 = _make_resource(channel.id, movie_id=movie.id,
                            episode=None, season=None, guid=_uuid())
        db_session.add(r2)
        await db_session.flush()
        result = await process_resources(agent, [r2], db_session)
        assert result.duplicates_skipped == 1
        assert result.dispatched == 0

    async def test_scope_channel_wide_dispatches_all_linked(
        self, db_session, channel, downloader, series
    ):
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True
        )
        r = _make_resource(channel.id, series_id=series.id, episode=5)
        db_session.add(r)
        await db_session.flush()
        result = await process_resources(agent, [r], db_session)
        assert result.matched == 1
        assert result.dispatched == 1

    async def test_conflict_ask_creates_pending_decision(
        self, db_session, channel, downloader, series
    ):
        agent = await self._make_agent(
            db_session, channel, downloader,
            scope_channel_wide=True, conflict_resolution="ask",
        )
        r1 = _make_resource(channel.id, series_id=series.id,
                            episode=5, guid=_uuid(), resolution="1080p")
        r2 = _make_resource(channel.id, series_id=series.id,
                            episode=5, guid=_uuid(), resolution="2160p")
        db_session.add_all([r1, r2])
        await db_session.flush()
        result = await process_resources(agent, [r1, r2], db_session)
        assert result.pending_decisions == 1
        assert result.dispatched == 0
        cnt = (await db_session.execute(
            select(func.count()).select_from(PendingDecision)
        )).scalar_one()
        assert cnt == 1

    async def test_conflict_auto_picks(
        self, db_session, channel, downloader, series
    ):
        agent = await self._make_agent(
            db_session, channel, downloader,
            scope_channel_wide=True, conflict_resolution="auto",
        )
        r1 = _make_resource(channel.id, series_id=series.id,
                            episode=5, guid=_uuid(),
                            resolution="1080p", file_size=500)
        r2 = _make_resource(channel.id, series_id=series.id,
                            episode=5, guid=_uuid(),
                            resolution="2160p", file_size=100)
        db_session.add_all([r1, r2])
        await db_session.flush()
        result = await process_resources(agent, [r1, r2], db_session)
        assert result.dispatched == 1
        assert result.pending_decisions == 0

    async def test_filter_overrides_merged(
        self, db_session, channel, downloader, series
    ):
        """Per-work filter_overrides forces container=MKV; resources with
        MP4 fail even though global filter is empty (pass-all)."""
        agent = await self._make_agent(
            db_session, channel, downloader,
            works=[{
                "content_type": "tv",
                "series_id": series.id,
                "enable_episode_dedup": True,
                "filter_overrides": {
                    "combinator": "and",
                    "conditions": [
                        {"field": "container", "operator": "eq", "value": "MKV"},
                    ],
                },
            }],
        )
        ok = _make_resource(channel.id, series_id=series.id,
                            episode=1, container="MKV", guid=_uuid())
        bad = _make_resource(channel.id, series_id=series.id,
                             episode=2, container="MP4", guid=_uuid())
        db_session.add_all([ok, bad])
        await db_session.flush()
        result = await process_resources(agent, [ok, bad], db_session)
        assert result.matched == 1
        assert result.filter_failed == 1
        assert result.dispatched == 1

    async def test_disable_episode_dedup_allows_dupes(
        self, db_session, channel, downloader, series
    ):
        """With enable_episode_dedup=False, same episode is not deduped and
        becomes a conflict (ask mode) instead of being skipped."""
        agent = await self._make_agent(
            db_session, channel, downloader,
            works=[{
                "content_type": "tv",
                "series_id": series.id,
                "enable_episode_dedup": False,
            }],
            conflict_resolution="ask",
        )
        r1 = _make_resource(channel.id, series_id=series.id,
                            episode=3, guid=_uuid())
        # Pre-existing completed task for ep3
        db_session.add(r1)
        await db_session.flush()
        existing = DownloadTask(
            id=_uuid(), agent_id=agent.id, file_resource_id=r1.id,
            downloader_id=downloader.id, download_dir="/downloads/rssripple",
            status="completed",
        )
        db_session.add(existing)
        await db_session.flush()
        r2 = _make_resource(channel.id, series_id=series.id,
                            episode=3, guid=_uuid())
        db_session.add(r2)
        await db_session.flush()
        result = await process_resources(agent, [r2], db_session)
        # Not deduped; matched. Single resource dispatches.
        assert result.duplicates_skipped == 0
        assert result.matched == 1
        assert result.dispatched == 1

    async def test_batch_resource_bypasses_dedup(
        self, db_session, channel, downloader, series
    ):
        """Batch resource dispatches directly, ignoring the (series_id, episode)
        conflict aggregation."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True,
        )
        r = _make_resource(
            channel.id, series_id=series.id, episode=None,
            is_batch=True, episode_start=1, episode_end=13,
        )
        db_session.add(r)
        await db_session.flush()
        result = await process_resources(agent, [r], db_session)
        assert result.matched == 1
        assert result.dispatched == 1
        assert result.pending_decisions == 0

    async def test_batch_and_single_do_not_conflict(
        self, db_session, channel, downloader, series
    ):
        """A batch and a single-episode resource in the same run both get
        dispatched without triggering a PendingDecision — the batch bypasses
        the per-episode conflict aggregation entirely."""
        agent = await self._make_agent(
            db_session, channel, downloader,
            scope_channel_wide=True, conflict_resolution="ask",
        )
        batch = _make_resource(
            channel.id, series_id=series.id, episode=None,
            is_batch=True, episode_start=1, episode_end=13, guid=_uuid(),
        )
        single = _make_resource(
            channel.id, series_id=series.id, episode=5, guid=_uuid(),
        )
        db_session.add_all([batch, single])
        await db_session.flush()

        result = await process_resources(agent, [batch, single], db_session)
        # Both get dispatched, no PendingDecision.
        assert result.dispatched == 2
        assert result.pending_decisions == 0
        assert result.matched == 2

    async def test_batch_same_resource_not_redispatched(
        self, db_session, channel, downloader, series
    ):
        """A batch resource with an active/completed task is not re-dispatched
        for the same FileResource (protects against re-runs / crash recovery)."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True,
        )
        r = _make_resource(
            channel.id, series_id=series.id, episode=None,
            is_batch=True, episode_start=1, episode_end=12,
        )
        db_session.add(r)
        await db_session.flush()
        db_session.add(DownloadTask(
            id=_uuid(), agent_id=agent.id, file_resource_id=r.id,
            downloader_id=downloader.id, download_dir="/downloads/rssripple",
            status="downloading",
        ))
        await db_session.flush()
        result = await process_resources(agent, [r], db_session)
        assert result.dispatched == 0
        assert result.duplicates_skipped == 1

    async def test_ambiguous_episode_routes_to_pending_decision(
        self, db_session, channel, downloader, series
    ):
        """Resources whose episode_confidence=='ambiguous' must not be
        dispatched — they're routed to a PendingDecision so the user can
        confirm the correct per-season episode number before download."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True,
        )
        r = _make_resource(
            channel.id, series_id=series.id, episode=200,
            episode_confidence="ambiguous",
        )
        db_session.add(r)
        await db_session.flush()
        result = await process_resources(agent, [r], db_session)
        assert result.dispatched == 0
        assert result.pending_decisions == 1
        assert len(result.suggestions) == 0
        pds = (await db_session.execute(
            select(PendingDecision).where(PendingDecision.agent_id == agent.id)
        )).scalars().all()
        assert len(pds) == 1
        assert pds[0].status == "pending"
        assert "集号不确定" in pds[0].reason
        assert r.id in (pds[0].candidates or [])
        # Ambiguous decisions carry no "pick the best candidate" semantics,
        # so the LLM suggestion must be skipped.
        assert pds[0].llm_suggestion is None

    async def test_ambiguous_decision_resolved_after_correction(
        self, db_session, channel, downloader, series
    ):
        """Once the user corrects the episode (confidence='manual'), the next
        run resolves the stale ambiguous PendingDecision and dispatches."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True,
        )
        r = _make_resource(
            channel.id, series_id=series.id, episode=12,
            episode_confidence="ambiguous",
        )
        db_session.add(r)
        await db_session.flush()
        # First run: ambiguous → PD created, nothing dispatched.
        first = await process_resources(agent, [r], db_session)
        assert first.pending_decisions == 1
        assert first.dispatched == 0
        # User corrects the episode via PATCH /resources/{id}/episode.
        r.episode_confidence = "manual"
        await db_session.flush()
        # Second run: resource passes the ambiguous gate, dispatches; the
        # stale ambiguous PD is marked decided by the cleanup pass.
        second = await process_resources(agent, [r], db_session)
        assert second.dispatched == 1
        pds = (await db_session.execute(
            select(PendingDecision).where(PendingDecision.agent_id == agent.id)
        )).scalars().all()
        assert len(pds) == 1
        assert pds[0].status == "decided"


# ---------------------------------------------------------------------------
# create_pending_decision
# ---------------------------------------------------------------------------


async def test_create_pending_decision_sets_fields(db_session, channel, downloader, series):
    agent = Agent(
        id=_uuid(), name="a", channel_id=channel.id,
        downloader_id=downloader.id, scope_channel_wide=True,
    )
    db_session.add(agent)
    await db_session.flush()
    r = _make_resource(channel.id, series_id=series.id, episode=2)
    db_session.add(r)
    await db_session.flush()
    pd = await create_pending_decision(
        agent, ("series", series.id, 2), [r], db_session
    )
    assert pd.series_id == series.id
    assert pd.movie_id is None
    assert pd.episode == 2
    assert r.id in pd.candidates
    assert pd.status == "pending"


async def test_create_pending_decision_movie_no_episode(db_session, channel, downloader, movie):
    """Movie-type pending decision: episode is None, reason mentions 电影."""
    agent = Agent(
        id=_uuid(), name="a", channel_id=channel.id,
        downloader_id=downloader.id, scope_channel_wide=True,
    )
    db_session.add(agent)
    await db_session.flush()
    r = _make_resource(channel.id, movie_id=movie.id, episode=None, season=None)
    db_session.add(r)
    await db_session.flush()
    pd = await create_pending_decision(
        agent, ("movie", movie.id, None), [r], db_session
    )
    assert pd.movie_id == movie.id
    assert pd.series_id is None
    assert pd.episode is None
    assert "电影" in pd.reason
    assert r.id in pd.candidates
    assert pd.status == "pending"


async def test_create_pending_decision_series_no_episode(db_session, channel, downloader, series):
    """Series-type pending decision with episode=None: reason omits episode number."""
    agent = Agent(
        id=_uuid(), name="a", channel_id=channel.id,
        downloader_id=downloader.id, scope_channel_wide=True,
    )
    db_session.add(agent)
    await db_session.flush()
    r = _make_resource(channel.id, series_id=series.id, episode=None)
    db_session.add(r)
    await db_session.flush()
    pd = await create_pending_decision(
        agent, ("series", series.id, None), [r], db_session
    )
    assert pd.series_id == series.id
    assert pd.episode is None
    # Should NOT contain "第XX集" since episode is None
    assert "第" not in pd.reason
    assert "剧集A" in pd.reason or "Series A" in pd.reason


async def test_create_pending_decision_is_idempotent(db_session, channel, downloader, series):
    """Same (agent, series, episode) key must upsert into one row instead of
    piling up duplicates across re-runs. Regression coverage for the 76-rows-
    for-4-episodes bug seen with the bangumi-2026S2 agent."""
    from sqlalchemy import func
    from sqlalchemy import select as sql_select
    agent = Agent(
        id=_uuid(), name="a", channel_id=channel.id,
        downloader_id=downloader.id, scope_channel_wide=True,
    )
    db_session.add(agent)
    await db_session.flush()

    r1 = _make_resource(channel.id, series_id=series.id, episode=5, guid=_uuid())
    r2 = _make_resource(channel.id, series_id=series.id, episode=5, guid=_uuid())
    r3 = _make_resource(channel.id, series_id=series.id, episode=5, guid=_uuid())
    db_session.add_all([r1, r2, r3])
    await db_session.flush()

    pd1 = await create_pending_decision(agent, ("series", series.id, 5), [r1, r2], db_session)
    pd2 = await create_pending_decision(agent, ("series", series.id, 5), [r2, r3], db_session)
    # Same row reused
    assert pd1.id == pd2.id
    # Candidates merged (r1, r2, r3), order preserved
    assert pd2.candidates == [r1.id, r2.id, r3.id]
    total = (await db_session.execute(
        sql_select(func.count()).select_from(PendingDecision)
    )).scalar_one()
    assert total == 1


async def test_create_pending_decision_idempotent_movie(db_session, channel, downloader, movie):
    """Same idempotency guarantee for movie-typed decisions (episode=None)."""
    from sqlalchemy import func
    from sqlalchemy import select as sql_select
    agent = Agent(
        id=_uuid(), name="a", channel_id=channel.id,
        downloader_id=downloader.id, scope_channel_wide=True,
    )
    db_session.add(agent)
    await db_session.flush()
    r1 = _make_resource(channel.id, movie_id=movie.id, episode=None, season=None, guid=_uuid())
    r2 = _make_resource(channel.id, movie_id=movie.id, episode=None, season=None, guid=_uuid())
    db_session.add_all([r1, r2])
    await db_session.flush()

    await create_pending_decision(agent, ("movie", movie.id, None), [r1], db_session)
    await create_pending_decision(agent, ("movie", movie.id, None), [r1, r2], db_session)
    total = (await db_session.execute(
        sql_select(func.count()).select_from(PendingDecision)
    )).scalar_one()
    assert total == 1


# ---------------------------------------------------------------------------
# process_resources – edge cases
# ---------------------------------------------------------------------------


class TestProcessResourcesEdgeCases:
    async def _make_agent(
        self, db_session, channel, downloader, *,
        scope_channel_wide=False, conflict_resolution="ask",
        filter_config=None, works=None, llm_enabled=False,
    ):
        agent = Agent(
            id=_uuid(), name="agent", channel_id=channel.id,
            downloader_id=downloader.id, status="active",
            scope_channel_wide=scope_channel_wide,
            conflict_resolution=conflict_resolution,
            filter_config=filter_config,
            llm_enabled=llm_enabled,
        )
        db_session.add(agent)
        await db_session.flush()
        if works:
            for w in works:
                db_session.add(AgentWork(agent_id=agent.id, **w))
            await db_session.flush()
        await db_session.refresh(agent)
        return agent

    @pytest.fixture(autouse=True)
    def patch_transmission(self):
        client_cls = MagicMock()
        client_instance = MagicMock()
        client_instance.add_torrent = MagicMock(
            return_value=SimpleNamespace(id=1, name="x", hashString="h")
        )
        client_cls.return_value = client_instance
        with patch("transmission_rpc.Client", client_cls):
            yield client_instance

    async def test_exception_during_candidate_processing(
        self, db_session, channel, downloader, series
    ):
        """When dispatch_download raises, the error is captured in result.errors."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True,
        )
        r = _make_resource(channel.id, series_id=series.id, episode=1)
        db_session.add(r)
        await db_session.flush()

        with patch(
            "app.services.agent_service.dispatch_download",
            new_callable=AsyncMock,
            side_effect=RuntimeError("dispatch boom"),
        ):
            result = await process_resources(agent, [r], db_session)

        assert len(result.errors) == 1
        assert "dispatch boom" in result.errors[0]
        assert result.dispatched == 0

    async def test_suggestions_fuzzy_clustering(
        self, db_session, channel, downloader
    ):
        """Two unrecognized resources with similar titles are grouped together."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True,
        )
        r1 = _make_resource(
            channel.id, series_id=None, movie_id=None,
            search_title="Attack on Titan Season 4",
            title_raw="[Group] Attack on Titan Season 4 - 01 [1080p]",
            guid=_uuid(),
        )
        r2 = _make_resource(
            channel.id, series_id=None, movie_id=None,
            search_title="Attack on Titan Season 4 Part 2",
            title_raw="[Group] Attack on Titan Season 4 Part 2 - 02 [1080p]",
            guid=_uuid(),
        )
        db_session.add_all([r1, r2])
        await db_session.flush()

        result = await process_resources(agent, [r1, r2], db_session)
        assert result.unrecognized == 2
        # The two similar titles should be clustered into 1 suggestion group
        assert len(result.suggestions) == 1
        assert len(result.suggestions[0]["resources"]) == 2

    async def test_suggestions_dissimilar_titles_separate_groups(
        self, db_session, channel, downloader
    ):
        """Two unrecognized resources with very different titles get separate groups."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True,
        )
        r1 = _make_resource(
            channel.id, series_id=None, movie_id=None,
            search_title="Attack on Titan",
            title_raw="[G] Attack on Titan - 01",
            guid=_uuid(),
        )
        r2 = _make_resource(
            channel.id, series_id=None, movie_id=None,
            search_title="One Piece",
            title_raw="[G] One Piece - 1000",
            guid=_uuid(),
        )
        db_session.add_all([r1, r2])
        await db_session.flush()

        result = await process_resources(agent, [r1, r2], db_session)
        assert result.unrecognized == 2
        assert len(result.suggestions) == 2

    async def test_scope_channel_wide_movie_dispatch(
        self, db_session, channel, downloader, movie
    ):
        """scope_channel_wide=True with a movie resource (work=None, movie dedup path)."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True,
        )
        r = _make_resource(
            channel.id, movie_id=movie.id, episode=None, season=None,
        )
        db_session.add(r)
        await db_session.flush()

        result = await process_resources(agent, [r], db_session)
        assert result.matched == 1
        assert result.dispatched == 1

    async def test_scope_channel_wide_with_filter_no_work(
        self, db_session, channel, downloader, series
    ):
        """scope_channel_wide=True with filter_config but no work (work=None).
        Filter is evaluated with work.filter_overrides=None."""
        agent = await self._make_agent(
            db_session, channel, downloader,
            scope_channel_wide=True,
            filter_config={"combinator": "and", "conditions": [
                {"field": "resolution", "operator": "eq", "value": "1080p"},
            ]},
        )
        ok = _make_resource(channel.id, series_id=series.id,
                            episode=1, resolution="1080p")
        bad = _make_resource(channel.id, series_id=series.id,
                             episode=2, resolution="720p")
        db_session.add_all([ok, bad])
        await db_session.flush()

        result = await process_resources(agent, [ok, bad], db_session)
        assert result.matched == 1
        assert result.filter_failed == 1
        assert result.dispatched == 1

    async def test_work_movie_scope_dispatch(
        self, db_session, channel, downloader, movie
    ):
        """Agent subscribes to a movie via AgentWork; resource matches and dispatches."""
        agent = await self._make_agent(
            db_session, channel, downloader,
            works=[{
                "content_type": "movie",
                "movie_id": movie.id,
                "enable_episode_dedup": True,
            }],
        )
        r = _make_resource(
            channel.id, movie_id=movie.id, episode=None, season=None,
        )
        db_session.add(r)
        await db_session.flush()

        result = await process_resources(agent, [r], db_session)
        assert result.matched == 1
        assert result.dispatched == 1

    async def test_exception_during_multi_candidate_processing(
        self, db_session, channel, downloader, series
    ):
        """When create_pending_decision raises for multi-candidate key, error is captured."""
        agent = await self._make_agent(
            db_session, channel, downloader,
            scope_channel_wide=True, conflict_resolution="ask",
        )
        r1 = _make_resource(channel.id, series_id=series.id,
                            episode=5, guid=_uuid(), resolution="1080p")
        r2 = _make_resource(channel.id, series_id=series.id,
                            episode=5, guid=_uuid(), resolution="2160p")
        db_session.add_all([r1, r2])
        await db_session.flush()

        with patch(
            "app.services.agent_service.create_pending_decision",
            new_callable=AsyncMock,
            side_effect=RuntimeError("decision boom"),
        ):
            result = await process_resources(agent, [r1, r2], db_session)

        assert len(result.errors) == 1
        assert "decision boom" in result.errors[0]
        assert result.pending_decisions == 0

    async def test_suggestion_with_empty_search_title_uses_title_raw(
        self, db_session, channel, downloader
    ):
        """Unrecognized resource with empty search_title falls back to title_raw for grouping."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True,
        )
        r = _make_resource(
            channel.id, series_id=None, movie_id=None,
            search_title="", title_raw="[Group] Some Show - 01",
        )
        db_session.add(r)
        await db_session.flush()

        result = await process_resources(agent, [r], db_session)
        assert result.unrecognized == 1
        # title_raw is used as key when search_title is empty/falsy
        assert len(result.suggestions) == 1

    async def test_suggestion_with_both_titles_empty_skips_grouping(
        self, db_session, channel, downloader
    ):
        """Unrecognized resource with both search_title and title_raw empty → no suggestion."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True,
        )
        r = _make_resource(
            channel.id, series_id=None, movie_id=None,
            search_title="", title_raw="",
        )
        db_session.add(r)
        await db_session.flush()

        result = await process_resources(agent, [r], db_session)
        assert result.unrecognized == 1
        assert len(result.suggestions) == 0


# ---------------------------------------------------------------------------
# Rule diff (scenario ② preview) + watermark helpers
# ---------------------------------------------------------------------------


async def test_compute_rule_diff_newly_and_no_longer_matching(
    db_session, channel, downloader, series
):
    """Diff correctly partitions resources into newly / no-longer matching.

    old rules: subscribe to ``series`` with filter subtitle_group=OldSub.
    new rules: subscribe to ``series`` with filter subtitle_group=NewSub.
    → r_keep (NewSub) is newly_matching; r_drop (OldSub) is no_longer_matching.
    """
    _W = type("W", (), {"filter_overrides": None})
    r_keep = _make_resource(channel.id, series_id=series.id, episode=1, subtitle_group="NewSub")
    r_drop = _make_resource(channel.id, series_id=series.id, episode=2, subtitle_group="OldSub")
    db_session.add_all([r_keep, r_drop])
    await db_session.flush()

    def _filter(val: str) -> dict:
        return {"combinator": "and", "conditions": [
            {"field": "subtitle_group", "operator": "eq", "value": val},
        ]}

    old = RuleSet(
        scope_channel_wide=False, filter_config=_filter("OldSub"),
        work_by_series_id={series.id: _W()},
    )
    new = RuleSet(
        scope_channel_wide=False, filter_config=_filter("NewSub"),
        work_by_series_id={series.id: _W()},
    )
    diff = await compute_rule_diff(old, new, [r_keep, r_drop], db_session)
    assert [r.id for r in diff["newly_matching"]] == [r_keep.id]
    assert [r.id for r in diff["no_longer_matching"]] == [r_drop.id]
    assert diff["in_queue_skipped"] == 0


async def test_compute_rule_diff_excludes_tasked_from_newly_matching(
    db_session, channel, downloader, series
):
    """A newly-matching resource that already has a DownloadTask is excluded
    from the backfill candidates (counted in in_queue_skipped instead)."""
    from app.models.download_task import DownloadTask

    r = _make_resource(channel.id, series_id=series.id, episode=1, subtitle_group="G")
    db_session.add(r)
    await db_session.flush()
    db_session.add(DownloadTask(
        agent_id=None, file_resource_id=r.id, downloader_id=downloader.id,
        download_dir="/tmp", status="completed", progress=1.0,
    ))
    await db_session.flush()

    old = RuleSet(scope_channel_wide=False, filter_config=None)
    new = RuleSet(
        scope_channel_wide=True,  # channel-wide so the resource is in scope
        filter_config=None,
    )
    diff = await compute_rule_diff(old, new, [r], db_session)
    assert diff["newly_matching"] == []
    assert diff["in_queue_skipped"] == 1


def test_resource_matches_rules_channel_wide_no_filter():
    """Channel-wide + no filter matches any resource with metadata."""
    r = _make_resource("ch", series_id="s1", episode=1)
    rules = RuleSet(scope_channel_wide=True, filter_config=None)
    matched, work = _resource_matches_rules(r, rules)
    assert matched is True
    assert work is None


def test_resource_matches_rules_unsubscribed_series_no_match():
    """Non-channel-wide agent with no matching work → no match."""
    r = _make_resource("ch", series_id="s1", episode=1)
    rules = RuleSet(
        scope_channel_wide=False, filter_config=None,
        work_by_series_id={"other": type("W", (), {"filter_overrides": None})()},
    )
    matched, _ = _resource_matches_rules(r, rules)
    assert matched is False

