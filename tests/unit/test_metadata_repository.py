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
        subtitle_group=None, subtitle_langs=None,
        episode_confidence=None, batch_scope=None,
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


async def test_movie_verdict_repairs_misfiled_movie_series(db_session):
    """A TVSeries row that itself says movie is migrated, not preserved."""
    series = TVSeries(
        id=_uuid(), title_cn="异形基地", title_en="Body Snatchers",
        external_id="tmdb:4722", external_source="tmdb", content_type="movie",
    )
    db_session.add(series)
    await db_session.flush()

    meta = _meta(
        content_type="movie",
        matched_entity={
            "external_id": "tmdb:4722", "external_source": "tmdb",
            "title_cn": "异形基地", "title_en": "Body Snatchers",
            "release_date": "1993-01-15",
        },
    )
    resource = _resource()
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)

    movies = (await db_session.execute(select(Movie))).scalars().all()
    assert len(movies) == 1
    assert movies[0].external_id == "tmdb:4722"
    assert resource.movie_id == movies[0].id
    assert resource.series_id is None
    assert await db_session.get(TVSeries, series.id) is None


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
    # jina is a retired alias normalized to wikipedia; use a distinct current
    # primary source to verify cache isolation.
    assert await _get_cache("Same Title", "tmdb", db_session) is None
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


async def test_not_found_does_not_overwrite_parsed_search_title(db_session):
    meta = _meta(found=False, clean_title="Cleaned Title")
    resource = _resource()
    resource.search_title = "Parser Title"
    await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)
    assert resource.search_title == "Parser Title"
    assert resource.series_id is None
    assert resource.metadata_matched_at is None


async def test_not_found_does_not_fill_search_title_from_failed_verdict(db_session):
    meta = _meta(found=False, clean_title="[Group] Noisy S01E03 1080p")
    resource = _resource()
    await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)
    assert resource.search_title is None


async def test_not_found_fills_missing_subtitle_group_without_overwriting(db_session):
    meta = _meta(found=False, subtitle_group="  喵萌奶茶屋  ")
    resource = _resource()
    await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)
    assert resource.subtitle_group == "喵萌奶茶屋"

    resource.subtitle_group = "人工修订组"
    await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)
    assert resource.subtitle_group == "人工修订组"


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


async def test_all_release_fields_are_written_back(db_session):
    meta = _meta(
        resolution="1080p", source="WEB-DL", video_codec="HEVC",
        audio_codec="AAC", subtitle_type="内封", subtitle_group="VARYG",
        subtitle_langs=[], container="mkv",
    )
    resource = _resource()
    await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)
    assert resource.resolution == "1080p"
    assert resource.source == "WEB-DL"
    assert resource.video_codec == "HEVC"
    assert resource.audio_codec == "AAC"
    assert resource.subtitle_type == "内封"
    assert resource.subtitle_group == "VARYG"
    assert resource.subtitle_langs == []
    assert resource.container == "mkv"


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


async def test_batch_scope_defaults_to_season(db_session):
    """LLM is_batch without a scope → single-season pack default."""
    meta = _meta(
        is_batch=True, episode_start=1, episode_end=12,
        matched_entity={
            "external_id": "tmdb:601", "external_source": "tmdb", "title_cn": "剧集",
        },
    )
    resource = _resource()
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)
    assert resource.is_batch is True
    assert resource.batch_scope == "season"


async def test_batch_scope_explicit_multi_season(db_session):
    """An explicit LLM scope lands on the resource."""
    meta = _meta(
        is_batch=True, batch_scope="multi_season",
        matched_entity={
            "external_id": "tmdb:602", "external_source": "tmdb", "title_cn": "剧集",
        },
    )
    resource = _resource()
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)
    assert resource.batch_scope == "multi_season"


async def test_batch_scope_not_downgraded_from_torrent_analysis(db_session):
    """Torrent analysis may run ahead of the LLM — a wider scope it set
    (multi_season/franchise) must survive the LLM's default."""
    meta = _meta(
        is_batch=True,  # no batch_scope from the LLM
        matched_entity={
            "external_id": "tmdb:603", "external_source": "tmdb", "title_cn": "剧集",
        },
    )
    resource = _resource()
    resource.batch_scope = "multi_season"
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)
    assert resource.batch_scope == "multi_season"


async def test_franchise_metadata_match_does_not_restore_flat_work_fk(db_session):
    meta = _meta(
        matched_entity={
            "external_id": "tmdb:700", "external_source": "tmdb",
            "title_cn": "福星小子",
        },
    )
    resource = _resource()
    resource.is_batch = True
    resource.batch_scope = "franchise"
    await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)
    assert resource.series_id is None
    assert resource.movie_id is None
    assert resource.audio_work_id is None
    assert resource.metadata_matched_at is not None
    assert (await db_session.execute(select(TVSeries))).scalars().all() == []


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


async def test_audio_verdict_injects_content_type_from_meta(db_session):
    """The LLM verdict's content_type lives on the ResourceMetadata, never in
    matched_entity — the upsert must not fall back to "other"."""
    from app.models.audio_work import AudioWork

    meta = _meta(
        content_type="drama_cd",
        matched_entity={
            "external_id": "wikipedia:78", "external_source": "wikipedia",
            "title_cn": "广播剧",
        },
    )
    resource = _resource()
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)
    aw = (await db_session.execute(select(AudioWork))).scalar_one()
    assert aw.content_type == "drama_cd"


