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
    pick_by_preferences,
    process_resources,
    resolve_torrent_payload,
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
    if base.get("is_batch") and base.get("batch_scope") is None:
        base["batch_scope"] = "season"
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

    async def test_summary_includes_title_year_rating(self, db_session, channel, downloader, monkeypatch):
        """Candidate summary sent to the LLM carries raw title plus the linked
        work's year (from start_date/release_date) and 0-10 rating."""
        import datetime as _dt

        agent = Agent(
            id=_uuid(), name="a", channel_id=channel.id,
            downloader_id=downloader.id, scope_channel_wide=True,
            llm_enabled=True,
        )
        work = TVSeries(
            id=_uuid(), title_cn="剧集X", content_type="tv",
            rating=8.5, start_date=_dt.date(2020, 4, 1),
        )
        db_session.add_all([agent, work])
        await db_session.flush()
        # Attach via the relationship so loaded_relation picks it up.
        res = _make_resource(channel.id, series=work)

        from app.config import settings
        monkeypatch.setattr(settings, "llm_api_key", "test-key-123")

        captured: dict = {}

        async def _fake_call_llm(messages, **kwargs):
            captured["messages"] = messages
            return '{"pick": 1, "reason": "ok"}'

        with patch("app.services.feed_analyzer.call_llm", new=_fake_call_llm):
            picked_id, reason = await _generate_llm_pick(agent, [res], ("series", work.id, 1))

        assert picked_id == res.id
        content = captured["messages"][1]["content"]
        assert f"title={res.title_raw}" in content
        assert "year=2020" in content
        assert "rating=8.5" in content
        assert "满分 10" in content

    async def test_summary_work_fields_null_without_link(self, db_session, channel, downloader, monkeypatch):
        """Without a linked work the summary shows null year/rating."""
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

        captured: dict = {}

        async def _fake_call_llm(messages, **kwargs):
            captured["messages"] = messages
            return '{"pick": 1, "reason": "ok"}'

        with patch("app.services.feed_analyzer.call_llm", new=_fake_call_llm):
            await _generate_llm_pick(agent, [res], ("series", "x", 1))

        content = captured["messages"][1]["content"]
        assert "year=None" in content
        assert "rating=None" in content


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


def test_score_and_pick_language_tiebreak(channel, downloader):
    """When resolution/size/published are identical, the heuristic fallback
    prefers 简体 (zh-CN) over 繁体 (zh-TW) over no Chinese track."""
    base = dict(resolution="1080p", file_size=500)
    r_tw = _make_resource(channel.id, subtitle_langs=["zh-TW", "ja"], **base)
    r_cn = _make_resource(channel.id, subtitle_langs=["ja", "zh-CN"], **base)
    agent = Agent(id=_uuid(), name="a", channel_id=channel.id,
                  downloader_id=downloader.id, scope_channel_wide=True,
                  conflict_resolution="auto")
    assert score_and_pick([r_tw, r_cn], None, agent).id == r_cn.id


# ---------------------------------------------------------------------------
# pick_by_preferences
# ---------------------------------------------------------------------------


def test_pick_by_preferences_unique_winner(channel):
    """Two same-episode releases differing only in subtitle language: a
    zh-CN preference deterministically picks the GB release."""
    big5 = _make_resource(channel.id, subtitle_langs=["zh-TW"])
    gb = _make_resource(channel.id, subtitle_langs=["zh-CN"])
    prefs = [{"field": "subtitle_langs", "operator": "contains", "value": "zh-CN"}]
    tier, deciding = pick_by_preferences([big5, gb], prefs)
    assert [r.id for r in tier] == [gb.id]
    assert deciding == prefs[0]


def test_pick_by_preferences_rule_order_matters(channel):
    """Lexicographic: the first discriminating rule wins; swapping rule
    order swaps the winner."""
    a = _make_resource(channel.id, resolution="2160p", subtitle_group="G1")
    b = _make_resource(channel.id, resolution="1080p", subtitle_group="G2")
    res_rule = {"field": "resolution", "operator": "eq", "value": "1080p"}
    grp_rule = {"field": "subtitle_group", "operator": "eq", "value": "G1"}
    tier, _ = pick_by_preferences([a, b], [res_rule, grp_rule])
    assert [r.id for r in tier] == [b.id]
    tier, _ = pick_by_preferences([a, b], [grp_rule, res_rule])
    assert [r.id for r in tier] == [a.id]


