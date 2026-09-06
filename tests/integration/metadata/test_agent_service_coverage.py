"""Branch coverage for app.services.agent_service.

Targets the branches the HTTP suites do not reach: movie-scope rule matching,
links-carried work matching, dispatch error paths, LLM-pick parsing edges,
PendingDecision key shapes/merging, preference narrowing, batch content-
coverage keys/dedup/conflict decisions, and legacy confirmation retirement.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

import app.services.agent_service as ag
from app.models.agent import Agent
from app.models.channel import Channel
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries


def _uuid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
async def channel(db_session):
    ch = Channel(
        id=_uuid(), name="ch", type="rss_feed", url="https://example.com/rss",
        field_mapping={}, metadata_agent_enabled=False, status="active",
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
        resolution="1080p", search_title="Title",
        episode=1, season=1, file_size=1_000_000_000,
        parsed_at=datetime.now(UTC),
    )
    base.update(overrides)
    return FileResource(**base)


async def _make_agent(db_session, channel, downloader, **overrides) -> Agent:
    base = dict(
        id=_uuid(), name="agent", channel_id=channel.id,
        downloader_id=downloader.id, status="active",
        scope_channel_wide=True, conflict_resolution="ask",
    )
    base.update(overrides)
    agent = Agent(**base)
    db_session.add(agent)
    await db_session.flush()
    await db_session.refresh(agent)
    return agent


# ---------------------------------------------------------------------------
# RuleSet / matching
# ---------------------------------------------------------------------------


class TestRuleMatching:
    def test_build_rule_set_indexes_movie_works(self, channel):
        movie_work = SimpleNamespace(series_id=None, movie_id="m1")
        agent = SimpleNamespace(
            works=[movie_work], scope_channel_wide=False, filter_config=None,
        )
        rules = ag._build_rule_set(agent)
        assert rules.work_by_movie_id == {"m1": movie_work}
        assert rules.work_by_series_id == {}

    def test_movie_scope_match(self, movie):
        work = SimpleNamespace(movie_id=movie.id, filter_overrides=None)
        rules = ag.RuleSet(scope_channel_wide=False, filter_config=None,
                           work_by_movie_id={movie.id: work})
        res = SimpleNamespace(series_id=None, movie_id=movie.id, work_links=[])
        matched, w = ag._resource_matches_rules(res, rules)
        assert matched is True and w is work

    def test_links_carried_work_matches_subscription(self, series):
        work = SimpleNamespace(series_id=series.id, filter_overrides=None)
        rules = ag.RuleSet(scope_channel_wide=False, filter_config=None,
                           work_by_series_id={series.id: work})
        link = SimpleNamespace(series_id=series.id, movie_id=None)
        res = SimpleNamespace(series_id=None, movie_id=None, work_links=[link])
        matched, w = ag._resource_matches_rules(res, rules)
        assert matched is True and w is work

    def test_unsubscribed_links_carried_pack_does_not_match(self, series):
        rules = ag.RuleSet(scope_channel_wide=False, filter_config=None,
                           work_by_series_id={series.id: object()})
        link = SimpleNamespace(series_id="other-series", movie_id=None)
        res = SimpleNamespace(series_id=None, movie_id=None, work_links=[link])
        assert ag._resource_matches_rules(res, rules) == (False, None)


def test_resolution_score_edges():
    assert ag._resolution_score(None) == 0
    assert ag._resolution_score("") == 0
    assert ag._resolution_score("480p") == 0
    assert ag._resolution_score(" 2160P ") == 3


# ---------------------------------------------------------------------------
# dispatch_download error paths
# ---------------------------------------------------------------------------


class TestDispatchDownloadErrors:
    async def test_missing_downloader_creates_error_task(
        self, db_session, channel, downloader, monkeypatch
    ):
        agent = await _make_agent(db_session, channel, downloader)
        res = _make_resource(channel.id, series_id=None, movie_id=None)
        db_session.add(res)
        await db_session.flush()
        monkeypatch.setattr(db_session, "get", AsyncMock(return_value=None))
        task = await ag.dispatch_download(agent, res, db_session)
        assert task.status == "error"
        assert "not found" in task.error_message

    async def test_download_path_error_falls_back_to_root(
        self, db_session, channel, downloader
    ):
        from app.utils.download_paths import DownloadPathError

        agent = await _make_agent(
            db_session, channel, downloader, download_subdir="sub/dir"
        )
        res = _make_resource(channel.id, series_id=None, movie_id=None)
        db_session.add(res)
        await db_session.flush()
        with patch(
            "app.services.agent_service.resolve_download_dir",
            side_effect=DownloadPathError("escapes root"),
        ):
            task = await ag.dispatch_download(agent, res, db_session)
        assert task.status == "error"
        assert "escapes root" in task.error_message
        assert task.download_dir == downloader.download_dir


# ---------------------------------------------------------------------------
# _parse_llm_pick
# ---------------------------------------------------------------------------


class TestParseLlmPick:
    def test_empty_text(self):
        assert ag._parse_llm_pick("", 3) == (None, None)
        assert ag._parse_llm_pick(None, 3) == (None, None)

    def test_invalid_json_falls_through_to_number_scan(self):
        # Braces present but not valid JSON: no leading integer either.
        assert ag._parse_llm_pick("{pick: 2, bad json}", 3) == (None, "{pick: 2, bad json}")

    def test_leading_int_in_range(self):
        pick, reason = ag._parse_llm_pick("2 — best encode", 3)
        assert pick == 2
        assert reason == "2 — best encode"

    def test_leading_int_out_of_range(self):
        pick, reason = ag._parse_llm_pick("5 because reasons", 3)
        assert pick is None
        assert reason == "5 because reasons"

    def test_no_number_at_all(self):
        assert ag._parse_llm_pick("totally clueless", 3) == (None, "totally clueless")


async def test_generate_llm_pick_exception_returns_none(channel, downloader, monkeypatch):
    monkeypatch.setattr(
        type(ag.runtime_config), "llm_api_key", property(lambda self: "k")
    )
    agent = SimpleNamespace(llm_enabled=True, llm_prompt=None)
    res = SimpleNamespace(
        id="r1", title_raw="t", subtitle_group=None, resolution=None, source=None,
        video_codec=None, audio_codec=None, subtitle_type=None, container=None,
        file_size=None, subtitle_langs=None, published_at=None,
    )
    with patch(
        "app.services.feed_analyzer.call_llm",
        new_callable=AsyncMock, side_effect=RuntimeError("llm boom"),
    ):
        assert await ag._generate_llm_pick(agent, [res], ("series", "x", 1)) == (None, None)


# ---------------------------------------------------------------------------
# create_pending_decision
# ---------------------------------------------------------------------------


class TestCreatePendingDecision:
    async def test_movie_key_and_reason(self, db_session, channel, downloader, movie):
        agent = await _make_agent(db_session, channel, downloader)
        r1 = _make_resource(channel.id, movie_id=movie.id, episode=None, season=None)
        r2 = _make_resource(channel.id, movie_id=movie.id, episode=None, season=None)
        db_session.add_all([r1, r2])
        await db_session.flush()
        pd = await ag.create_pending_decision(
            agent, ("movie", movie.id, None, None), [r1, r2], db_session, skip_llm=True
        )
        assert pd.movie_id == movie.id and pd.series_id is None
        assert pd.episode is None and pd.season is None
        assert "电影" in pd.reason
        assert pd.llm_picked_resource_id is None

    async def test_reason_override_template(self, db_session, channel, downloader, movie):
        agent = await _make_agent(db_session, channel, downloader)
        r1 = _make_resource(channel.id, movie_id=movie.id)
        db_session.add(r1)
        await db_session.flush()
        pd = await ag.create_pending_decision(
            agent, ("movie", movie.id, None, -1), [r1], db_session,
            reason_override="多个合集资源匹配电影 {title}，内容相同", skip_llm=True,
        )
        assert pd.reason == "多个合集资源匹配电影 电影A，内容相同"

    async def test_series_season_filled_from_work(self, db_session, channel, downloader):
        s4 = TVSeries(id=_uuid(), title_cn="剧集S4", content_type="tv", season_number=4)
        db_session.add(s4)
        agent = await _make_agent(db_session, channel, downloader)
        r1 = _make_resource(channel.id, series_id=s4.id, episode=3, season=4)
        db_session.add(r1)
        await db_session.flush()
        pd = await ag.create_pending_decision(
            agent, ("series", s4.id, 3), [r1], db_session, skip_llm=True
        )
        assert pd.season == 4  # derived from the per-season work identity
        assert pd.episode == 3
        assert "第4季第03集" in pd.reason

    async def test_series_without_episode_reason(self, db_session, channel, downloader, series):
        agent = await _make_agent(db_session, channel, downloader)
        r1 = _make_resource(channel.id, series_id=series.id, episode=None)
        db_session.add(r1)
        await db_session.flush()
        pd = await ag.create_pending_decision(
            agent, ("series", series.id, None), [r1], db_session, skip_llm=True
        )
        assert pd.episode is None
        assert pd.reason == f"多个资源匹配 {series.title_cn}"

    async def test_series_episode_reason_without_work(
        self, db_session, channel, downloader, series, monkeypatch
    ):
        """A series key whose work row lookup returns nothing keeps
        season=None and renders the episode-only reason."""
        agent = await _make_agent(db_session, channel, downloader)
        r1 = _make_resource(channel.id, series_id=series.id, episode=5)
        db_session.add(r1)
        await db_session.flush()
        real_get = db_session.get

        async def _get(entity, ident, **kw):
            if entity is TVSeries:
                return None
            return await real_get(entity, ident, **kw)

        monkeypatch.setattr(db_session, "get", _get)
        pd = await ag.create_pending_decision(
            agent, ("series", series.id, 5), [r1], db_session, skip_llm=True
        )
        assert pd.season is None
        assert pd.reason == "多个资源匹配  第05集"

    async def test_merge_appends_new_candidates(self, db_session, channel, downloader, series):
        agent = await _make_agent(db_session, channel, downloader)
        r1 = _make_resource(channel.id, series_id=series.id, episode=5)
        r2 = _make_resource(channel.id, series_id=series.id, episode=5)
        r3 = _make_resource(channel.id, series_id=series.id, episode=5)
        db_session.add_all([r1, r2, r3])
        await db_session.flush()
        pd1 = await ag.create_pending_decision(
            agent, ("series", series.id, 5), [r1, r2], db_session, skip_llm=True
        )
        pd2 = await ag.create_pending_decision(
            agent, ("series", series.id, 5), [r2, r3], db_session, skip_llm=True
        )
        assert pd2.id == pd1.id  # same idempotency key → same row
        assert pd2.candidates == [r1.id, r2.id, r3.id]  # merged, no duplicates

    async def test_non_override_reason_without_braces(self, db_session, channel, downloader, movie):
        agent = await _make_agent(db_session, channel, downloader)
        r1 = _make_resource(channel.id, movie_id=movie.id)
        db_session.add(r1)
        await db_session.flush()
        pd = await ag.create_pending_decision(
            agent, ("movie", movie.id, None, -1), [r1], db_session,
            reason_override="plain reason", skip_llm=True,
        )
        assert pd.reason == "plain reason"


# ---------------------------------------------------------------------------
# pick_by_preferences / _describe_preference / _suggest_pick
# ---------------------------------------------------------------------------


class TestPreferences:
    def _res(self, **kw):
        defaults = dict(id=_uuid(), subtitle_langs=None, subtitle_group=None,
                        resolution="1080p", file_size=100)
        defaults.update(kw)
        return SimpleNamespace(**defaults)

    def test_non_dict_rule_skipped(self):
        r1, r2 = self._res(), self._res(subtitle_group="G2")
        prefs = [
            "not-a-dict",
            {"field": "subtitle_group", "operator": "eq", "value": "G2"},
        ]
        tier, deciding = ag.pick_by_preferences([r1, r2], prefs)
        assert [r.id for r in tier] == [r2.id]
        assert deciding == prefs[1]

    def test_raising_rule_skipped(self):
        r1, r2 = self._res(), self._res(file_size=200)
        prefs = [
            {"operator": "eq", "value": "x"},  # no "field" key → KeyError inside
            {"field": "file_size", "operator": "gt", "value": 150},
        ]
        tier, deciding = ag.pick_by_preferences([r1, r2], prefs)
        assert [r.id for r in tier] == [r2.id]
        assert deciding == prefs[1]

    def test_describe_preference_shapes(self):
        assert ag._describe_preference(
            {"field": "subtitle_langs", "operator": "is_not_empty"}
        ) == "subtitle_langs is_not_empty"
        assert ag._describe_preference(
            {"field": "resolution", "operator": "eq", "value": "2160p"}
        ) == "resolution eq 2160p"

    async def test_suggest_pick_deterministic_preference(self):
        big5 = self._res(subtitle_langs=["zh-TW"])
        gb = self._res(subtitle_langs=["zh-CN"])
        agent = SimpleNamespace(
            pick_preferences=[
                {"field": "subtitle_langs", "operator": "contains", "value": "zh-CN"}
            ],
            llm_enabled=False,
        )
        picked, reason = await ag._suggest_pick(agent, [big5, gb], ("series", "x", 1))
        assert picked == gb.id
        assert "命中优选偏好规则" in reason


# ---------------------------------------------------------------------------
# _persist_suggestions
# ---------------------------------------------------------------------------


async def test_persist_suggestions_skips_empty_groups(db_session, channel, downloader):
    from app.models.agent_suggestion import AgentSuggestion

    agent = await _make_agent(db_session, channel, downloader)
    await ag._persist_suggestions(agent.id, [
        {"sample_title": "", "resources": [{"id": "r1"}]},      # blank title
        {"sample_title": "Keep", "resources": []},              # no resources
        {"sample_title": "Keep", "resources": [{"id": "r1"}]},  # valid
    ], db_session)
    await db_session.flush()
    rows = (await db_session.execute(
        select(AgentSuggestion).where(AgentSuggestion.agent_id == agent.id)
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].sample_title == "Keep"


# ---------------------------------------------------------------------------
# Batch coverage keys / dedup / decisions
# ---------------------------------------------------------------------------


class TestBatchCoverageKey:
    def test_movie_pack(self):
        res = SimpleNamespace(movie_id="m1", batch_scope="season", series_id=None)
        assert ag._batch_coverage_key(res) == ("movie",)

    def test_season_pack_without_work_is_unknown(self):
        res = SimpleNamespace(movie_id=None, batch_scope="season", series_id=None)
        assert ag._batch_coverage_key(res) is None

    def test_season_pack_per_season_work(self):
        res = SimpleNamespace(movie_id=None, batch_scope="season", series_id="s1",
                              series=SimpleNamespace(seasons=None, number_of_seasons=None))
        assert ag._batch_coverage_key(res) == ("season", "s1")

    def test_season_pack_legacy_unsplit_uses_parsed_season(self):
        legacy = SimpleNamespace(
            seasons=[{"season_number": 1, "episode_count": 12},
                     {"season_number": 2, "episode_count": 12}],
            number_of_seasons=None,
        )
        res = SimpleNamespace(movie_id=None, batch_scope="season", series_id="legacy",
                              series=legacy, season=2)
        assert ag._batch_coverage_key(res) == ("season", 2)
        res2 = SimpleNamespace(movie_id=None, batch_scope="season", series_id="legacy",
                               series=legacy, season=None)
        assert ag._batch_coverage_key(res2) is None

    def test_multi_season_links_carried(self):
        links = [SimpleNamespace(series_id="s2", movie_id=None),
                 SimpleNamespace(series_id="s1", movie_id=None)]
        res = SimpleNamespace(movie_id=None, batch_scope="multi_season", series_id=None,
                              work_links=links)
        assert ag._batch_coverage_key(res) == ("multi_season", ("s1", "s2"))

    def test_multi_season_links_carried_empty_is_unknown(self):
        res = SimpleNamespace(movie_id=None, batch_scope="multi_season", series_id=None,
                              work_links=[])
        assert ag._batch_coverage_key(res) is None

    def test_multi_season_legacy_fk_uses_batch_seasons(self):
        res = SimpleNamespace(movie_id=None, batch_scope="multi_season", series_id="s1",
                              batch_seasons=[2, 1])
        assert ag._batch_coverage_key(res) == ("multi_season", (1, 2))
        res2 = SimpleNamespace(movie_id=None, batch_scope="multi_season", series_id="s1",
                               batch_seasons=None)
        assert ag._batch_coverage_key(res2) is None

    def test_franchise_scope_has_no_coverage(self):
        res = SimpleNamespace(movie_id=None, batch_scope="franchise", series_id=None)
        assert ag._batch_coverage_key(res) is None


class TestBatchDecisionKey:
    def test_movie_batch(self):
        key, reason = ag._batch_decision_key(("batch", "m1", ("movie",)))
        assert key == ("movie", "m1", None, -1)
        assert "电影" in reason

    def test_season_batch(self):
        key, reason = ag._batch_decision_key(("batch", "s1", ("season", "s1")))
        assert key == ("series", "s1", None, -1)
        assert "同季包" in reason

    def test_multi_season_batch(self):
        key, reason = ag._batch_decision_key(("batch", None, ("multi_season", ("s1",))))
        assert key == ("series", None, None, -1)
        assert "跨季包" in reason


class TestFindActiveBatchDuplicate:
    async def test_movie_pack_match(self, db_session, channel, downloader, movie):
        agent = await _make_agent(db_session, channel, downloader)
        old = _make_resource(channel.id, movie_id=movie.id, is_batch=True,
                             batch_scope="season", episode=None, season=None)
        db_session.add(old)
        await db_session.flush()
        db_session.add(DownloadTask(
            id=_uuid(), agent_id=agent.id, file_resource_id=old.id,
            downloader_id=downloader.id, download_dir="/d", status="completed",
        ))
        await db_session.flush()
        new = _make_resource(channel.id, movie_id=movie.id, is_batch=True,
                             batch_scope="season", episode=None, season=None)
        dup = await ag._find_active_batch_duplicate(agent, new, ("movie",), db_session)
        assert dup is not None and dup.file_resource_id == old.id

    async def test_links_carried_pack_match(self, db_session, channel, downloader, series):
        agent = await _make_agent(db_session, channel, downloader)
        old = _make_resource(channel.id, series_id=None, movie_id=None, is_batch=True,
                             batch_scope="multi_season", episode=None, season=None)
        db_session.add(old)
        await db_session.flush()
        db_session.add(ResourceWorkLink(
            id=_uuid(), resource_id=old.id, series_id=series.id, source="auto",
        ))
        db_session.add(DownloadTask(
            id=_uuid(), agent_id=agent.id, file_resource_id=old.id,
            downloader_id=downloader.id, download_dir="/d", status="completed",
        ))
        await db_session.flush()
        new = _make_resource(channel.id, series_id=None, movie_id=None, is_batch=True,
                             batch_scope="multi_season", episode=None, season=None)
        dup = await ag._find_active_batch_duplicate(
            agent, new, ("multi_season", (series.id,)), db_session
        )
        assert dup is not None and dup.file_resource_id == old.id


# ---------------------------------------------------------------------------
# process_resources batch paths
# ---------------------------------------------------------------------------


@pytest.fixture
def patch_transmission():
    client_instance = SimpleNamespace(
        add_torrent=lambda *a, **kw: SimpleNamespace(id=1, name="x", hashString="h")
    )
    with patch("transmission_rpc.Client", return_value=client_instance):
        yield


class TestProcessResourcesBatch:
    async def test_unknown_coverage_stopped_defensively(
        self, db_session, channel, downloader, series, patch_transmission
    ):
        """A batch resource whose coverage cannot be determined and that the
        (absent) channel gate did not stop falls into the unrecognized bucket."""
        agent = await _make_agent(db_session, channel, downloader)
        res = _make_resource(channel.id, series_id=series.id, is_batch=True,
                             batch_scope="franchise", episode=None, season=None)
        db_session.add(res)
        await db_session.flush()
        result = await ag.process_resources(agent, [res], db_session)
        assert result.unrecognized == 1
        assert result.dispatched == 0

    async def test_batch_dedup_against_same_coverage_task(
        self, db_session, channel, downloader, series, patch_transmission
    ):
        agent = await _make_agent(db_session, channel, downloader)
        old = _make_resource(channel.id, series_id=series.id, is_batch=True,
                             batch_scope="season", episode=None, season=1,
                             episode_start=1, episode_end=12)
        db_session.add(old)
        await db_session.flush()
        db_session.add(DownloadTask(
            id=_uuid(), agent_id=agent.id, file_resource_id=old.id,
            downloader_id=downloader.id, download_dir="/d", status="completed",
        ))
        new = _make_resource(channel.id, series_id=series.id, is_batch=True,
                             batch_scope="season", episode=None, season=1,
                             episode_start=1, episode_end=12)
        db_session.add(new)
        await db_session.flush()
        result = await ag.process_resources(agent, [new], db_session)
        assert result.duplicates_skipped == 1
        assert result.dispatched == 0

    async def test_batch_conflict_ask_creates_coverage_decision(
        self, db_session, channel, downloader, series, patch_transmission
    ):
        agent = await _make_agent(db_session, channel, downloader,
                                  conflict_resolution="ask")
        r1 = _make_resource(channel.id, series_id=series.id, is_batch=True,
                            batch_scope="season", episode=None, season=1,
                            episode_start=1, episode_end=12, resolution="1080p")
        r2 = _make_resource(channel.id, series_id=series.id, is_batch=True,
                            batch_scope="season", episode=None, season=1,
                            episode_start=1, episode_end=12, resolution="2160p")
        db_session.add_all([r1, r2])
        await db_session.flush()
        result = await ag.process_resources(agent, [r1, r2], db_session)
        assert result.pending_decisions == 1
        pd = (await db_session.execute(
            select(PendingDecision).where(PendingDecision.agent_id == agent.id)
        )).scalars().one()
        assert pd.episode == -1  # batch sentinel
        assert "同季包" in pd.reason
        assert sorted(pd.candidates) == sorted([r1.id, r2.id])

    async def test_auto_conflict_unique_preference_winner_dispatches(
        self, db_session, channel, downloader, series, patch_transmission
    ):
        """auto mode: a preference rule with a unique winner dispatches it
        directly — no LLM call, no heuristic scoring."""
        agent = await _make_agent(db_session, channel, downloader,
                                  conflict_resolution="auto")
        agent.pick_preferences = [
            {"field": "subtitle_langs", "operator": "contains", "value": "zh-CN"},
        ]
        await db_session.flush()
        big5 = _make_resource(channel.id, series_id=series.id, episode=5,
                              resolution="2160p", subtitle_langs=["zh-TW"])
        gb = _make_resource(channel.id, series_id=series.id, episode=5,
                            resolution="1080p", subtitle_langs=["zh-CN"])
        db_session.add_all([big5, gb])
        await db_session.flush()
        with patch(
            "app.services.agent_service._generate_llm_pick",
            new_callable=AsyncMock,
        ) as llm_pick:
            result = await ag.process_resources(agent, [big5, gb], db_session)
        assert result.dispatched == 1
        llm_pick.assert_not_awaited()
        task = (await db_session.execute(
            select(DownloadTask).where(DownloadTask.agent_id == agent.id)
        )).scalars().one()
        assert task.file_resource_id == gb.id

    async def test_dispatch_failure_is_recorded_not_raised(
        self, db_session, channel, downloader, series, patch_transmission
    ):
        agent = await _make_agent(db_session, channel, downloader)
        res = _make_resource(channel.id, series_id=series.id, episode=5)
        db_session.add(res)
        await db_session.flush()
        with patch(
            "app.services.agent_service.dispatch_download",
            new_callable=AsyncMock, side_effect=RuntimeError("dispatch boom"),
        ):
            result = await ag.process_resources(agent, [res], db_session)
        assert result.errors == ["dispatch boom"]
        assert result.dispatched == 0


# ---------------------------------------------------------------------------
# Legacy confirmation retirement
# ---------------------------------------------------------------------------


async def test_retire_legacy_confirmation_decisions(db_session, channel, downloader):
    agent = await _make_agent(db_session, channel, downloader)
    legacy = PendingDecision(
        id=_uuid(), agent_id=agent.id, status="pending",
        reason="集号不确定，请人工确认", candidates=["r1"],
        expires_at=datetime(2030, 1, 1),
    )
    normal = PendingDecision(
        id=_uuid(), agent_id=agent.id, status="pending",
        reason="多个资源匹配 剧集A 第01集", candidates=["r1", "r2"],
        expires_at=datetime(2030, 1, 1),
    )
    db_session.add_all([legacy, normal])
    await db_session.flush()
    await ag._retire_legacy_resource_confirmation_decisions(agent, db_session)
    assert legacy.status == "skipped"
    assert legacy.decided_at is not None
    assert normal.status == "pending"
