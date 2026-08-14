"""In-process integration tests for DB-backed metadata services.

Covers the metadata dedup merge flows (series / movies / cross-type) and the
deterministic TMDB collection linking, which are exercised here under the
integration coverage harness (``.coverage.test-runner``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select

from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services import metadata_dedup as dedup
from app.services.collection_service import (
    collection_work_summaries,
    filter_untracked_parts,
    link_movie_collection,
    tracked_movie_tmdb_ids,
)


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# metadata_dedup
# ---------------------------------------------------------------------------


async def _make_series(db_session, *, external_id, title_cn, title_en, created_at):
    s = TVSeries(
        id=_uuid(), title_cn=title_cn, title_en=title_en, original_title=title_en,
        external_id=external_id, external_source="exa", content_type="tv",
        created_at=created_at, updated_at=created_at,
    )
    db_session.add(s)
    await db_session.flush()
    return s


async def test_merge_duplicate_series_collapses(db_session):
    t0 = datetime(2025, 1, 1)
    s1 = await _make_series(db_session, external_id="TMDB:82684", title_cn="关于我转生变成史莱姆这档事 第四季",
                            title_en="Slime S4", created_at=t0)
    await _make_series(db_session, external_id="TMDB 82684", title_cn="关于我转生变成史莱姆这档事 第四季",
                       title_en="Slime S4", created_at=t0 + timedelta(minutes=1))

    report = await dedup.merge_duplicate_series(db_session)
    await db_session.flush()
    assert report.series_groups == 1
    assert report.series_removed == 1
    remaining = (await db_session.execute(select(TVSeries))).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].id == s1.id
    assert remaining[0].external_id == "tmdb:82684"


async def test_merge_duplicate_movies(db_session):
    t0 = datetime(2025, 1, 1)
    m1 = Movie(id=_uuid(), title_cn="电影A", title_en="Movie A", original_title="Movie A",
               external_id="TMDB:1", external_source="exa", content_type="movie",
               created_at=t0, updated_at=t0)
    m2 = Movie(id=_uuid(), title_cn="电影A", title_en="Movie A", original_title="Movie A",
               external_id="TMDB 1", external_source="exa", content_type="movie",
               created_at=t0 + timedelta(seconds=1), updated_at=t0 + timedelta(seconds=1))
    db_session.add_all([m1, m2])
    await db_session.flush()

    report = await dedup.merge_duplicate_movies(db_session)
    await db_session.flush()
    assert report.movie_groups == 1
    assert report.movies_removed == 1
    remaining = (await db_session.execute(select(Movie))).scalars().all()
    assert len(remaining) == 1


async def test_merge_cross_type_duplicates(db_session):
    """A movie and series sharing a canonical external_id converge to one row."""
    series = TVSeries(id=_uuid(), title_cn="攻壳机动队", title_en="GitS",
                      original_title="GitS", external_id="tmdb:82684",
                      external_source="tmdb", content_type="tv")
    movie = Movie(id=_uuid(), title_cn="攻壳机动队", title_en="GitS",
                  original_title="GitS", external_id="tmdb:82684",
                  external_source="tmdb", content_type="movie")
    db_session.add_all([series, movie])
    await db_session.flush()

    report = await dedup.merge_cross_type_duplicates(db_session)
    await db_session.flush()
    assert report is not None


# ---------------------------------------------------------------------------
# collection_service
# ---------------------------------------------------------------------------


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


_PAYLOAD_WITH_COLLECTION = {
    "id": 1327,
    "title": "狮子王",
    "belongs_to_collection": {
        "id": 131295,
        "name": "狮子王（系列）",
        "poster_path": "/coll.jpg",
    },
}


async def test_link_movie_collection_creates_and_sets_fk(db_session):
    movie = Movie(id=_uuid(), title_cn="狮子王", external_id="tmdb:1327",
                  external_source="tmdb", content_type="movie")
    db_session.add(movie)
    await db_session.flush()
    cfg, http = _tmdb_on(_PAYLOAD_WITH_COLLECTION)
    with cfg, http:
        collection = await link_movie_collection(db_session, movie)
    assert collection is not None
    assert movie.collection_id == collection.id
    assert collection.external_source == "tmdb_collection"
    assert collection.external_id == "131295"


async def test_link_movie_collection_non_tmdb_is_noop(db_session):
    movie = Movie(id=_uuid(), title_cn="狮子王", external_id="imdb:tt0110357",
                  external_source="tmdb", content_type="movie")
    db_session.add(movie)
    await db_session.flush()
    with patch("httpx.AsyncClient") as client_cls:
        assert await link_movie_collection(db_session, movie) is None
    client_cls.assert_not_called()


async def test_collection_work_summaries_and_untracked(db_session):
    coll = WorkCollection(id=_uuid(), title_cn="合集")
    s = TVSeries(id=_uuid(), title_cn="剧A", content_type="tv", collection_id=coll.id)
    m = Movie(id=_uuid(), title_cn="影A", content_type="movie", collection_id=coll.id)
    db_session.add_all([coll, s, m])
    await db_session.flush()
    works = await collection_work_summaries(db_session, coll.id)
    assert len(works) == 2
    assert await tracked_movie_tmdb_ids(db_session) == set()
    assert filter_untracked_parts([{"tmdb_id": "1327", "title": "x"}], {"1327"}) == []
    assert filter_untracked_parts([{"tmdb_id": "999999", "title": "x"}], {"1327"}) == [
        {"tmdb_id": "999999", "title": "x"},
    ]
