"""Production association/parking writers followed by actual DB cleanup.

Recorded torrent bytes and paths are unchanged. Work identities, timestamps,
channel settings and the user's choice of season 1 are explicit test inputs.
"""

import hashlib
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.schemas.file_resource import ResourceAssociationUpdateRequest
from app.services.metadata_episode_reconcile import park_resource_on_collection
from app.services.resource_association import apply_association_update
from app.services.resource_cleanup import cleanup_channel_unresolved_resources, cleanup_stale_unresolved_resources
from app.services.torrent_inspect import analyze_torrent_files, parse_torrent_files
from app.utils.time import utcnow

TORRENT_SHA = "912c2bd9bd70a9556cfaa974cd29d0f1748c05e26dbf6ae87453418a7f801ef1"


@pytest.mark.parametrize("handled", ["unresolved", "collection", "multiwork"])
@pytest.mark.parametrize("manual_cleanup", [False, True])
async def test_cleanup_preserves_authoritative_associations(
    db_session, handled, manual_cleanup, record_testsuite_property, *, entrypoint="service",
):
    torrent = Path(__file__).resolve().parents[2] / "fixtures/metadata_corpus_v1/torrents" / f"{TORRENT_SHA}.torrent"
    assert hashlib.sha256(torrent.read_bytes()).hexdigest() == TORRENT_SHA
    report = analyze_torrent_files(parse_torrent_files(str(torrent)))
    assert report.video_file_count == 27 and report.scope == "franchise"
    record_testsuite_property("torrent_sha256", TORRENT_SHA)
    record_testsuite_property("synthetic_inputs", "work identities, age, policy, explicit manual season 1")
    channel = Channel(id=str(uuid.uuid4()), name="Synthetic cleanup", type="rss_feed", url="https://example.invalid/rss", field_mapping={},
                      auto_cleanup_unresolved_enabled=not manual_cleanup, auto_cleanup_unresolved_days=21)
    collection = WorkCollection(id=str(uuid.uuid4()), title_cn="Synthetic collection")
    series = TVSeries(id=str(uuid.uuid4()), title_cn="Synthetic season", season_number=1,
                      number_of_episodes=26, collection_id=collection.id)
    movie = Movie(id=str(uuid.uuid4()), title_cn="Synthetic movie")
    db_session.add_all([channel, collection, series, movie])
    await db_session.flush()
    resource = FileResource(id=str(uuid.uuid4()), channel_id=channel.id, guid=str(uuid.uuid4()),
                            title_raw="Synthetic recorded pack", torrent_url="https://example.invalid/pack.torrent",
                            torrent_file=str(torrent), created_at=utcnow() - timedelta(days=30))
    db_session.add(resource)
    await db_session.flush()
    if handled == "collection":
        park_resource_on_collection(resource, collection)
        assert resource.episode_confidence == "ambiguous"
    elif handled == "multiwork":
        assignments = []
        for entry in report.file_parses:
            is_movie = entry["path"].startswith("Film ")
            assignments.append(dict(file_path=entry["path"], file_size=entry["size"],
                                    work_type="movie" if is_movie else "series", work_id=movie.id if is_movie else series.id,
                                    season=None if is_movie else 1, episode_start=entry["episode"], episode_end=entry["episode"]))
        await apply_association_update(db_session, resource, ResourceAssociationUpdateRequest(
            is_batch=True, works=[dict(work_type="series", work_id=series.id), dict(work_type="movie", work_id=movie.id)],
            assignments=assignments,
        ))
        assert resource.collection_id is None and resource.episode_confidence is None
    assert resource.series_id is None and resource.movie_id is None and resource.metadata_matched_at is None
    resource_id = resource.id
    await db_session.commit()
    if entrypoint == "api":
        from fastapi import FastAPI
        from httpx import ASGITransport, AsyncClient

        from app.api.v1.channels import router

        app = FastAPI()
        app.include_router(router, prefix="/api/v1")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post(f"/api/v1/channels/{channel.id}/cleanup-unresolved")
        assert response.status_code == 200 and response.json()["success"]
        deleted = response.json()["data"]["deleted"]
    elif entrypoint == "scheduler":
        from app.services.scheduler import _cleanup_expired

        await _cleanup_expired()
        remaining_count = await db_session.scalar(select(func.count()).select_from(FileResource))
        deleted = 1 - remaining_count
    elif manual_cleanup:
        deleted = await cleanup_channel_unresolved_resources(db_session, channel.id, force=True)
    else:
        deleted = (await cleanup_stale_unresolved_resources(db_session))["deleted"]
    await db_session.commit()
    remaining = await db_session.scalar(select(FileResource.id).where(FileResource.id == resource_id))
    links = await db_session.scalar(select(func.count()).select_from(ResourceWorkLink).where(ResourceWorkLink.resource_id == resource_id))
    placements = await db_session.scalar(select(func.count()).select_from(ResourceFileAssignment).where(ResourceFileAssignment.resource_id == resource_id))
    assert (deleted, remaining, links, placements) == (
        (1, None, 0, 0) if handled == "unresolved"
        else (0, resource_id, 2 if handled == "multiwork" else 0, 27 if handled == "multiwork" else 0)
    )
    assert hashlib.sha256(torrent.read_bytes()).hexdigest() == TORRENT_SHA


@pytest.mark.parametrize("source,bound,expected_deleted", [("auto", False, 1), ("llm", False, 1),
                                                          ("manual", False, 0), ("auto", True, 0), ("llm", True, 0)])
async def test_cleanup_assignment_protection_is_selective(db_session, source, bound, expected_deleted):
    channel = Channel(id=str(uuid.uuid4()), name="Synthetic assignment cleanup", type="rss_feed",
                      url="https://example.invalid/rss", field_mapping={})
    movie = Movie(id=str(uuid.uuid4()), title_cn="Synthetic movie")
    db_session.add_all([channel, movie])
    await db_session.flush()
    resource = FileResource(id=str(uuid.uuid4()), channel_id=channel.id, guid=str(uuid.uuid4()),
                            title_raw="Synthetic assignment", torrent_url="https://example.invalid/resource.torrent",
                            created_at=utcnow() - timedelta(days=30))
    db_session.add(resource)
    await db_session.flush()
    assignment = ResourceFileAssignment(resource_id=resource.id, file_path="synthetic.mkv",
                                        movie_id=movie.id if bound else None, source=source)
    db_session.add(assignment)
    await db_session.commit()
    assert await cleanup_channel_unresolved_resources(db_session, channel.id, force=True) == expected_deleted
    await db_session.commit()
    assert await db_session.scalar(select(func.count()).select_from(ResourceFileAssignment)) == 1 - expected_deleted


@pytest.mark.parametrize("entrypoint", ["api", "scheduler"])
@pytest.mark.parametrize("handled", ["unresolved", "collection", "multiwork"])
async def test_cleanup_production_entrypoints_commit_safely(db_session, handled, entrypoint, record_testsuite_property):
    await test_cleanup_preserves_authoritative_associations(
        db_session, handled, entrypoint == "api", record_testsuite_property, entrypoint=entrypoint,
    )
