"""Synthetic franchise bindings in real database transactions."""

import uuid
from unittest.mock import AsyncMock

import pytest

from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.work_collection import WorkCollection
from app.services.metadata_agent import UnifiedMetadataAgent
from app.services.metadata_service import (
    apply_manual_title_mapping,
    extract_search_title,
    fetch_and_link_metadata,
)
from app.services.text_normalizer import normalize_title


@pytest.mark.parametrize("entry,force_refresh", [
    ("mapping", False), ("agent", False), ("legacy", False),
    ("pipeline_agent", False), ("pipeline_legacy", False), ("metadata_api", False),
    ("agent", True), ("pipeline_agent", True), ("pipeline_legacy", True),
])
async def test_single_work_mapping_preserves_franchise_shape(db_session, sample_channel, entry, force_refresh, monkeypatch):
    collection = WorkCollection(title_cn="Synthetic franchise collection")
    movie = Movie(title_en="Synthetic manual movie")
    db_session.add_all([collection, movie])
    await db_session.flush()
    resource = FileResource(
        channel_id=sample_channel.id, guid=str(uuid.uuid4()),
        title_raw="Synthetic franchise TV plus Film pack", torrent_url="",
        is_batch=True, batch_scope="franchise", collection_id=collection.id,
    )
    db_session.add(resource)
    await db_session.flush()
    resource_id, collection_id = resource.id, collection.id
    from app.models.resource_file_assignment import ResourceFileAssignment
    from app.models.resource_work_link import ResourceWorkLink
    from app.models.series import TVSeries

    series = TVSeries(title_cn="Synthetic franchise season", season_number=1, collection_id=collection_id)
    db_session.add(series)
    await db_session.flush()
    links = [ResourceWorkLink(resource_id=resource_id, movie_id=movie.id, source="manual"),
             ResourceWorkLink(resource_id=resource_id, series_id=series.id, source="manual")]
    assignments = [
        ResourceFileAssignment(resource_id=resource_id, file_path="Film/movie.mkv", movie_id=movie.id,
                               file_size=123, source="manual"),
        ResourceFileAssignment(resource_id=resource_id, file_path="TV/episode01.mkv", series_id=series.id,
                               season=1, episode_start=1, episode_end=1, file_size=456, source="manual"),
    ]
    db_session.add_all(links + assignments)
    await db_session.flush()
    link_snapshot = [(row.id, row.series_id, row.movie_id, row.source) for row in links]
    file_snapshot = [(row.id, row.file_path, row.series_id, row.movie_id, row.season,
                      row.episode_start, row.episode_end, row.file_size, row.source) for row in assignments]
    db_session.add(ChannelRawTitleMapping(
        channel_id=sample_channel.id, raw_title=resource.title_raw,
        search_title_key=normalize_title(extract_search_title(resource)),
        movie_id=movie.id, content_type="movie",
    ))
    sample_channel.metadata_agent_enabled = entry == "pipeline_agent"
    await db_session.commit()
    if entry == "metadata_api":
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        from app.api.v1.resources import router
        from app.database import get_db

        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        async def session_override():
            yield db_session
        app.dependency_overrides[get_db] = session_override
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.get(f"/api/v1/resources/{resource_id}/metadata")
        assert response.status_code == 200, response.text
    elif entry.startswith("pipeline_"):
        from app.services.fetch_service import _process_resource_metadata_once

        # Shape is pre-established; this test isolates matching and its real
        # final commit, not torrent inspection or external graph enrichment.
        monkeypatch.setattr("app.services.torrent_inspect.ensure_torrent_cached", AsyncMock())
        monkeypatch.setattr("app.services.torrent_inspect.maybe_inspect_torrent", AsyncMock())
        monkeypatch.setattr("app.services.bangumi_relations.expand_bangumi_series_graph", AsyncMock())
        monkeypatch.setattr("app.services.cluster_work_binding.bind_hint_clusters", AsyncMock())
        await _process_resource_metadata_once(resource_id, sample_channel.id, force_refresh=force_refresh)
    elif entry == "agent":
        await UnifiedMetadataAgent().process(resource, sample_channel, db_session, force_refresh=force_refresh)
    elif entry == "legacy":
        await fetch_and_link_metadata(db_session, resource, sample_channel)
    else:
        await apply_manual_title_mapping(db_session, resource, sample_channel)
    await db_session.commit()
    from app.database import async_session_factory

    async with async_session_factory() as observer:
        saved = await observer.get(FileResource, resource_id)
        assert saved.collection_id == collection_id
        assert saved.batch_scope == "franchise" and saved.is_batch
        assert saved.movie_id is None and saved.series_id is None
        assert saved.episode is None
        for expected in link_snapshot:
            row = await observer.get(ResourceWorkLink, expected[0])
            assert row is not None
            assert (row.id, row.series_id, row.movie_id, row.source) == expected
        for expected in file_snapshot:
            row = await observer.get(ResourceFileAssignment, expected[0])
            assert row is not None
            assert (row.id, row.file_path, row.series_id, row.movie_id, row.season,
                    row.episode_start, row.episode_end, row.file_size, row.source) == expected
