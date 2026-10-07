"""Ownership loss aborts a metadata batch instead of becoming a work error."""

from unittest.mock import AsyncMock

import pytest

from app import job_handlers
from app.models.movie import Movie
from app.services import task_queue


async def test_metadata_parent_cancellation_waits_for_child_cleanup(db_session):
    import asyncio

    from sqlalchemy import select

    from app.database import async_session_factory
    from app.services.fetch_service import _gather_metadata

    entered, cleaning, release, finished = (asyncio.Event() for _ in range(4))

    async def child():
        async with async_session_factory() as db:
            async with db.begin():
                db.add(Movie(title_cn="Cancelled metadata"))
                await db.flush()
                entered.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleaning.set()
                    await release.wait()
                    finished.set()

    parent = asyncio.create_task(_gather_metadata(child()))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        parent.cancel()
        await asyncio.wait_for(cleaning.wait(), 2)
        done, _ = await asyncio.wait({parent}, timeout=0.05)
        assert not done, "Cancelled parent exited before its child's cleanup"
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await parent
        assert finished.is_set()
        async with async_session_factory() as observer:
            assert (await observer.scalars(select(Movie))).all() == []
    finally:
        release.set()
        await asyncio.gather(parent, return_exceptions=True)


@pytest.mark.parametrize("phase", ["publication", "poster"])
@pytest.mark.parametrize("kind", ["movie", "series"])
async def test_resource_metadata_commit_boundaries(db_session, monkeypatch, phase, kind):
    import asyncio

    from sqlalchemy import select

    from app.database import async_session_factory
    from app.models.channel import Channel
    from app.models.file_resource import FileResource
    from app.models.resource_publication import ResourcePublication
    from app.models.series import TVSeries
    from app.models.work_collection import WorkCollection
    from app.services import fetch_service, resource_publication

    channel = Channel(name="Boundary", type="rss_feed", url="https://example.invalid",
                      field_mapping={}, metadata_agent_enabled=False)
    collection = WorkCollection(title_cn="Synthetic")
    db_session.add_all([channel, collection])
    await db_session.flush()
    original_poster = "https://example.invalid/poster.jpg"
    work = (Movie(title_cn="Synthetic", poster_url=original_poster) if kind == "movie"
            else TVSeries(title_cn="Synthetic", collection_id=collection.id, season_number=1, poster_url=original_poster))
    db_session.add(work)
    await db_session.flush()
    resource = FileResource(channel_id=channel.id, guid="boundary", title_raw="Synthetic",
                            search_title="Original", torrent_url="https://example.invalid/file.torrent")
    db_session.add(resource)
    await db_session.flush()
    await resource_publication.publish_resource(db_session, resource.id, kind="created")
    await db_session.commit()
    resource_id, channel_id, work_id = resource.id, channel.id, work.id
    expired = False
    reached = []
    publish = resource_publication.publish_resource

    async def guard():
        if expired:
            raise task_queue.ExecutionOwnershipLostError("Expired at commit boundary")

    async def link(db, row, channel):
        row.search_title = "Linked"
        setattr(row, "movie_id" if kind == "movie" else "series_id", work_id)

    async def publish_then_expire(db, *args, **kwargs):
        nonlocal expired
        result = await publish(db, *args, **kwargs)
        await db.flush()
        reached.append("publication")
        expired = phase == "publication"
        return result

    async def poster(url):
        nonlocal expired
        reached.append("poster")
        expired = True
        return "/posters/synthetic.jpg"

    monkeypatch.setattr(task_queue, "require_execution_ownership", guard)
    monkeypatch.setattr("app.services.torrent_inspect.ensure_torrent_cached", AsyncMock())
    monkeypatch.setattr("app.services.torrent_inspect.maybe_inspect_torrent", AsyncMock())
    monkeypatch.setattr(fetch_service, "fetch_and_link_metadata", link)
    monkeypatch.setattr(resource_publication, "publish_resource", publish_then_expire)
    monkeypatch.setattr("app.services.metadata_service.download_and_cache_poster", poster)
    with pytest.raises(task_queue.ExecutionOwnershipLostError):
        await fetch_service._process_resource_metadata(resource_id, channel_id, asyncio.Semaphore(1))
    assert reached == (["publication"] if phase == "publication" else ["publication", "poster"])
    async with async_session_factory() as observer:
        row = await observer.get(FileResource, resource_id)
        assert row.search_title == ("Original" if phase == "publication" else "Linked")
        assert getattr(row, "movie_id" if kind == "movie" else "series_id") == (None if phase == "publication" else work_id)
        publications = list(await observer.scalars(select(ResourcePublication)))
        assert len(publications) == (1 if phase == "publication" else 2)
        assert {event.kind for event in publications} == (
            {"created"} if phase == "publication" else {"created", "metadata"}
        )
        assert (await observer.get(type(work), work_id)).poster_url == original_poster


