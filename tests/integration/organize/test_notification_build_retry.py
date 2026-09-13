"""Real persistence checks; synthetic task metadata, no downloaded media."""
import uuid
from datetime import datetime, timedelta

from sqlalchemy import delete, select

from app.models.download_notification import DownloadNotification
from app.models.download_task import DownloadTask
from app.models.notification_build_failure import NotificationBuildFailure
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services import notification_build as build
from tests.integration.organize.test_organize_pipeline import _seed_chain


async def seed_retry_task(db):
    collection = WorkCollection(id=str(uuid.uuid4()), title_cn="synthetic retry fixture")
    db.add(collection)
    work = TVSeries(id=str(uuid.uuid4()), title_cn="synthetic retry fixture", season_number=1,
                    collection_id=collection.id, content_type="tv")
    chain = await _seed_chain(db, work=work, resource_kw={"season": 1, "episode": 1},
                              download_dir="/synthetic/no-media")
    chain.task.transmission_torrent_id = None
    await db.commit()
    return chain.task.id


async def test_backoff_cap_success_and_cancelled_task(db_session, session_factory, monkeypatch):
    task_id = await seed_retry_task(db_session)
    now = datetime(2026, 9, 13)
    monkeypatch.setattr(build, "utcnow", lambda: now)
    for attempt in range(1, 11):
        await build._record_failure(task_id, "synthetic failure")
        async with session_factory() as db:
            failure = await db.scalar(select(NotificationBuildFailure))
            assert failure.attempt_count == attempt
            assert failure.next_attempt_at == now + timedelta(seconds=min(1800, 30 * 2 ** (attempt - 1)))
        assert await build._attempt(task_id) is None
    now += timedelta(seconds=1801)
    notification_id = await build._attempt(task_id)
    assert notification_id
    await build._record_failure(task_id, "late error after success")
    async with session_factory() as db:
        assert await db.scalar(select(NotificationBuildFailure.id)) is None
        notification = await db.get(DownloadNotification, notification_id)
        assert notification.download_task_id == task_id
    cancelled_id = await seed_retry_task(db_session)
    async with session_factory() as db:
        task = await db.get(DownloadTask, cancelled_id)
        task.status = "cancelled"
        await db.commit()
    await build._record_failure(cancelled_id, "late error after cancellation")
    assert await build._attempt(cancelled_id) is None
    async with session_factory() as db:
        assert await db.scalar(select(NotificationBuildFailure.id)) is None
        await db.execute(delete(DownloadTask).where(DownloadTask.id == cancelled_id))
        await db.commit()
    await build._record_failure(cancelled_id, "late error after deletion")
    assert await build._attempt(cancelled_id) is None


async def test_failure_table_upgrade_is_idempotent_and_cascades(db_session, db_engine, session_factory):
    from sqlalchemy import inspect

    from app.database import Base, _apply_light_migrations

    task_id = await seed_retry_task(db_session)
    async with db_engine.begin() as connection:
        await connection.run_sync(NotificationBuildFailure.__table__.drop)
        assert not await connection.run_sync(lambda c: inspect(c).has_table("notification_build_failures"))
        await connection.run_sync(Base.metadata.create_all)
        await _apply_light_migrations(connection)
    await build._record_failure(task_id, "preserve this diagnostic across upgrade")
    async with session_factory() as db:
        original = await db.scalar(select(NotificationBuildFailure))
        original_state = (original.id, original.attempt_count, original.next_attempt_at, original.error_message)
    async with db_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await _apply_light_migrations(connection)
    async with session_factory() as db:
        failure = await db.scalar(select(NotificationBuildFailure))
        assert (failure.id, failure.attempt_count, failure.next_attempt_at, failure.error_message) == original_state
        assert (await db.get(DownloadTask, task_id)).status == "completed"
        await db.execute(delete(DownloadTask).where(DownloadTask.id == task_id))
        await db.commit()
    async with session_factory() as db:
        assert await db.scalar(select(NotificationBuildFailure.id)) is None
