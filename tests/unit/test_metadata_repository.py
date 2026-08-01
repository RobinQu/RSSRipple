"""Tests for metadata_repository: cache generations (F1), cross-table
movie/series dispatch guard (F2), and cache invalidation on manual link (F3).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.models.metadata_cache import METADATA_CACHE_GENERATION, MetadataCache
from app.models.movie import Movie
from app.models.series import TVSeries
from app.services.metadata_repository import _apply_to_resource, _get_cache, _set_cache
from app.services.metadata_resource_meta import ResourceMetadata
from app.services.metadata_service import invalidate_metadata_cache_for_external_id


def _uuid() -> str:
    return str(uuid.uuid4())


def _meta(**kw) -> ResourceMetadata:
    base = dict(clean_title="Show", found=True, content_type="tv")
    base.update(kw)
    return ResourceMetadata(**base)


# ---------------------------------------------------------------------------
# F1: cache logic generations
# ---------------------------------------------------------------------------


async def test_cache_roundtrip_writes_current_generation(db_session):
    meta = _meta()
    await _set_cache("Some Raw Title", "wikipedia", meta, db_session)

    row = (await db_session.execute(select(MetadataCache))).scalar_one()
    assert row.generation == METADATA_CACHE_GENERATION

    cached = await _get_cache("Some Raw Title", "wikipedia", db_session)
    assert cached is not None
    assert cached.found is True


async def test_stale_generation_is_discarded_and_deleted(db_session):
    meta = _meta(content_type="movie")
    await _set_cache("Old Verdict", "wikipedia", meta, db_session)
    row = (await db_session.execute(select(MetadataCache))).scalar_one()
    row.generation = METADATA_CACHE_GENERATION - 1  # simulate pre-fix logic
    await db_session.flush()

    cached = await _get_cache("Old Verdict", "wikipedia", db_session)
    assert cached is None
    # Lazily deleted, not served again.
    assert (await db_session.execute(select(MetadataCache))).scalars().all() == []


# ---------------------------------------------------------------------------
# F2: cross-table dispatch guard
# ---------------------------------------------------------------------------


def _resource() -> SimpleNamespace:
    return SimpleNamespace(
        search_title=None, episode=16, season=1, is_batch=False,
        episode_start=None, episode_end=None, title_cn=None, title_en=None,
        subtitle_langs=None, episode_confidence=None,
        series_id=None, movie_id=None, audio_work_id=None,
        metadata_matched_at=None,
    )


async def test_movie_verdict_flips_to_existing_series(db_session):
    series = TVSeries(
        id=_uuid(), title_cn="剧集", external_id="wikipedia:5139056",
        external_source="wikipedia", content_type="tv",
    )
    db_session.add(series)
    await db_session.flush()

    meta = _meta(
        content_type="movie",
        matched_entity={
            "external_id": "wikipedia:5139056",
            "external_source": "wikipedia",
            "title_cn": "剧集",
        },
    )
    resource = _resource()
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)

    # Linked to the existing series; no Movie row created.
    assert resource.series_id == series.id
    assert resource.movie_id is None
    assert (await db_session.execute(select(Movie))).scalars().all() == []


async def test_tv_verdict_flips_to_existing_movie(db_session):
    movie = Movie(
        id=_uuid(), title_en="Real Film", external_id="tmdb:42",
        external_source="tmdb", content_type="movie",
    )
    db_session.add(movie)
    await db_session.flush()

    meta = _meta(
        content_type="tv",
        matched_entity={
            "external_id": "tmdb:42",
            "external_source": "tmdb",
            "title_en": "Real Film",
        },
    )
    resource = _resource()
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)

    assert resource.movie_id == movie.id
    assert resource.series_id is None
    assert (await db_session.execute(select(TVSeries))).scalars().all() == []


async def test_movie_verdict_creates_movie_when_no_cross_type_row(db_session):
    meta = _meta(
        content_type="movie",
        matched_entity={
            "external_id": "tmdb:99",
            "external_source": "tmdb",
            "title_en": "Fresh Film",
        },
    )
    resource = _resource()
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)

    assert resource.movie_id is not None
    assert resource.series_id is None


# ---------------------------------------------------------------------------
# F3: cache invalidation on manual link
# ---------------------------------------------------------------------------


async def test_invalidate_cache_for_external_id(db_session):
    for title, ext in (("Title A", "wikipedia:1"), ("Title B", "wikipedia:2")):
        db_session.add(MetadataCache(
            id=_uuid(), title=title, source="metadata_agent:wikipedia",
            content_type="movie",
            metadata_json={"found": True, "content_type": "movie",
                           "matched_entity": {"external_id": ext}},
            generation=METADATA_CACHE_GENERATION,
        ))
    db_session.add(MetadataCache(
        id=_uuid(), title="No Entity", source="metadata_agent:wikipedia",
        content_type=None, metadata_json={"found": False},
        generation=METADATA_CACHE_GENERATION,
    ))
    await db_session.flush()

    removed = await invalidate_metadata_cache_for_external_id(db_session, "wikipedia:1")
    assert removed == 1

    remaining = (await db_session.execute(select(MetadataCache))).scalars().all()
    assert {r.title for r in remaining} == {"Title B", "No Entity"}


# ---------------------------------------------------------------------------
# Cache source namespacing + misses
# ---------------------------------------------------------------------------


async def test_cache_is_namespaced_by_source(db_session):
    await _set_cache("Same Title", "wikipedia", _meta(), db_session)
    # A different source must not serve the wikipedia verdict.
    assert await _get_cache("Same Title", "exa", db_session) is None
    assert await _get_cache("Same Title", "wikipedia", db_session) is not None


async def test_set_cache_upserts_replacing_stale_row(db_session):
    await _set_cache("T", "wikipedia", _meta(content_type="movie"), db_session)
    await _set_cache("T", "wikipedia", _meta(content_type="tv"), db_session)
    rows = (await db_session.execute(select(MetadataCache))).scalars().all()
    assert len(rows) == 1
    assert rows[0].content_type == "tv"


# ---------------------------------------------------------------------------
# _apply_to_resource: field write-back, batch, reconciliation, audio
# ---------------------------------------------------------------------------


async def test_not_found_only_sets_search_title(db_session):
    meta = _meta(found=False, clean_title="Cleaned Title")
    resource = _resource()
    await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)
    assert resource.search_title == "Cleaned Title"
    assert resource.series_id is None
    assert resource.metadata_matched_at is None


async def test_episode_season_and_titles_filled_from_meta(db_session):
    meta = _meta(
        episode=5, season=2, title_cn="标题", title_en="Title",
        subtitle_langs=["zh-CN", "ja"],
        matched_entity={
            "external_id": "tmdb:11", "external_source": "tmdb",
            "title_cn": "标题",
            "seasons": [{"season_number": 2, "episode_count": 12}],
        },
    )
    resource = _resource()
    resource.episode = None
    resource.season = None
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)
    assert resource.episode == 5
    assert resource.season == 2
    assert resource.title_cn == "标题"
    assert resource.title_en == "Title"
    assert resource.subtitle_langs == ["zh-CN", "ja"]


async def test_batch_flags_clear_single_episode(db_session):
    meta = _meta(
        is_batch=True, episode_start=1, episode_end=12,
        matched_entity={
            "external_id": "tmdb:12", "external_source": "tmdb", "title_cn": "剧集",
        },
    )
    resource = _resource()  # episode=16 set by the factory
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)
    assert resource.is_batch is True
    assert resource.episode is None  # batches must not carry a stray episode
    assert resource.episode_start == 1
    assert resource.episode_end == 12
    # No episode reconciliation for batches
    assert resource.episode_confidence is None


async def test_episode_reconciliation_absolute_to_per_season(db_session):
    meta = _meta(
        matched_entity={
            "external_id": "tmdb:13", "external_source": "tmdb", "title_cn": "剧集",
            "seasons": [
                {"season_number": 1, "episode_count": 12},
                {"season_number": 2, "episode_count": 12},
            ],
        },
    )
    resource = _resource()  # episode=16, season=1 -> season 1 has no prior seasons
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)
    # 16 > 12+2 with no prior-season total -> ambiguous
    assert resource.episode_confidence == "ambiguous"
    assert resource.episode == 16

    # Season 2, absolute 20 -> 20 - 12 = 8 within range -> reconciled
    resource2 = _resource()
    resource2.episode = 20
    resource2.season = 2
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await _apply_to_resource(meta, resource2, SimpleNamespace(id=_uuid()), db_session)
    assert resource2.episode == 8
    assert resource2.absolute_episode == 20
    assert resource2.episode_confidence == "reconciled"


async def test_episode_confidence_raw_when_no_seasons_map(db_session):
    meta = _meta(
        matched_entity={
            "external_id": "tmdb:14", "external_source": "tmdb", "title_cn": "剧集",
        },
    )
    resource = _resource()
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)
    assert resource.episode_confidence == "raw"


async def test_tv_verdict_creates_series_when_no_conflict(db_session):
    meta = _meta(
        matched_entity={
            "external_id": "tmdb:15", "external_source": "tmdb", "title_cn": "新剧集",
        },
    )
    resource = _resource()
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)
    assert resource.series_id is not None
    assert resource.movie_id is None
    assert resource.audio_work_id is None
    assert resource.metadata_matched_at is not None
    series = (await db_session.execute(select(TVSeries))).scalar_one()
    assert series.id == resource.series_id


async def test_audio_verdict_links_audio_work(db_session):
    from app.models.audio_work import AudioWork

    meta = _meta(
        content_type="asmr",
        matched_entity={
            "external_id": "wikipedia:77", "external_source": "wikipedia",
            "title_cn": "音声作品",
        },
    )
    resource = _resource()
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)
    aw = (await db_session.execute(select(AudioWork))).scalar_one()
    assert resource.audio_work_id == aw.id
    assert resource.series_id is None
    assert resource.movie_id is None
