"""In-process service-layer coverage for ``app.services.metadata_dedup``.

Complements tests/integration/http/test_dedup_seed.py (which only seeds rows
for the post-suite script run) by exercising the dedup machine directly:

- ``_series_cluster_bucket`` legacy/season isolation and
  ``_pick_canonical_external_id`` preference order (pure helpers)
- series merge: survivor enrichment, AgentWork/PendingDecision/Episode
  collision drops, link/assignment re-pointing with manual-provenance transfer
- movie merge: the same child-collision matrix plus runtime/collection fields
- year guards for both tables (remakes/reboots never merge)
- cross-type merges: external-id pairing, the title-pairing year guard, the
  keep-series (episode evidence) and keep-movie branches, and the
  series→movie assignment TV-placement clearing
- ``rehome_series_as_movie`` targeted repair
- ``merge_duplicate_metadata`` idempotency
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from types import SimpleNamespace

import pytest_asyncio
from sqlalchemy import select

from app.models.agent import Agent
from app.models.agent_work import AgentWork
from app.models.channel import Channel
from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
from app.models.downloader import DownloaderInstance
from app.models.episode import Episode
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services import metadata_dedup as md
from app.services.external_ids import find_work_by_external_id, list_external_ids


def _uid() -> str:
    return str(uuid.uuid4())


@pytest_asyncio.fixture
async def channel(db_session) -> Channel:
    ch = Channel(
        id=_uid(),
        name="Dedup Channel",
        type="rss_feed",
        url="https://example.com/rss",
        fetch_interval=1800,
        status="active",
        field_mapping={"list_locator": {"source": "entries"}},
        metadata_agent_enabled=False,
    )
    db_session.add(ch)
    await db_session.flush()
    return ch


@pytest_asyncio.fixture
async def downloader(db_session) -> DownloaderInstance:
    dl = DownloaderInstance(
        id=_uid(),
        name="Dedup Downloader",
        type="transmission",
        url="http://127.0.0.1:9091/transmission/rpc",
        download_dir="/downloads/rssripple",
        status="disconnected",
    )
    db_session.add(dl)
    await db_session.flush()
    return dl


async def _series(db, *, created_at: datetime | None = None, **kw) -> TVSeries:
    defaults: dict = dict(id=_uid(), content_type="tv", external_source="manual")
    defaults.update(kw)
    s = TVSeries(**defaults)
    if created_at is not None:
        s.created_at = created_at
    db.add(s)
    await db.flush()
    return s


async def _movie(db, *, created_at: datetime | None = None, **kw) -> Movie:
    defaults = dict(id=_uid(), content_type="movie", external_source="manual")
    defaults.update(kw)
    m = Movie(**defaults)
    if created_at is not None:
        m.created_at = created_at
    db.add(m)
    await db.flush()
    return m


async def _agent(db, channel, downloader, name: str) -> Agent:
    ag = Agent(id=_uid(), name=name, channel_id=channel.id, downloader_id=downloader.id)
    db.add(ag)
    await db.flush()
    return ag


async def _resource(db, channel, **kw) -> FileResource:
    defaults = dict(
        id=_uid(),
        channel_id=channel.id,
        guid=_uid(),
        title_raw="[Group] Title - 01 [1080p]",
        torrent_url=f"magnet:?xt=urn:btih:{uuid.uuid4().hex}",
    )
    defaults.update(kw)
    r = FileResource(**defaults)
    db.add(r)
    await db.flush()
    return r


async def _mapping(db, channel, key: str, **kw) -> ChannelRawTitleMapping:
    m = ChannelRawTitleMapping(
        id=_uid(),
        channel_id=channel.id,
        raw_title=f"raw {key}",
        search_title_key=key,
        **kw,
    )
    db.add(m)
    await db.flush()
    return m


async def _decision(db, agent, **kw) -> PendingDecision:
    defaults = dict(
        id=_uid(), agent_id=agent.id, candidates=[], reason="conflict", status="pending"
    )
    defaults.update(kw)
    d = PendingDecision(**defaults)
    db.add(d)
    await db.flush()
    return d


async def _agent_work(db, agent, **kw) -> AgentWork:
    aw = AgentWork(id=_uid(), agent_id=agent.id, **kw)
    db.add(aw)
    await db.flush()
    return aw


async def _refresh(db, model, row_id: str):
    return await db.get(model, row_id, populate_existing=True)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_series_cluster_bucket_isolates_legacy_and_seasons():
    legacy = SimpleNamespace(number_of_seasons=3, seasons=None, season_number=1)
    assert md._series_cluster_bucket(legacy) == ("legacy",)
    season2 = SimpleNamespace(number_of_seasons=None, seasons=None, season_number=2)
    assert md._series_cluster_bucket(season2) == ("season", 2)
    no_number = SimpleNamespace(number_of_seasons=None, seasons=None, season_number=None)
    assert md._series_cluster_bucket(no_number) == ("season", 1)


def test_pick_canonical_external_id_prefers_tmdb_canonical():
    messy_first = SimpleNamespace(
        external_id="A Much Longer Identifier", external_source="manual",
        content_type="tv",
    )
    tmdb = SimpleNamespace(
        external_id="TMDB TV 82684 / season 4", external_source=None,
        content_type="tv",
    )
    assert md._pick_canonical_external_id([messy_first, tmdb]) == "tmdb:82684"


def test_pick_canonical_external_id_shortest_and_raw_fallbacks():
    longest = SimpleNamespace(
        external_id="Longer Name Here", external_source="manual", content_type="tv"
    )
    short = SimpleNamespace(
        external_id="abc", external_source="manual", content_type="tv"
    )
    assert md._pick_canonical_external_id([longest, short]) == "abc"
    # Canonicalization yields None for a bare synthetic season suffix — the
    # raw external_id itself becomes the candidate.
    suffix_only = SimpleNamespace(
        external_id="#s2", external_source="manual", content_type="tv"
    )
    assert md._pick_canonical_external_id([suffix_only]) == "#s2"
    empty = SimpleNamespace(external_id=None, external_source=None, content_type="tv")
    assert md._pick_canonical_external_id([empty]) is None


# ---------------------------------------------------------------------------
# Series merge
# ---------------------------------------------------------------------------


async def test_merge_series_enriches_survivor_and_resolves_child_collisions(
    db_session, channel, downloader
):
    ch, dl = channel, downloader
    ag1 = await _agent(db_session, ch, dl, "agent-one")
    ag2 = await _agent(db_session, ch, dl, "agent-two")
    coll = WorkCollection(id=_uid(), title_cn="合集")
    db_session.add(coll)

    survivor = await _series(
        db_session, title_cn="重复剧", created_at=datetime(2020, 1, 1)
    )
    dup = await _series(
        db_session,
        title_cn="重复剧",
        aliases=["重复剧 别名"],
        title_en="Dup EN",
        original_title="Dup OT",
        poster_url="https://img.example/p.jpg",
        description="dup description",
        rating=8.5,
        genre=["Drama"],
        number_of_episodes=12,
        number_of_seasons=1,
        collection_id=coll.id,
        external_id="4242",
        external_source="tmdb",
        created_at=datetime(2021, 1, 1),
    )

    # Child rows: collisions must be dropped, the rest re-pointed.
    r1 = await _resource(db_session, ch, series_id=dup.id)
    r2 = await _resource(db_session, ch)
    aw_collide = await _agent_work(
        db_session, ag1, series_id=dup.id, content_type="tv"
    )
    await _agent_work(db_session, ag1, series_id=survivor.id, content_type="tv")
    aw_move = await _agent_work(db_session, ag2, series_id=dup.id, content_type="tv")
    map_move = await _mapping(db_session, ch, "dup-key", series_id=dup.id)
    pd_collide = await _decision(
        db_session, ag1, series_id=dup.id, season=1, episode=1
    )
    await _decision(db_session, ag1, series_id=survivor.id, season=1, episode=1)
    pd_move = await _decision(db_session, ag1, series_id=dup.id, season=1, episode=2)
    ep_collide = Episode(id=_uid(), series_id=dup.id, season=1, episode=1)
    db_session.add(ep_collide)
    await db_session.flush()
    db_session.add(Episode(id=_uid(), series_id=survivor.id, season=1, episode=1))
    ep_move = Episode(id=_uid(), series_id=dup.id, season=1, episode=2)
    db_session.add(ep_move)
    await db_session.flush()

    # Links: same-resource collision (manual dup link must transfer its
    # provenance onto the surviving auto link) plus a plain re-point.
    link_survivor = ResourceWorkLink(
        id=_uid(), resource_id=r1.id, series_id=survivor.id, source="auto"
    )
    link_dup = ResourceWorkLink(
        id=_uid(), resource_id=r1.id, series_id=dup.id, source="manual"
    )
    link_move = ResourceWorkLink(
        id=_uid(), resource_id=r2.id, series_id=dup.id, source="auto"
    )
    db_session.add_all([link_survivor, link_dup, link_move])
    assignment_move = ResourceFileAssignment(
        id=_uid(), resource_id=r2.id, file_path="S01E03.mkv",
        series_id=dup.id, season=1, episode_start=3, episode_end=3,
    )
    db_session.add(assignment_move)
    await db_session.flush()

    report = await md.merge_duplicate_series(db_session)
    await db_session.flush()

    assert report.series_groups == 1
    assert report.series_removed == 1
    assert await _refresh(db_session, TVSeries, dup.id) is None

    # Survivor enrichment from the duplicate.
    assert survivor.title_en == "Dup EN"
    assert survivor.original_title == "Dup OT"
    assert survivor.poster_url == "https://img.example/p.jpg"
    assert survivor.description == "dup description"
    assert survivor.rating == 8.5
    assert survivor.genre == ["Drama"]
    assert survivor.number_of_episodes == 12
    assert survivor.number_of_seasons == 1
    assert survivor.collection_id == coll.id
    assert survivor.external_id == "tmdb:4242"
    assert set(survivor.aliases or []) >= {"重复剧", "Dup EN", "重复剧 别名"}
    # The duplicate's primary id stays reachable through the survivor's bag.
    found = await find_work_by_external_id(db_session, "series", "tmdb", "4242")
    assert found is not None and found.id == survivor.id

    # Re-pointed children.
    assert report.file_resources_updated == 1
    assert (await _refresh(db_session, FileResource, r1.id)).series_id == survivor.id

    assert report.agent_works_updated == 1  # collision dropped, ag2 row moved
    assert await _refresh(db_session, AgentWork, aw_collide.id) is None
    assert (await _refresh(db_session, AgentWork, aw_move.id)).series_id == survivor.id
    ag1_rows = (
        await db_session.execute(select(AgentWork).where(AgentWork.agent_id == ag1.id))
    ).scalars().all()
    assert len(ag1_rows) == 1 and ag1_rows[0].series_id == survivor.id

    assert report.mappings_updated == 1
    assert (
        await _refresh(db_session, ChannelRawTitleMapping, map_move.id)
    ).series_id == survivor.id

    assert report.pending_decisions_updated == 1
    assert await _refresh(db_session, PendingDecision, pd_collide.id) is None
    assert (
        await _refresh(db_session, PendingDecision, pd_move.id)
    ).series_id == survivor.id

    assert report.episodes_updated == 1
    assert await _refresh(db_session, Episode, ep_collide.id) is None
    moved_ep = await _refresh(db_session, Episode, ep_move.id)
    assert moved_ep.series_id == survivor.id

    assert report.work_links_updated == 1  # only the plain re-point counts
    assert await _refresh(db_session, ResourceWorkLink, link_dup.id) is None
    surviving_link = await _refresh(db_session, ResourceWorkLink, link_survivor.id)
    assert surviving_link.source == "manual"  # provenance transferred
    assert (
        await _refresh(db_session, ResourceWorkLink, link_move.id)
    ).series_id == survivor.id

    assert report.file_assignments_updated == 1
    moved_assignment = await _refresh(
        db_session, ResourceFileAssignment, assignment_move.id
    )
    assert moved_assignment.series_id == survivor.id
    assert moved_assignment.season == 1  # TV placement kept within series merge


async def test_series_year_conflict_skips_merge(db_session):
    a = await _series(
        db_session, title_cn="同名剧", start_date=date(2000, 1, 1)
    )
    b = await _series(
        db_session, title_cn="同名剧", start_date=date(2012, 1, 1)
    )
    report = await md.merge_duplicate_series(db_session)
    assert report.series_groups == 0
    assert any("year-conflicting" in n for n in report.notes)
    assert await _refresh(db_session, TVSeries, a.id) is not None
    assert await _refresh(db_session, TVSeries, b.id) is not None


async def test_merge_series_inherits_title_cn_via_alias_clustering(db_session):
    # No shared title_cn: the cluster forms because the duplicate's alias
    # matches the survivor's title_en; the survivor then inherits title_cn.
    survivor = await _series(
        db_session, title_en="Same Show", created_at=datetime(2020, 1, 1)
    )
    dup = await _series(
        db_session,
        title_cn="同一剧",
        aliases=["Same Show"],
        created_at=datetime(2021, 1, 1),
    )
    report = await md.merge_duplicate_series(db_session)
    assert report.series_removed == 1
    assert survivor.title_cn == "同一剧"
    assert await _refresh(db_session, TVSeries, dup.id) is None


async def test_merge_group_single_row_early_return(db_session):
    # Direct calls below the two-row threshold are no-ops.
    solo_series = await _series(db_session, title_cn="单行剧")
    solo_movie = await _movie(db_session, title_cn="单行电影")
    report = md.DedupReport()
    await md._merge_series_group(db_session, [solo_series], report)
    await md._merge_movie_group(db_session, [solo_movie], report)
    assert report.series_removed == 0
    assert report.movies_removed == 0
    assert await _refresh(db_session, TVSeries, solo_series.id) is not None
    assert await _refresh(db_session, Movie, solo_movie.id) is not None


# ---------------------------------------------------------------------------
# Movie merge
# ---------------------------------------------------------------------------


async def test_merge_movie_enriches_survivor_and_resolves_child_collisions(
    db_session, channel, downloader
):
    ch, dl = channel, downloader
    ag1 = await _agent(db_session, ch, dl, "agent-one")
    ag2 = await _agent(db_session, ch, dl, "agent-two")
    coll = WorkCollection(id=_uid(), title_cn="电影合集")
    db_session.add(coll)

    # The survivor carries only a title_en; it must inherit the duplicate's
    # title_cn (and cluster with it through the shared normalized title_en).
    survivor = await _movie(
        db_session, title_en="Dup Movie EN", created_at=datetime(2020, 1, 1)
    )
    dup = await _movie(
        db_session,
        title_cn="重复电影",
        title_en="Dup Movie EN",
        original_title="Dup Movie OT",
        poster_url="https://img.example/m.jpg",
        description="movie dup description",
        rating=7.0,
        genre=["Action"],
        runtime=120,
        collection_id=coll.id,
        external_id="8888",
        external_source="tmdb",
        created_at=datetime(2021, 1, 1),
    )

    r1 = await _resource(db_session, ch, movie_id=dup.id)
    aw_collide = await _agent_work(
        db_session, ag1, movie_id=dup.id, content_type="movie"
    )
    await _agent_work(db_session, ag1, movie_id=survivor.id, content_type="movie")
    aw_move = await _agent_work(db_session, ag2, movie_id=dup.id, content_type="movie")
    map_move = await _mapping(db_session, ch, "movie-dup-key", movie_id=dup.id)
    pd_collide = await _decision(db_session, ag1, movie_id=dup.id, episode=1)
    await _decision(db_session, ag1, movie_id=survivor.id, episode=1)
    pd_move = await _decision(db_session, ag1, movie_id=dup.id, episode=2)

    report = await md.merge_duplicate_movies(db_session)
    await db_session.flush()

    assert report.movie_groups == 1
    assert report.movies_removed == 1
    assert await _refresh(db_session, Movie, dup.id) is None

    assert survivor.title_cn == "重复电影"  # inherited from the duplicate
    assert survivor.title_en == "Dup Movie EN"
    assert survivor.original_title == "Dup Movie OT"
    assert survivor.poster_url == "https://img.example/m.jpg"
    assert survivor.description == "movie dup description"
    assert survivor.rating == 7.0
    assert survivor.genre == ["Action"]
    assert survivor.runtime == 120
    assert survivor.collection_id == coll.id
    assert survivor.external_id == "tmdb:8888"

    assert report.file_resources_updated == 1
    assert (await _refresh(db_session, FileResource, r1.id)).movie_id == survivor.id
    assert report.agent_works_updated == 1
    assert await _refresh(db_session, AgentWork, aw_collide.id) is None
    assert (await _refresh(db_session, AgentWork, aw_move.id)).movie_id == survivor.id
    assert report.mappings_updated == 1
    assert (
        await _refresh(db_session, ChannelRawTitleMapping, map_move.id)
    ).movie_id == survivor.id
    assert report.pending_decisions_updated == 1
    assert await _refresh(db_session, PendingDecision, pd_collide.id) is None
    assert (
        await _refresh(db_session, PendingDecision, pd_move.id)
    ).movie_id == survivor.id


async def test_movie_year_conflict_skips_merge(db_session):
    a = await _movie(db_session, title_cn="同名电影", release_date=date(1995, 5, 1))
    b = await _movie(db_session, title_cn="同名电影", release_date=date(2026, 5, 1))
    report = await md.merge_duplicate_movies(db_session)
    assert report.movie_groups == 0
    assert any("year-conflicting" in n for n in report.notes)
    assert await _refresh(db_session, Movie, a.id) is not None
    assert await _refresh(db_session, Movie, b.id) is not None


async def test_merge_movie_inherits_title_en(db_session):
    # Mirror of the title_cn inheritance case: here the survivor keeps
    # title_cn as the cluster key and picks up the duplicate's title_en.
    survivor = await _movie(
        db_session, title_cn="继承电影", created_at=datetime(2020, 1, 1)
    )
    dup = await _movie(
        db_session,
        title_cn="继承电影",
        title_en="Inherited EN",
        created_at=datetime(2021, 1, 1),
    )
    report = await md.merge_duplicate_movies(db_session)
    assert report.movies_removed == 1
    assert survivor.title_en == "Inherited EN"
    assert await _refresh(db_session, Movie, dup.id) is None


# ---------------------------------------------------------------------------
# Cross-type (Movie <-> TVSeries) merges
# ---------------------------------------------------------------------------


async def test_cross_type_no_movies_early_return(db_session):
    await _series(db_session, title_cn="只有剧")
    report = await md.merge_cross_type_duplicates(db_session)
    assert report.cross_type_merges == 0


async def test_cross_type_second_movie_skips_already_removed_series(db_session):
    # Both movies pair with the same series; after the first merge deletes
    # the series, the second movie's scan must skip it (removed_series guard).
    series = await _series(db_session, title_cn="三胞胎", start_date=date(2020, 1, 1))
    m1 = await _movie(db_session, title_cn="三胞胎", release_date=date(2020, 3, 1))
    m2 = await _movie(db_session, title_cn="三胞胎", release_date=date(2020, 5, 1))
    report = await md.merge_cross_type_duplicates(db_session)
    # No episode evidence anywhere → the first movie absorbs the series.
    assert report.cross_type_merges == 1
    assert await _refresh(db_session, TVSeries, series.id) is None
    assert await _refresh(db_session, Movie, m1.id) is not None
    assert await _refresh(db_session, Movie, m2.id) is not None


async def test_cross_type_title_pairing_blocked_by_year_conflict(db_session):
    movie = await _movie(
        db_session, title_cn="重生", release_date=date(1995, 1, 1),
        external_id="movie-ext",
    )
    series = await _series(
        db_session, title_cn="重生", start_date=date(2020, 1, 1),
        external_id="series-ext",
    )
    report = await md.merge_cross_type_duplicates(db_session)
    assert report.cross_type_merges == 0
    assert await _refresh(db_session, Movie, movie.id) is not None
    assert await _refresh(db_session, TVSeries, series.id) is not None


async def test_cross_type_keeps_series_with_episode_evidence(
    db_session, channel, downloader
):
    ch, dl = channel, downloader
    ag = await _agent(db_session, ch, dl, "agent")
    movie = await _movie(
        db_session,
        title_cn="同一作品",
        title_en="Shared EN",
        poster_url="https://img.example/shared.jpg",
        description="movie side description",
        rating=7.5,
        genre=["Drama"],
        external_id="555",
        external_source="tmdb",
    )
    series = await _series(
        db_session,
        title_cn="同一作品",
        external_id="TMDB TV 555",  # canonicalizes to tmdb:555 → same entity
        external_source="exa_web",
    )
    # Episode rows are TV evidence: the series must survive.
    db_session.add(Episode(id=_uid(), series_id=series.id, season=1, episode=1))
    await db_session.flush()

    r1 = await _resource(db_session, ch, movie_id=movie.id)
    r2 = await _resource(db_session, ch)
    aw = await _agent_work(db_session, ag, movie_id=movie.id, content_type="movie")
    mapping = await _mapping(db_session, ch, "shared-key", movie_id=movie.id)
    decision = await _decision(db_session, ag, movie_id=movie.id, episode=3)
    link = ResourceWorkLink(id=_uid(), resource_id=r2.id, movie_id=movie.id)
    db_session.add(link)
    await db_session.flush()

    report = await md.merge_cross_type_duplicates(db_session)
    await db_session.flush()

    assert report.cross_type_merges == 1
    assert await _refresh(db_session, Movie, movie.id) is None

    # Movie-side references moved onto the series with tv content_type.
    moved_r = await _refresh(db_session, FileResource, r1.id)
    assert moved_r.series_id == series.id and moved_r.movie_id is None
    moved_aw = await _refresh(db_session, AgentWork, aw.id)
    assert moved_aw.series_id == series.id and moved_aw.content_type == "tv"
    moved_map = await _refresh(db_session, ChannelRawTitleMapping, mapping.id)
    assert moved_map.series_id == series.id and moved_map.content_type == "tv"
    moved_pd = await _refresh(db_session, PendingDecision, decision.id)
    assert moved_pd.series_id == series.id and moved_pd.movie_id is None
    moved_link = await _refresh(db_session, ResourceWorkLink, link.id)
    assert moved_link.series_id == series.id and moved_link.movie_id is None

    # Series enriched from the movie's metadata; identity bag unioned.
    assert series.title_en == "Shared EN"
    assert series.poster_url == "https://img.example/shared.jpg"
    assert series.description == "movie side description"
    assert series.rating == 7.5
    assert series.genre == ["Drama"]
    found = await find_work_by_external_id(db_session, "series", "tmdb", "555")
    assert found is not None and found.id == series.id


async def test_cross_type_keeps_movie_without_episode_evidence(
    db_session, channel, downloader
):
    ch, dl = channel, downloader
    ag = await _agent(db_session, ch, dl, "agent")
    movie = await _movie(
        db_session,
        title_cn="无集作品",
        release_date=date(2020, 6, 1),
        external_id="movie-ext",
    )
    series = await _series(
        db_session,
        title_cn="无集作品",
        title_en="NoEp EN",
        poster_url="https://img.example/noep.jpg",
        description="series side description",
        rating=6.5,
        genre=["Action"],
        start_date=date(2020, 1, 1),
        # Registry source so the primary id is baggable on merge.
        external_id="777",
        external_source="bangumi",
    )

    # A series-side resource with no episode number is not TV evidence.
    r1 = await _resource(db_session, ch, series_id=series.id, episode=None)
    r2 = await _resource(db_session, ch)
    aw = await _agent_work(db_session, ag, series_id=series.id, content_type="tv")
    mapping = await _mapping(db_session, ch, "noep-key", series_id=series.id)
    decision = await _decision(db_session, ag, series_id=series.id, episode=None)
    link = ResourceWorkLink(id=_uid(), resource_id=r2.id, series_id=series.id)
    assignment = ResourceFileAssignment(
        id=_uid(), resource_id=r2.id, file_path="ep01.mkv",
        series_id=series.id, season=1, episode_start=1, episode_end=1,
    )
    db_session.add_all([link, assignment])
    await db_session.flush()

    report = await md.merge_cross_type_duplicates(db_session)
    await db_session.flush()

    assert report.cross_type_merges == 1
    assert await _refresh(db_session, TVSeries, series.id) is None

    moved_r = await _refresh(db_session, FileResource, r1.id)
    assert moved_r.movie_id == movie.id and moved_r.series_id is None
    moved_aw = await _refresh(db_session, AgentWork, aw.id)
    assert moved_aw.movie_id == movie.id and moved_aw.content_type == "movie"
    moved_map = await _refresh(db_session, ChannelRawTitleMapping, mapping.id)
    assert moved_map.movie_id == movie.id and moved_map.content_type == "movie"
    moved_pd = await _refresh(db_session, PendingDecision, decision.id)
    assert moved_pd.movie_id == movie.id and moved_pd.series_id is None
    moved_link = await _refresh(db_session, ResourceWorkLink, link.id)
    assert moved_link.movie_id == movie.id and moved_link.series_id is None

    # Cross-type assignment re-point clears the TV placement fields.
    moved_assignment = await _refresh(
        db_session, ResourceFileAssignment, assignment.id
    )
    assert moved_assignment.movie_id == movie.id
    assert moved_assignment.series_id is None
    assert moved_assignment.season is None
    assert moved_assignment.episode_start is None
    assert moved_assignment.episode_end is None

    # Movie enriched from the series' metadata; identity bag unioned.
    assert movie.title_en == "NoEp EN"
    assert movie.poster_url == "https://img.example/noep.jpg"
    assert movie.description == "series side description"
    assert movie.rating == 6.5
    assert movie.genre == ["Action"]
    found = await find_work_by_external_id(db_session, "movie", "bangumi", "777")
    assert found is not None and found.id == movie.id


# ---------------------------------------------------------------------------
# rehome_series_as_movie
# ---------------------------------------------------------------------------


async def test_rehome_series_as_movie_moves_references_and_bag(
    db_session, channel, downloader
):
    ch, dl = channel, downloader
    ag = await _agent(db_session, ch, dl, "agent")
    series = await _series(db_session, title_cn="错标电影", content_type="movie")
    movie = await _movie(db_session, title_cn="错标电影")

    r1 = await _resource(
        db_session, ch, series_id=series.id, episode=None,
        episode_confidence="reconciled",
    )
    r2 = await _resource(db_session, ch)
    aw = await _agent_work(db_session, ag, series_id=series.id, content_type="tv")
    mapping = await _mapping(db_session, ch, "rehome-key", series_id=series.id)
    decision = await _decision(db_session, ag, series_id=series.id, episode=None)
    link = ResourceWorkLink(
        id=_uid(), resource_id=r2.id, series_id=series.id, source="manual"
    )
    assignment = ResourceFileAssignment(
        id=_uid(), resource_id=r2.id, file_path="film.mkv",
        series_id=series.id, season=1, episode_start=1, episode_end=1,
    )
    db_session.add_all([link, assignment])
    # A bag id only the series carries must follow it onto the movie.
    from app.services.external_ids import add_external_id

    assert await add_external_id(db_session, "series", series.id, "bangumi", "123")
    await db_session.flush()

    await md.rehome_series_as_movie(db_session, series, movie)
    await db_session.flush()

    assert await _refresh(db_session, TVSeries, series.id) is None

    moved_r = await _refresh(db_session, FileResource, r1.id)
    assert moved_r.movie_id == movie.id
    assert moved_r.series_id is None
    assert moved_r.episode_confidence is None  # TV confidence is meaningless now

    moved_aw = await _refresh(db_session, AgentWork, aw.id)
    assert moved_aw.movie_id == movie.id and moved_aw.content_type == "movie"
    moved_map = await _refresh(db_session, ChannelRawTitleMapping, mapping.id)
    assert moved_map.movie_id == movie.id and moved_map.content_type == "movie"
    moved_pd = await _refresh(db_session, PendingDecision, decision.id)
    assert moved_pd.movie_id == movie.id and moved_pd.series_id is None
    moved_link = await _refresh(db_session, ResourceWorkLink, link.id)
    assert moved_link.movie_id == movie.id and moved_link.source == "manual"
    moved_assignment = await _refresh(
        db_session, ResourceFileAssignment, assignment.id
    )
    assert moved_assignment.movie_id == movie.id
    assert moved_assignment.season is None
    assert moved_assignment.episode_start is None

    # Bag rows store the canonical ``source:id`` form.
    bag = await list_external_ids(db_session, "movie", movie.id)
    assert ("bangumi", "bangumi:123") in {
        (row.source, row.external_id) for row in bag
    }


# ---------------------------------------------------------------------------
# Full pipeline idempotency
# ---------------------------------------------------------------------------


async def test_merge_duplicate_metadata_merges_then_is_idempotent(db_session):
    await _series(db_session, title_cn="双双剧", created_at=datetime(2020, 1, 1))
    dup_series = await _series(
        db_session, title_cn="双双剧", created_at=datetime(2021, 1, 1)
    )
    await _movie(db_session, title_cn="双双影", created_at=datetime(2020, 1, 1))
    dup_movie = await _movie(
        db_session, title_cn="双双影", created_at=datetime(2021, 1, 1)
    )

    first = await md.merge_duplicate_metadata(db_session)
    await db_session.flush()
    assert first.series_removed == 1
    assert first.movies_removed == 1
    assert first.cross_type_merges == 0  # series/movie titles do not overlap

    second = await md.merge_duplicate_metadata(db_session)
    assert second.series_removed == 0
    assert second.movies_removed == 0
    assert second.cross_type_merges == 0

    assert await _refresh(db_session, TVSeries, dup_series.id) is None
    assert await _refresh(db_session, Movie, dup_movie.id) is None
    remaining_series = (
        await db_session.execute(select(TVSeries))
    ).scalars().all()
    remaining_movies = (await db_session.execute(select(Movie))).scalars().all()
    assert len(remaining_series) == 1
    assert len(remaining_movies) == 1
