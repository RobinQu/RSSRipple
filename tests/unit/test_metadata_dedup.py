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
