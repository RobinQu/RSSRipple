"""Execute the captured association cleanup cases on isolated PostgreSQL."""

import os
import uuid
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from tests.integration.organize.test_cleanup_associations import (
    test_cleanup_preserves_authoritative_associations as _cleanup_case,
)


@pytest.fixture
async def cleanup_pg_session():
    admin_url = os.environ.get("CLEANUP_TEST_POSTGRES_URL") or os.environ.get("QUEUE_RECOVERY_POSTGRES_URL")
    if not admin_url:
        if os.environ.get("QUEUE_RECOVERY_REQUIRED") == "1":
            pytest.fail("Cleanup gate requires isolated PostgreSQL")
        pytest.skip("Run isolated integration gate for PostgreSQL cleanup")
    parts = urlsplit(admin_url)
    assert parts.path in {"/queue_recovery", "/cleanup_probe"}, "Refuse a non-test administrator database"
    name = "cleanup_association_" + uuid.uuid4().hex
    admin = await asyncpg.connect(admin_url)
    engine = None
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
        engine = create_async_engine(urlunsplit(parts._replace(scheme="postgresql+asyncpg", path="/" + name)))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            yield session
    finally:
        if engine is not None:
            await engine.dispose()
        await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        await admin.close()


@pytest.mark.parametrize("handled", ["unresolved", "collection", "multiwork"])
@pytest.mark.parametrize("manual_cleanup", [False, True])
async def test_postgres_cleanup_preserves_associations(cleanup_pg_session, handled, manual_cleanup, record_testsuite_property):
    await _cleanup_case(cleanup_pg_session, handled, manual_cleanup, record_testsuite_property)


async def test_cleanup_does_not_erase_associations_committed_while_waiting(cleanup_pg_session):
    import asyncio

    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models.channel import Channel
    from app.models.file_resource import FileResource
    from app.models.movie import Movie
    from app.models.resource_work_link import ResourceWorkLink
    from app.schemas.file_resource import ResourceAssociationUpdateRequest
    from app.services.resource_association import apply_association_update
    from app.services.resource_cleanup import cleanup_channel_unresolved_resources
    from tests.unit.test_resource_cleanup import _make_resource

    db = cleanup_pg_session
    channel = Channel(id=str(uuid.uuid4()), name="Synthetic concurrent cleanup", type="rss_feed",
                      url="https://example.invalid/rss", field_mapping={})
    movies = [Movie(id=str(uuid.uuid4()), title_cn=f"Synthetic movie {i}") for i in range(2)]
    db.add_all([channel, *movies])
    await db.flush()
    resource = _make_resource(channel.id)
    resource.is_batch = True
    resource.batch_scope = "movies"
    resource.season_ranges = []
    db.add(resource)
    await db.commit()
    resource_id, channel_id = resource.id, channel.id
    body = ResourceAssociationUpdateRequest(
        is_batch=True, works=[dict(work_type="movie", work_id=m.id) for m in movies],
        assignments=[dict(file_path=f"synthetic-{i}.mkv", work_type="movie", work_id=m.id)
                     for i, m in enumerate(movies)],
    )
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    ready = asyncio.Event()
    cleaner_pid = None

    async def clean():
        nonlocal cleaner_pid
        async with factory() as cleaner:
            cleaner_pid = await cleaner.scalar(text("SELECT pg_backend_pid()"))
            ready.set()
            deleted = await cleanup_channel_unresolved_resources(cleaner, channel_id, force=True)
            await cleaner.commit()
            return deleted

    async with factory() as writer:
        target = await writer.get(FileResource, resource_id)
        await apply_association_update(writer, target, body)
        await writer.flush()
        task = asyncio.create_task(clean())
        try:
            await asyncio.wait_for(ready.wait(), timeout=5)
            for _ in range(200):
                blockers = await db.scalar(text("SELECT cardinality(pg_blocking_pids(:pid))"), {"pid": cleaner_pid})
                if blockers:
                    break
                if task.done():
                    break
                await asyncio.sleep(0.01)
            # A safe implementation may skip the locked resource immediately.
            # Otherwise it must reassess associations after the writer commits.
            assert blockers or task.done(), "Cleanup never reached a known concurrency boundary"
            await writer.commit()
            deleted = await asyncio.wait_for(task, timeout=5)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
    assert deleted == 0
    assert await db.scalar(select(FileResource.id).where(FileResource.id == resource_id)) == resource_id
    assert len((await db.scalars(select(ResourceWorkLink).where(ResourceWorkLink.resource_id == resource_id))).all()) == 2


async def test_cleanup_batches_skip_locked_rows_and_rollback_atomically(cleanup_pg_session):
    from sqlalchemy import func, select, text

    from app.models.channel import Channel
    from app.models.file_resource import FileResource
    from app.models.work_collection import WorkCollection
    from app.services.resource_cleanup import cleanup_channel_unresolved_resources
    from tests.unit.test_resource_cleanup import _make_resource

    db = cleanup_pg_session
    assert await db.scalar(text("SHOW transaction_isolation")) == "read committed"
    channel = Channel(id=str(uuid.uuid4()), name="Synthetic batch cleanup", type="rss_feed",
                      url="https://example.invalid/rss", field_mapping={})
    collection = WorkCollection(id=str(uuid.uuid4()), title_cn="Synthetic protected collection")
    db.add_all([channel, collection])
    await db.flush()
    ordinary = [_make_resource(channel.id) for _ in range(501)]
    busy = _make_resource(channel.id)
    protected = _make_resource(channel.id)
    protected.collection_id = collection.id
    db.add_all([*ordinary, busy, protected])
    await db.commit()
    channel_id, busy_id, protected_id, first_id = channel.id, busy.id, protected.id, ordinary[0].id
    factory = async_sessionmaker(db.bind, expire_on_commit=False)
    async with factory() as locker:
        await locker.execute(select(FileResource.id).where(FileResource.id == busy_id).with_for_update())
        async with factory() as cleaner:
            assert await cleanup_channel_unresolved_resources(cleaner, channel_id, force=True) == 501
            assert await cleaner.scalar(select(func.count()).select_from(FileResource)) == 2
            await cleaner.rollback()
        # Both deletion batches roll back together. NOWAIT proves that the
        # cleanup's parent-row locks were released, not only its ORM objects.
        await locker.execute(select(FileResource.id).where(FileResource.id == first_id).with_for_update(nowait=True))
        assert await locker.scalar(select(func.count()).select_from(FileResource)) == 503
        await locker.rollback()
        await locker.execute(select(FileResource.id).where(FileResource.id == busy_id).with_for_update())
        async with factory() as cleaner:
            assert await cleanup_channel_unresolved_resources(cleaner, channel_id, force=True) == 501
            await cleaner.commit()
        assert set((await locker.scalars(select(FileResource.id))).all()) == {busy_id, protected_id}
        await locker.rollback()
    # A later sweep can reclaim a previously busy but still unresolved row.
    async with factory() as cleaner:
        assert await cleanup_channel_unresolved_resources(cleaner, channel_id, force=True) == 1
        await cleaner.commit()
        assert (await cleaner.scalars(select(FileResource.id))).all() == [protected_id]
