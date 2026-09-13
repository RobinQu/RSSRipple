"""Dedicated disposable PostgreSQL only; real processes and transactions."""
import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

from app.models.notification_build_failure import NotificationBuildFailure
from sqlalchemy import MetaData, event, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.util.concurrency import await_only
from tests.integration.organize.test_notification_build_retry import seed_retry_task

import app.database as database
import app.models  # noqa: F401
from app.services import notification_build as build
from app.services.scheduler import _process_download_notifications


async def main():
    url = make_url(os.environ['DATABASE_URL'])
    assert url.host == '127.0.0.1' and url.database == url.username == url.password == 'organize_test'
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    database.engine, database.async_session_factory = engine, factory
    now = datetime(2026, 9, 13)
    build.utcnow = lambda: now
    try:
        if len(sys.argv) > 1:
            for _ in range(3):
                await build._record_failure(sys.argv[1], 'synthetic process failure')
            return
        async with engine.begin() as connection:
            await connection.run_sync(database.Base.metadata.drop_all)
            legacy = MetaData()
            for table in database.Base.metadata.sorted_tables:
                if table.name != 'notification_build_failures':
                    table.to_metadata(legacy)
            await connection.run_sync(legacy.create_all)
            assert not await connection.run_sync(lambda c: inspect(c).has_table('notification_build_failures'))
            await connection.run_sync(database.Base.metadata.create_all)
            await database._apply_light_migrations(connection)
        async with factory() as session:
            task_id = await seed_retry_task(session)
        processes = [await asyncio.create_subprocess_exec(sys.executable, __file__, task_id) for _ in range(2)]
        codes = await asyncio.gather(*(p.wait() for p in processes))
        assert codes == [0, 0], codes
        async with factory() as session:
            [failure] = (await session.scalars(select(NotificationBuildFailure))).all()
            assert failure.attempt_count == 6
            assert failure.next_attempt_at == now + timedelta(seconds=960)
            failure_id = failure.id
        # Repeat actual startup migration without losing retry state.
        async with engine.begin() as connection:
            await connection.run_sync(database.Base.metadata.create_all)
            await database._apply_light_migrations(connection)
        async with factory() as session:
            failure = await session.get(NotificationBuildFailure, failure_id)
            assert failure.attempt_count == 6
        now += timedelta(seconds=961)
        notification_id = await build._attempt(task_id)
        assert notification_id
        await build._record_failure(task_id, 'late failure after success')
        async with factory() as session:
            assert await session.scalar(select(NotificationBuildFailure.id)) is None
            race_task_id = await seed_retry_task(session)
        reached_insert, release_insert = asyncio.Event(), asyncio.Event()

        def pause_failure_insert(connection, cursor, statement, parameters, context, executemany):
            if statement.startswith('INSERT INTO notification_build_failures'):
                reached_insert.set()
                await_only(release_insert.wait())

        event.listen(engine.sync_engine, 'before_cursor_execute', pause_failure_insert)
        late_failure = asyncio.create_task(build._record_failure(race_task_id, 'late concurrent failure'))
        try:
            await asyncio.wait_for(reached_insert.wait(), 10)
            raced_notification_id = await build._attempt(race_task_id)
            assert raced_notification_id
        finally:
            release_insert.set()
            await asyncio.wait_for(late_failure, 10)
            event.remove(engine.sync_engine, 'before_cursor_execute', pause_failure_insert)
        async with factory() as session:
            failure = await session.scalar(select(NotificationBuildFailure))
            assert failure.download_task_id == race_task_id
        await _process_download_notifications()
        async with factory() as session:
            assert await session.scalar(select(NotificationBuildFailure.id)) is None
        print(json.dumps({'backend':'PostgreSQL 16','data':'synthetic task metadata; no media',
            'process_exit_codes':codes,'concurrent_attempts':6,'retry_seconds':960,
            'old_schema_upgrade_and_repeat_preserve_state':True,
            'real_success_commit_before_late_failure_insert_cleanup':True},indent=2))
    finally:
        await engine.dispose()


asyncio.run(main())
