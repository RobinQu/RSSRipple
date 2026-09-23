"""Recorded release title with explicitly synthetic mapping/cache identities."""

import gzip
import json
import uuid
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.models.channel import Channel
from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.services.metadata_agent import UnifiedMetadataAgent
from app.services.metadata_resource_meta import ResourceMetadata
from app.services.metadata_service import extract_search_title
from app.services.text_normalizer import normalize_title


async def test_movie_target_lookup_does_not_apply_candidate(db_session):
    from sqlalchemy import func, select

    from app.models.work_external_id import WorkExternalId
    from app.services.metadata_service import find_existing_movie_for_external

    movie = Movie(title_en="Synthetic identity lookup", is_anime=False)
    db_session.add(movie)
    await db_session.commit()
    selected = await find_existing_movie_for_external(db_session, {
        "title_en": movie.title_en, "content_type": "movie", "external_source": "tmdb",
        "external_id": "90000019", "description": "Must not apply", "release_date": "2011-12-07",
    })
    assert selected.id == movie.id
    await db_session.flush()
    assert movie.description is None and movie.release_date is None and movie.external_id is None
    assert (await db_session.execute(select(func.count()).select_from(WorkExternalId))).scalar_one() == 0


@pytest.mark.parametrize("mapping_kind", ["normalized", "raw_fallback", "empty_target", "other_channel"])
@pytest.mark.parametrize("force_refresh,candidate_matches_manual", [(False, False), (True, False), (True, True)])
@pytest.mark.parametrize("already_linked", [False, True])
async def test_manual_mapping_precedes_success_cache(
    db_session, sample_channel, mapping_kind, force_refresh, candidate_matches_manual, already_linked,
):
    corpus = Path(__file__).resolve().parents[1] / "fixtures/metadata_corpus_v1/candidates.json.gz"
    with gzip.open(corpus, "rt") as stream:
        cases = json.load(stream)["cases"]
    case = next(c for c in cases if c["id"] == "00e48de8-b5fd-4e99-8467-d384bd4a3183")
    raw = case["input"]["title_raw"]
    sample_channel.metadata_source = "tmdb"
    manual = Movie(title_en="Synthetic manual choice", is_anime=False)
    cached = Movie(title_en="Synthetic automatic choice", is_anime=False)
    resource = FileResource(channel_id=sample_channel.id, guid=str(uuid.uuid4()), title_raw=raw, torrent_url="")
    db_session.add_all([manual, cached, resource])
    await db_session.flush()
    if already_linked:
        resource.movie_id = manual.id
    channel_id = sample_channel.id
    if mapping_kind == "other_channel":
        other = Channel(name="Synthetic other channel", url="https://example.invalid/other", field_mapping={})
        db_session.add(other)
        await db_session.flush()
        channel_id = other.id
    key = normalize_title(extract_search_title(resource))
    if mapping_kind == "raw_fallback":
        key = "synthetic-legacy-key"
    db_session.add(ChannelRawTitleMapping(
        channel_id=channel_id, raw_title=raw, search_title_key=key,
        movie_id=None if mapping_kind == "empty_target" else manual.id,
        content_type="movie",
    ))
    agent = UnifiedMetadataAgent()
    incoming_title = manual.title_en if candidate_matches_manual else cached.title_en
    agent._run_react = AsyncMock(return_value=(
        {"clean_title": incoming_title, "content_type": "movie", "found": True,
         "matched_entity": {"title_en": incoming_title, "content_type": "movie", "is_anime": False,
                            "release_date": "2011-12-07"}},
        {"method": "synthetic", "data_sources_used": [], "source_errors": {}, "error": None},
    ))
    agent._ensure_genre = AsyncMock()
    agent._get_cache = AsyncMock(wraps=agent._get_cache)
    await agent._set_cache(raw, "tmdb", ResourceMetadata(
        clean_title=cached.title_en, content_type="movie", found=True,
        matched_entity={"title_en": cached.title_en, "content_type": "movie", "is_anime": False},
    ), db_session)
    await db_session.commit()
    await agent.process(resource, sample_channel, db_session, force_refresh=force_refresh)
    await db_session.commit()
    if force_refresh:
        agent._get_cache.assert_not_awaited()

    from app.database import async_session_factory

    async with async_session_factory() as observer:
        saved = await observer.get(FileResource, resource.id)
        keep_manual = (
            mapping_kind in {"normalized", "raw_fallback"}
            or candidate_matches_manual
            or (already_linked and not force_refresh)
        )
        expected = manual.id if keep_manual else cached.id
        assert saved.movie_id == expected
        assert saved.series_id is None
        assert saved.metadata_matched_at is not None
        if force_refresh and candidate_matches_manual:
            refreshed = await observer.get(Movie, manual.id)
            assert refreshed.release_date == date(2011, 12, 7)