def test_pick_by_preferences_never_filters(channel):
    """A rule matching nothing (or everything) cannot shrink the pool — it
    is skipped as non-discriminating and the full list comes back."""
    r1 = _make_resource(channel.id, subtitle_group="G1")
    r2 = _make_resource(channel.id, subtitle_group="G2")
    no_match = [{"field": "subtitle_group", "operator": "eq", "value": "GX"}]
    tier, deciding = pick_by_preferences([r1, r2], no_match)
    assert {r.id for r in tier} == {r1.id, r2.id}
    assert deciding is None
    # No preferences at all → identity.
    tier, deciding = pick_by_preferences([r1, r2], None)
    assert len(tier) == 2 and deciding is None
    tier, deciding = pick_by_preferences([r1, r2], [])
    assert len(tier) == 2 and deciding is None


def test_pick_by_preferences_tie_returns_pool(channel):
    r1 = _make_resource(channel.id, subtitle_langs=["zh-CN"], resolution="1080p")
    r2 = _make_resource(channel.id, subtitle_langs=["zh-CN"], resolution="1080p")
    prefs = [{"field": "subtitle_langs", "operator": "contains", "value": "zh-CN"}]
    tier, deciding = pick_by_preferences([r1, r2], prefs)
    assert {r.id for r in tier} == {r1.id, r2.id}
    assert deciding is None


