"""In-process coverage for metadata_repository write-back branches.

Complements the HTTP-suite coverage of ``app/services/metadata_repository.py``:

- ``_series_has_episode_evidence`` (Episode-row and resource-row evidence)
  via the movie-verdict cross-table guard, plus the online repair of a
  misfiled ``content_type="movie"`` TVSeries row.
- ``_fill_subtitle_group`` legacy-list mirroring and the unresolved-source
  overwrite.
- ``_register_subtitle_group_mapping`` insert / upgrade / manual-protected /
  non-subset-rejected paths.
- Batch write-back without a work match (scope default, no-downgrade, stray
  episode cleared, subtitle_langs).
- The franchise invariant short-circuit, audio title fallback + warning,
  the tv-verdict-on-movie-entity flip, and season-indeterminate parking on
  the collection.
"""

from __future__ import annotations

import logging
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.models.episode import Episode
from app.models.file_resource import FileResource
from app.models.metadata_cache import MetadataCache  # noqa: F401  (table registration)
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.subtitle_group_mapping import SubtitleGroupMapping
from app.models.work_collection import WorkCollection
from app.services.metadata_repository import _apply_to_resource
from app.services.metadata_resource_meta import ResourceMetadata


def _uuid() -> str:
    return str(uuid.uuid4())


def _meta(**kw) -> ResourceMetadata:
    base = dict(clean_title="Show", found=True, content_type="tv")
    base.update(kw)
    return ResourceMetadata(**base)


