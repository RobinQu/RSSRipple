"""Unified resource listing, retry state and reconciliation tests."""

from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.models.agent import Agent
from app.models.agent_resource_request import AgentResourceRequest
from app.models.download_task import DownloadTask
from app.models.file_resource import FileResource
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_publication import ResourcePublication
from app.services import resource_listing as listing_service
from app.services.resource_publication import publish_resource
from app.utils.time import utcnow


def _uuid() -> str:
    return str(uuid.uuid4())


async def _resource(db_session, channel_id: str, **overrides) -> FileResource:
    values = dict(
        id=_uuid(), channel_id=channel_id, guid=_uuid(), title_raw="Show - 01",
        torrent_url="magnet:?xt=urn:btih:abc",
    )
    values.update(overrides)
    resource = FileResource(**values)
    db_session.add(resource)
    await db_session.commit()
    return resource


async def test_downloader_tries_every_related_task(
    db_session, sample_channel, sample_downloader, monkeypatch
):
    resource = await _resource(db_session, sample_channel.id)
    old_id, new_id = _uuid(), _uuid()
    now = utcnow()
    db_session.add_all([
        DownloadTask(
            id=old_id, file_resource_id=resource.id, downloader_id=sample_downloader.id,
            download_dir="/d", transmission_torrent_id=11, status="completed", created_at=now,
        ),
        DownloadTask(
            id=new_id, file_resource_id=resource.id, downloader_id=sample_downloader.id,
            download_dir="/d", transmission_torrent_id=22, status="error", created_at=now + timedelta(seconds=1),
        ),
    ])
    await db_session.commit()
    calls = []

    async def get_files(torrent_id):
        calls.append(torrent_id)
        if torrent_id == 22:
            raise RuntimeError("removed from daemon")
        return {"name": "old", "files": [{"name": "old.mkv", "size": 10}]}

    monkeypatch.setattr(
        "app.clients.downloader.get_downloader_client",
        lambda _downloader: SimpleNamespace(get_torrent_files=get_files),
    )
    result = await listing_service.resolve_resource_listing(db_session, resource)

    assert calls == [22, 11]
    assert result.source == "downloader" and result.kind == "listing"
    assert result.provider == "transmission" and result.name == "old"
    assert result.files == [{"name": "old.mkv", "size": 10}]


async def test_http_retry_backoff_permanent_failure_and_attempt_cas(
    db_session, sample_channel
):
    resource = await _resource(
        db_session, sample_channel.id, torrent_url="https://example.com/a.torrent"
    )
    first = await listing_service.claim_torrent_fetch(db_session, resource.id)
    await db_session.commit()
    assert first is not None
    await db_session.refresh(resource)
    assert resource.torrent_fetch_status == "running"
    assert resource.torrent_fetch_attempts == 1

    assert await listing_service.record_torrent_fetch_failure(
        db_session, resource.id, first, "timeout", retryable=True
    )
    await db_session.commit()
    await db_session.refresh(resource)
    assert resource.torrent_fetch_status == "failed"
    assert resource.torrent_fetch_next_attempt_at > utcnow()
    assert await listing_service.claim_torrent_fetch(db_session, resource.id) is None

    second = await listing_service.claim_torrent_fetch(db_session, resource.id, force=True)
    await db_session.commit()
    assert second is not None and second != first
    assert not await listing_service.record_torrent_fetch_success(
        db_session, resource.id, first, "/tmp/stale.torrent", expected_torrent_file=None
    )
    assert await listing_service.record_torrent_fetch_failure(
        db_session, resource.id, second, "invalid bencode", retryable=False
    )
    await db_session.commit()
    await db_session.refresh(resource)
    assert resource.torrent_fetch_attempts == 2
    assert resource.torrent_fetch_status == "failed"
    assert resource.torrent_fetch_error == "invalid bencode"
    assert resource.torrent_fetch_next_attempt_at is None
    assert await listing_service.claim_torrent_fetch(db_session, resource.id) is None


