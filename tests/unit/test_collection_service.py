"""Tests for collection_service: deterministic TMDB collection linking.

httpx is mocked; ``runtime_config`` values are patched via its ``_overrides``
dict (same pattern as test_metadata_source_io.py).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.collection_service import (
    collection_work_summaries,
    fetch_tmdb_collection_parts,
    filter_untracked_parts,
    link_movie_collection,
    same_collection_family,
    tracked_movie_tmdb_ids,
    try_absorb_same_name_collection,
    try_absorb_shell_collection,
    upsert_collection_from_tmdb,
)


def _uuid() -> str:
    return str(uuid.uuid4())


def _httpx_client_mock(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = payload
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    client_cls = MagicMock(return_value=client)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client_cls


def _tmdb_on(payload: dict):
    return (
        patch.dict(
            "app.services.runtime_config._overrides",
            {"tmdb_api_key": "k", "tmdb_enabled": "true"},
        ),
        patch("httpx.AsyncClient", _httpx_client_mock(payload)),
    )


def _movie(external_id: str | None, collection_id: str | None = None) -> Movie:
    return Movie(
        id=_uuid(),
        title_cn="狮子王",
        external_id=external_id,
        external_source="tmdb",
        content_type="movie",
        collection_id=collection_id,
    )


_PAYLOAD_WITH_COLLECTION = {
    "id": 1327,
    "title": "狮子王",
    "belongs_to_collection": {
        "id": 131295,
        "name": "狮子王（系列）",
        "poster_path": "/coll.jpg",
    },
}

_PAYLOAD_NO_COLLECTION = {
    "id": 1327,
    "title": "狮子王",
    "belongs_to_collection": None,
}


async def test_link_creates_collection_and_sets_fk(db_session):
    movie = _movie("tmdb:1327")
    db_session.add(movie)
    await db_session.flush()

    cfg, http = _tmdb_on(_PAYLOAD_WITH_COLLECTION)
    with cfg, http:
        collection = await link_movie_collection(db_session, movie)

    assert collection is not None
    assert movie.collection_id == collection.id
    assert collection.title_cn == "狮子王（系列）"
    # Raw numeric id + tmdb_collection source — NOT canonicalize_external_id's
    # tmdb:<digits> form (that would collide with the movie id space).
    assert collection.external_source == "tmdb_collection"
    assert collection.external_id == "131295"
    assert collection.poster_url == "https://image.tmdb.org/t/p/w500/coll.jpg"
    # TMDB movie details carry no overview/en-title — stay NULL.
    assert collection.title_en is None
    assert collection.description is None


async def test_link_upsert_is_idempotent(db_session):
    """Two movies in the same TMDB collection share one WorkCollection row."""
    m1 = _movie("tmdb:1327")
    m2 = _movie("tmdb:8587")
    m2.title_cn = "狮子王2"
    db_session.add_all([m1, m2])
    await db_session.flush()

    cfg, http = _tmdb_on(_PAYLOAD_WITH_COLLECTION)
    with cfg, http:
        c1 = await link_movie_collection(db_session, m1)
        c2 = await link_movie_collection(db_session, m2)

    assert c1 is not None and c2 is not None
    assert c1.id == c2.id
    assert m1.collection_id == m2.collection_id == c1.id
    rows = (await db_session.execute(select(WorkCollection))).scalars().all()
    assert len(rows) == 1


async def test_no_collection_is_noop(db_session):
    movie = _movie("tmdb:1327")
    db_session.add(movie)
    await db_session.flush()

    cfg, http = _tmdb_on(_PAYLOAD_NO_COLLECTION)
    with cfg, http:
        assert await link_movie_collection(db_session, movie) is None
    assert movie.collection_id is None


async def test_non_tmdb_external_id_is_noop(db_session):
    movie = _movie("imdb:tt0110357")
    db_session.add(movie)
    await db_session.flush()

    # No httpx mock needed — must not even attempt a request.
    with patch("httpx.AsyncClient") as client_cls:
        assert await link_movie_collection(db_session, movie) is None
    client_cls.assert_not_called()
    assert movie.collection_id is None


async def test_already_linked_is_noop(db_session):
    coll = WorkCollection(id=_uuid(), title_cn="已有系列")
    movie = _movie("tmdb:1327", collection_id=coll.id)
    db_session.add_all([coll, movie])
    await db_session.flush()

    with patch("httpx.AsyncClient") as client_cls:
        assert await link_movie_collection(db_session, movie) is None
    client_cls.assert_not_called()


async def test_tmdb_disabled_is_noop(db_session):
    movie = _movie("tmdb:1327")
    db_session.add(movie)
    await db_session.flush()

    with (
        patch.dict(
            "app.services.runtime_config._overrides",
            {"tmdb_api_key": "", "tmdb_enabled": "false"},
        ),
        patch("httpx.AsyncClient") as client_cls,
    ):
        assert await link_movie_collection(db_session, movie) is None
    client_cls.assert_not_called()
    assert movie.collection_id is None


async def test_collection_work_summaries(db_session):
    coll = WorkCollection(id=_uuid(), title_cn="狮子王（系列）")
    m1 = Movie(id=_uuid(), title_cn="狮子王", content_type="movie", collection_id=coll.id)
    s1 = TVSeries(id=_uuid(), title_cn="狮子王 动画剧", content_type="tv", collection_id=coll.id)
    other = Movie(id=_uuid(), title_cn="无关电影", content_type="movie")
    db_session.add_all([coll, m1, s1, other])
    await db_session.flush()

    works = await collection_work_summaries(db_session, coll.id)
    assert {w["id"] for w in works} == {m1.id, s1.id}
    assert {w["type"] for w in works} == {"series", "movie"}

    siblings = await collection_work_summaries(
        db_session, coll.id, exclude=("movie", m1.id)
    )
    assert [w["id"] for w in siblings] == [s1.id]


# ---------------------------------------------------------------------------
# TMDB collection parts (on-demand, never persisted)
# ---------------------------------------------------------------------------

_PARTS_PAYLOAD = {
    "id": 131295,
    "name": "狮子王（系列）",
    "parts": [
        {
            "id": 1327,
            "title": "狮子王",
            "release_date": "1994-06-23",
            "poster_path": "/p1.jpg",
        },
        {
            "id": 999999,
            "title": "狮子王 2026",
            "release_date": "2026-01-01",
            "poster_path": None,
        },
        {"id": None, "title": "no id — skipped"},
    ],
}


def _tmdb_collection(external_id: str | None = "131295") -> WorkCollection:
    return WorkCollection(
        id=_uuid(),
        title_cn="狮子王（系列）",
        external_source="tmdb_collection",
        external_id=external_id,
    )


async def test_fetch_parts_success_and_parsing():
    from app.services import collection_service as cs

    cs._parts_cache.clear()
    collection = _tmdb_collection()
    cfg, http = _tmdb_on(_PARTS_PAYLOAD)
    with cfg, http:
        parts = await fetch_tmdb_collection_parts(collection)

    assert [p["tmdb_id"] for p in parts] == ["1327", "999999"]
    assert parts[0]["title"] == "狮子王"
    assert parts[0]["year"] == 1994
    assert parts[0]["poster_url"] == "https://image.tmdb.org/t/p/w500/p1.jpg"
    assert parts[1]["poster_url"] is None


async def test_fetch_parts_non_tmdb_collection_is_none():
    collection = _tmdb_collection()
    collection.external_source = "wikidata"
    with patch("httpx.AsyncClient") as client_cls:
        assert await fetch_tmdb_collection_parts(collection) is None
    client_cls.assert_not_called()


async def test_fetch_parts_tmdb_disabled_is_none():
    collection = _tmdb_collection()
    with (
        patch.dict(
            "app.services.runtime_config._overrides",
            {"tmdb_api_key": "", "tmdb_enabled": "false"},
        ),
        patch("httpx.AsyncClient") as client_cls,
    ):
        assert await fetch_tmdb_collection_parts(collection) is None
    client_cls.assert_not_called()


async def test_fetch_parts_cache_hit_skips_second_request():
    from app.services import collection_service as cs

    cs._parts_cache.clear()
    collection = _tmdb_collection("131296")
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = _PARTS_PAYLOAD
    client = MagicMock()
    client.get = AsyncMock(return_value=resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    with (
        patch.dict(
            "app.services.runtime_config._overrides",
            {"tmdb_api_key": "k", "tmdb_enabled": "true"},
        ),
        patch("httpx.AsyncClient", MagicMock(return_value=client)),
    ):
        first = await fetch_tmdb_collection_parts(collection)
        second = await fetch_tmdb_collection_parts(collection)
    assert first == second
    assert client.get.await_count == 1


def test_filter_untracked_parts():
    parts = [
        {"tmdb_id": "1327", "title": "tracked"},
        {"tmdb_id": "999999", "title": "untracked"},
    ]
    assert filter_untracked_parts(parts, {"1327"}) == [{"tmdb_id": "999999", "title": "untracked"}]
    assert filter_untracked_parts(parts, {"1327", "999999"}) == []
    assert filter_untracked_parts([], set()) == []


async def test_tracked_movie_tmdb_ids(db_session):
    db_session.add_all([
        _movie("tmdb:1327"),
        _movie("tmdb:8587"),
        _movie("imdb:tt0110357"),
        _movie(None),
    ])
    await db_session.flush()
    assert await tracked_movie_tmdb_ids(db_session) == {"1327", "8587"}


# ---------------------------------------------------------------------------
# Error paths + collection absorption
# ---------------------------------------------------------------------------


def _httpx_error_mock(exc: Exception = RuntimeError("boom")) -> MagicMock:
    client = MagicMock()
    client.get = AsyncMock(side_effect=exc)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=client)


async def test_tmdb_request_failure_is_noop(db_session):
    movie = _movie("tmdb:1327")
    db_session.add(movie)
    await db_session.flush()

    with (
        patch.dict(
            "app.services.runtime_config._overrides",
            {"tmdb_api_key": "k", "tmdb_enabled": "true"},
        ),
        patch("httpx.AsyncClient", _httpx_error_mock()),
    ):
        assert await link_movie_collection(db_session, movie) is None
    assert movie.collection_id is None


async def test_upsert_updates_existing_name_and_poster(db_session):
    existing = WorkCollection(
        id=_uuid(), title_cn="旧名", external_source="tmdb_collection",
        external_id="131295",
    )
    db_session.add(existing)
    await db_session.flush()

    coll = await upsert_collection_from_tmdb(
        db_session,
        {"id": 131295, "name": "新名", "poster_path": "/new.jpg"},
    )
    assert coll.id == existing.id
    assert coll.title_cn == "新名"
    assert coll.poster_url == "https://image.tmdb.org/t/p/w500/new.jpg"


async def test_try_absorb_shell_collection_noop_cases(db_session):
    coll = WorkCollection(id=_uuid(), title_cn="目标")
    work = TVSeries(id=_uuid(), title_cn="作品", content_type="tv")
    db_session.add(coll)
    await db_session.flush()
    assert await try_absorb_shell_collection(db_session, coll, work) is False

    work.collection_id = coll.id
    assert await try_absorb_shell_collection(db_session, coll, work) is False

    # A non-series_group source is not an absorbable shell.
    non_shell = WorkCollection(
        id=_uuid(), title_cn="非壳", external_source="tmdb_collection",
    )
    work.collection_id = non_shell.id
    db_session.add(non_shell)
    await db_session.flush()
    assert await try_absorb_shell_collection(db_session, coll, work) is False

    # A multi-member series_group shell is not single-member → not absorbable.
    multi = WorkCollection(
        id=_uuid(), title_cn="多成员壳", external_source="series_group",
    )
    m1 = TVSeries(
        id=_uuid(), title_cn="成员1", content_type="tv",
        season_number=1, collection_id=multi.id,
    )
    m2 = TVSeries(
        id=_uuid(), title_cn="成员2", content_type="tv",
        season_number=2, collection_id=multi.id,
    )
    work.collection_id = multi.id
    db_session.add_all([multi, m1, m2])
    await db_session.flush()
    assert await try_absorb_shell_collection(db_session, coll, work) is False


def test_same_collection_family_equality_containment_and_mismatch():
    a = WorkCollection(id=_uuid(), title_cn="头文字D")
    b = WorkCollection(id=_uuid(), title_cn="头文字D")
    c = WorkCollection(id=_uuid(), title_cn="完全不同的作品")
    # Base-name containment in either direction (two-char floor).
    d = WorkCollection(id=_uuid(), title_cn="头文字D Initial D")
    assert same_collection_family(a, b) is True
    assert same_collection_family(a, d) is True
    assert same_collection_family(a, c) is False


async def test_try_absorb_shell_collection_success(db_session):
    from app.models.channel import Channel
    from app.models.file_resource import FileResource

    ch = Channel(
        id=_uuid(), name="ch", type="rss_feed", url="https://example.com/rss",
        field_mapping={"title": "title"}, status="active",
    )
    coll = WorkCollection(
        id=_uuid(), title_cn="目标合集", external_source="franchise_pack",
    )
    shell = WorkCollection(
        id=_uuid(), title_cn="壳", external_source="series_group",
        aliases=["壳别名"],
    )
    work = TVSeries(
        id=_uuid(), title_cn="作品", content_type="tv",
        season_number=1, collection_id=shell.id,
    )
    parked = FileResource(
        id=_uuid(), channel_id=ch.id, guid=_uuid(), title_raw="x",
        torrent_url="magnet:?xt=urn:btih:abc", collection_id=shell.id,
    )
    db_session.add_all([ch, coll, shell, work, parked])
    await db_session.flush()

    assert await try_absorb_shell_collection(db_session, coll, work) is True
    await db_session.flush()
    await db_session.refresh(work)
    await db_session.refresh(parked)
    assert work.collection_id == coll.id
    assert parked.collection_id == coll.id
    assert coll.aliases == ["壳别名"]
    assert await db_session.get(WorkCollection, shell.id) is None


async def test_try_absorb_same_name_collection_noop_cases(db_session):
    coll = WorkCollection(
        id=_uuid(), title_cn="作品X", external_source="franchise_pack",
    )
    work = TVSeries(id=_uuid(), title_cn="作品X", content_type="tv")
    db_session.add(coll)
    await db_session.flush()
    # No collection at all → not absorbable.
    assert await try_absorb_same_name_collection(db_session, coll, work) is False

    # Manual edits on the source block absorption.
    source = WorkCollection(
        id=_uuid(), title_cn="作品X", external_source="series_group",
        manually_edited_fields=["title_cn"],
    )
    work.collection_id = source.id
    db_session.add(source)
    await db_session.flush()
    assert await try_absorb_same_name_collection(db_session, coll, work) is False
    assert await db_session.get(WorkCollection, source.id) is not None

    # Non-auto source is not absorbable.
    source2 = WorkCollection(id=_uuid(), title_cn="作品X", external_source="tmdb_collection")
    work.collection_id = source2.id
    db_session.add(source2)
    await db_session.flush()
    assert await try_absorb_same_name_collection(db_session, coll, work) is False

    # Auto source but a different name family → not absorbable.
    source3 = WorkCollection(id=_uuid(), title_cn="无关作品", external_source="series_group")
    work.collection_id = source3.id
    db_session.add(source3)
    await db_session.flush()
    assert await try_absorb_same_name_collection(db_session, coll, work) is False


async def test_try_absorb_same_name_collection_merges_members_and_aliases(db_session):
    coll = WorkCollection(
        id=_uuid(), title_cn="作品X", external_source="franchise_pack",
        aliases=[],
    )
    source = WorkCollection(
        id=_uuid(), title_cn="作品X", external_source="series_group",
        aliases=["别名X"],
    )
    member = TVSeries(
        id=_uuid(), title_cn="作品X", content_type="tv",
        season_number=1, collection_id=source.id,
    )
    movie = Movie(
        id=_uuid(), title_cn="作品X 剧场版", content_type="movie",
        collection_id=source.id,
    )
    db_session.add_all([coll, source, member, movie])
    await db_session.flush()

    assert await try_absorb_same_name_collection(db_session, coll, member) is True
    await db_session.flush()
    await db_session.refresh(member)
    await db_session.refresh(movie)
    assert member.collection_id == coll.id
    assert movie.collection_id == coll.id
    assert coll.aliases == ["别名X"]
    assert await db_session.get(WorkCollection, source.id) is None


async def test_try_absorb_same_name_collection_keeps_colliding_season(db_session):
    coll = WorkCollection(
        id=_uuid(), title_cn="作品X", external_source="franchise_pack",
    )
    occupied = TVSeries(
        id=_uuid(), title_cn="作品X S1", content_type="tv",
        season_number=1, collection_id=coll.id,
    )
    source = WorkCollection(
        id=_uuid(), title_cn="作品X", external_source="series_group",
    )
    clash = TVSeries(
        id=_uuid(), title_cn="作品X S1 dup", content_type="tv",
        season_number=1, collection_id=source.id,
    )
    db_session.add_all([coll, occupied, source, clash])
    await db_session.flush()

    assert await try_absorb_same_name_collection(db_session, coll, clash) is False
    await db_session.flush()
    # The source row is kept for the colliding member.
    assert await db_session.get(WorkCollection, source.id) is not None
    await db_session.refresh(clash)
    assert clash.collection_id == source.id


async def test_collection_work_summaries_excludes_series(db_session):
    coll = WorkCollection(id=_uuid(), title_cn="合集")
    s1 = TVSeries(id=_uuid(), title_cn="剧1", content_type="tv", collection_id=coll.id)
    s2 = TVSeries(id=_uuid(), title_cn="剧2", content_type="tv", collection_id=coll.id)
    db_session.add_all([coll, s1, s2])
    await db_session.flush()

    siblings = await collection_work_summaries(db_session, coll.id, exclude=("series", s1.id))
    assert {w["id"] for w in siblings} == {s2.id}


async def test_fetch_parts_missing_external_id_is_none(db_session):
    collection = _tmdb_collection(external_id=None)
    with patch("httpx.AsyncClient") as client_cls:
        assert await fetch_tmdb_collection_parts(collection) is None
    client_cls.assert_not_called()


async def test_fetch_parts_request_failure_is_none(db_session):
    from app.services import collection_service as cs

    cs._parts_cache.clear()
    collection = _tmdb_collection("777")
    with (
        patch.dict(
            "app.services.runtime_config._overrides",
            {"tmdb_api_key": "k", "tmdb_enabled": "true"},
        ),
        patch("httpx.AsyncClient", _httpx_error_mock()),
    ):
        assert await fetch_tmdb_collection_parts(collection) is None
