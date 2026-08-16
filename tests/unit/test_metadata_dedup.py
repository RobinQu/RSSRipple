"""Tests for the metadata dedup service.

Covers the merge-duplicate-series/movie flow that repairs rows created before
canonical-external-id upsert was in place.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models.agent import Agent
from app.models.agent_work import AgentWork
from app.models.channel import Channel
from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services import metadata_dedup as dedup


def _uuid() -> str:
    return str(uuid.uuid4())


TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"torrent_url": {"source": "link"}},
}


@pytest.fixture
async def channel(db_session):
    ch = Channel(
        id=_uuid(),
        name="ch",
        type="rss_feed",
        url="https://example.com/rss",
        field_mapping=TEST_FIELD_MAPPING,
        metadata_agent_enabled=False,
    )
    db_session.add(ch)
    await db_session.flush()
    return ch


async def _make_series(db_session, *, external_id: str, title_cn: str, title_en: str,
                       created_at: datetime, poster: str | None = None,
                       aliases: list[str] | None = None) -> TVSeries:
    s = TVSeries(
        id=_uuid(),
        title_cn=title_cn,
        title_en=title_en,
        original_title=title_en,
        aliases=aliases,
        external_id=external_id,
        external_source="exa",
        poster_url=poster,
        content_type="tv",
        created_at=created_at,
        updated_at=created_at,
    )
    db_session.add(s)
    await db_session.flush()
    return s


async def test_merge_duplicate_series_collapses_and_repoints(db_session, channel):
    """Three rows for the same work; oldest survives, FKs re-pointed."""
    t0 = datetime(2025, 1, 1, tzinfo=UTC)
    s1 = await _make_series(
        db_session, external_id="TMDB:82684",
        title_cn="关于我转生变成史莱姆这档事 第四季",
        title_en="That Time I Got Reincarnated as a Slime Season 4",
        created_at=t0, poster="/posters/keep.jpg",
    )
    s2 = await _make_series(
        db_session, external_id="TMDB 82684",
        title_cn="关于我转生变成史莱姆这档事 第四季",
        title_en="That Time I Got Reincarnated as a Slime Season 4",
        created_at=t0 + timedelta(minutes=1),
    )
    s3 = await _make_series(
        db_session, external_id="TMDB TV 82684 / season 4",
        title_cn="关于我转生变成史莱姆这档事 第四季",
        title_en="That Time I Got Reincarnated as a Slime Season 4",
        created_at=t0 + timedelta(minutes=2),
        aliases=["转生史莱姆"],
    )

    # Point one FileResource at each series.
    for s in (s1, s2, s3):
        r = FileResource(
            id=_uuid(),
            channel_id=channel.id,
            guid=f"g-{s.id}",
            title_raw="raw",
            torrent_url="magnet:?xt=1",
            series_id=s.id,
        )
        db_session.add(r)
    await db_session.flush()

    report = await dedup.merge_duplicate_series(db_session)
    await db_session.flush()

    assert report.series_groups == 1
    assert report.series_removed == 2
    assert report.file_resources_updated == 2

    # Survivor is s1 (oldest); duplicates gone.
    remaining = (await db_session.execute(
        __import__("sqlalchemy").select(TVSeries)
    )).scalars().all()
    assert len(remaining) == 1
    survivor = remaining[0]
    assert survivor.id == s1.id
    assert survivor.external_id == "tmdb:82684"  # canonicalized
    assert survivor.poster_url == "/posters/keep.jpg"
    assert "转生史莱姆" in (survivor.aliases or [])

    # All FileResources point at survivor.
    from sqlalchemy import select
    resources = (await db_session.execute(select(FileResource))).scalars().all()
    assert all(r.series_id == s1.id for r in resources)


async def test_merge_duplicate_series_repoints_agent_works_and_mappings(db_session, channel):
    """AgentWork and ChannelRawTitleMapping FKs must be re-pointed too."""
    t0 = datetime(2025, 1, 1, tzinfo=UTC)
    s1 = await _make_series(
        db_session, external_id="TMDB:1",
        title_cn="剧A", title_en="Show A", created_at=t0,
    )
    s2 = await _make_series(
        db_session, external_id="TMDB 1",
        title_cn="剧A", title_en="Show A", created_at=t0 + timedelta(seconds=1),
    )

    # Fixture setup for AgentWork
    dl = DownloaderInstance(
        id=_uuid(), name="dl", type="transmission",
        url="http://x", download_dir="/tmp",
    )
    db_session.add(dl)
    await db_session.flush()
    agent = Agent(
        id=_uuid(), name="a", channel_id=channel.id, downloader_id=dl.id,
        task_expire_days=30, llm_enabled=False,
        scope_channel_wide=False, conflict_resolution="ask",
    )
    db_session.add(agent)
    await db_session.flush()

    aw = AgentWork(
        id=_uuid(), agent_id=agent.id, content_type="tv",
        series_id=s2.id, enable_episode_dedup=True,
    )
    db_session.add(aw)

    m = ChannelRawTitleMapping(
        id=_uuid(), channel_id=channel.id,
        raw_title="raw title",
        search_title_key="剧a",
        content_type="tv",
        series_id=s2.id,
    )
    db_session.add(m)
    await db_session.flush()

    report = await dedup.merge_duplicate_series(db_session)
    await db_session.flush()

    assert report.agent_works_updated == 1
    assert report.mappings_updated == 1
    await db_session.refresh(aw)
    await db_session.refresh(m)
    assert aw.series_id == s1.id
    assert m.series_id == s1.id


async def test_merge_duplicate_series_drops_conflicting_children(db_session, channel):
    """Regression: duplicates owning child rows whose natural key the survivor
    already has (Episode uq, mapping uq, app-level AgentWork/PendingDecision
    singletons) must not abort the whole merge with an IntegrityError —
    conflicting duplicate rows are dropped, the rest re-pointed."""
    from sqlalchemy import select

    from app.models.episode import Episode
    from app.models.pending_decision import PendingDecision

    t0 = datetime(2025, 1, 1, tzinfo=UTC)
    s1 = await _make_series(
        db_session, external_id="TMDB:1",
        title_cn="剧A", title_en="Show A", created_at=t0,
    )
    s2 = await _make_series(
        db_session, external_id="TMDB 1",
        title_cn="剧A", title_en="Show A", created_at=t0 + timedelta(seconds=1),
    )
    dl = DownloaderInstance(
        id=_uuid(), name="dl", type="transmission", url="http://x", download_dir="/tmp",
    )
    db_session.add(dl)
    await db_session.flush()
    agent = Agent(
        id=_uuid(), name="a", channel_id=channel.id, downloader_id=dl.id,
        task_expire_days=30, llm_enabled=False,
        scope_channel_wide=False, conflict_resolution="ask",
    )
    db_session.add(agent)
    await db_session.flush()

    # Both series own (1,1); s1 additionally (1,2), s2 additionally (1,3).
    # (Mapping collisions can't exist — its uq is series-independent.)
    db_session.add_all([
        Episode(id=_uuid(), series_id=s1.id, season=1, episode=1, title="s1e1"),
        Episode(id=_uuid(), series_id=s1.id, season=1, episode=2),
        Episode(id=_uuid(), series_id=s2.id, season=1, episode=1, title="s2e1"),
        Episode(id=_uuid(), series_id=s2.id, season=1, episode=3),
        AgentWork(
            id=_uuid(), agent_id=agent.id, content_type="tv",
            series_id=s1.id, enable_episode_dedup=True,
        ),
        AgentWork(
            id=_uuid(), agent_id=agent.id, content_type="tv",
            series_id=s2.id, enable_episode_dedup=True,
        ),
        ChannelRawTitleMapping(
            id=_uuid(), channel_id=channel.id, raw_title="raw",
            search_title_key="剧a", content_type="tv", series_id=s2.id,
        ),
        PendingDecision(
            id=_uuid(), agent_id=agent.id, series_id=s1.id,
            season=1, episode=1, candidates=["r1"], reason="x", status="pending",
        ),
        PendingDecision(
            id=_uuid(), agent_id=agent.id, series_id=s2.id,
            season=1, episode=1, candidates=["r2"], reason="x", status="pending",
        ),
    ])
    await db_session.flush()

    report = await dedup.merge_duplicate_series(db_session)
    await db_session.flush()

    assert report.series_removed == 1
    episodes = (await db_session.execute(
        select(Episode).where(Episode.series_id == s1.id)
    )).scalars().all()
    assert sorted((e.season, e.episode) for e in episodes) == [(1, 1), (1, 2), (1, 3)]
    # Survivor's own (1,1) wins over the duplicate's.
    e11 = next(e for e in episodes if e.episode == 1)
    assert e11.title == "s1e1"
    assert len((await db_session.execute(select(AgentWork))).scalars().all()) == 1
    mappings = (await db_session.execute(select(ChannelRawTitleMapping))).scalars().all()
    assert len(mappings) == 1
    assert mappings[0].series_id == s1.id
    decisions = (await db_session.execute(select(PendingDecision))).scalars().all()
    assert len(decisions) == 1
    assert decisions[0].series_id == s1.id


async def test_merge_skips_year_conflicting_group(db_session):
    """Regression: rows sharing a normalized title but premiered years apart
    (remake/reboot/同名系列, e.g. a 1995 film page and a 2026 TV series page)
    must NOT be merged — the oldest row would otherwise swallow the newer
    work and re-point its resources."""
    from sqlalchemy import select

    t0 = datetime(2025, 1, 1, tzinfo=UTC)
    old = TVSeries(
        id=_uuid(), title_cn="攻殼機動隊", title_en="Ghost in the Shell",
        original_title="攻殻機動隊", external_id="wikipedia:190601",
        external_source="wikipedia", content_type="tv",
        start_date=datetime(1995, 11, 18, tzinfo=UTC).date(),
        created_at=t0, updated_at=t0,
    )
    new = TVSeries(
        id=_uuid(), title_cn="攻壳机动队", title_en="THE GHOST IN THE SHELL",
        original_title="攻殻機動隊 THE GHOST IN THE SHELL",
        external_id="wikipedia:9390967", external_source="wikipedia",
        content_type="tv",
        start_date=datetime(2026, 7, 7, tzinfo=UTC).date(),
        created_at=t0 + timedelta(days=1), updated_at=t0,
    )
    db_session.add_all([old, new])
    await db_session.flush()

    report = await dedup.merge_duplicate_series(db_session)
    await db_session.flush()

    assert report.series_removed == 0
    assert any("year-conflicting" in n for n in report.notes)
    assert len((await db_session.execute(select(TVSeries))).scalars().all()) == 2


async def test_merge_duplicate_series_is_idempotent(db_session, channel):
    t0 = datetime(2025, 1, 1, tzinfo=UTC)
    await _make_series(
        db_session, external_id="TMDB:1", title_cn="剧A", title_en="Show A",
        created_at=t0,
    )
    await _make_series(
        db_session, external_id="TMDB 1", title_cn="剧A", title_en="Show A",
        created_at=t0 + timedelta(seconds=1),
    )

    r1 = await dedup.merge_duplicate_series(db_session)
    await db_session.flush()
    r2 = await dedup.merge_duplicate_series(db_session)
    await db_session.flush()

    assert r1.series_removed == 1
    assert r2.series_removed == 0


async def test_merge_duplicate_movies(db_session):
    t0 = datetime(2025, 1, 1, tzinfo=UTC)
    m1 = Movie(
        id=_uuid(), title_cn="电影A", title_en="Movie A",
        external_id="TMDB:100", external_source="exa", content_type="movie",
        created_at=t0, updated_at=t0,
    )
    m2 = Movie(
        id=_uuid(), title_cn="电影A", title_en="Movie A",
        external_id="TMDB 100", external_source="exa", content_type="movie",
        created_at=t0 + timedelta(seconds=1), updated_at=t0 + timedelta(seconds=1),
    )
    db_session.add_all([m1, m2])
    await db_session.flush()

    report = await dedup.merge_duplicate_movies(db_session)
    await db_session.flush()

    assert report.movie_groups == 1
    assert report.movies_removed == 1
    from sqlalchemy import select
    survivors = (await db_session.execute(select(Movie))).scalars().all()
    assert len(survivors) == 1
    assert survivors[0].id == m1.id
    assert survivors[0].external_id == "tmdb:100"


# ---------------------------------------------------------------------------
# Cross-table (Movie <-> TVSeries) dedup
# ---------------------------------------------------------------------------


async def test_cross_type_movie_with_episode_resources_folds_into_series(db_session, channel):
    """The Slime case: same wikipedia entity filed as both Movie and TVSeries.
    Episode-bearing resources on the Movie side prove it's a series."""
    movie = Movie(
        id=_uuid(), title_cn="關於我轉生變成史萊姆這檔事",
        external_id="wikipedia:5139056", external_source="wikipedia",
        content_type="movie",
    )
    series = TVSeries(
        id=_uuid(), title_en="That Time I Got Reincarnated as a Slime",
        external_id="wikipedia:5139056", external_source="wikipedia",
        content_type="tv",
    )
    db_session.add_all([movie, series])
    await db_session.flush()
    r = FileResource(
        id=_uuid(), channel_id=channel.id, guid="g1", title_raw="raw",
        torrent_url="magnet:?xt=1", movie_id=movie.id, episode=88, is_batch=False,
    )
    db_session.add(r)
    await db_session.flush()

    report = await dedup.merge_cross_type_duplicates(db_session)
    await db_session.flush()

    assert report.cross_type_merges == 1
    from sqlalchemy import select
    assert (await db_session.execute(select(Movie))).scalars().all() == []
    await db_session.refresh(r)
    assert r.series_id == series.id
    assert r.movie_id is None
    # Survivor enriched from the loser's title slots.
    assert series.title_cn == "關於我轉生變成史萊姆這檔事"