async def test_http_listing_records_retryable_failure(
    db_session, sample_channel, monkeypatch
):
    resource = await _resource(
        db_session, sample_channel.id, torrent_url="https://example.com/a.torrent"
    )
    from app.services.torrent_inspect import TorrentFetchError

    fetch = AsyncMock(side_effect=TorrentFetchError("validation failed", retryable=True))
    monkeypatch.setattr(listing_service, "fetch_torrent_file_detailed", fetch)
    result = await listing_service.resolve_resource_listing(db_session, resource)

    assert result.files == [] and result.kind is None
    assert result.provider == "http" and "validation failed" in result.error
    await db_session.refresh(resource)
    assert resource.torrent_fetch_attempts == 1
    assert resource.torrent_fetch_next_attempt_at is not None
    fetch.assert_awaited_once_with(resource.torrent_url, resource.id)


async def test_http_claim_is_committed_before_network(db_session, sample_channel, monkeypatch):
    from app.database import async_session_factory

    resource = await _resource(
        db_session, sample_channel.id, torrent_url="https://example.com/a.torrent"
    )
    observed = []

    async def fetch(_url, _resource_id):
        async with async_session_factory() as observer:
            saved = await observer.get(FileResource, resource.id)
            observed.append((saved.torrent_fetch_status, saved.torrent_fetch_attempts))
        return "/tmp/new.torrent"

    monkeypatch.setattr(listing_service, "fetch_torrent_file_detailed", fetch)
    monkeypatch.setattr(
        listing_service, "parse_torrent_files", lambda _path: [{"name": "a.mkv", "size": 1}]
    )
    result = await listing_service.fetch_resource_torrent(db_session, resource)

    assert observed == [("running", 1)]
    assert result.source == "torrent_fetch" and result.files
    await db_session.refresh(resource)
    assert resource.torrent_fetch_status == "done"


async def test_detailed_permanent_failure_is_not_retried(db_session, sample_channel, monkeypatch):
    from app.services.torrent_inspect import TorrentFetchError

    resource = await _resource(
        db_session, sample_channel.id, torrent_url="https://example.com/bad.torrent"
    )
    fetch = AsyncMock(side_effect=TorrentFetchError("invalid bencode", retryable=False))
    monkeypatch.setattr(listing_service, "fetch_torrent_file_detailed", fetch)

    first = await listing_service.fetch_resource_torrent(db_session, resource)
    second = await listing_service.fetch_resource_torrent(db_session, resource)

    assert first.error == "http: invalid bencode"
    assert "invalid bencode" in second.error
    assert fetch.await_count == 1
    await db_session.refresh(resource)
    assert resource.torrent_fetch_status == "failed"
    assert resource.torrent_fetch_next_attempt_at is None


async def test_reconcile_publishes_commits_then_dispatches(
    db_session, sample_channel, sample_downloader, monkeypatch
):
    resource = await _resource(db_session, sample_channel.id)
    agent = Agent(
        id=_uuid(), name="Agent", channel_id=sample_channel.id,
        downloader_id=sample_downloader.id, status="active",
    )
    db_session.add(agent)
    await db_session.flush()
    await publish_resource(db_session, resource.id, kind="created")
    await db_session.commit()
    observed = []

    async def dispatch():
        from app.database import async_session_factory

        async with async_session_factory() as observer:
            publication = await observer.scalar(select(ResourcePublication).where(
                ResourcePublication.resource_id == resource.id,
                ResourcePublication.kind == "metadata",
            ))
            assignment = await observer.scalar(select(ResourceFileAssignment).where(
                ResourceFileAssignment.resource_id == resource.id,
            ))
        observed.append((publication is not None, assignment is not None))

    monkeypatch.setattr("app.services.publication_dispatch.dispatch_pending_publications", dispatch)
    result = await listing_service.reconcile_resource(
        resource.id, files=[{"name": "Show.S01E01.mkv", "size": 100 * 1024 * 1024}]
    )

    assert result.files and result.error is None
    assert observed == [(True, True)]
    request = await db_session.scalar(select(AgentResourceRequest).where(
        AgentResourceRequest.resource_id == resource.id
    ))
    assert request is None
    await db_session.refresh(resource)
    assert resource.listing_reconcile_status == "done"
    assert resource.listing_reconcile_attempts == 1
    repeated = await listing_service.reconcile_resource(
        resource.id, files=[{"name": "Show.S01E01.mkv", "size": 100 * 1024 * 1024}]
    )
    assert repeated.error is None
    await db_session.refresh(resource)
    assert resource.listing_reconcile_attempts == 1
    assert observed == [(True, True)]

    forced = await listing_service.reconcile_resource(
        resource.id,
        files=[{"name": "Show.S01E01.mkv", "size": 100 * 1024 * 1024}],
        force=True,
    )
    assert forced.files and forced.error is None
    await db_session.refresh(resource)
    assert resource.listing_reconcile_attempts == 2
    assert observed == [(True, True), (True, True)]


