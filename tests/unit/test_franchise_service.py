"""Tests for franchise_service.link_franchise_pack.

The UnifiedMetadataAgent is mocked at ``get_agent`` (same pattern as
test_fetch_service.py); work upserts and the WorkCollection get-or-create
run against the real test DB.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.models.work_external_id import WorkExternalId
from app.services.franchise_service import (
    FRANCHISE_PACK_SOURCE,
    _ensure_auto_link,
    _get_or_create_franchise_collection,
    _is_pack_base_name,
    _movie_base_names,
    _pack_title,
    _resolve_member,
    _same_ip_movies,
    dedupe_resource_movies,
    enforce_franchise_resource_invariant,
    link_franchise_pack,
)
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


def test_enforce_franchise_resource_invariant_covers_shortcut_paths():
    resource = _resource(
        "channel", series_id="series", movie_id=None, audio_work_id=None,
    )
    assert enforce_franchise_resource_invariant(resource) is True
    assert resource.series_id is None
    assert resource.movie_id is None
    assert resource.audio_work_id is None
    assert enforce_franchise_resource_invariant(resource) is False


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


async def test_all_members_fail_still_links_parent_collection(db_session):
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

    collections = await _collections(db_session)
    assert len(collections) == 1
    assert resource.collection_id == collections[0].id
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

    assert len(await _collections(db_session)) == 1
    assert (await db_session.execute(select(TVSeries))).scalars().all() == []
    assert resource.collection_id is not None


# ---------------------------------------------------------------------------
# F3a — pack title cleanup
# ---------------------------------------------------------------------------


def test_pack_title_never_uses_raw_release_string():
    """F3a: the franchise collection is named after the WORK, not the release."""
    resource = _resource(
        "channel",
        search_title=None,
        title_cn=None,
        title_raw="头文字D.全六季.日语.英文字幕.Initial D [BD] [1080p] [AV1]",
    )
    title = _pack_title(resource)
    assert title == "头文字D Initial D"
    for token in ("全六季", "日语", "英文字幕", "BD", "1080p", "AV1"):
        assert token not in title


def test_pack_title_prefers_clean_search_title():
    resource = _resource("channel", search_title="头文字D")
    assert _pack_title(resource) == "头文字D"


async def test_collection_created_with_cleaned_title(db_session):
    ch = await _channel(db_session)
    resource = _resource(
        ch.id,
        search_title=None,
        title_cn=None,
        title_raw="头文字D.全六季.日语.英文字幕.Initial D [BD] [1080p] [AV1]",
    )
    db_session.add(resource)
    await db_session.flush()

    agent = _agent({"Initial D First Stage": _miss("Initial D First Stage")})
    with patch("app.services.metadata_agent.get_agent", return_value=agent):
        await link_franchise_pack(db_session, resource, _report("Initial D First Stage"), ch)
    await db_session.flush()

    coll = (await _collections(db_session))[0]
    assert coll.title_cn == "头文字D Initial D"


# ---------------------------------------------------------------------------
# F3b — same-name auto collection absorption (franchise member attach side)
# ---------------------------------------------------------------------------


async def test_same_name_multi_member_auto_collection_absorbed(db_session):
    """F3b: member works stuck in a multi-member series_group duplicate of
    the same IP are absorbed into the pack collection (shell absorber only
    covers single-member shells)."""
    ch = await _channel(db_session)
    shell = WorkCollection(
        id=_uuid(), title_cn="作品X", external_id=None, external_source="series_group",
    )
    w1 = TVSeries(
        id=_uuid(), title_cn="作品X", external_id="tmdb:100", external_source="tmdb",
        content_type="tv", season_number=1, collection_id=shell.id,
    )
    w2 = TVSeries(
        id=_uuid(), title_cn="作品X 第二季", external_id="tmdb:101", external_source="tmdb",
        content_type="tv", season_number=2, collection_id=shell.id,
    )
    resource = _resource(ch.id)
    db_session.add_all([shell, w1, w2, resource])
    await db_session.flush()

    agent = _agent({
        "作品X TV": _tv_hit("tmdb:100", "作品X"),
        "作品X TV2": _tv_hit("tmdb:101", "作品X 第二季"),
    })
    with patch("app.services.metadata_agent.get_agent", return_value=agent):
        await link_franchise_pack(db_session, resource, _report("作品X TV", "作品X TV2"), ch)
    await db_session.flush()

    colls = await _collections(db_session)
    assert len(colls) == 1
    assert colls[0].external_source == FRANCHISE_PACK_SOURCE
    await db_session.refresh(w1)
    await db_session.refresh(w2)
    assert w1.collection_id == colls[0].id
    assert w2.collection_id == colls[0].id


async def test_absorbed_collection_repoints_parked_resources(db_session):
    """Resources parked on an absorbed collection must follow to the
    survivor — their collection_id must never dangle after the source row
    is deleted."""
    ch = await _channel(db_session)
    shell = WorkCollection(
        id=_uuid(), title_cn="作品X", external_id=None, external_source="series_group",
    )
    w1 = TVSeries(
        id=_uuid(), title_cn="作品X", external_id="tmdb:100", external_source="tmdb",
        content_type="tv", season_number=1, collection_id=shell.id,
    )
    resource = _resource(ch.id)
    parked = _resource(ch.id, guid=_uuid(), collection_id=shell.id)
    db_session.add_all([shell, w1, resource, parked])
    await db_session.flush()

    agent = _agent({"作品X TV": _tv_hit("tmdb:100", "作品X")})
    with patch("app.services.metadata_agent.get_agent", return_value=agent):
        await link_franchise_pack(db_session, resource, _report("作品X TV"), ch)
    await db_session.flush()

    colls = await _collections(db_session)
    assert len(colls) == 1
    await db_session.refresh(parked)
    assert parked.collection_id == colls[0].id


# ---------------------------------------------------------------------------
# F4 — OVA/番外 member shape (season-0 slot)
# ---------------------------------------------------------------------------


def _ova_hit(external_id: str, title_cn: str) -> ResourceMetadata:
    return ResourceMetadata(
        clean_title=title_cn,
        found=True,
        content_type="tv",
        matched_entity={
            "external_id": external_id,
            "external_source": "bangumi",
            "title_cn": title_cn,
            "_platform": "OVA",
        },
    )


async def test_ova_member_upserted_as_season_zero(db_session):
    """F4: an OVA-platform member takes the pack collection's season-0 slot
    (previously it became an ordinary season-1 work)."""
    ch = await _channel(db_session)
    resource = _resource(ch.id)
    db_session.add(resource)
    await db_session.flush()

    def _bangumi_tv(external_id: str, title_cn: str) -> ResourceMetadata:
        return ResourceMetadata(
            clean_title=title_cn,
            found=True,
            content_type="tv",
            matched_entity={
                "external_id": external_id,
                "external_source": "bangumi",
                "title_cn": title_cn,
            },
        )

    agent = _agent({
        "作品X TV": _bangumi_tv("bangumi:100", "作品X"),
        "作品X OVA": _ova_hit("bangumi:110", "作品X OVA"),
    })
    with patch("app.services.metadata_agent.get_agent", return_value=agent):
        await link_franchise_pack(db_session, resource, _report("作品X TV", "作品X OVA"), ch)
    await db_session.flush()

    coll = (await _collections(db_session))[0]
    works = (await db_session.execute(select(TVSeries))).scalars().all()
    by_id = {w.external_id: w for w in works}
    assert by_id["bangumi:100"].season_number == 1
    specials = by_id["bangumi:110"]
    assert specials.season_number == 0
    assert specials.collection_id == coll.id


async def test_second_ova_member_kept_in_shell_and_linked(db_session):
    """F4: the (collection, s0) slot holds ONE work — a second distinct 番外
    member keeps its own shell collection and is linked to the resource."""
    ch = await _channel(db_session)
    resource = _resource(ch.id)
    db_session.add(resource)
    await db_session.flush()

    agent = _agent({
        "作品X OVA": _ova_hit("bangumi:110", "作品X OVA"),
        "作品X Battle Stage": _ova_hit("bangumi:111", "作品X Battle Stage"),
    })
    with patch("app.services.metadata_agent.get_agent", return_value=agent):
        await link_franchise_pack(
            db_session, resource, _report("作品X OVA", "作品X Battle Stage"), ch
        )
    await db_session.flush()

    coll = next(
        c for c in await _collections(db_session)
        if c.external_source == FRANCHISE_PACK_SOURCE
    )
    works = {w.external_id: w for w in (await db_session.execute(select(TVSeries))).scalars().all()}
    first, second = works["bangumi:110"], works["bangumi:111"]
    # Exactly one of them occupies the pack collection's season-0 slot.
    in_pack = [w for w in (first, second) if w.collection_id == coll.id]
    assert len(in_pack) == 1
    assert in_pack[0].season_number == 0
    other = second if in_pack[0] is first else first
    assert other.collection_id != coll.id
    assert other.season_number == 0  # its own shell collection's s0 work
    shell = await db_session.get(WorkCollection, other.collection_id)
    assert shell is not None and shell.external_source == "series_group"
    # …and the shell work is still linked to the resource (attached members
    # get their links from the later cluster-binding/graph passes).
    links = (await db_session.execute(
        select(ResourceWorkLink).where(ResourceWorkLink.resource_id == resource.id)
    )).scalars().all()
    assert {link.series_id for link in links} == {other.id}


# ---------------------------------------------------------------------------
# F5 — local-first reuse in member resolution
# ---------------------------------------------------------------------------


async def test_member_resolution_reuses_local_work(db_session):
    """F5: a member whose matched entity identifies an existing local work is
    reused — identity bagged (creator-wins, primary untouched), no new row."""
    ch = await _channel(db_session)
    movie = Movie(
        id=_uuid(), title_cn="作品X 剧场版", external_id="tmdb:200",
        external_source="tmdb", content_type="movie",
    )
    resource = _resource(ch.id)
    db_session.add_all([movie, resource])
    await db_session.flush()

    agent = _agent({
        "作品X 剧场版": ResourceMetadata(
            clean_title="作品X 剧场版",
            found=True,
            content_type="movie",
            matched_entity={
                "external_id": "mal:999",
                "external_source": "mal",
                "title_cn": "作品X 剧场版",
            },
        ),
    })
    with patch("app.services.metadata_agent.get_agent", return_value=agent):
        await link_franchise_pack(db_session, resource, _report("作品X 剧场版"), ch)
    await db_session.flush()

    movies = (await db_session.execute(select(Movie))).scalars().all()
    assert len(movies) == 1  # no duplicate row
    await db_session.refresh(movie)
    assert movie.external_id == "tmdb:200"  # creator-wins: primary kept
    assert movie.external_source == "tmdb"
    bag = (await db_session.execute(
        select(WorkExternalId).where(
            WorkExternalId.work_type == "movie", WorkExternalId.work_id == movie.id,
        )
    )).scalars().all()
    assert {(b.source, b.external_id) for b in bag} == {("mal", "mal:999")}
    # The reused work still attaches to the pack collection.
    assert movie.collection_id == resource.collection_id


# ---------------------------------------------------------------------------
# C4 — same-resource same-date movie dedup
# ---------------------------------------------------------------------------


async def test_dedupe_resource_movies_merges_same_date_rows(db_session):
    """C4: same resource, same date evidence, same IP, two identities → one
    row (channel-source survivor), assignments/links/bags re-pointed."""
    from datetime import date

    from app.services.franchise_service import dedupe_resource_movies

    ch = await _channel(db_session, metadata_source="bangumi")
    coll = WorkCollection(
        id=_uuid(), title_cn="头文字D Initial D",
        external_id=None, external_source=FRANCHISE_PACK_SOURCE,
    )
    bgm = Movie(
        id=_uuid(), title_cn="头文字D Legend1 -觉醒-",
        external_id="bangumi:78796", external_source="bangumi",
        content_type="movie", release_date=date(2014, 8, 23),
        collection_id=coll.id,
    )
    # Web-fallback row: no release_date — its date evidence comes from the
    # bound file path's [2014] directory year.
    mal = Movie(
        id=_uuid(), title_cn="头文字D 新剧场版 第1章 觉醒",
        external_id="mal:19613", external_source="mal",
        content_type="movie", collection_id=coll.id,
    )
    resource = _resource(ch.id, collection_id=coll.id)
    db_session.add_all([coll, bgm, mal, resource])
    await db_session.flush()
    db_session.add_all([
        ResourceWorkLink(id=_uuid(), resource_id=resource.id, movie_id=bgm.id, source="auto"),
        ResourceWorkLink(id=_uuid(), resource_id=resource.id, movie_id=mal.id, source="llm"),
    ])
    from app.models.resource_file_assignment import ResourceFileAssignment

    db_session.add(ResourceFileAssignment(
        id=_uuid(), resource_id=resource.id,
        file_path="[2014] [Movie] New Initial D Movie Legend 1 - Kakusei/a.mkv",
        movie_id=mal.id, source="llm",
    ))
    await db_session.commit()

    merged = await dedupe_resource_movies(db_session, resource, channel=ch)
    assert merged == 1

    movies = (await db_session.execute(select(Movie))).scalars().all()
    assert len(movies) == 1
    survivor = movies[0]
    assert survivor.id == bgm.id
    assert survivor.external_id == "bangumi:78796"  # creator-wins primary
    assert survivor.external_source == "bangumi"
    bag = (await db_session.execute(
        select(WorkExternalId).where(
            WorkExternalId.work_type == "movie",
            WorkExternalId.work_id == survivor.id,
        )
    )).scalars().all()
    assert {b.external_id for b in bag} == {"mal:19613"}
    links = (await db_session.execute(
        select(ResourceWorkLink).where(ResourceWorkLink.resource_id == resource.id)
    )).scalars().all()
    assert len(links) == 1
    assert links[0].movie_id == bgm.id
    assignments = (await db_session.execute(
        select(ResourceFileAssignment).where(
            ResourceFileAssignment.resource_id == resource.id
        )
    )).scalars().all()
    assert {a.movie_id for a in assignments} == {bgm.id}
    # llm provenance on the assignment survived the re-point.
    assert assignments[0].source == "llm"


async def test_dedupe_resource_movies_no_date_evidence_keeps_both(db_session):
    """C4: without date evidence on either row, nothing merges."""
    from app.services.franchise_service import dedupe_resource_movies

    ch = await _channel(db_session, metadata_source="bangumi")
    coll = WorkCollection(
        id=_uuid(), title_cn="作品X", external_id=None,
        external_source=FRANCHISE_PACK_SOURCE,
    )
    m1 = Movie(
        id=_uuid(), title_cn="作品X 剧场版", external_id="bangumi:1",
        external_source="bangumi", content_type="movie", collection_id=coll.id,
    )
    m2 = Movie(
        id=_uuid(), title_cn="作品X Movie", external_id="mal:2",
        external_source="mal", content_type="movie", collection_id=coll.id,
    )
    resource = _resource(ch.id, collection_id=coll.id)
    db_session.add_all([coll, m1, m2, resource])
    await db_session.flush()
    db_session.add_all([
        ResourceWorkLink(id=_uuid(), resource_id=resource.id, movie_id=m1.id, source="auto"),
        ResourceWorkLink(id=_uuid(), resource_id=resource.id, movie_id=m2.id, source="auto"),
    ])
    await db_session.commit()

    assert await dedupe_resource_movies(db_session, resource, channel=ch) == 0
    assert len((await db_session.execute(select(Movie))).scalars().all()) == 2


async def test_dedupe_resource_movies_different_years_keeps_both(db_session):
    """C4: same IP but different release years never merge."""
    from datetime import date

    from app.services.franchise_service import dedupe_resource_movies

    ch = await _channel(db_session, metadata_source="bangumi")
    coll = WorkCollection(
        id=_uuid(), title_cn="作品X", external_id=None,
        external_source=FRANCHISE_PACK_SOURCE,
    )
    m1 = Movie(
        id=_uuid(), title_cn="作品X Legend1", external_id="bangumi:1",
        external_source="bangumi", content_type="movie",
        release_date=date(2014, 8, 23), collection_id=coll.id,
    )
    m2 = Movie(
        id=_uuid(), title_cn="作品X Legend2", external_id="mal:2",
        external_source="mal", content_type="movie",
        release_date=date(2015, 5, 23), collection_id=coll.id,
    )
    resource = _resource(ch.id, collection_id=coll.id)
    db_session.add_all([coll, m1, m2, resource])
    await db_session.flush()
    db_session.add_all([
        ResourceWorkLink(id=_uuid(), resource_id=resource.id, movie_id=m1.id, source="auto"),
        ResourceWorkLink(id=_uuid(), resource_id=resource.id, movie_id=m2.id, source="auto"),
    ])
    await db_session.commit()

    assert await dedupe_resource_movies(db_session, resource, channel=ch) == 0
    assert len((await db_session.execute(select(Movie))).scalars().all()) == 2


# ---------------------------------------------------------------------------
# D1 — marker-less TV members never seize the pack's season-1 slot
# ---------------------------------------------------------------------------


def _tv_entity(external_id: str, source: str, title_cn: str, title_en: str | None = None):
    return ResourceMetadata(
        clean_title=title_cn,
        found=True,
        content_type="tv",
        matched_entity={
            "external_id": external_id,
            "external_source": source,
            "title_cn": title_cn,
            "title_en": title_en,
        },
    )


async def test_unmarked_tv_member_goes_to_shell_not_pack_s1(db_session):
    """D1: a no-season-evidence TV member ("Initial D Battle Stage" via web
    fallback) keeps its own named shell + link; the true First Stage takes
    the pack's season-1 slot regardless of resolution order."""
    ch = await _channel(db_session, metadata_source="bangumi")
    resource = _resource(ch.id, search_title="头文字D Initial D")
    db_session.add(resource)
    await db_session.flush()

    agent = _agent({
        "Initial D Battle Stage": _tv_entity(
            "mal:821", "mal", "头文字D 战斗舞台", "Initial D Battle Stage",
        ),
        "Initial D First Stage": _tv_entity(
            "bangumi:8290", "bangumi", "头文字D First Stage",
        ),
    })
    with patch("app.services.metadata_agent.get_agent", return_value=agent):
        await link_franchise_pack(
            db_session, resource,
            _report("Initial D Battle Stage", "Initial D First Stage"), ch,
        )
    await db_session.flush()

    pack = next(
        c for c in await _collections(db_session)
        if c.external_source == FRANCHISE_PACK_SOURCE
    )
    works = {
        w.external_id: w
        for w in (await db_session.execute(select(TVSeries))).scalars().all()
    }
    battle, first = works["mal:821"], works["bangumi:8290"]
    # The squatter candidate never enters the pack…
    assert battle.collection_id != pack.id
    shell = await db_session.get(WorkCollection, battle.collection_id)
    assert shell is not None and shell.external_source == "series_group"
    assert battle.season_number == 1  # its own shell's s1 work
    # …the true First Stage occupies (pack, s1) instead.
    assert first.collection_id == pack.id
    assert first.season_number == 1
    # The shell work is still linked to the resource.
    links = (await db_session.execute(
        select(ResourceWorkLink).where(ResourceWorkLink.resource_id == resource.id)
    )).scalars().all()
    assert battle.id in {link.series_id for link in links}
    assert resource.collection_id == pack.id