async def test_cross_type_no_episode_evidence_keeps_movie(db_session, channel):
    """A genuine film duplicated into both tables (no episodes anywhere):
    the Movie survives."""
    movie = Movie(
        id=_uuid(), title_cn="剧场版甲", title_en="Film A",
        external_id="tmdb:777", external_source="tmdb", content_type="movie",
    )
    series = TVSeries(
        id=_uuid(), title_cn="剧场版甲", external_id="tmdb:777",
        external_source="tmdb", content_type="tv",
    )
    db_session.add_all([movie, series])
    await db_session.flush()
    r = FileResource(
        id=_uuid(), channel_id=channel.id, guid="g2", title_raw="raw",
        torrent_url="magnet:?xt=1", series_id=series.id,
    )
    db_session.add(r)
    await db_session.flush()

    report = await dedup.merge_cross_type_duplicates(db_session)
    await db_session.flush()

    assert report.cross_type_merges == 1
    from sqlalchemy import select
    assert (await db_session.execute(select(TVSeries))).scalars().all() == []
    await db_session.refresh(r)
    assert r.movie_id == movie.id
    assert r.series_id is None


async def test_cross_type_shared_title_without_external_id(db_session, channel):
    """Pairs can also be detected via shared normalized title (trad/simp fold)."""
    movie = Movie(
        id=_uuid(), title_cn="關於我轉生變成史萊姆這檔事",
        external_id="wikipedia:5139056", external_source="wikipedia",
        content_type="movie",
    )
    series = TVSeries(
        id=_uuid(), title_cn="关于我转生变成史莱姆这档事",  # simplified twin
        external_id="exa:abc", external_source="exa", content_type="tv",
    )
    db_session.add_all([movie, series])
    await db_session.flush()
    r = FileResource(
        id=_uuid(), channel_id=channel.id, guid="g3", title_raw="raw",
        torrent_url="magnet:?xt=1", movie_id=movie.id, episode=1, is_batch=False,
    )
    db_session.add(r)
    await db_session.flush()

    report = await dedup.merge_cross_type_duplicates(db_session)
    await db_session.flush()

    assert report.cross_type_merges == 1
    from sqlalchemy import select
    assert (await db_session.execute(select(Movie))).scalars().all() == []
    await db_session.refresh(r)
    assert r.series_id == series.id


