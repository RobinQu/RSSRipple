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
    tracked_movie_tmdb_ids,
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