@pytest.mark.parametrize("conflicting_link", [False, True])
async def test_mapping_respects_existing_link_and_caller_rollback(
    db_session, sample_channel, conflicting_link,
):
    from app.database import async_session_factory
    from app.services.metadata_service import apply_manual_title_mapping

    mapped = Movie(title_en="Synthetic mapped identity", is_anime=False)
    existing = Movie(title_en="Synthetic existing identity", is_anime=False)
    resource = FileResource(
        channel_id=sample_channel.id, guid=str(uuid.uuid4()),
        title_raw="Synthetic mapping transaction 2011", torrent_url="",
    )
    db_session.add_all([mapped, existing, resource])
    await db_session.flush()
    original_id = existing.id if conflicting_link else None
    resource.movie_id = original_id
    resource_id = resource.id
    mapped_id = mapped.id
    db_session.add(ChannelRawTitleMapping(
        channel_id=sample_channel.id, raw_title=resource.title_raw,
        search_title_key=normalize_title(extract_search_title(resource)),
        movie_id=mapped_id, content_type="movie",
        search_title_override="Synthetic manual title override",
    ))
    await db_session.commit()

    applied = await apply_manual_title_mapping(db_session, resource, sample_channel)
    assert applied is (not conflicting_link)
    if conflicting_link:
        assert resource.movie_id == original_id
        assert resource.search_title is None
        assert resource.metadata_matched_at is None
    else:
        assert resource.movie_id == mapped_id
        assert resource.search_title == "Synthetic manual title override"
        assert resource.metadata_matched_at is not None
    await db_session.flush()
    await db_session.rollback()
    async with async_session_factory() as observer:
        saved = await observer.get(FileResource, resource_id)
        assert saved.movie_id == original_id
        assert saved.search_title is None
        assert saved.metadata_matched_at is None


@pytest.mark.parametrize("force_refresh", [False, True])
@pytest.mark.parametrize("candidate_kind", ["movie", "other_tv", "same_tv"])
async def test_manual_season_mapping_preserves_identity(db_session, sample_channel, force_refresh, candidate_kind):
    from app.database import async_session_factory
    from app.models.series import TVSeries
    from app.models.work_collection import WorkCollection

    sample_channel.metadata_source = "tmdb"
    collection = WorkCollection(title_cn="Synthetic season mapping collection")
    db_session.add(collection)
    await db_session.flush()
    series = TVSeries(title_cn="Synthetic season mapping S2", season_number=2,
                      collection_id=collection.id, is_anime=False)
    resource = FileResource(channel_id=sample_channel.id, guid=str(uuid.uuid4()),
                            title_raw="Synthetic series S02E01", season=2, episode=1, torrent_url="")
    db_session.add_all([series, resource])
    await db_session.flush()
    series_id, resource_id = series.id, resource.id
    db_session.add(ChannelRawTitleMapping(
        channel_id=sample_channel.id, raw_title=resource.title_raw,
        search_title_key=normalize_title(extract_search_title(resource)),
        series_id=series_id, content_type="tv",
    ))
    await db_session.commit()
    agent = UnifiedMetadataAgent()
    agent._get_cache = AsyncMock(return_value=None)
    agent._ensure_genre = AsyncMock()
    candidate_type = "movie" if candidate_kind == "movie" else "tv"
    candidate_title = series.title_cn if candidate_kind == "same_tv" else "Synthetic conflicting candidate S2"
    agent._run_react = AsyncMock(return_value=(
        {"clean_title": candidate_title, "content_type": candidate_type, "found": True,
         "matched_entity": {"title_cn": candidate_title, "content_type": candidate_type,
                            "start_date": "2020-01-01", "is_anime": False,
                            "seasons": [{"season_number": 2, "air_date": "2024-01-01"}]}},
        {"method": "synthetic", "data_sources_used": [], "source_errors": {}, "error": None},
    ))
    await agent.process(resource, sample_channel, db_session, force_refresh=force_refresh)
    await db_session.commit()
    async with async_session_factory() as observer:
        saved = await observer.get(FileResource, resource_id)
        from sqlalchemy import func, select

        assert (await observer.execute(select(func.count()).select_from(TVSeries))).scalar_one() == 1
        assert (await observer.execute(select(func.count()).select_from(WorkCollection))).scalar_one() == 1
        assert saved.series_id == series_id
        assert saved.movie_id is None
        assert saved.season == 2

        from app.models.metadata_cache import MetadataCache

        assert (await observer.execute(select(func.count()).select_from(Movie))).scalar_one() == 0
        if candidate_kind != "same_tv" or not force_refresh:
            assert saved.search_title is None, "Rejected candidate parse fields must roll back"
            assert (await observer.execute(select(func.count()).select_from(MetadataCache))).scalar_one() == 0
        if candidate_kind == "same_tv" and force_refresh:
            refreshed = await observer.get(TVSeries, series_id)
            assert refreshed.start_date == date(2024, 1, 1)