@pytest.mark.parametrize("scope", ["channel", "global"])
async def test_metadata_backfill_waits_for_siblings_before_raising(db_session, monkeypatch, scope):
    import asyncio

    from app.models.channel import Channel
    from app.models.file_resource import FileResource
    from app.services import fetch_service

    channel = Channel(name="Drain", type="rss_feed", url="https://example.invalid",
                      field_mapping={}, metadata_agent_enabled=True)
    db_session.add(channel)
    await db_session.flush()
    rows = [FileResource(channel_id=channel.id, guid=str(i), title_raw="Synthetic",
                         torrent_url="magnet:?xt=synthetic") for i in range(2)]
    db_session.add_all(rows)
    await db_session.commit()
    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    first_id = rows[0].id

    async def process(resource_id, *args, **kwargs):
        if resource_id == first_id:
            await entered.wait()
            raise task_queue.ExecutionOwnershipLostError("Expired sibling")
        entered.set()
        await release.wait()
        finished.set()

    monkeypatch.setattr(fetch_service, "_process_resource_metadata", process)
    operation = (fetch_service.backfill_unmatched_resources_global(db_session) if scope == "global"
                 else fetch_service._backfill_unmatched_resources(channel, db_session, asyncio.Semaphore(2), force=True))
    parent = asyncio.create_task(operation)
    try:
        await asyncio.wait_for(entered.wait(), 2)
        done, _ = await asyncio.wait({parent}, timeout=0.05)
        assert not done, "Parent returned while a metadata sibling was still running"
        release.set()
        with pytest.raises(task_queue.ExecutionOwnershipLostError):
            await parent
        assert finished.is_set()
    finally:
        release.set()
        await asyncio.gather(parent, return_exceptions=True)
        await asyncio.wait_for(finished.wait(), 2)


@pytest.mark.parametrize("agent_enabled", [False, True])
@pytest.mark.parametrize("raises_loss", [False, True])
async def test_resource_metadata_loss_rolls_back_flushed_fields_and_publication(
    db_session, monkeypatch, agent_enabled, raises_loss,
):
    import asyncio
    from types import SimpleNamespace

    from sqlalchemy import select

    from app.database import async_session_factory
    from app.models.channel import Channel
    from app.models.file_resource import FileResource
    from app.models.resource_publication import ResourcePublication
    from app.services import fetch_service

    channel = Channel(name="Loss", type="rss_feed", url="https://example.invalid",
                      field_mapping={}, metadata_agent_enabled=agent_enabled)
    db_session.add(channel)
    await db_session.flush()
    resource = FileResource(channel_id=channel.id, guid="loss", title_raw="Synthetic",
                            search_title="Original", torrent_url="https://example.invalid/file.torrent")
    db_session.add(resource)
    await db_session.commit()
    resource_id, channel_id = resource.id, channel.id
    expired = False

    async def guard():
        if expired:
            raise task_queue.ExecutionOwnershipLostError("Expired after metadata flush")

    async def change(db, row, channel):
        nonlocal expired
        row.search_title = "Stale replacement"
        await db.flush()
        expired = True
        if raises_loss:
            raise task_queue.ExecutionOwnershipLostError("Metadata provider lost ownership")

    async def agent_process(row, channel, db, **kwargs):
        await change(db, row, channel)

    monkeypatch.setattr(task_queue, "require_execution_ownership", guard)
    monkeypatch.setattr("app.services.torrent_inspect.ensure_torrent_cached", AsyncMock())
    monkeypatch.setattr("app.services.torrent_inspect.maybe_inspect_torrent", AsyncMock())
    monkeypatch.setattr(fetch_service, "fetch_and_link_metadata", change)
    monkeypatch.setattr("app.services.metadata_agent.get_agent", lambda: SimpleNamespace(process=agent_process))
    with pytest.raises(task_queue.ExecutionOwnershipLostError):
        await fetch_service._process_resource_metadata(resource_id, channel_id, asyncio.Semaphore(1))
    async with async_session_factory() as observer:
        assert (await observer.get(FileResource, resource_id)).search_title == "Original"
        assert (await observer.scalars(select(ResourcePublication))).all() == []