async def test_unmarked_base_name_member_takes_pack_s1(db_session):
    """D1: an unmarked title that IS the pack's base name ("头文字D" in the
    bilingual "头文字D Initial D") may still take the season-1 slot."""
    ch = await _channel(db_session, metadata_source="bangumi")
    resource = _resource(ch.id, search_title="头文字D Initial D")
    db_session.add(resource)
    await db_session.flush()

    agent = _agent({
        "头文字D": _tv_entity("bangumi:8290", "bangumi", "头文字D"),
    })
    with patch("app.services.metadata_agent.get_agent", return_value=agent):
        await link_franchise_pack(db_session, resource, _report("头文字D"), ch)
    await db_session.flush()

    pack = next(
        c for c in await _collections(db_session)
        if c.external_source == FRANCHISE_PACK_SOURCE
    )
    work = (await db_session.execute(select(TVSeries))).scalars().one()
    assert work.collection_id == pack.id
    assert work.season_number == 1


# ---------------------------------------------------------------------------
# Gap coverage: helper bodies, degraded resolution, empty reports
# ---------------------------------------------------------------------------


async def test_ensure_auto_link_is_idempotent(db_session):
    ch = await _channel(db_session)
    resource = _resource(ch.id)
    series = TVSeries(id=_uuid(), title_cn="作品", content_type="tv")
    db_session.add_all([resource, series])
    await db_session.flush()

    assert await _ensure_auto_link(db_session, resource.id, "series", series.id) is True
    assert await _ensure_auto_link(db_session, resource.id, "series", series.id) is False