# ---------------------------------------------------------------------------
# Batch 3: collection_id preservation across merges
# ---------------------------------------------------------------------------


async def test_merge_movies_inherits_collection_when_survivor_has_none(db_session):
    """Survivor without a collection inherits the duplicate's membership."""
    coll_id = _uuid()
    db_session.add(WorkCollection(id=coll_id, title_cn="狮子王（系列）"))
    t0 = datetime(2025, 1, 1, tzinfo=UTC)
    m1 = Movie(
        id=_uuid(), title_cn="狮子王", external_id="exa:a", external_source="exa",
        content_type="movie", created_at=t0, updated_at=t0,
    )
    m2 = Movie(
        id=_uuid(), title_cn="狮子王", external_id="exa:b", external_source="exa",
        content_type="movie", created_at=t0 + timedelta(minutes=1),
        updated_at=t0, collection_id=coll_id,
    )
    db_session.add_all([m1, m2])
    await db_session.flush()

    await dedup.merge_duplicate_movies(db_session)
    await db_session.flush()

    from sqlalchemy import select
    survivor = (await db_session.execute(select(Movie))).scalars().one()
    assert survivor.id == m1.id  # oldest survives
    assert survivor.collection_id == coll_id


async def test_merge_movies_keeps_survivor_collection(db_session):
    """A survivor with its own collection keeps it over the duplicate's."""
    keep_id, dup_id = _uuid(), _uuid()
    db_session.add_all([
        WorkCollection(id=keep_id, title_cn="A"),
        WorkCollection(id=dup_id, title_cn="B"),
    ])
    t0 = datetime(2025, 1, 1, tzinfo=UTC)
    m1 = Movie(
        id=_uuid(), title_cn="狮子王", external_id="exa:a", external_source="exa",
        content_type="movie", created_at=t0, updated_at=t0, collection_id=keep_id,
    )
    m2 = Movie(
        id=_uuid(), title_cn="狮子王", external_id="exa:b", external_source="exa",
        content_type="movie", created_at=t0 + timedelta(minutes=1),
        updated_at=t0, collection_id=dup_id,
    )
    db_session.add_all([m1, m2])
    await db_session.flush()

    await dedup.merge_duplicate_movies(db_session)
    await db_session.flush()

    from sqlalchemy import select
    survivor = (await db_session.execute(select(Movie))).scalars().one()
    assert survivor.collection_id == keep_id


async def test_merge_series_inherits_collection_when_survivor_has_none(db_session):
    coll_id = _uuid()
    db_session.add(WorkCollection(id=coll_id, title_cn="攻壳机动队（系列）"))
    t0 = datetime(2025, 1, 1, tzinfo=UTC)
    s1 = TVSeries(
        id=_uuid(), title_cn="攻壳机动队", external_id="exa:a", external_source="exa",
        content_type="tv", created_at=t0, updated_at=t0,
    )
    s2 = TVSeries(
        id=_uuid(), title_cn="攻壳机动队", external_id="exa:b", external_source="exa",
        content_type="tv", created_at=t0 + timedelta(minutes=1),
        updated_at=t0, collection_id=coll_id,
    )
    db_session.add_all([s1, s2])
    await db_session.flush()

    await dedup.merge_duplicate_series(db_session)
    await db_session.flush()

    from sqlalchemy import select
    survivor = (await db_session.execute(select(TVSeries))).scalars().one()
    assert survivor.id == s1.id
    assert survivor.collection_id == coll_id