async def test_reconcile_writes_scope_and_converges_graph(db_session, sample_channel, monkeypatch):
    resource = await _resource(db_session, sample_channel.id)
    await publish_resource(db_session, resource.id, kind="created")
    await db_session.commit()
    expand = AsyncMock()
    rebind = AsyncMock()
    bind_clusters = AsyncMock()
    dedupe = AsyncMock()
    monkeypatch.setattr("app.services.bangumi_relations.expand_bangumi_series_graph", expand)
    monkeypatch.setattr("app.services.batch_content_analysis.rebind_absolute_season_assignments", rebind)
    monkeypatch.setattr("app.services.cluster_work_binding.bind_hint_clusters", bind_clusters)
    monkeypatch.setattr("app.services.franchise_service.dedupe_resource_movies", dedupe)
    monkeypatch.setattr("app.services.publication_dispatch.dispatch_pending_publications", AsyncMock())
    files = [
        {"name": f"Show/S{season}/Show S{season:02d}E{episode:02d}.mkv", "size": 100 * 1024 * 1024}
        for season in (1, 2) for episode in (1, 2)
    ]

    await listing_service.reconcile_resource(resource.id, files=files)

    await db_session.refresh(resource)
    assert resource.is_batch is True and resource.batch_scope == "multi_season"
    assert resource.batch_seasons == [1, 2]
    assert resource.episode is None and resource.season is None
    expand.assert_awaited_once()
    rebind.assert_awaited_once()
    assert bind_clusters.await_count == 2
    dedupe.assert_awaited_once()


async def test_reconcile_rejects_stale_magnet_attempt(
    db_session, sample_channel, monkeypatch
):
    resource = await _resource(
        db_session, sample_channel.id, magnet_resolve_attempt_id=_uuid(),
        magnet_resolve_status="done",
    )
    await publish_resource(db_session, resource.id, kind="created")
    await db_session.commit()
    wake = AsyncMock()
    monkeypatch.setattr("app.services.publication_dispatch.dispatch_pending_publications", wake)

    result = await listing_service.reconcile_resource(
        resource.id,
        files=[{"name": "Show.S01E01.mkv", "size": 100 * 1024 * 1024}],
        expected_magnet_attempt_id=_uuid(),
    )

    assert result.files == [] and "replaced" in result.error
    wake.assert_not_awaited()
    assert await db_session.scalar(select(ResourcePublication).where(
        ResourcePublication.resource_id == resource.id,
        ResourcePublication.kind == "metadata",
    )) is None


async def test_http_stale_claim_and_cache_competition_are_cas_fenced(
    db_session, sample_channel,
):
    old_attempt = _uuid()
    resource = await _resource(
        db_session, sample_channel.id, torrent_url="https://example.com/a.torrent",
        torrent_fetch_status="running", torrent_fetch_attempt_id=old_attempt,
        torrent_fetch_updated_at=utcnow() - timedelta(minutes=10),
    )
    new_attempt = await listing_service.claim_torrent_fetch(db_session, resource.id)
    await db_session.commit()
    assert new_attempt is not None and new_attempt != old_attempt
    assert not await listing_service.record_torrent_fetch_success(
        db_session, resource.id, old_attempt, "/tmp/old.torrent", expected_torrent_file=None
    )

    # Simulate a valid magnet winner after the HTTP claim. The late HTTP write
    # must match both its new attempt and the cache reference observed at claim.
    resource.torrent_file = "/tmp/magnet.torrent"
    await db_session.commit()
    assert not await listing_service.record_torrent_fetch_success(
        db_session, resource.id, new_attempt, "/tmp/http.torrent", expected_torrent_file=None
    )
    await db_session.refresh(resource)
    assert resource.torrent_file == "/tmp/magnet.torrent"