@pytest.mark.parametrize("candidate_kind", ["same_season", "other_season", "new_collection"])
async def test_series_upsert_fixed_target_precedes_writes(db_session, candidate_kind):
    from sqlalchemy import func, select

    from app.database import async_session_factory
    from app.models.series import TVSeries
    from app.models.work_collection import WorkCollection
    from app.models.work_external_id import WorkExternalId
    from app.services.metadata_service import (
        MetadataTargetMismatchError,
        create_or_update_series_from_external,
    )

    collection = WorkCollection(title_cn="Synthetic fixed collection", aliases=["Original alias"])
    db_session.add(collection)
    await db_session.flush()
    first = TVSeries(title_cn="Synthetic fixed S1", season_number=1, collection_id=collection.id)
    target = TVSeries(title_cn="Synthetic fixed S2", season_number=2, collection_id=collection.id)
    db_session.add_all([first, target])
    await db_session.commit()
    target_id, collection_id = target.id, collection.id
    season = 1 if candidate_kind == "other_season" else 2
    data = {
        "title_cn": "Synthetic unrelated identity" if candidate_kind == "new_collection" else collection.title_cn,
        "alt_titles": ["Candidate alias must not leak"],
        "content_type": "tv", "description": "Candidate metadata",
        "seasons": [{"season_number": season, "air_date": "2024-01-01"}],
    }
    if candidate_kind == "same_season":
        saved = await create_or_update_series_from_external(
            db_session, data, season_hint=season, expected_series_id=target_id,
        )
        assert saved.id == target_id
    else:
        with pytest.raises(MetadataTargetMismatchError):
            await create_or_update_series_from_external(
                db_session, data, season_hint=season, expected_series_id=target_id,
            )
    # Commit even after rejection: rollback must not hide early side effects.
    await db_session.commit()
    async with async_session_factory() as observer:
        assert (await observer.execute(select(func.count()).select_from(TVSeries))).scalar_one() == 2
        assert (await observer.execute(select(func.count()).select_from(WorkCollection))).scalar_one() == 1
        saved_target = await observer.get(TVSeries, target_id)
        saved_collection = await observer.get(WorkCollection, collection_id)
        if candidate_kind == "same_season":
            assert saved_target.start_date == date(2024, 1, 1)
        else:
            assert saved_target.start_date is None and saved_target.description is None
            assert saved_collection.aliases == ["Original alias"]
            assert (await observer.execute(select(func.count()).select_from(WorkExternalId))).scalar_one() == 0


async def test_mapping_lookup_preserves_unlinked_resource(db_session, sample_channel):
    from app.services.metadata_service import find_manual_title_mapping

    movie = Movie(title_en="Synthetic lookup target", is_anime=False)
    resource = FileResource(channel_id=sample_channel.id, guid=str(uuid.uuid4()),
                            title_raw="Synthetic lookup release", torrent_url="")
    db_session.add_all([movie, resource])
    await db_session.flush()
    mapping = ChannelRawTitleMapping(
        channel_id=sample_channel.id, raw_title=resource.title_raw,
        search_title_key=normalize_title(extract_search_title(resource)),
        movie_id=movie.id, content_type="movie", search_title_override="Manual override",
    )
    db_session.add(mapping)
    await db_session.commit()
    found = await find_manual_title_mapping(db_session, resource, sample_channel)
    assert found.id == mapping.id
    assert resource.movie_id is None
    assert resource.search_title is None
    assert resource.metadata_matched_at is None
    assert not db_session.dirty


@pytest.mark.parametrize("raw_title", ["Synthetic deferred release", "[ASMR] Synthetic deferred release"])
async def test_forced_mapping_is_applied_only_after_external_lookup(db_session, sample_channel, raw_title):
    sample_channel.metadata_source = "tmdb"
    movie = Movie(title_en="Synthetic deferred manual target", is_anime=False)
    resource = FileResource(channel_id=sample_channel.id, guid=str(uuid.uuid4()),
                            title_raw=raw_title, torrent_url="")
    db_session.add_all([movie, resource])
    await db_session.flush()
    db_session.add(ChannelRawTitleMapping(
        channel_id=sample_channel.id, raw_title=resource.title_raw,
        search_title_key=normalize_title(extract_search_title(resource)),
        movie_id=movie.id, content_type="movie",
    ))
    await db_session.commit()
    agent = UnifiedMetadataAgent()
    agent._ensure_genre = AsyncMock()
    agent._resolve_audio_work = AsyncMock(side_effect=AssertionError("Manual movie identity bypassed by audio resolver"))

    async def lookup(*args, **kwargs):
        assert resource.movie_id is None
        assert not db_session.dirty, "Manual link must not be written before external I/O"
        return ({"found": False, "clean_title": "Synthetic rejected candidate"},
                {"method": "synthetic", "data_sources_used": [], "source_errors": {}, "error": None})

    agent._run_react = AsyncMock(side_effect=lookup)
    result = await agent.process(resource, sample_channel, db_session, force_refresh=True)
    await db_session.commit()
    await db_session.refresh(resource)
    agent._run_react.assert_awaited_once()
    assert result.found
    assert resource.movie_id == movie.id