async def test_get_or_create_collection_matches_existing_title_en(db_session):
    existing = WorkCollection(
        id=_uuid(), title_cn="头文字D（合集）", title_en="Initial D",
        external_source=FRANCHISE_PACK_SOURCE,
    )
    db_session.add(existing)
    await db_session.flush()

    coll = await _get_or_create_franchise_collection(db_session, "Initial D")
    assert coll.id == existing.id


async def test_resolve_member_local_lookup_failure_still_upserts(db_session):
    agent = _agent({"作品X": _tv_hit("tmdb:100", "作品X")})
    with patch(
        "app.services.metadata_service.find_local_work_for_entity",
        new_callable=AsyncMock, side_effect=RuntimeError("local lookup down"),
    ):
        result = await _resolve_member(db_session, agent, "tmdb", "作品X")
    assert result is not None
    work, attach = result
    assert work.external_id.startswith("tmdb:100")
    assert attach is True


async def test_resolve_member_series_upsert_none_and_exception(db_session):
    agent = _agent({"作品X": _tv_hit("tmdb:100", "作品X")})
    with patch(
        "app.services.metadata_service.create_or_update_series_from_external",
        new_callable=AsyncMock, return_value=None,
    ):
        assert await _resolve_member(db_session, agent, "tmdb", "作品X") is None
    with patch(
        "app.services.metadata_service.create_or_update_series_from_external",
        new_callable=AsyncMock, side_effect=RuntimeError("upsert down"),
    ):
        assert await _resolve_member(db_session, agent, "tmdb", "作品X") is None