async def test_dispatch_failure_leaves_committed_publication_recoverable(
    db_session, sample_channel, monkeypatch,
):
    resource = await _resource(db_session, sample_channel.id)
    await publish_resource(db_session, resource.id, kind="created")
    await db_session.commit()
    monkeypatch.setattr(
        "app.services.publication_dispatch.dispatch_pending_publications",
        AsyncMock(side_effect=RuntimeError("queue unavailable")),
    )

    result = await listing_service.reconcile_resource(
        resource.id, files=[{"name": "Show.S01E01.mkv", "size": 100 * 1024 * 1024}]
    )

    assert result.error is None
    await db_session.refresh(resource)
    assert resource.listing_reconcile_status == "done"
    assert await db_session.scalar(select(ResourcePublication).where(
        ResourcePublication.resource_id == resource.id,
        ResourcePublication.kind == "metadata",
    )) is not None


async def test_reconcile_graph_failure_is_retryable(db_session, sample_channel, monkeypatch):
    resource = await _resource(db_session, sample_channel.id)
    await publish_resource(db_session, resource.id, kind="created")
    await db_session.commit()
    expand = AsyncMock(side_effect=RuntimeError("graph unavailable"))
    dispatch = AsyncMock()
    monkeypatch.setattr("app.services.bangumi_relations.expand_bangumi_series_graph", expand)
    monkeypatch.setattr("app.services.publication_dispatch.dispatch_pending_publications", dispatch)
    files = [
        {"name": f"Show/S{season}/Show S{season:02d}E{episode:02d}.mkv", "size": 100 * 1024 * 1024}
        for season in (1, 2) for episode in (1, 2)
    ]

    failed = await listing_service.reconcile_resource(resource.id, files=files)
    assert "graph unavailable" in failed.error
    await db_session.refresh(resource)
    assert resource.listing_reconcile_status == "failed"
    assert resource.listing_reconcile_next_attempt_at > utcnow()
    dispatch.assert_not_awaited()
    assert await db_session.scalar(select(ResourcePublication).where(
        ResourcePublication.resource_id == resource.id,
        ResourcePublication.kind == "metadata",
    )) is None

    expand.side_effect = None
    recovered = await listing_service.reconcile_resource(resource.id, files=files, force=True)
    assert recovered.error is None
    await db_session.refresh(resource)
    assert resource.listing_reconcile_status == "done"
    assert resource.listing_reconcile_attempts == 2
    dispatch.assert_awaited_once()


async def test_reconcile_publish_failure_is_retryable(db_session, sample_channel, monkeypatch):
    import app.services.resource_publication as publication

    resource = await _resource(db_session, sample_channel.id)
    await publish_resource(db_session, resource.id, kind="created")
    await db_session.commit()
    original = publication.publish_resource
    calls = 0

    async def flaky_publish(db, resource_id, *, kind):
        nonlocal calls
        if kind == "metadata":
            calls += 1
            if calls == 1:
                raise RuntimeError("publication unavailable")
        return await original(db, resource_id, kind=kind)

    monkeypatch.setattr(publication, "publish_resource", flaky_publish)
    monkeypatch.setattr("app.services.publication_dispatch.dispatch_pending_publications", AsyncMock())
    files = [{"name": "Show.S01E01.mkv", "size": 100 * 1024 * 1024}]
    failed = await listing_service.reconcile_resource(resource.id, files=files)
    assert "publication unavailable" in failed.error
    await db_session.refresh(resource)
    assert resource.listing_reconcile_status == "failed"

    recovered = await listing_service.reconcile_resource(resource.id, files=files, force=True)
    assert recovered.error is None
    await db_session.refresh(resource)
    assert resource.listing_reconcile_status == "done"
    assert resource.listing_reconcile_attempts == 2


