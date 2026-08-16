"""Tests for franchise_service.link_franchise_pack.

The UnifiedMetadataAgent is mocked at ``get_agent`` (same pattern as
test_fetch_service.py); work upserts and the WorkCollection get-or-create
run against the real test DB.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock, patch

from sqlalchemy import select

from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.franchise_service import FRANCHISE_PACK_SOURCE, link_franchise_pack
from app.services.metadata_resource_meta import ResourceMetadata
from app.services.torrent_inspect import TorrentReport


def _uuid() -> str:
    return str(uuid.uuid4())


async def _channel(db_session, *, metadata_source="wikipedia") -> Channel:
    ch = Channel(
        id=_uuid(),
        name=f"ch-{_uuid()[:8]}",
        type="rss_feed",
        url="https://example.com/rss",
        field_mapping={"title": "title", "link": "link"},
        metadata_source=metadata_source,
        status="active",
    )
    db_session.add(ch)
    await db_session.flush()
    return ch


def _resource(channel_id: str, **over) -> FileResource:
    base = dict(
        id=_uuid(),
        channel_id=channel_id,
        guid=_uuid(),
        title_raw="[字幕组] 作品X 系列 [TV+剧场版][合集]",
        search_title="作品X 系列",
        torrent_url="https://x/pack.torrent",
        is_batch=True,
        batch_scope="franchise",
    )
    base.update(over)
    return FileResource(**base)


def _report(*titles: str) -> TorrentReport:
    return TorrentReport(scope="franchise", is_batch=True, work_titles=list(titles))


def _tv_hit(external_id: str, title_cn: str) -> ResourceMetadata:
    return ResourceMetadata(
        clean_title=title_cn,
        found=True,
        content_type="tv",
        matched_entity={
            "external_id": external_id,
            "external_source": "tmdb",
            "title_cn": title_cn,
        },
    )


def _movie_hit(external_id: str, title_cn: str) -> ResourceMetadata:
    return ResourceMetadata(
        clean_title=title_cn,
        found=True,
        content_type="movie",
        matched_entity={
            "external_id": external_id,
            "external_source": "tmdb",
            "title_cn": title_cn,
        },
    )


def _miss(title: str) -> ResourceMetadata:
    return ResourceMetadata(clean_title=title, found=False, reason="no match")


def _agent(results: dict[str, ResourceMetadata | Exception]) -> MagicMock:
    """Mock UnifiedMetadataAgent whose process_title_only dispatches by title."""
    mock = MagicMock()
    calls: list[tuple[str, str | None]] = []

    async def _process_title_only(title, data_source_type=None):
        calls.append((title, data_source_type))
        r = results[title]
        if isinstance(r, Exception):
            raise r
        return r

    mock.process_title_only = _process_title_only
    mock.calls = calls
    return mock


async def _collections(db_session) -> list[WorkCollection]:
    return (await db_session.execute(select(WorkCollection))).scalars().all()


async def test_two_members_create_collection_and_link(db_session):
    ch = await _channel(db_session, metadata_source="tmdb")
    resource = _resource(ch.id)
    db_session.add(resource)
    await db_session.flush()

    agent = _agent({
        "作品X TV": _tv_hit("tmdb:100", "作品X"),
        "作品X 剧场版": _movie_hit("tmdb:200", "作品X 剧场版"),
    })
    with patch("app.services.metadata_agent.get_agent", return_value=agent):
        await link_franchise_pack(db_session, resource, _report("作品X TV", "作品X 剧场版"), ch)
    await db_session.flush()

    # Channel source config drives the agent call.
    assert agent.calls == [("作品X TV", "tmdb"), ("作品X 剧场版", "tmdb")]

    colls = await _collections(db_session)
    assert len(colls) == 1
    coll = colls[0]
    assert coll.external_source == FRANCHISE_PACK_SOURCE
    assert coll.external_id is None
    assert coll.title_cn == "作品X 系列"

    series = (await db_session.execute(select(TVSeries))).scalars().one()
    movie = (await db_session.execute(select(Movie))).scalars().one()
    assert series.collection_id == coll.id
    assert movie.collection_id == coll.id

    assert resource.collection_id == coll.id
    # FK-exclusivity invariant.
    assert resource.series_id is None
    assert resource.movie_id is None
    assert resource.audio_work_id is None
    # Batch verdict untouched.
    assert resource.is_batch is True and resource.batch_scope == "franchise"


async def test_partial_member_failure_still_links(db_session):
    ch = await _channel(db_session)
    resource = _resource(ch.id)
    db_session.add(resource)
    await db_session.flush()

    agent = _agent({
        "作品X TV": _tv_hit("tmdb:100", "作品X"),
        "作品X 剧场版": _miss("作品X 剧场版"),
        "作品X OVA": RuntimeError("llm down"),
    })
    report = _report("作品X TV", "作品X 剧场版", "作品X OVA")
    with patch("app.services.metadata_agent.get_agent", return_value=agent):
        await link_franchise_pack(db_session, resource, report, ch)
    await db_session.flush()

    colls = await _collections(db_session)
    assert len(colls) == 1
    series = (await db_session.execute(select(TVSeries))).scalars().one()
    assert series.collection_id == colls[0].id
    assert (await db_session.execute(select(Movie))).scalars().all() == []
    assert resource.collection_id == colls[0].id


async def test_collection_reused_on_second_run(db_session):
    """Two franchise packs with the same normalized title share one collection."""
    ch = await _channel(db_session)
    r1 = _resource(ch.id)
    r2 = _resource(ch.id, search_title="[字幕组] 作品X 系列")  # brackets cleaned -> same title
    db_session.add_all([r1, r2])
    await db_session.flush()

    agent = _agent({
        "作品X TV": _tv_hit("tmdb:100", "作品X"),
        "作品X 剧场版": _movie_hit("tmdb:200", "作品X 剧场版"),
    })
    with patch("app.services.metadata_agent.get_agent", return_value=agent):
        await link_franchise_pack(db_session, r1, _report("作品X TV", "作品X 剧场版"), ch)
        await db_session.flush()
        await link_franchise_pack(db_session, r2, _report("作品X TV", "作品X 剧场版"), ch)
    await db_session.flush()

    colls = await _collections(db_session)
    assert len(colls) == 1
    assert r1.collection_id == r2.collection_id == colls[0].id
    # Work upserts converged too (identity by external_id).
    assert len((await db_session.execute(select(TVSeries))).scalars().all()) == 1
    assert len((await db_session.execute(select(Movie))).scalars().all()) == 1


async def test_work_with_existing_collection_not_stolen(db_session):
    ch = await _channel(db_session)
    other = WorkCollection(
        id=_uuid(), title_cn="TMDB 合集", external_source="tmdb_collection",
        external_id="131295",
    )
    movie = Movie(
        id=_uuid(), title_cn="作品X 剧场版", external_id="tmdb:200",
        external_source="tmdb", content_type="movie", collection_id=other.id,
    )
    resource = _resource(ch.id)
    db_session.add_all([other, movie, resource])
    await db_session.flush()

    agent = _agent({"作品X 剧场版": _movie_hit("tmdb:200", "作品X 剧场版")})
    with patch("app.services.metadata_agent.get_agent", return_value=agent):
        await link_franchise_pack(db_session, resource, _report("作品X 剧场版"), ch)
    await db_session.flush()

    colls = await _collections(db_session)
    assert len(colls) == 2
    new_coll = next(c for c in colls if c.external_source == FRANCHISE_PACK_SOURCE)
    # The movie keeps its TMDB collection; the resource still links to the new one.
    assert movie.collection_id == other.id
    assert resource.collection_id == new_coll.id


async def test_all_members_fail_keeps_batch_verdict_only(db_session):
    ch = await _channel(db_session)
    resource = _resource(ch.id)
    db_session.add(resource)
    await db_session.flush()

    agent = _agent({
        "作品X TV": _miss("作品X TV"),
        "作品X 剧场版": _miss("作品X 剧场版"),
    })
    with patch("app.services.metadata_agent.get_agent", return_value=agent):
        await link_franchise_pack(db_session, resource, _report("作品X TV", "作品X 剧场版"), ch)
    await db_session.flush()

    assert await _collections(db_session) == []
    assert resource.collection_id is None
    assert resource.series_id is None and resource.movie_id is None
    assert resource.is_batch is True and resource.batch_scope == "franchise"


async def test_entity_without_title_skipped(db_session):
    """found=True but a title-less matched_entity never upserts a shell row."""
    ch = await _channel(db_session)
    resource = _resource(ch.id)
    db_session.add(resource)
    await db_session.flush()

    agent = _agent({
        "作品X TV": ResourceMetadata(
            clean_title="作品X TV", found=True, content_type="tv",
            matched_entity={"external_id": "tmdb:100", "external_source": "tmdb"},
        ),
    })
    with patch("app.services.metadata_agent.get_agent", return_value=agent):
        await link_franchise_pack(db_session, resource, _report("作品X TV"), ch)
    await db_session.flush()

    assert await _collections(db_session) == []
    assert (await db_session.execute(select(TVSeries))).scalars().all() == []
    assert resource.collection_id is None