@pytest.mark.parametrize("phase", ["before", "during", "success", "failure"])
async def test_reparse_only_current_execution_acknowledges_request(db_session, monkeypatch, phase):
    from app.database import async_session_factory
    from app.models.channel import Channel
    from app.models.file_resource import FileResource
    from app.models.resource_reparse_request import ResourceReparseRequest
    from app.services.resource_reparse_requests import create_request
    from app.utils.time import utcnow

    channel = Channel(name="Reparse", type="rss_feed", url="https://example.invalid", field_mapping={})
    db_session.add(channel)
    await db_session.flush()
    marker = utcnow()
    resource = FileResource(channel_id=channel.id, guid="reparse", title_raw="Synthetic",
                            torrent_url="magnet:?xt=synthetic", confirmation_ignored_at=marker)
    db_session.add(resource)
    await db_session.commit()
    resource_id, channel_id = resource.id, channel.id
    request = await create_request(db_session, resource_id, channel_id)
    await db_session.commit()
    expired = phase == "before"

    async def guard():
        if expired:
            raise task_queue.ExecutionOwnershipLostError("Expired reparse")

    async def process(*args, **kwargs):
        nonlocal expired
        expired = phase == "during"
        if phase == "failure":
            raise RuntimeError("Metadata unavailable")

    process_mock = AsyncMock(side_effect=process)
    monkeypatch.setattr(task_queue, "require_execution_ownership", guard)
    monkeypatch.setattr("app.services.fetch_service._process_resource_metadata", process_mock)
    payload = {"request_id": request.id, "resource_id": resource_id, "channel_id": channel_id}
    if phase in {"before", "during"}:
        with pytest.raises(task_queue.ExecutionOwnershipLostError):
            await job_handlers._handle_reprocess_resource_metadata(payload)
    elif phase == "failure":
        with pytest.raises(RuntimeError, match="Metadata unavailable"):
            await job_handlers._handle_reprocess_resource_metadata(payload)
    else:
        assert await job_handlers._handle_reprocess_resource_metadata(payload) == {"status": "done"}
    assert process_mock.await_count == (0 if phase == "before" else 1)
    async with async_session_factory() as observer:
        saved = await observer.get(FileResource, resource_id)
        assert saved.confirmation_ignored_at == marker
        pending = await observer.get(ResourceReparseRequest, request.id)
        assert (pending is not None) == (phase in {"before", "during"})
    if phase in {"before", "during"}:
        expired = False
        monkeypatch.setattr("app.services.fetch_service._process_resource_metadata", AsyncMock())
        assert await job_handlers._handle_reprocess_resource_metadata(payload) == {"status": "done"}
        async with async_session_factory() as observer:
            assert (await observer.get(FileResource, resource_id)).confirmation_ignored_at == marker
            assert await observer.get(ResourceReparseRequest, request.id) is None


@pytest.mark.parametrize("phase", ["before_batch", "during_refresh"])
async def test_metadata_batch_stops_on_queue_loss(db_session, monkeypatch, phase):
    first, second = Movie(title_cn="First"), Movie(title_cn="Second")
    db_session.add_all([first, second])
    await db_session.commit()
    error = task_queue.ExecutionOwnershipLostError("Metadata worker lost ownership")
    guard = AsyncMock(side_effect=error if phase == "before_batch" else None)
    refresh = AsyncMock(side_effect=error)
    monkeypatch.setattr(task_queue, "require_execution_ownership", guard)
    monkeypatch.setattr("app.services.metadata_search.refresh_work_by_source", refresh)
    with pytest.raises(task_queue.ExecutionOwnershipLostError):
        await job_handlers._refresh_works_batch(
            [{"id": first.id, "content_type": "movie"}, {"id": second.id, "content_type": "movie"}],
            "tmdb",
        )
    assert refresh.await_count == (0 if phase == "before_batch" else 1)


async def test_refresh_losing_ownership_during_search_does_not_commit(db_session, monkeypatch):
    from sqlalchemy import select

    from app.models.work_external_id import WorkExternalId
    from app.services import metadata_search
    from tests.unit.test_metadata_search import _external_candidate

    work = Movie(title_cn="Original")
    db_session.add(work)
    await db_session.commit()
    work_id = work.id
    expired = False
    candidate = _external_candidate("Replacement")
    candidate.content_type = "movie"

    async def search(*args, **kwargs):
        nonlocal expired
        expired = True
        return [candidate]

    async def guard():
        if expired:
            raise task_queue.ExecutionOwnershipLostError("Expired during metadata search")

    monkeypatch.setattr(metadata_search, "search_metadata_candidates", search)
    monkeypatch.setattr(task_queue, "require_execution_ownership", guard)
    with pytest.raises(task_queue.ExecutionOwnershipLostError):
        await metadata_search.refresh_work_by_source(
            db_session, work, "movie", "tmdb", only_missing=False,
        )
    await db_session.rollback()
    saved = await db_session.get(Movie, work_id)
    assert saved.title_cn == "Original"
    assert (await db_session.scalars(select(WorkExternalId))).all() == []