async def test_audio_verdict_titleless_entity_falls_back_to_meta_title(db_session):
    """A matched_entity with no title at all borrows the meta-level title
    instead of inserting a shell AudioWork."""
    from app.models.audio_work import AudioWork

    meta = _meta(
        content_type="music", title_cn="专辑名",
        matched_entity={"external_id": "wikipedia:79", "external_source": "wikipedia"},
    )
    resource = _resource()
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)
    aw = (await db_session.execute(select(AudioWork))).scalar_one()
    assert aw.title_cn == "专辑名"
    assert aw.content_type == "music"
    assert resource.audio_work_id == aw.id


async def test_audio_verdict_titleless_entity_falls_back_to_clean_title(db_session):
    from app.models.audio_work import AudioWork

    meta = _meta(
        content_type="radio", clean_title="Cleaned Radio Show",
        matched_entity={"external_id": "wikipedia:80"},
    )
    resource = _resource()
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)
    aw = (await db_session.execute(select(AudioWork))).scalar_one()
    assert aw.title_cn == "Cleaned Radio Show"


async def test_audio_verdict_without_any_title_creates_no_shell(db_session, caplog):
    """No title anywhere (even clean_title empty) → no AudioWork row, the
    resource stays unmatched, and a warning is logged."""
    import logging

    from app.models.audio_work import AudioWork

    meta = _meta(
        content_type="other", clean_title="",
        matched_entity={"external_id": "wikipedia:81"},
    )
    resource = _resource()
    with (
        patch(
            "app.services.metadata_service.download_and_cache_poster",
            new_callable=AsyncMock, return_value=None,
        ),
        caplog.at_level(logging.WARNING, logger="app.services.metadata_repository"),
    ):
        await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)
    assert (await db_session.execute(select(AudioWork))).scalars().all() == []
    assert resource.audio_work_id is None
    assert resource.series_id is None
    assert resource.movie_id is None
    assert any("no usable title" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Batch 2: season-uncertain marking (after reconciliation, never before)
# ---------------------------------------------------------------------------


def _season_uncertain_resource(**kw) -> SimpleNamespace:
    base = dict(
        search_title=None, episode=14, season=None, is_batch=False,
        episode_start=None, episode_end=None, title_cn=None, title_en=None,
        subtitle_langs=None, episode_confidence=None, absolute_episode=None,
        series_id=None, movie_id=None, audio_work_id=None,
        metadata_matched_at=None, batch_scope=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


async def _apply_tv(meta: ResourceMetadata, resource, db_session) -> None:
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock, return_value=None,
    ):
        await _apply_to_resource(meta, resource, SimpleNamespace(id=_uuid()), db_session)


async def test_season_ambiguous_marks_resource_ambiguous(db_session):
    """Multi-season work + no season marker → season None + ambiguous."""
    meta = _meta(
        season_ambiguous=True,
        matched_entity={
            "external_id": "tmdb:501", "external_source": "tmdb",
            "title_en": "Multi Season Show", "number_of_seasons": 3,
            "seasons": [{"season_number": n, "episode_count": 24} for n in (1, 2, 3)],
        },
    )
    resource = _season_uncertain_resource()
    await _apply_tv(meta, resource, db_session)

    assert resource.series_id is not None
    assert resource.season is None
    assert resource.episode_confidence == "ambiguous"


async def test_season_ambiguous_skipped_when_reconcile_derives_season(db_session):
    """Reconcile runs FIRST: an absolute number that pins down the season is a
    legitimate derivation — the season-uncertain marking must not fire."""
    meta = _meta(
        season_ambiguous=True,
        matched_entity={
            "external_id": "tmdb:502", "external_source": "tmdb",
            "title_en": "Multi Season Show 2", "number_of_seasons": 2,
            "seasons": [{"season_number": n, "episode_count": 24} for n in (1, 2)],
        },
    )
    # NN(MM) double-label: absolute 30 → S2E6.
    resource = _season_uncertain_resource(episode=6, absolute_episode=30,
                                          episode_confidence="reconciled")
    await _apply_tv(meta, resource, db_session)

    assert resource.season == 2
    assert resource.episode_confidence == "reconciled"


async def test_season_ambiguous_skipped_for_batch(db_session):
    """A 合集 bypasses per-episode flow — no season-uncertain marking."""
    meta = _meta(
        season_ambiguous=True,
        matched_entity={
            "external_id": "tmdb:503", "external_source": "tmdb",
            "title_en": "Multi Season Show 3", "number_of_seasons": 2,
            "seasons": [{"season_number": n, "episode_count": 24} for n in (1, 2)],
        },
    )
    resource = _season_uncertain_resource(episode=None, is_batch=True)
    await _apply_tv(meta, resource, db_session)

    assert resource.season is None
    assert resource.episode_confidence is None


async def test_single_season_entity_sets_season_1(db_session):
    """Verified single-season work → meta.season=1 applied to the resource."""
    meta = _meta(
        season=1,
        matched_entity={
            "external_id": "tmdb:504", "external_source": "tmdb",
            "title_en": "One Season Show", "number_of_seasons": 1,
            "seasons": [{"season_number": 1, "episode_count": 12}],
        },
    )
    resource = _season_uncertain_resource(episode=5)
    await _apply_tv(meta, resource, db_session)

    assert resource.season == 1
    assert resource.episode_confidence == "raw"


async def test_season_ambiguous_cache_roundtrip(db_session):
    """season_ambiguous must survive the MetadataCache round-trip."""
    meta = _meta(season_ambiguous=True, ambiguous=True,
                 ambiguous_candidates=[{"season": 2}])
    await _set_cache("Season Uncertain Title", "exa", meta, db_session)
    cached = await _get_cache("Season Uncertain Title", "exa", db_session)
    assert cached is not None
    assert cached.season_ambiguous is True
    assert cached.ambiguous is True
    assert cached.ambiguous_candidates == [{"season": 2}]