async def test_resolve_member_non_tv_movie_content_type_skipped(db_session):
    audio_hit = ResourceMetadata(
        clean_title="声音作品",
        found=True,
        content_type="audio",
        matched_entity={
            "external_id": "wikipedia:1", "external_source": "wikipedia",
            "title_cn": "声音作品",
        },
    )
    agent = _agent({"声音作品": audio_hit})
    assert await _resolve_member(db_session, agent, "tmdb", "声音作品") is None


def test_is_pack_base_name_no_bases_and_different_work():
    assert _is_pack_base_name({"title_cn": "作品A"}, None) is True
    assert _is_pack_base_name({"title_cn": "作品A"}, frozenset({"某大ip"})) is True
    # Same family but a decorated variant → not attachable.
    assert _is_pack_base_name(
        {"title_cn": "Initial D Battle Stage"},
        frozenset({"initial d"}),
    ) is False


async def test_link_franchise_pack_without_member_titles(db_session):
    ch = await _channel(db_session)
    resource = _resource(ch.id)
    db_session.add(resource)
    await db_session.flush()

    await link_franchise_pack(db_session, resource, _report(), ch)
    await db_session.flush()
    assert resource.collection_id is not None
    coll = await db_session.get(WorkCollection, resource.collection_id)
    assert coll is not None and coll.external_source == FRANCHISE_PACK_SOURCE