@pytest.mark.parametrize("phase", ["poster", "identity_flush"])
async def test_refresh_batch_rolls_back_partial_metadata_on_loss(db_session, monkeypatch, phase):
    from sqlalchemy import select

    from app.database import async_session_factory
    from app.models.work_external_id import WorkExternalId
    from app.services import metadata_search
    from tests.unit.test_metadata_search import _external_candidate

    work = Movie(title_cn="Original")
    db_session.add(work)
    await db_session.commit()
    work_id = work.id
    expired = False
    reached = []
    candidate = _external_candidate("Replacement", poster_url="https://example.invalid/poster.jpg")
    candidate.content_type = "movie"
    add_identity = metadata_search.add_external_id

    async def poster(url):
        nonlocal expired
        reached.append("poster")
        expired = phase == "poster"
        return "/posters/fixture.jpg"

    async def identity(db, *args):
        nonlocal expired
        result = await add_identity(db, *args)
        await db.flush()
        reached.append("identity_flush")
        expired = phase == "identity_flush"
        return result

    async def guard():
        if expired:
            raise task_queue.ExecutionOwnershipLostError("Expired after partial metadata application")

    monkeypatch.setattr(metadata_search, "search_metadata_candidates", AsyncMock(return_value=[candidate]))
    monkeypatch.setattr(metadata_search, "download_and_cache_poster", poster)
    monkeypatch.setattr(metadata_search, "add_external_id", identity)
    monkeypatch.setattr(task_queue, "require_execution_ownership", guard)
    with pytest.raises(task_queue.ExecutionOwnershipLostError):
        await job_handlers._refresh_works_batch(
            [{"id": work_id, "content_type": "movie"}], "tmdb", strategy="sync_non_manual",
        )
    assert reached == (["poster"] if phase == "poster" else ["poster", "identity_flush"])
    # Observe a fresh transaction: rollback must come from the production
    # batch's committed_session, not cleanup performed by the test.
    async with async_session_factory() as reader:
        saved = await reader.get(Movie, work_id)
        assert saved.title_cn == "Original" and saved.poster_url is None
        assert (await reader.scalars(select(WorkExternalId))).all() == []


@pytest.mark.parametrize("override", [False, True])
async def test_background_refresh_respects_concurrent_manual_edit(db_session, monkeypatch, override):
    from app.database import async_session_factory
    from app.services import metadata_search
    from tests.unit.test_metadata_search import _external_candidate

    work = Movie(title_cn="Original")
    db_session.add(work)
    await db_session.commit()
    work_id = work.id
    candidate = _external_candidate("Automatic", poster_url="https://example.invalid/poster.jpg")
    candidate.content_type = "movie"
    edited = False

    async def poster(_url):
        nonlocal edited
        if not edited:
            async with async_session_factory() as editor:
                latest = await editor.get(Movie, work_id)
                latest.title_cn = "Manual"
                latest.manually_edited_fields = ["title_cn"]
                await editor.commit()
            edited = True
        return "/posters/fixture.jpg"

    monkeypatch.setattr(metadata_search, "search_metadata_candidates", AsyncMock(return_value=[candidate]))
    monkeypatch.setattr(metadata_search, "download_and_cache_poster", poster)
    [result] = await job_handlers._refresh_works_batch(
        [{"id": work_id, "content_type": "movie"}], "tmdb", strategy="sync_non_manual",
        override_manual_edits=override,
    )
    assert edited and result["found"], result
    async with async_session_factory() as reader:
        latest = await reader.get(Movie, work_id)
        assert latest.title_cn == ("Automatic" if override else "Manual")
        assert latest.manually_edited_fields == ["title_cn"]
        assert latest.poster_url == "/posters/fixture.jpg"


async def test_concurrent_background_refreshes_finish_after_write_conflict(db_session, monkeypatch):
    import asyncio

    from app.services import metadata_search
    from tests.unit.test_metadata_search import _external_candidate

    work = Movie(title_cn="Original")
    db_session.add(work)
    await db_session.commit()
    candidate = _external_candidate("Automatic", poster_url="https://example.invalid/poster.jpg")
    candidate.content_type = "movie"
    barrier = asyncio.Barrier(2)
    calls = 0

    async def poster(_url):
        nonlocal calls
        calls += 1
        if calls <= 2:
            await asyncio.wait_for(barrier.wait(), 3)
        return "/posters/fixture.jpg"

    monkeypatch.setattr(metadata_search, "search_metadata_candidates", AsyncMock(return_value=[candidate]))
    monkeypatch.setattr(metadata_search, "download_and_cache_poster", poster)
    item = {"id": work.id, "content_type": "movie"}
    results = await asyncio.gather(*(
        job_handlers._refresh_works_batch([item], "tmdb", strategy="sync_non_manual") for _ in range(2)
    ))
    assert all(result[0]["found"] for result in results), results