def _resource(**kw) -> SimpleNamespace:
    base = dict(
        id=_uuid(),
        channel_id="ch",
        search_title=None,
        episode=None,
        season=None,
        is_batch=False,
        episode_start=None,
        episode_end=None,
        title_cn=None,
        title_en=None,
        subtitle_group=None,
        subtitle_langs=None,
        episode_confidence=None,
        absolute_episode=None,
        batch_scope=None,
        series_id=None,
        movie_id=None,
        audio_work_id=None,
        collection_id=None,
        metadata_matched_at=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _channel():
    return SimpleNamespace(id=_uuid(), default_is_anime=False)


async def _apply(meta, resource, db_session):
    with patch(
        "app.services.metadata_service.download_and_cache_poster",
        new_callable=AsyncMock,
        return_value=None,
    ):
        await _apply_to_resource(meta, resource, _channel(), db_session)


# ---------------------------------------------------------------------------
# _fill_subtitle_group
# ---------------------------------------------------------------------------


async def test_legacy_scalar_group_is_mirrored_into_plural_list(db_session):
    """A resource carrying only the legacy scalar gets the parsed list
    mirrored with provenance ``legacy`` (no LLM candidate involved)."""
    meta = _meta(found=False)
    resource = _resource(subtitle_group="喵萌&桜都")
    await _apply(meta, resource, db_session)
    assert resource.subtitle_groups == ["喵萌", "桜都"]
    assert resource.subtitle_groups_source == "legacy"
    assert resource.subtitle_group == "喵萌&桜都"  # scalar untouched


async def test_llm_candidate_overwrites_unresolved_compound(db_session):
    meta = _meta(found=False, subtitle_groups=["喵萌", "桜都"])
    resource = _resource(
        subtitle_group="喵萌&桜都",
        subtitle_groups=["喵萌&桜都"],
        subtitle_groups_source="unresolved",
    )
    await _apply(meta, resource, db_session)
    assert resource.subtitle_groups == ["喵萌", "桜都"]
    assert resource.subtitle_groups_source == "llm"
    assert resource.subtitle_group == "喵萌&桜都"


async def test_existing_resolved_groups_are_not_overwritten(db_session):
    meta = _meta(found=False, subtitle_groups=["他组"])
    resource = _resource(
        subtitle_group="桜都",
        subtitle_groups=["桜都"],
        subtitle_groups_source="llm",
    )
    await _apply(meta, resource, db_session)
    assert resource.subtitle_groups == ["桜都"]


# ---------------------------------------------------------------------------
# _register_subtitle_group_mapping
# ---------------------------------------------------------------------------


async def test_llm_split_registers_reusable_mapping(db_session):
    meta = _meta(found=False, subtitle_groups=["喵萌", "桜都"])
    resource = _resource(subtitle_group="喵萌&桜都", subtitle_groups=["喵萌", "桜都"])
    await _apply(meta, resource, db_session)
    row = (await db_session.execute(select(SubtitleGroupMapping))).scalar_one()
    assert row.normalized_key == "喵萌&桜都"
    assert row.groups == ["喵萌", "桜都"]
    assert row.resolution == "llm"


async def test_existing_heuristic_mapping_is_upgraded(db_session):
    db_session.add(SubtitleGroupMapping(
        id=_uuid(), raw_value="A&B", normalized_key="a&b",
        groups=["A&B"], resolution="heuristic",
    ))
    await db_session.flush()
    meta = _meta(found=False, subtitle_groups=["A", "B"])
    resource = _resource(subtitle_group="A&B", subtitle_groups=["A", "B"])
    await _apply(meta, resource, db_session)
    row = (await db_session.execute(select(SubtitleGroupMapping))).scalar_one()
    assert row.groups == ["A", "B"]
    assert row.resolution == "llm"


async def test_manual_mapping_is_never_rewritten(db_session):
    db_session.add(SubtitleGroupMapping(
        id=_uuid(), raw_value="A&B", normalized_key="a&b",
        groups=["A&B联合"], resolution="manual",
    ))
    await db_session.flush()
    meta = _meta(found=False, subtitle_groups=["A", "B"])
    resource = _resource(subtitle_group="A&B", subtitle_groups=["A", "B"])
    await _apply(meta, resource, db_session)
    row = (await db_session.execute(select(SubtitleGroupMapping))).scalar_one()
    assert row.resolution == "manual"
    assert row.groups == ["A&B联合"]


async def test_candidate_outside_parser_split_is_rejected(db_session):
    """The learned registry may only register exact members of the parser's
    own candidate split — an inventive LLM label is dropped."""
    meta = _meta(found=False, subtitle_groups=["A", "C"])
    resource = _resource(subtitle_group="A&B", subtitle_groups=["A", "C"])
    await _apply(meta, resource, db_session)
    assert (await db_session.execute(select(SubtitleGroupMapping))).scalars().all() == []


async def test_single_group_candidate_registers_nothing(db_session):
    meta = _meta(found=False, subtitle_groups=["A"])
    resource = _resource(subtitle_group="A&B", subtitle_groups=["A"])
    await _apply(meta, resource, db_session)
    assert (await db_session.execute(select(SubtitleGroupMapping))).scalars().all() == []


# ---------------------------------------------------------------------------
# Batch write-back without a work match
# ---------------------------------------------------------------------------


async def test_batch_writeback_defaults_scope_and_clears_stray_episode(db_session):
    meta = _meta(
        found=False,
        is_batch=True,
        episode_start=1,
        episode_end=24,
        subtitle_langs=["zh-CN"],
        title_cn="合集标题",
    )
    resource = _resource(episode=9)
    await _apply(meta, resource, db_session)
    assert resource.is_batch is True
    assert resource.batch_scope == "season"  # LLM gave no scope → default
    assert resource.episode is None  # batches never carry a stray episode
    assert resource.episode_start == 1
    assert resource.episode_end == 24
    assert resource.subtitle_langs == ["zh-CN"]
    assert resource.title_cn == "合集标题"


async def test_batch_writeback_never_downgrades_torrent_scope(db_session):
    meta = _meta(found=False, is_batch=True)
    resource = _resource(is_batch=True, batch_scope="franchise")
    await _apply(meta, resource, db_session)
    assert resource.batch_scope == "franchise"


# ---------------------------------------------------------------------------
# Franchise invariant short-circuit
# ---------------------------------------------------------------------------


async def test_franchise_match_enforces_collection_owned_invariant(db_session):
    meta = _meta(
        matched_entity={
            "external_id": "tmdb:7701",
            "external_source": "tmdb",
            "title_cn": "福星小子合集",
        },
    )
    # A stale flat FK must be cleared, never restored by the title match.
    resource = _resource(is_batch=True, batch_scope="franchise", series_id="stale")
    await _apply(meta, resource, db_session)
    assert resource.series_id is None
    assert resource.movie_id is None
    assert resource.audio_work_id is None
    assert resource.metadata_matched_at is not None
    assert (await db_session.execute(select(TVSeries))).scalars().all() == []


# ---------------------------------------------------------------------------
# Audio verdict title fallback
# ---------------------------------------------------------------------------


async def test_audio_titleless_entity_falls_back_to_meta_title_en(db_session):
    from app.models.audio_work import AudioWork

    meta = _meta(
        content_type="asmr",
        title_en="Whisper Session",
        matched_entity={"external_id": "wikipedia:9101", "external_source": "wikipedia"},
    )
    resource = _resource()
    await _apply(meta, resource, db_session)
    aw = (await db_session.execute(select(AudioWork))).scalar_one()
    assert aw.title_cn == "Whisper Session"
    assert resource.audio_work_id == aw.id
    assert resource.series_id is None


async def test_audio_verdict_without_any_title_logs_and_stays_unmatched(
    db_session, caplog
):
    from app.models.audio_work import AudioWork

    meta = _meta(
        content_type="music",
        clean_title="",
        matched_entity={"external_id": "wikipedia:9102"},
    )
    resource = _resource()
    with caplog.at_level(logging.WARNING, logger="app.services.metadata_repository"):
        await _apply(meta, resource, db_session)
    assert (await db_session.execute(select(AudioWork))).scalars().all() == []
    assert resource.audio_work_id is None
    assert any("no usable title" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Cross-table dispatch guards
# ---------------------------------------------------------------------------


async def test_tv_verdict_links_existing_movie_owner(db_session):
    movie = Movie(
        id=_uuid(), title_en="Owned Film", external_id="imdb:tt7654321",
        external_source="imdb", content_type="movie",
    )
    db_session.add(movie)
    await db_session.flush()

    meta = _meta(
        content_type="tv",
        matched_entity={
            "external_id": "imdb:tt7654321",
            "external_source": "imdb",
            "title_en": "Owned Film",
        },
    )
    resource = _resource()
    await _apply(meta, resource, db_session)
    assert resource.movie_id == movie.id
    assert resource.series_id is None
    assert (await db_session.execute(select(TVSeries))).scalars().all() == []


async def test_movie_verdict_with_episode_row_evidence_keeps_series(db_session):
    """A misfiled-looking series that owns Episode rows is genuinely
    episodic: creator-wins, the movie verdict links the series instead."""
    series = TVSeries(
        id=_uuid(), title_cn="异形", external_id="tmdb:9123",
        external_source="tmdb", content_type="movie",
    )
    db_session.add(series)
    await db_session.flush()
    db_session.add(Episode(id=_uuid(), series_id=series.id, season=1, episode=1))
    await db_session.flush()

    meta = _meta(
        content_type="movie",
        matched_entity={
            "external_id": "tmdb:9123", "external_source": "tmdb",
            "title_cn": "异形",
        },
    )
    resource = _resource(season=1, episode=2)
    await _apply(meta, resource, db_session)
    assert resource.series_id == series.id
    assert resource.movie_id is None
    assert (await db_session.execute(select(Movie))).scalars().all() == []


async def test_movie_verdict_with_resource_row_evidence_keeps_series(db_session):
    """Same guard, but the episodic evidence is a linked single-episode
    FileResource rather than an Episode row."""
    from app.models.channel import Channel

    channel = Channel(
        id=_uuid(), name="Evidence Channel", type="rss_feed",
        url="https://example.com/feed", fetch_interval=1800, status="active",
        field_mapping={"list_locator": {"source": "entries"},
                       "field_mappings": {"torrent_url": {"source": "link"}}},
        metadata_agent_enabled=False,
    )
    series = TVSeries(
        id=_uuid(), title_cn="有资源证据", external_id="tmdb:9456",
        external_source="tmdb", content_type="movie",
    )
    db_session.add_all([channel, series])
    await db_session.flush()
    db_session.add(FileResource(
        id=_uuid(), channel_id=channel.id, guid="ev-1", title_raw="ev-1",
        torrent_url="magnet:?xt=urn:btih:ev1", series_id=series.id,
        is_batch=False, episode=3,
    ))
    await db_session.flush()

    meta = _meta(
        content_type="movie",
        matched_entity={
            "external_id": "tmdb:9456", "external_source": "tmdb",
            "title_cn": "有资源证据",
        },
    )
    resource = _resource(season=1, episode=4)
    await _apply(meta, resource, db_session)
    assert resource.series_id == series.id
    assert resource.movie_id is None
    assert (await db_session.execute(select(Movie))).scalars().all() == []


async def test_movie_verdict_repairs_truly_misfiled_series(db_session):
    """No episodic evidence at all → the legacy row is re-homed as a Movie."""
    series = TVSeries(
        id=_uuid(), title_cn="错放的电影", title_en="Misfiled",
        external_id="tmdb:9789", external_source="tmdb", content_type="movie",
    )
    db_session.add(series)
    await db_session.flush()

    meta = _meta(
        content_type="movie",
        matched_entity={
            "external_id": "tmdb:9789", "external_source": "tmdb",
            "title_cn": "错放的电影", "release_date": "2001-03-02",
        },
    )
    resource = _resource()
    await _apply(meta, resource, db_session)
    movie = (await db_session.execute(select(Movie))).scalar_one()
    assert movie.external_id == "tmdb:9789"
    assert resource.movie_id == movie.id
    assert resource.series_id is None
    assert await db_session.get(TVSeries, series.id) is None


async def test_movie_verdict_season_indeterminate_links_known_owner_row(db_session):
    """Guard fallback: the series-level id is *also* bagged on a collection,
    so the series upsert parks (returns None) — the resource then links the
    known owner row directly instead of guessing a season work."""
    from app.models.work_external_id import WorkExternalId

    series = TVSeries(
        id=_uuid(), title_cn="冲突归属", external_id="wikipedia:zh:8888",
        external_source="wikipedia", content_type="movie",
    )
    collection = WorkCollection(id=_uuid(), title_cn="冲突归属（系列）")
    db_session.add_all([series, collection])
    await db_session.flush()
    db_session.add_all([
        Episode(id=_uuid(), series_id=series.id, season=1, episode=1),
        WorkExternalId(
            id=_uuid(), work_type="collection", work_id=collection.id,
            source="wikipedia", external_id="wikipedia:zh:8888",
        ),
    ])
    await db_session.flush()

    meta = _meta(
        content_type="movie",
        matched_entity={
            "external_id": "wikipedia:zh:8888", "external_source": "wikipedia",
            "title_cn": "冲突归属",
            "number_of_seasons": 2,
            "seasons": [
                {"season_number": 1, "episode_count": 12},
                {"season_number": 2, "episode_count": 12},
            ],
        },
    )
    resource = _resource(episode=5)
    await _apply(meta, resource, db_session)
    assert resource.series_id == series.id  # known owner linked, no season guessed
    assert resource.movie_id is None
    assert resource.audio_work_id is None
    assert (await db_session.execute(select(Movie))).scalars().all() == []


# ---------------------------------------------------------------------------
# Season-indeterminate TV verdict → park on collection
# ---------------------------------------------------------------------------


async def test_season_indeterminate_match_parks_on_collection(db_session):
    meta = _meta(
        matched_entity={
            "external_id": "tmdb:9601",
            "external_source": "tmdb",
            "title_en": "Long Running Show",
            "number_of_seasons": 2,
            "seasons": [
                {"season_number": 1, "episode_count": 12},
                {"season_number": 2, "episode_count": 12},
            ],
        },
    )
    resource = _resource(episode=5)  # no season marker anywhere
    await _apply(meta, resource, db_session)

    assert resource.series_id is None  # never guess a season work
    assert resource.collection_id is not None
    assert resource.episode_confidence == "ambiguous"
    collection = (await db_session.execute(select(WorkCollection))).scalar_one()
    assert collection.id == resource.collection_id