def test_movie_base_names_and_same_ip():
    a = Movie(
        id=_uuid(), title_cn="头文字D", title_en=None, original_title=None,
        aliases=["Initial D"], content_type="movie",
    )
    b = Movie(
        id=_uuid(), title_cn="头文字D 剧场版", content_type="movie",
    )
    c = Movie(id=_uuid(), title_cn="完全无关的电影", content_type="movie")
    assert "头文字d" in _movie_base_names(a)
    assert "initial d" in _movie_base_names(a)
    assert _same_ip_movies(a, b) is True  # containment
    assert _same_ip_movies(a, c) is False


def test_same_ip_movies_shared_collection():
    a = Movie(id=_uuid(), title_cn="A", collection_id="c1", content_type="movie")
    b = Movie(id=_uuid(), title_cn="B", collection_id="c1", content_type="movie")
    assert _same_ip_movies(a, b) is True


def test_same_ip_movies_equal_base_name():
    a = Movie(id=_uuid(), title_cn="同名电影", content_type="movie")
    b = Movie(id=_uuid(), title_cn="同名电影", content_type="movie")
    assert _same_ip_movies(a, b) is True


def test_enforce_non_franchise_resource_returns_false():
    resource = _resource("c", is_batch=False, batch_scope=None)
    assert enforce_franchise_resource_invariant(resource) is False