@pytest.mark.parametrize("change", ["manual_date", "delete", "season", "collection"])
async def test_specials_fallback_preserves_concurrent_changes(db_session, monkeypatch, change):
    from datetime import date

    from app.database import async_session_factory
    from app.models.series import TVSeries
    from app.models.work_collection import WorkCollection
    from app.services import metadata_search

    collection = WorkCollection(title_cn="Series")
    db_session.add(collection)
    await db_session.flush()
    regular = TVSeries(title_cn="Season 1", season_number=1, collection_id=collection.id,
                       start_date=date(2020, 1, 1))
    special = TVSeries(title_cn="Special", season_number=0, collection_id=collection.id)
    db_session.add_all([regular, special])
    await db_session.commit()
    special_id = special.id
    fallback = metadata_search._collection_fallback_start_date

    async def compute_then_edit(*args):
        result = await fallback(*args)
        async with async_session_factory() as editor:
            latest = await editor.get(TVSeries, special_id)
            if change == "manual_date":
                latest.start_date = date(2024, 5, 6)
                latest.manually_edited_fields = ["start_date"]
            elif change == "delete":
                await editor.delete(latest)
            elif change == "season":
                latest.season_number = 2
            else:
                replacement = WorkCollection(title_cn="Different series")
                editor.add(replacement)
                await editor.flush()
                latest.collection_id = replacement.id
            await editor.commit()
        return result

    monkeypatch.setattr(metadata_search, "_collection_fallback_start_date", compute_then_edit)
    [result] = await job_handlers._refresh_works_batch(
        [{"id": special_id, "content_type": "tv"}], "tmdb",
    )
    assert result["found"] is (change != "delete")
    assert result["applied"] == []
    async with async_session_factory() as reader:
        latest = await reader.get(TVSeries, special_id)
        if change == "delete":
            assert latest is None
        elif change == "manual_date":
            assert latest.start_date == date(2024, 5, 6)
            assert latest.manually_edited_fields == ["start_date"]
        else:
            assert latest.start_date is None
            if change == "season":
                assert latest.season_number == 2
            else:
                assert latest.collection_id != collection.id


@pytest.mark.parametrize("change", ["season", "collection", "delete"])
async def test_refresh_rejects_candidate_for_changed_work_scope(db_session, monkeypatch, change):
    from sqlalchemy import select

    from app.database import async_session_factory
    from app.models.series import TVSeries
    from app.models.work_collection import WorkCollection
    from app.models.work_external_id import WorkExternalId
    from app.services import metadata_search
    from tests.unit.test_metadata_search import _external_candidate

    collection = WorkCollection(title_cn="Original collection")
    db_session.add(collection)
    await db_session.flush()
    work = TVSeries(title_cn="Original", season_number=1, collection_id=collection.id)
    db_session.add(work)
    await db_session.commit()
    work_id = work.id
    candidate = _external_candidate("Old candidate", poster_url="https://example.invalid/poster.jpg")

    async def poster(_url):
        async with async_session_factory() as editor:
            current = await editor.get(TVSeries, work_id)
            if change == "delete":
                await editor.delete(current)
            elif change == "season":
                current.season_number = 2
            else:
                other = WorkCollection(title_cn="New collection")
                editor.add(other)
                await editor.flush()
                current.collection_id = other.id
            await editor.commit()
        return "/posters/old.jpg"

    monkeypatch.setattr(metadata_search, "search_metadata_candidates", AsyncMock(return_value=[candidate]))
    monkeypatch.setattr(metadata_search, "download_and_cache_poster", poster)
    [result] = await job_handlers._refresh_works_batch(
        [{"id": work_id, "content_type": "tv"}], "tmdb", strategy="sync_non_manual",
    )
    assert result["found"] is False, result
    if change != "delete":
        assert result.get("scope_changed") is True
        assert not result.get("identity_conflict")
    async with async_session_factory() as reader:
        current = await reader.get(TVSeries, work_id)
        if change == "delete":
            assert current is None
        else:
            assert current.title_cn == "Original" and current.poster_url is None
        assert (await reader.scalars(select(WorkExternalId))).all() == []