def test_pick_by_preferences_skips_malformed_rule(channel):
    r1 = _make_resource(channel.id, subtitle_group="G1")
    r2 = _make_resource(channel.id, subtitle_group="G2")
    prefs = [
        {"field": "no_such_field", "operator": "eq", "value": "x"},
        {"field": "subtitle_group", "operator": "eq", "value": "G2"},
    ]
    tier, deciding = pick_by_preferences([r1, r2], prefs)
    assert [r.id for r in tier] == [r2.id]
    assert deciding == prefs[1]


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

    async def test_resource_without_metadata_stays_channel_scoped(
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
        assert result.suggestions == []

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

    async def test_episode_dedup_is_season_aware(
        self, db_session, channel, downloader, series
    ):
        """S1E3 and S4E3 of the same series must NOT dedup against each
        other; same (series, season, episode) must still dedup."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True
        )
        r_s1 = _make_resource(channel.id, series_id=series.id,
                              season=1, episode=3, guid=_uuid())
        db_session.add(r_s1)
        await db_session.flush()
        task = DownloadTask(
            id=_uuid(), agent_id=agent.id, file_resource_id=r_s1.id,
            downloader_id=downloader.id, download_dir="/downloads/rssripple",
            status="completed",
        )
        db_session.add(task)
        await db_session.flush()

        # Different season, same episode → not a duplicate.
        r_s4 = _make_resource(channel.id, series_id=series.id,
                              season=4, episode=3, guid=_uuid())
        # Same season + episode → duplicate.
        r_s1_dup = _make_resource(channel.id, series_id=series.id,
                                  season=1, episode=3, guid=_uuid())
        db_session.add_all([r_s4, r_s1_dup])
        await db_session.flush()
        result = await process_resources(agent, [r_s4, r_s1_dup], db_session)
        assert result.duplicates_skipped == 1
        assert result.dispatched == 1

    async def test_episode_dedup_season_none_matches_null(
        self, db_session, channel, downloader, series
    ):
        """Season-less resources still dedup against season-less tasks
        (``season == None`` must compile to IS NULL, not = NULL)."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True
        )
        r1 = _make_resource(channel.id, series_id=series.id,
                            season=None, episode=3, guid=_uuid())
        db_session.add(r1)
        await db_session.flush()
        task = DownloadTask(
            id=_uuid(), agent_id=agent.id, file_resource_id=r1.id,
            downloader_id=downloader.id, download_dir="/downloads/rssripple",
            status="completed",
        )
        db_session.add(task)
        await db_session.flush()
        r2 = _make_resource(channel.id, series_id=series.id,
                            season=None, episode=3, guid=_uuid())
        db_session.add(r2)
        await db_session.flush()
        result = await process_resources(agent, [r2], db_session)
        assert result.duplicates_skipped == 1
        assert result.dispatched == 0

    async def test_episode_dedup_matches_numbered_sibling_for_seasonless(
        self, db_session, channel, downloader, series
    ):
        """Season-compatibility: a completed S1E3 task blocks a season-less
        E3 variant of the same series. Regression for the same episode
        downloading twice when release variants were attributed different
        seasons across runs (S1 vs unknown)."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True
        )
        r_s1 = _make_resource(channel.id, series_id=series.id,
                              season=1, episode=3, guid=_uuid(),
                              subtitle_langs=["zh-CN", "ja"])
        db_session.add(r_s1)
        await db_session.flush()
        task = DownloadTask(
            id=_uuid(), agent_id=agent.id, file_resource_id=r_s1.id,
            downloader_id=downloader.id, download_dir="/downloads/rssripple",
            status="completed",
        )
        db_session.add(task)
        await db_session.flush()
        r_none = _make_resource(channel.id, series_id=series.id,
                                season=None, episode=3, guid=_uuid(),
                                subtitle_langs=["zh-TW", "ja"])
        db_session.add(r_none)
        await db_session.flush()
        result = await process_resources(agent, [r_none], db_session)
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

    async def test_conflict_merges_seasonless_variant_with_numbered(
        self, db_session, channel, downloader, series
    ):
        """Same episode with mismatched season attribution (S1 vs unknown)
        lands in ONE conflict group and the subtitle-language preference
        picks 简体 — instead of two independent single-candidate groups
        downloading both variants (regression for 攻壳 E05 简/繁 double
        download)."""
        agent = await self._make_agent(
            db_session, channel, downloader,
            scope_channel_wide=True, conflict_resolution="auto",
        )
        agent.pick_preferences = [
            {"field": "subtitle_langs", "operator": "contains", "value": "zh-CN"},
        ]
        await db_session.flush()
        gb = _make_resource(channel.id, series_id=series.id,
                            season=1, episode=5, guid=_uuid(),
                            resolution="1080p", subtitle_langs=["zh-CN", "ja"])
        big5 = _make_resource(channel.id, series_id=series.id,
                              season=None, episode=5, guid=_uuid(),
                              resolution="1080p", subtitle_langs=["zh-TW", "ja"])
        db_session.add_all([gb, big5])
        await db_session.flush()
        result = await process_resources(agent, [gb, big5], db_session)
        assert result.dispatched == 1
        assert result.pending_decisions == 0
        task = (await db_session.execute(
            select(DownloadTask).where(DownloadTask.agent_id == agent.id)
        )).scalars().one()
        assert task.file_resource_id == gb.id

    async def test_conflict_auto_prefers_llm_pick(
        self, db_session, channel, downloader, series
    ):
        """auto mode: when the LLM returns a valid pick it wins over the
        heuristic scorer (which would pick the 2160p candidate here)."""
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
        with patch(
            "app.services.agent_service._generate_llm_pick",
            new_callable=AsyncMock,
            return_value=(r1.id, "better subs"),
        ) as llm_pick:
            result = await process_resources(agent, [r1, r2], db_session)
        assert result.dispatched == 1
        llm_pick.assert_awaited_once()
        task = (await db_session.execute(
            select(DownloadTask).where(DownloadTask.agent_id == agent.id)
        )).scalars().one()
        assert task.file_resource_id == r1.id

    async def test_conflict_auto_falls_back_to_heuristic(
        self, db_session, channel, downloader, series
    ):
        """auto mode: LLM unavailable / no valid pick → heuristic score_and_pick
        (2160p wins)."""
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
        with patch(
            "app.services.agent_service._generate_llm_pick",
            new_callable=AsyncMock,
            return_value=(None, None),
        ):
            result = await process_resources(agent, [r1, r2], db_session)
        assert result.dispatched == 1
        task = (await db_session.execute(
            select(DownloadTask).where(DownloadTask.agent_id == agent.id)
        )).scalars().one()
        assert task.file_resource_id == r2.id

    async def test_conflict_auto_preference_unique_winner_skips_llm(
        self, db_session, channel, downloader, series
    ):
        """auto mode + pick preferences: a unique preference winner dispatches
        deterministically without any LLM call."""
        agent = await self._make_agent(
            db_session, channel, downloader,
            scope_channel_wide=True, conflict_resolution="auto",
        )
        agent.pick_preferences = [
            {"field": "subtitle_langs", "operator": "contains", "value": "zh-CN"},
        ]
        await db_session.flush()
        big5 = _make_resource(channel.id, series_id=series.id, episode=5,
                              guid=_uuid(), resolution="2160p",
                              subtitle_langs=["zh-TW"])
        gb = _make_resource(channel.id, series_id=series.id, episode=5,
                            guid=_uuid(), resolution="1080p",
                            subtitle_langs=["zh-CN"])
        db_session.add_all([big5, gb])
        await db_session.flush()
        with patch(
            "app.services.agent_service._generate_llm_pick",
            new_callable=AsyncMock,
        ) as llm_pick:
            result = await process_resources(agent, [big5, gb], db_session)
        assert result.dispatched == 1
        llm_pick.assert_not_awaited()
        task = (await db_session.execute(
            select(DownloadTask).where(DownloadTask.agent_id == agent.id)
        )).scalars().one()
        # Preference beats the heuristic's 2160p bias.
        assert task.file_resource_id == gb.id

    async def test_conflict_auto_preference_tie_goes_to_llm(
        self, db_session, channel, downloader, series
    ):
        """auto mode + pick preferences: a remaining tie is decided by the
        LLM pick on the narrowed tier."""
        agent = await self._make_agent(
            db_session, channel, downloader,
            scope_channel_wide=True, conflict_resolution="auto",
        )
        agent.pick_preferences = [
            {"field": "subtitle_langs", "operator": "contains", "value": "zh-CN"},
        ]
        await db_session.flush()
        r1 = _make_resource(channel.id, series_id=series.id, episode=5,
                            guid=_uuid(), subtitle_langs=["zh-CN"],
                            resolution="1080p", file_size=100)
        r2 = _make_resource(channel.id, series_id=series.id, episode=5,
                            guid=_uuid(), subtitle_langs=["zh-CN"],
                            resolution="2160p", file_size=200)
        db_session.add_all([r1, r2])
        await db_session.flush()
        with patch(
            "app.services.agent_service._generate_llm_pick",
            new_callable=AsyncMock,
            return_value=(r1.id, "smaller"),
        ) as llm_pick:
            result = await process_resources(agent, [r1, r2], db_session)
        assert result.dispatched == 1
        llm_pick.assert_awaited_once()
        task = (await db_session.execute(
            select(DownloadTask).where(DownloadTask.agent_id == agent.id)
        )).scalars().one()
        assert task.file_resource_id == r1.id

    async def test_filter_by_work_rating(
        self, db_session, channel, downloader
    ):
        """Work-namespaced DSL field: only resources whose linked series has
        rating >= 7 pass the filter."""
        import datetime as _dt

        good = TVSeries(id=_uuid(), title_cn="高分剧", content_type="tv",
                        rating=8.1, start_date=_dt.date(2021, 1, 1))
        bad = TVSeries(id=_uuid(), title_cn="低分剧", content_type="tv",
                       rating=5.5, start_date=_dt.date(2021, 1, 1))
        db_session.add_all([good, bad])
        await db_session.flush()
        agent = await self._make_agent(
            db_session, channel, downloader,
            scope_channel_wide=True,
            filter_config={"combinator": "and", "conditions": [
                {"field": "series.rating", "operator": "gte", "value": 7},
            ]},
        )
        # Attach via relationships so the sync evaluator can resolve
        # series.rating without an async lazy load.
        ok = _make_resource(channel.id, series=good, episode=1, guid=_uuid())
        nope = _make_resource(channel.id, series=bad, episode=1, guid=_uuid())
        db_session.add_all([ok, nope])
        await db_session.flush()
        result = await process_resources(agent, [ok, nope], db_session)
        assert result.matched == 1
        assert result.filter_failed == 1
        assert result.dispatched == 1

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

    async def test_batch_same_season_versions_conflict_ask(
        self, db_session, channel, downloader, series
    ):
        """Two season packs covering the exact same season (different
        encodes) go through conflict resolution: ask mode → one
        PendingDecision, no dispatch."""
        agent = await self._make_agent(
            db_session, channel, downloader,
            scope_channel_wide=True, conflict_resolution="ask",
        )
        r1 = _make_resource(
            channel.id, series_id=series.id, episode=None, season=1,
            is_batch=True, batch_scope="season", episode_start=1,
            episode_end=13, guid=_uuid(),
        )
        r2 = _make_resource(
            channel.id, series_id=series.id, episode=None, season=1,
            is_batch=True, batch_scope="season", episode_start=1,
            episode_end=13, guid=_uuid(),
        )
        db_session.add_all([r1, r2])
        await db_session.flush()
        result = await process_resources(agent, [r1, r2], db_session)
        assert result.dispatched == 0
        assert result.pending_decisions == 1
        assert result.matched == 2
        pd = (await db_session.execute(
            select(PendingDecision).where(PendingDecision.agent_id == agent.id)
        )).scalar_one()
        assert pd.season == 1
        assert pd.episode == -1  # batch sentinel
        assert sorted(pd.candidates) == sorted([r1.id, r2.id])

    async def test_batch_same_season_versions_auto_dispatches_one(
        self, db_session, channel, downloader, series
    ):
        """Same-coverage season packs in auto mode: exactly one version is
        dispatched (heuristic scorer — LLM disabled in tests)."""
        agent = await self._make_agent(
            db_session, channel, downloader,
            scope_channel_wide=True, conflict_resolution="auto",
        )
        r1 = _make_resource(
            channel.id, series_id=series.id, episode=None, season=1,
            is_batch=True, batch_scope="season", resolution="1080p",
            guid=_uuid(),
        )
        r2 = _make_resource(
            channel.id, series_id=series.id, episode=None, season=1,
            is_batch=True, batch_scope="season", resolution="720p",
            guid=_uuid(),
        )
        db_session.add_all([r1, r2])
        await db_session.flush()
        result = await process_resources(agent, [r1, r2], db_session)
        assert result.dispatched == 1
        assert result.pending_decisions == 0
        task = (await db_session.execute(
            select(DownloadTask).where(DownloadTask.agent_id == agent.id)
        )).scalar_one()
        assert task.file_resource_id == r1.id  # 1080p beats 720p

    async def test_batch_different_seasons_do_not_conflict(
        self, db_session, channel, downloader, series
    ):
        """S1 pack vs S2 pack: different coverage → both dispatch, no
        PendingDecision."""
        agent = await self._make_agent(
            db_session, channel, downloader,
            scope_channel_wide=True, conflict_resolution="ask",
        )
        r1 = _make_resource(
            channel.id, series_id=series.id, episode=None, season=1,
            is_batch=True, batch_scope="season", guid=_uuid(),
        )
        r2 = _make_resource(
            channel.id, series_id=series.id, episode=None, season=2,
            is_batch=True, batch_scope="season", guid=_uuid(),
        )
        db_session.add_all([r1, r2])
        await db_session.flush()
        result = await process_resources(agent, [r1, r2], db_session)
        assert result.dispatched == 2
        assert result.pending_decisions == 0

    async def test_batch_multi_season_same_coverage_conflicts(
        self, db_session, channel, downloader, series
    ):
        """Multi-season packs with identical season sets conflict; different
        season sets (S1-S2 vs S1-S3) do not."""
        agent = await self._make_agent(
            db_session, channel, downloader,
            scope_channel_wide=True, conflict_resolution="ask",
        )
        r1 = _make_resource(
            channel.id, series_id=series.id, episode=None, season=None,
            is_batch=True, batch_scope="multi_season", batch_seasons=[1, 2],
            guid=_uuid(),
        )
        r2 = _make_resource(
            channel.id, series_id=series.id, episode=None, season=None,
            is_batch=True, batch_scope="multi_season", batch_seasons=[2, 1],
            guid=_uuid(),
        )
        r3 = _make_resource(
            channel.id, series_id=series.id, episode=None, season=None,
            is_batch=True, batch_scope="multi_season", batch_seasons=[1, 2, 3],
            guid=_uuid(),
        )
        db_session.add_all([r1, r2, r3])
        await db_session.flush()
        result = await process_resources(agent, [r1, r2, r3], db_session)
        # r1+r2 (same coverage, order-independent) → one decision; r3 → dispatch.
        assert result.pending_decisions == 1
        assert result.dispatched == 1

    async def test_batch_unknown_coverage_creates_decision(
        self, db_session, channel, downloader, series
    ):
        """Title-marked packs without season evidence (coverage unknown) are
        no longer dispatched: coverage is mandatory for organize planning, so
        they route to a PendingDecision for manual correction instead."""
        agent = await self._make_agent(
            db_session, channel, downloader,
            scope_channel_wide=True, conflict_resolution="ask",
        )
        r1 = _make_resource(
            channel.id, series_id=series.id, episode=None, season=None,
            is_batch=True, guid=_uuid(),
        )
        r2 = _make_resource(
            channel.id, series_id=series.id, episode=None, season=None,
            is_batch=True, guid=_uuid(),
        )
        db_session.add_all([r1, r2])
        await db_session.flush()
        result = await process_resources(agent, [r1, r2], db_session)
        assert result.dispatched == 0
        # Coverage is a Channel/FileResource confirmation issue, never an
        # Agent candidate-choice decision.
        assert result.pending_decisions == 0
        pds = (await db_session.execute(
            select(PendingDecision).where(PendingDecision.agent_id == agent.id)
        )).scalars().all()
        assert pds == []

        # 人工修订（PATCH 语义：补季号/集数范围）后定向重跑：覆盖度键完整
        # → 正常冲突解决（同季两版本 ask → 1 条真正的多选一决策）。
        r1.season = 1
        r1.batch_scope = "season"
        r1.episode_start, r1.episode_end = 1, 12
        r2.season = 1
        r2.batch_scope = "season"
        r2.episode_start, r2.episode_end = 1, 12
        await db_session.flush()
        result2 = await process_resources(agent, [r1, r2], db_session)
        assert result2.pending_decisions == 1

    async def test_batch_multi_season_without_seasons_creates_decision(
        self, db_session, channel, downloader, series
    ):
        """multi_season scope without batch_seasons: coverage unknown → held
        for manual correction, not dispatched."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True,
        )
        r = _make_resource(
            channel.id, series_id=series.id, episode=None, season=None,
            is_batch=True, batch_scope="multi_season", batch_seasons=None,
            guid=_uuid(),
        )
        db_session.add(r)
        await db_session.flush()
        result = await process_resources(agent, [r], db_session)
        assert result.dispatched == 0
        assert result.pending_decisions == 0

    async def test_batch_cross_run_same_coverage_skipped(
        self, db_session, channel, downloader, series
    ):
        """A season pack is skipped when an active/completed task already
        covers the same season (different FileResource, same coverage)."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True,
        )
        old = _make_resource(
            channel.id, series_id=series.id, episode=None, season=1,
            is_batch=True, batch_scope="season", guid=_uuid(),
        )
        db_session.add(old)
        await db_session.flush()
        db_session.add(DownloadTask(
            id=_uuid(), agent_id=agent.id, file_resource_id=old.id,
            downloader_id=downloader.id, download_dir="/downloads/rssripple",
            status="completed",
        ))
        new = _make_resource(
            channel.id, series_id=series.id, episode=None, season=1,
            is_batch=True, batch_scope="season", guid=_uuid(),
        )
        db_session.add(new)
        await db_session.flush()
        result = await process_resources(agent, [new], db_session)
        assert result.dispatched == 0
        assert result.duplicates_skipped == 1

    async def test_batch_cross_run_organized_completed_task_still_skipped(
        self, db_session, channel, downloader, series
    ):
        """Organize(move) cancellation retains proof of completion."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True,
        )
        old = _make_resource(
            channel.id, series_id=series.id, episode=None, season=1,
            is_batch=True, batch_scope="season", guid=_uuid(),
        )
        db_session.add(old)
        await db_session.flush()
        db_session.add(DownloadTask(
            id=_uuid(), agent_id=agent.id, file_resource_id=old.id,
            downloader_id=downloader.id, download_dir="/downloads/rssripple",
            status="cancelled", completed_at=datetime.now(UTC), progress=1,
        ))
        new = _make_resource(
            channel.id, series_id=series.id, episode=None, season=1,
            is_batch=True, batch_scope="season", guid=_uuid(),
        )
        db_session.add(new)
        await db_session.flush()

        result = await process_resources(agent, [new], db_session)

        assert result.dispatched == 0
        assert result.duplicates_skipped == 1

    async def test_batch_cross_run_unfinished_cancelled_task_is_retryable(
        self, db_session, channel, downloader, series
    ):
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True,
        )
        old = _make_resource(
            channel.id, series_id=series.id, episode=None, season=1,
            is_batch=True, batch_scope="season", guid=_uuid(),
        )
        db_session.add(old)
        await db_session.flush()
        db_session.add(DownloadTask(
            id=_uuid(), agent_id=agent.id, file_resource_id=old.id,
            downloader_id=downloader.id, download_dir="/downloads/rssripple",
            status="cancelled", completed_at=None,
        ))
        new = _make_resource(
            channel.id, series_id=series.id, episode=None, season=1,
            is_batch=True, batch_scope="season", guid=_uuid(),
        )
        db_session.add(new)
        await db_session.flush()

        result = await process_resources(agent, [new], db_session)

        assert result.dispatched == 1
        assert result.duplicates_skipped == 0

    async def test_batch_cross_run_different_coverage_dispatches(
        self, db_session, channel, downloader, series
    ):
        """An S2 pack is NOT blocked by a completed S1 pack task."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True,
        )
        old = _make_resource(
            channel.id, series_id=series.id, episode=None, season=1,
            is_batch=True, batch_scope="season", guid=_uuid(),
        )
        db_session.add(old)
        await db_session.flush()
        db_session.add(DownloadTask(
            id=_uuid(), agent_id=agent.id, file_resource_id=old.id,
            downloader_id=downloader.id, download_dir="/downloads/rssripple",
            status="completed",
        ))
        new = _make_resource(
            channel.id, series_id=series.id, episode=None, season=2,
            is_batch=True, batch_scope="season", guid=_uuid(),
        )
        db_session.add(new)
        await db_session.flush()
        result = await process_resources(agent, [new], db_session)
        assert result.dispatched == 1
        assert result.duplicates_skipped == 0

    async def test_ambiguous_episode_is_channel_confirmation_not_agent_decision(
        self, db_session, channel, downloader, series
    ):
        """Resources whose episode_confidence=='ambiguous' must not be
        dispatched, but they never create an Agent candidate decision."""
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
        assert result.pending_decisions == 0
        assert len(result.suggestions) == 0
        assert result.matched == 0
        assert result.matched_resource_ids == []
        pds = (await db_session.execute(
            select(PendingDecision).where(PendingDecision.agent_id == agent.id)
        )).scalars().all()
        assert pds == []

    async def test_ambiguous_season_is_not_an_agent_decision(
        self, db_session, channel, downloader, series
    ):
        """Season uncertainty is owned by the Channel confirmation queue."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True,
        )
        r = _make_resource(
            channel.id, series_id=series.id, season=None, episode=14,
            episode_confidence="ambiguous",
        )
        db_session.add(r)
        await db_session.flush()
        result = await process_resources(agent, [r], db_session)
        assert result.dispatched == 0
        assert result.pending_decisions == 0
        pds = (await db_session.execute(
            select(PendingDecision).where(PendingDecision.agent_id == agent.id)
        )).scalars().all()
        assert pds == []

    async def test_ambiguous_season_dispatches_after_correction(
        self, db_session, channel, downloader, series
    ):
        """Correcting a Channel confirmation lets a targeted run dispatch."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True,
        )
        r = _make_resource(
            channel.id, series_id=series.id, season=None, episode=14,
            episode_confidence="ambiguous",
        )
        db_session.add(r)
        await db_session.flush()
        first = await process_resources(agent, [r], db_session)
        assert first.pending_decisions == 0
        # User confirms season + episode via PATCH /resources/{id}/episode.
        r.season = 2
        r.episode_confidence = "manual"
        await db_session.flush()
        second = await process_resources(agent, [r], db_session)
        assert second.dispatched == 1
        assert second.pending_decisions == 0

    async def test_ambiguous_episode_dispatches_after_correction(
        self, db_session, channel, downloader, series
    ):
        """Once the user corrects the episode (confidence='manual'), the next
        run dispatches without creating or resolving an Agent decision."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True,
        )
        r = _make_resource(
            channel.id, series_id=series.id, episode=12,
            episode_confidence="ambiguous",
        )
        db_session.add(r)
        await db_session.flush()
        # First run: held by Channel confirmation, nothing dispatched.
        first = await process_resources(agent, [r], db_session)
        assert first.pending_decisions == 0
        assert first.dispatched == 0
        # User corrects the episode via PATCH /resources/{id}/episode.
        r.episode_confidence = "manual"
        await db_session.flush()
        # Second run: resource passes the confirmation gate and dispatches.
        second = await process_resources(agent, [r], db_session)
        assert second.dispatched == 1
        assert second.pending_decisions == 0

    async def test_ambiguous_batch_resource_skips_episode_gate(
        self, db_session, channel, downloader, series
    ):
        """A 合集 bypasses per-episode flow (it dedups by content coverage),
        so the ambiguous episode/season gate must not route it to a
        PendingDecision — it enters the normal batch flow."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True,
        )
        r = _make_resource(
            channel.id, series_id=series.id, season=1, episode=None,
            is_batch=True, batch_scope="season", episode_confidence="ambiguous",
        )
        db_session.add(r)
        await db_session.flush()
        result = await process_resources(agent, [r], db_session)
        assert result.pending_decisions == 0
        # Known coverage (series, S1) + single version → dispatched.
        assert result.dispatched == 1
        pds = (await db_session.execute(
            select(PendingDecision).where(PendingDecision.agent_id == agent.id)
        )).scalars().all()
        assert len(pds) == 0

    async def test_ambiguous_movie_resource_skips_episode_gate(
        self, db_session, channel, downloader, movie
    ):
        """A movie carries no episode/season question: a stale ambiguous flag
        (left over from a previous tv link) must not block dispatch."""
        agent = await self._make_agent(
            db_session, channel, downloader, scope_channel_wide=True,
        )
        r = _make_resource(
            channel.id, series_id=None, movie_id=movie.id,
            season=None, episode=None, episode_confidence="ambiguous",
        )
        db_session.add(r)
        await db_session.flush()
        result = await process_resources(agent, [r], db_session)
        assert result.pending_decisions == 0
        assert result.dispatched == 1


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


async def test_create_pending_decision_season_aware_key(db_session, channel, downloader, series):
    """The 4-tuple key (type, id, season, episode) makes S1E3 and S4E3
    distinct decisions; the same full key still upserts into one row."""
    from sqlalchemy import func
    from sqlalchemy import select as sql_select
    agent = Agent(
        id=_uuid(), name="a", channel_id=channel.id,
        downloader_id=downloader.id, scope_channel_wide=True,
    )
    db_session.add(agent)
    await db_session.flush()
    r1 = _make_resource(channel.id, series_id=series.id, season=1, episode=3, guid=_uuid())
    r4 = _make_resource(channel.id, series_id=series.id, season=4, episode=3, guid=_uuid())
    r1b = _make_resource(channel.id, series_id=series.id, season=1, episode=3, guid=_uuid())
    db_session.add_all([r1, r4, r1b])
    await db_session.flush()

    pd_s1 = await create_pending_decision(agent, ("series", series.id, 1, 3), [r1], db_session)
    pd_s4 = await create_pending_decision(agent, ("series", series.id, 4, 3), [r4], db_session)
    assert pd_s1.id != pd_s4.id
    assert pd_s1.season == 1
    assert pd_s4.season == 4
    assert "第4季" in pd_s4.reason

    pd_s1_again = await create_pending_decision(
        agent, ("series", series.id, 1, 3), [r1, r1b], db_session
    )
    assert pd_s1_again.id == pd_s1.id
    assert pd_s1_again.candidates == [r1.id, r1b.id]
    total = (await db_session.execute(
        sql_select(func.count()).select_from(PendingDecision)
    )).scalar_one()
    assert total == 2


async def test_create_pending_decision_legacy_3tuple_key(db_session, channel, downloader, series):
    """The legacy (type, id, episode) key shape is still accepted → season=None."""
    agent = Agent(
        id=_uuid(), name="a", channel_id=channel.id,
        downloader_id=downloader.id, scope_channel_wide=True,
    )
    db_session.add(agent)
    await db_session.flush()
    r = _make_resource(channel.id, series_id=series.id, season=None, episode=7)
    db_session.add(r)
    await db_session.flush()
    pd1 = await create_pending_decision(agent, ("series", series.id, 7), [r], db_session)
    pd2 = await create_pending_decision(agent, ("series", series.id, None, 7), [r], db_session)
    assert pd1.season is None
    assert pd1.id == pd2.id


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

    async def test_unrecognized_similar_titles_do_not_create_agent_suggestions(
        self, db_session, channel, downloader
    ):
        """Unrecognized metadata is owned by the Channel, not the Agent."""
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
        assert result.suggestions == []

    async def test_unrecognized_dissimilar_titles_do_not_create_agent_suggestions(
        self, db_session, channel, downloader
    ):
        """Title similarity does not move Channel confirmations to the Agent."""
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
        assert result.suggestions == []

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

    async def test_unrecognized_empty_search_title_stays_channel_scoped(
        self, db_session, channel, downloader
    ):
        """Missing titles remain a Channel confirmation concern."""
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
        assert result.suggestions == []

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



# ---------------------------------------------------------------------------
# resolve_torrent_payload
# ---------------------------------------------------------------------------


class TestResolveTorrentPayload:
    def test_cached_file_returns_bytes(self, tmp_path):
        cached = tmp_path / "abc.torrent"
        cached.write_bytes(b"d8:announce4:teste")
        r = _make_resource("ch", torrent_file=str(cached))
        assert resolve_torrent_payload(r) == b"d8:announce4:teste"

    def test_missing_file_falls_back_to_url(self, tmp_path):
        r = _make_resource("ch", torrent_file=str(tmp_path / "gone.torrent"))
        assert resolve_torrent_payload(r) == r.torrent_url

    def test_no_torrent_file_returns_url(self):
        r = _make_resource("ch", torrent_file=None)
        assert resolve_torrent_payload(r) == r.torrent_url


class TestDispatchPushesCachedTorrent:
    async def test_dispatch_sends_cached_torrent_bytes(
        self, db_session, channel, downloader, tmp_path
    ):
        agent = Agent(
            id=_uuid(), name="a", channel_id=channel.id,
            downloader_id=downloader.id, status="active",
            download_subdir="", scope_channel_wide=True, conflict_resolution="ask",
        )
        db_session.add(agent)
        await db_session.flush()
        payload = b"d4:infod4:name4:test6:lengthi1e4:pathl4:testee"
        cached = tmp_path / "res.torrent"
        cached.write_bytes(payload)
        res = _make_resource(
            channel.id, series_id=None, movie_id=None, torrent_file=str(cached),
        )
        db_session.add(res)
        await db_session.flush()

        with patch(
            "app.clients.transmission.TransmissionWrapper.add_torrent",
            new_callable=AsyncMock,
            return_value={"torrent_id": 9, "name": "x", "hash": "h"},
        ) as add_torrent:
            task = await dispatch_download(agent, res, db_session)

        assert task.status == "downloading"
        add_torrent.assert_awaited_once_with(
            payload,
            download_dir="/downloads/rssripple",
        )