async def test_dedupe_less_than_two_movie_ids_returns_zero(db_session):
    ch = await _channel(db_session, metadata_source="bangumi")
    resource = _resource(ch.id)
    db_session.add(resource)
    await db_session.flush()

    assert await dedupe_resource_movies(db_session, resource, channel=ch) == 0


async def test_dedupe_uses_resource_movie_fk(db_session):
    """One movie is only reachable through the resource FK, not a link row."""
    from datetime import date

    ch = await _channel(db_session, metadata_source="bangumi")
    coll = WorkCollection(
        id=_uuid(), title_cn="作品X", external_id=None,
        external_source=FRANCHISE_PACK_SOURCE,
    )
    m1 = Movie(
        id=_uuid(), title_cn="作品X Legend1", external_id="bangumi:1",
        external_source="bangumi", content_type="movie",
        release_date=date(2014, 8, 23), collection_id=coll.id,
    )
    m2 = Movie(
        id=_uuid(), title_cn="作品X Legend1 剧场版", external_id="mal:2",
        external_source="mal", content_type="movie",
        release_date=date(2014, 8, 24), collection_id=coll.id,
    )
    resource = _resource(ch.id, collection_id=coll.id, movie_id=m1.id)
    db_session.add_all([coll, m1, m2, resource])
    await db_session.flush()
    db_session.add(ResourceWorkLink(
        id=_uuid(), resource_id=resource.id, movie_id=m2.id, source="auto",
    ))
    await db_session.commit()

    assert await dedupe_resource_movies(db_session, resource, channel=ch) == 1