async def test_reconcile_commit_failure_is_retryable(db_session, sample_channel, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    resource = await _resource(db_session, sample_channel.id)
    await publish_resource(db_session, resource.id, kind="created")
    await db_session.commit()
    original = AsyncSession.commit
    calls = 0
    fail_commit = True

    async def flaky_commit(session):
        nonlocal calls
        calls += 1
        if fail_commit and calls == 2:
            raise RuntimeError("commit unavailable")
        return await original(session)

    monkeypatch.setattr(AsyncSession, "commit", flaky_commit)
    monkeypatch.setattr("app.services.publication_dispatch.dispatch_pending_publications", AsyncMock())
    files = [{"name": "Show.S01E01.mkv", "size": 100 * 1024 * 1024}]
    failed = await listing_service.reconcile_resource(resource.id, files=files)
    assert "commit unavailable" in failed.error
    await db_session.refresh(resource)
    assert resource.listing_reconcile_status == "failed"

    fail_commit = False
    recovered = await listing_service.reconcile_resource(resource.id, files=files, force=True)
    assert recovered.error is None
    await db_session.refresh(resource)
    assert resource.listing_reconcile_status == "done"
    assert resource.listing_reconcile_attempts == 2


async def test_late_listing_preserves_manual_resource_shape(db_session, sample_channel, monkeypatch):
    resource = await _resource(
        db_session, sample_channel.id, is_batch=True, batch_scope="season",
        episode_start=3, episode_end=7, episode_confidence="manual",
    )
    manual = ResourceFileAssignment(
        resource_id=resource.id, file_path="manual.mkv", source="manual", file_role="episode"
    )
    db_session.add(manual)
    await db_session.flush()
    await publish_resource(db_session, resource.id, kind="created")
    await db_session.commit()
    expand = AsyncMock()
    monkeypatch.setattr("app.services.bangumi_relations.expand_bangumi_series_graph", expand)
    monkeypatch.setattr("app.services.publication_dispatch.dispatch_pending_publications", AsyncMock())
    files = [
        {"name": f"Show/S{season}/Show S{season:02d}E{episode:02d}.mkv", "size": 100 * 1024 * 1024}
        for season in (1, 2) for episode in (1, 2)
    ]

    result = await listing_service.reconcile_resource(resource.id, files=files)

    assert result.error is None
    await db_session.refresh(resource, ["file_assignments"])
    assert resource.batch_scope == "season"
    assert (resource.episode_start, resource.episode_end) == (3, 7)
    assert resource.episode_confidence == "manual"
    manual_row = next(row for row in resource.file_assignments if row.file_path == "manual.mkv")
    assert manual_row.source == "manual"
    expand.assert_not_awaited()


async def test_late_listing_preserves_manual_links_only_shape(
    db_session, sample_channel, monkeypatch,
):
    from app.models.resource_work_link import ResourceWorkLink
    from app.models.series import TVSeries

    resource = await _resource(
        db_session, sample_channel.id, is_batch=True, batch_scope="season",
        episode_start=2, episode_end=6,
    )
    series = TVSeries(
        id=_uuid(), title_en="Show", title_cn="Show", content_type="tv", season_number=1
    )
    db_session.add(series)
    await db_session.flush()
    db_session.add(ResourceWorkLink(
        resource_id=resource.id, series_id=series.id, source="manual"
    ))
    await db_session.flush()
    await publish_resource(db_session, resource.id, kind="created")
    await db_session.commit()
    expand = AsyncMock()
    monkeypatch.setattr("app.services.bangumi_relations.expand_bangumi_series_graph", expand)
    monkeypatch.setattr("app.services.publication_dispatch.dispatch_pending_publications", AsyncMock())
    files = [
        {"name": f"Show/S{season}/Show S{season:02d}E{episode:02d}.mkv", "size": 100 * 1024 * 1024}
        for season in (1, 2) for episode in (1, 2)
    ]

    result = await listing_service.reconcile_resource(resource.id, files=files)

    assert result.error is None
    await db_session.refresh(resource)
    assert resource.batch_scope == "season"
    assert (resource.episode_start, resource.episode_end) == (2, 6)
    expand.assert_not_awaited()

