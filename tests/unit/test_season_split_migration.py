"""Unit tests for the season-split migration core (scripts/season_split_migration.py).

Each test builds a legacy series-level object graph on a per-test Turso
database and runs ``migrate_series`` in-process, asserting the split, the
children re-pointing, the identity move, collection parking, idempotency and
the single-season shell path.
"""

from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import func, select

from app.models.agent import Agent
from app.models.agent_work import AgentWork
from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
from app.models.episode import Episode
from app.models.file_resource import FileResource
from app.models.pending_decision import PendingDecision
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.external_ids import add_external_id, list_external_ids
from scripts.season_split_migration import migrate_series
from scripts.verify_season_split import (
    _check_consistency,
    _check_dangling_fks,
    _check_search_text,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _resource(channel_id, **kw) -> FileResource:
    defaults = dict(
        id=_uuid(),
        channel_id=channel_id,
        guid=_uuid(),
        title_raw="[Group] 无职转生 - 01 [1080p]",
        torrent_url="magnet:?xt=urn:btih:abc",
    )
    defaults.update(kw)
    return FileResource(**defaults)


async def _make_agent(db, channel, downloader) -> Agent:
    agent = Agent(
        id=_uuid(), name="追新番", channel_id=channel.id, downloader_id=downloader.id
    )
    db.add(agent)
    await db.flush()
    return agent


async def _legacy_multi_season_series(db) -> TVSeries:
    series = TVSeries(
        id=_uuid(),
        title_cn="无职转生",
        title_en="Mushoku Tensei",
        original_title="無職転生",
        aliases=["无职转生 第二季"],
        external_id="tmdb:82684",
        external_source="tmdb",
        seasons=[
            {"season_number": 1, "episode_count": 12},
            {"season_number": 2, "episode_count": 12},
        ],
        number_of_seasons=2,
        number_of_episodes=24,
        start_date=date(2021, 1, 10),
        rating=8.3,
        genre=["Animation"],
        status="Ended",
        is_anime=True,
        content_type="tv",
    )
    db.add(series)
    await db.flush()
    await add_external_id(db, "series", series.id, "bangumi", "501963")
    await add_external_id(db, "series", series.id, "wikipedia", "wikipedia:zh:8498329")
    return series


async def _series_bag(db, work_id) -> set[str]:
    return {r.external_id for r in await list_external_ids(db, "series", work_id)}


async def test_split_multi_season_series(db_session, sample_channel, sample_downloader):
    series = await _legacy_multi_season_series(db_session)
    ch = sample_channel.id

    # Episodes across both seasons.
    db_session.add_all([
        Episode(id=_uuid(), series_id=series.id, season=1, episode=1),
        Episode(id=_uuid(), series_id=series.id, season=1, episode=2),
        Episode(id=_uuid(), series_id=series.id, season=2, episode=1),
        Episode(id=_uuid(), series_id=series.id, season=2, episode=2),
    ])
    # Resources: per-season, absolute-locatable, indeterminate, multi_season pack.
    r1 = _resource(ch, series_id=series.id, season=1, episode=3)
    r2 = _resource(ch, series_id=series.id, season=2, episode=5)
    r3 = _resource(ch, series_id=series.id, season=None, episode=None,
                   absolute_episode=20, episode_confidence="reconciled")
    r4 = _resource(ch, series_id=series.id, season=None, episode=7)
    r5 = _resource(ch, series_id=series.id, season=None, is_batch=True,
                   batch_scope="multi_season", batch_seasons=[1, 2])
    db_session.add_all([r1, r2, r3, r4, r5])
    # File assignments on the pack + a season-less one.
    a1 = ResourceFileAssignment(id=_uuid(), resource_id=r5.id, file_path="S01/e01.mkv",
                                series_id=series.id, season=1, episode_start=1, episode_end=1)
    a2 = ResourceFileAssignment(id=_uuid(), resource_id=r5.id, file_path="S02/e01.mkv",
                                series_id=series.id, season=2, episode_start=1, episode_end=1)
    a3 = ResourceFileAssignment(id=_uuid(), resource_id=r1.id, file_path="e03.mkv",
                                series_id=series.id, season=None)
    db_session.add_all([a1, a2, a3])
    # Links: a manual legacy link on the pack + a plain link on r2.
    link_pack = ResourceWorkLink(id=_uuid(), resource_id=r5.id, series_id=series.id,
                                 source="manual")
    link_r2 = ResourceWorkLink(id=_uuid(), resource_id=r2.id, series_id=series.id)
    db_session.add_all([link_pack, link_r2])
    # Decisions / mappings / subscription.
    agent = await _make_agent(db_session, sample_channel, sample_downloader)
    pd1 = PendingDecision(id=_uuid(), agent_id=agent.id, series_id=series.id, season=2,
                          episode=5, candidates=[], reason="conflict")
    pd2 = PendingDecision(id=_uuid(), agent_id=agent.id, series_id=series.id, season=None,
                          episode=3, candidates=[], reason="conflict")
    aw = AgentWork(id=_uuid(), agent_id=agent.id, series_id=series.id, content_type="tv")
    m1 = ChannelRawTitleMapping(id=_uuid(), channel_id=ch, series_id=series.id,
                                raw_title="[Group] 无职转生 第二季 - 05 [1080p]",
                                search_title_key="无职转生 第二季")
    m2 = ChannelRawTitleMapping(id=_uuid(), channel_id=ch, series_id=series.id,
                                raw_title="[Group] 无职转生 - 03 [1080p]",
                                search_title_key="无职转生")
    db_session.add_all([pd1, pd2, aw, m1, m2])
    await db_session.commit()

    report = await migrate_series(db_session, series, apply=True)
    await db_session.commit()

    assert report.status == "split"
    assert report.seasons == [1, 2]
    assert report.collection_action == "created"
    collection = await db_session.get(WorkCollection, report.collection_id)
    assert collection.external_source == "series_group"
    assert collection.title_cn == "无职转生"

    # S1 reuses the original row; legacy columns truncated.
    await db_session.refresh(series)
    assert series.season_number == 1
    assert series.collection_id == collection.id
    assert series.number_of_episodes == 12
    assert series.number_of_seasons is None
    assert series.seasons == [{"season_number": 1, "episode_count": 12}]
    assert series.start_date == date(2021, 1, 10)

    # S2 is a new work with copied metadata and the synthetic identity.
    s2_outcome = next(o for o in report.outcomes if o.season == 2)
    assert s2_outcome.action == "create"
    s2 = await db_session.get(TVSeries, s2_outcome.work_id)
    assert s2.collection_id == collection.id
    assert s2.season_number == 2
    assert s2.number_of_episodes == 12
    assert s2.start_date is None
    assert (s2.rating, s2.genre, s2.is_anime, s2.status) == (8.3, ["Animation"], True, "Ended")
    assert s2.title_cn == "无职转生"  # base title kept
    assert "无职转生 第二季" in (s2.aliases or [])  # reused season variant
    assert s2.external_id == "tmdb:82684#s2"
    assert s2.search_text  # before_flush hook maintained

    # Identity move: series-level ids on the COLLECTION bag; season-level stay.
    coll_bag = {r.external_id for r in await list_external_ids(db_session, "collection", collection.id)}
    assert {"tmdb:82684", "wikipedia:zh:8498329"} <= coll_bag
    s1_bag = await _series_bag(db_session, series.id)
    assert "bangumi:501963" in s1_bag
    assert "tmdb:82684#s1" in s1_bag
    assert "tmdb:82684" not in s1_bag  # moved to the collection bag
    assert "tmdb:82684#s2" in await _series_bag(db_session, s2.id)
    # Primary columns untouched (creator-wins).
    assert (series.external_id, series.external_source) == ("tmdb:82684", "tmdb")

    # Episodes routed by season.
    s1_eps = (await db_session.execute(
        select(Episode).where(Episode.series_id == series.id))).scalars().all()
    s2_eps = (await db_session.execute(
        select(Episode).where(Episode.series_id == s2.id))).scalars().all()
    assert sorted((e.season, e.episode) for e in s1_eps) == [(1, 1), (1, 2)]
    assert sorted((e.season, e.episode) for e in s2_eps) == [(2, 1), (2, 2)]

    # Resources.
    await db_session.refresh(r1)
    await db_session.refresh(r2)
    await db_session.refresh(r3)
    await db_session.refresh(r4)
    await db_session.refresh(r5)
    assert r1.series_id == series.id
    assert r2.series_id == s2.id
    # Absolute episode 20 located along the collection: S2 episode 8.
    assert (r3.series_id, r3.season, r3.episode) == (s2.id, 2, 8)
    assert r3.episode_confidence == "reconciled"
    # Indeterminate → parked on the collection (Channel confirmation).
    assert r4.series_id is None
    assert r4.collection_id == collection.id
    assert r4.episode_confidence == "ambiguous"
    assert r4.id in report.parked_resources
    # multi_season pack: FK cleared, one link per season work.
    assert r5.series_id is None
    pack_links = (await db_session.execute(
        select(ResourceWorkLink).where(ResourceWorkLink.resource_id == r5.id))).scalars().all()
    assert {link.series_id for link in pack_links} == {series.id, s2.id}
    # The manual legacy link collapsed onto the S1 link (manual provenance wins).
    assert {link.source for link in pack_links if link.series_id == series.id} == {"manual"}

    # Assignments / decisions / mappings routed by season (NULL → S1 anchor).
    await db_session.refresh(a1)
    await db_session.refresh(a2)
    await db_session.refresh(a3)
    assert (a1.series_id, a2.series_id, a3.series_id) == (series.id, s2.id, series.id)
    await db_session.refresh(pd1)
    await db_session.refresh(pd2)
    assert (pd1.series_id, pd2.series_id) == (s2.id, series.id)
    await db_session.refresh(m1)
    await db_session.refresh(m2)
    assert (m1.series_id, m2.series_id) == (s2.id, series.id)

    # AgentWork untouched (still on S1) + suggestion lists S2.
    await db_session.refresh(aw)
    assert aw.series_id == series.id
    assert report.agent_suggestions
    suggestion = report.agent_suggestions[0]
    assert suggestion["agent_name"] == "追新番"
    assert [s["season"] for s in suggestion["suggested"]] == [2]

    # Verify-script checks pass on the migrated state.
    assert await _check_dangling_fks(db_session) == []
    assert await _check_consistency(db_session) == []
    assert await _check_search_text(db_session) == []


async def test_single_season_series_gets_shell_collection(db_session, sample_channel):
    series = TVSeries(
        id=_uuid(), title_cn="孤独摇滚", title_en="Bocchi the Rock",
        external_id="tmdb:119051", external_source="tmdb",
        seasons=[{"season_number": 1, "episode_count": 12}],
        number_of_seasons=1, number_of_episodes=12, content_type="tv",
    )
    db_session.add(series)
    r = _resource(sample_channel.id, series_id=series.id, season=1, episode=3)
    db_session.add(r)
    await db_session.commit()

    report = await migrate_series(db_session, series, apply=True)
    await db_session.commit()

    assert report.status == "single-season"
    await db_session.refresh(series)
    assert series.season_number == 1
    assert series.collection_id == report.collection_id
    assert series.number_of_episodes == 12
    # No new season works created.
    total = (await db_session.execute(select(func.count()).select_from(TVSeries))).scalar_one()
    assert total == 1
    # The resource stays on the work (single-season path does not re-route).
    await db_session.refresh(r)
    assert r.series_id == series.id


async def test_idempotent_second_run(db_session, sample_channel):
    series = await _legacy_multi_season_series(db_session)
    db_session.add(_resource(sample_channel.id, series_id=series.id, season=2, episode=1))
    await db_session.commit()

    first = await migrate_series(db_session, series, apply=True)
    await db_session.commit()
    works = (await db_session.execute(select(func.count()).select_from(TVSeries))).scalar_one()
    assert works == 2

    # Re-running the migrated original row converges (skip), no new works.
    second = await migrate_series(db_session, series, apply=True)
    await db_session.commit()
    assert second.status == "skipped"
    works = (await db_session.execute(select(func.count()).select_from(TVSeries))).scalar_one()
    assert works == 2

    # The new S2 work is likewise skipped.
    s2_id = next(o.work_id for o in first.outcomes if o.season == 2)
    s2 = await db_session.get(TVSeries, s2_id)
    third = await migrate_series(db_session, s2, apply=True)
    assert third.status == "skipped"


async def test_duplicate_ip_legacy_series_absorbed(db_session, sample_channel):
    """Two legacy rows of the same IP collapse into one collection: the second
    row's seasons adopt the existing members and the redundant row is removed
    instead of violating (collection_id, season_number) uniqueness."""
    a = await _legacy_multi_season_series(db_session)
    b = TVSeries(
        id=_uuid(), title_cn="无职转生", title_en="Mushoku Tensei",
        external_id="tmdb:999999", external_source="tmdb",
        seasons=[{"season_number": 1, "episode_count": 12},
                 {"season_number": 2, "episode_count": 12}],
        number_of_seasons=2, content_type="tv",
    )
    db_session.add(b)
    b_ep = Episode(id=_uuid(), series_id=b.id, season=1, episode=7)
    db_session.add(b_ep)
    await db_session.commit()

    report_a = await migrate_series(db_session, a, apply=True)
    await db_session.commit()
    report_b = await migrate_series(db_session, b, apply=True)
    await db_session.commit()

    assert report_b.status == "absorbed"
    assert report_b.collection_action == "title-hit"
    assert report_b.collection_id == report_a.collection_id
    assert await db_session.get(TVSeries, b.id) is None
    # B's episode moved onto A's S1 member.
    await db_session.refresh(b_ep)
    assert b_ep.series_id == a.id
    # Exactly two members, unique (collection, season).
    members = (await db_session.execute(
        select(TVSeries).where(TVSeries.collection_id == report_a.collection_id))
    ).scalars().all()
    assert sorted(m.season_number for m in members) == [1, 2]
    # Both tmdb ids reachable via the collection bag / the S1 work bag.
    coll_bag = {r.external_id for r in await list_external_ids(db_session, "collection", report_a.collection_id)}
    assert {"tmdb:82684", "tmdb:999999"} <= coll_bag or "tmdb:999999" in await _series_bag(
        db_session, a.id
    )
    assert await _check_consistency(db_session) == []


async def test_split_derives_season_start_dates_from_episode_air_dates(
    db_session, sample_channel
):
    """拆分出的非锚点季作品：start_date 由本季 Episode 的最早 air_date 离线
    推导（无则保持 NULL 待刷新）；锚点季保留原值。"""
    series = await _legacy_multi_season_series(db_session)
    db_session.add_all([
        Episode(id=_uuid(), series_id=series.id, season=2, episode=2,
                air_date=date(2023, 7, 9)),
        Episode(id=_uuid(), series_id=series.id, season=2, episode=1,
                air_date=date(2023, 7, 2)),
    ])
    await db_session.commit()

    report = await migrate_series(db_session, series, apply=True)
    await db_session.commit()

    s2 = await db_session.get(
        TVSeries, next(o.work_id for o in report.outcomes if o.season == 2)
    )
    assert s2.start_date == date(2023, 7, 2)
    await db_session.refresh(series)
    assert series.start_date == date(2021, 1, 10)  # S1 保留原值


async def test_dry_run_run_migration_rolls_back(db_session, sample_channel):
    """The CLI driver stages everything in one transaction; dry-run rolls back."""
    from scripts.season_split_migration import run_migration

    await _legacy_multi_season_series(db_session)
    await db_session.commit()

    await run_migration(apply=False, limit=None)
    works = (await db_session.execute(select(func.count()).select_from(TVSeries))).scalar_one()
    colls = (await db_session.execute(select(func.count()).select_from(WorkCollection))).scalar_one()
    assert works == 1
    assert colls == 0

    await run_migration(apply=True, limit=None)
    works = (await db_session.execute(select(func.count()).select_from(TVSeries))).scalar_one()
    colls = (await db_session.execute(select(func.count()).select_from(WorkCollection))).scalar_one()
    assert works == 2
    assert colls == 1


# ---------------------------------------------------------------------------
# Season-level identity routing (P6 增强)
# ---------------------------------------------------------------------------


async def test_season_identity_follows_consistent_resource_season(
    db_session, sample_channel
):
    """逐季源 id（bangumi 等）归属资源季号一致多数指向的季作品，而不是滞留 S1。

    无职转生事故形态：合并后的 legacy 行 seasons JSON 声明 {1,2}，但全部
    非合集资源都落在 S2，袋里挂着该季的 bangumi 条目——拆分后
    bangumi:501963 必须落在 S2 作品袋，否则同一条目的后续匹配无法袋命中
    S2 作品。
    """
    series = await _legacy_multi_season_series(db_session)
    ch = sample_channel.id
    # Unanimous S2 evidence; season-less rows and batch packs don't vote.
    db_session.add_all([
        _resource(ch, series_id=series.id, season=2, episode=1),
        _resource(ch, series_id=series.id, season=2, episode=2),
        _resource(ch, series_id=series.id, season=None, episode=None),
        _resource(ch, series_id=series.id, season=1, is_batch=True,
                  batch_scope="season", episode_start=1, episode_end=12),
    ])
    await db_session.commit()

    report = await migrate_series(db_session, series, apply=True)
    await db_session.commit()

    assert report.status == "split"
    s2_id = next(o.work_id for o in report.outcomes if o.season == 2)
    assert "bangumi:501963" in report.season_identities_routed
    assert "bangumi:501963" in await _series_bag(db_session, s2_id)
    assert "bangumi:501963" not in await _series_bag(db_session, series.id)
    # Series-level ids are unaffected: they still move to the collection bag.
    coll_bag = {
        r.external_id
        for r in await list_external_ids(db_session, "collection", report.collection_id)
    }
    assert "wikipedia:zh:8498329" in coll_bag
    assert await _check_dangling_fks(db_session) == []


async def test_season_identity_stays_on_anchor_when_votes_disagree(
    db_session, sample_channel
):
    """资源季号不一致（S1+S2 都有）时无法确定归属，逐季 id 维持现状留 S1。"""
    series = await _legacy_multi_season_series(db_session)
    ch = sample_channel.id
    db_session.add_all([
        _resource(ch, series_id=series.id, season=1, episode=1),
        _resource(ch, series_id=series.id, season=2, episode=1),
    ])
    await db_session.commit()

    report = await migrate_series(db_session, series, apply=True)
    await db_session.commit()

    assert report.season_identities_routed == []
    assert "bangumi:501963" in await _series_bag(db_session, series.id)


async def test_season_identity_stays_on_anchor_without_votes(
    db_session, sample_channel
):
    """没有带季号的非合集资源（全无季标记）时无法确定归属，维持现状。"""
    series = await _legacy_multi_season_series(db_session)
    db_session.add(
        _resource(sample_channel.id, series_id=series.id, season=None, episode=3)
    )
    await db_session.commit()

    report = await migrate_series(db_session, series, apply=True)
    await db_session.commit()

    assert report.season_identities_routed == []
    assert "bangumi:501963" in await _series_bag(db_session, series.id)


async def test_season_identity_stays_when_consistent_season_is_s1(
    db_session, sample_channel
):
    """一致季号为 1 时规则不触发（≠1 才移动；留在 S1 本就是正确归属）。"""
    series = await _legacy_multi_season_series(db_session)
    db_session.add(
        _resource(sample_channel.id, series_id=series.id, season=1, episode=1)
    )
    await db_session.commit()

    report = await migrate_series(db_session, series, apply=True)
    await db_session.commit()

    assert report.season_identities_routed == []
    assert "bangumi:501963" in await _series_bag(db_session, series.id)


async def test_subscription_retargets_to_downloaded_season(
    db_session, sample_channel, sample_downloader
):
    """订阅按 Agent 下载历史重指向：锚点是 S1，但该 Agent 的完成下载都在 S2。"""
    from datetime import UTC, datetime

    from app.models.download_task import DownloadTask

    series = await _legacy_multi_season_series(db_session)
    agent = await _make_agent(db_session, sample_channel, sample_downloader)
    aw = AgentWork(id=_uuid(), agent_id=agent.id, series_id=series.id, content_type="tv")
    # Control agent: subscribed too, but has no download history.
    other = await _make_agent(db_session, sample_channel, sample_downloader)
    aw_idle = AgentWork(id=_uuid(), agent_id=other.id, series_id=series.id, content_type="tv")
    r_s1 = _resource(sample_channel.id, series_id=series.id, season=1, episode=1)
    r_s2 = _resource(sample_channel.id, series_id=series.id, season=2, episode=3)
    db_session.add_all([aw, aw_idle, r_s1, r_s2])
    await db_session.flush()
    db_session.add_all([
        DownloadTask(id=_uuid(), agent_id=agent.id, file_resource_id=r_s2.id,
                     downloader_id=sample_downloader.id, download_dir="/d",
                     status="completed", completed_at=datetime.now(UTC)),
        # Another agent's download does not count for this agent.
        DownloadTask(id=_uuid(), agent_id=other.id, file_resource_id=r_s1.id,
                     downloader_id=sample_downloader.id, download_dir="/d",
                     status="downloading"),
    ])
    await db_session.commit()

    report = await migrate_series(db_session, series, apply=True)
    await db_session.commit()

    s2 = next(o for o in report.outcomes if o.season == 2)
    await db_session.refresh(aw)
    await db_session.refresh(aw_idle)
    assert aw.series_id == s2.work_id
    assert report.subscriptions_retargeted[0]["from_season"] == 1
    assert report.subscriptions_retargeted[0]["to_season"] == 2
    # No history → stays on the anchor.
    assert aw_idle.series_id == series.id
    # The retargeted agent now covers S2, so the suggestion list is empty;
    # the idle agent still covers only S1 and gets the S2 suggestion.
    by_agent = {s["agent_id"]: s for s in report.agent_suggestions}
    assert agent.id not in by_agent
    assert [x["season"] for x in by_agent[other.id]["suggested"]] == [2]