async def test_dedupe_unresolvable_movie_ids_returns_zero():
    """Two referenced ids but fewer than two resolvable rows → no-op."""

    class _Scalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return _Scalars(self._rows)

    class _FakeDB:
        async def execute(self, stmt):
            return _Result(["ghost-1", "ghost-2"])

        async def get(self, model, ident):
            return None

    resource = SimpleNamespace(id="res-1", movie_id=None)
    assert await dedupe_resource_movies(_FakeDB(), resource, channel=None) == 0


async def test_dedupe_same_year_different_ip_keeps_both(db_session):
    from datetime import date

    ch = await _channel(db_session, metadata_source="bangumi")
    m1 = Movie(
        id=_uuid(), title_cn="甲电影", external_id="bangumi:1",
        external_source="bangumi", content_type="movie",
        release_date=date(2014, 1, 1),
    )
    m2 = Movie(
        id=_uuid(), title_cn="乙电影", external_id="mal:2",
        external_source="mal", content_type="movie",
        release_date=date(2014, 2, 2),
    )
    resource = _resource(ch.id)
    db_session.add_all([m1, m2, resource])
    await db_session.flush()
    db_session.add_all([
        ResourceWorkLink(id=_uuid(), resource_id=resource.id, movie_id=m1.id, source="auto"),
        ResourceWorkLink(id=_uuid(), resource_id=resource.id, movie_id=m2.id, source="auto"),
    ])
    await db_session.commit()

    assert await dedupe_resource_movies(db_session, resource, channel=ch) == 0
    assert len((await db_session.execute(select(Movie))).scalars().all()) == 2
