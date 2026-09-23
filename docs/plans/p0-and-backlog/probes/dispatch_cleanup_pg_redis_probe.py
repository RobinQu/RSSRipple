# ruff: noqa: E402
"""Upgrade a legacy schema through production startup, then exercise raw SQL."""

import asyncio
import json
import os
import subprocess
import uuid
from pathlib import Path

probe_root = Path(os.environ["PROBE_ROOT"]).resolve()
assert probe_root.is_relative_to(Path("/tmp"))
project = os.environ.get("PROBE_POSTGRES_PROJECT")
if project:
    assert project.startswith("rssripple-v14-schema-")
    [state] = json.loads(subprocess.check_output(["docker", "inspect", f"{project}-postgres-1"]))
    assert state["Config"]["Labels"]["com.docker.compose.project"] == project
    address = state["NetworkSettings"]["Networks"][project + "_isolated"]["IPAddress"]
    os.environ["DATABASE_URL"] = f"postgresql+asyncpg://probe:probe@{address}:5432/probe"
else:
    assert os.environ["DATABASE_URL"] == f"sqlite+aioturso:///{probe_root / 'legacy.db'}"

from datetime import timedelta
from unittest.mock import patch

import redis.asyncio as redis
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession

import app.database as database  # noqa: E402
import app.models  # noqa: E402,F401
from app.models.channel import Channel
from app.models.download_dispatch import DownloadDispatch  # noqa: E402
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.services.download_dispatch import DispatchReservation, persist_dispatch_result
from app.services.download_dispatch_cleanup import cleanup_dispatch_reservations
from app.services.task_queue import RedisQueue
from app.utils.time import utcnow


def row(name):
    return DownloadDispatch(
        id=str(uuid.uuid4()),
        operation_key=uuid.uuid4().hex * 2,
        task_id=str(uuid.uuid4()),
        job_key=name,
        job_id="old",
        parameters={},
        created_at=utcnow() - timedelta(days=8),
    )


async def main():
    [state] = json.loads(subprocess.check_output(["docker", "inspect", f"{project}-redis-1"]))
    assert state["Config"]["Labels"]["com.docker.compose.project"] == project
    host = state["NetworkSettings"]["Networks"][project + "_isolated"]["IPAddress"]
    client = redis.from_url(f"redis://{host}:6379/0", decode_responses=True)
    assert await client.dbsize() == 0
    queue = RedisQueue(redis_client=client)
    waiting, release = asyncio.Event(), asyncio.Event()
    children = []
    try:
        fixtures = [row(name) for name in ["running", "queued", "absent", "done", "replaced"]]
        async with database.async_session_factory() as db:
            db.add_all(fixtures)
            channel = Channel(name="cleanup", type="rss_feed", url="https://example.invalid", field_mapping={})
            downloader = DownloaderInstance(
                name="cleanup", type="mock", url="http://example.invalid", download_dir="/downloads"
            )
            db.add_all([channel, downloader])
            await db.flush()
            resource = FileResource(
                channel_id=channel.id, guid="cleanup", title_raw="Synthetic", torrent_url="magnet:?xt=synthetic"
            )
            db.add(resource)
            await db.commit()
            resource_id, downloader_id = resource.id, downloader.id
        for name in ["running", "queued", "done"]:
            await client.hset("rssripple:job:" + name, mapping={"job_id": "old", "status": name})
        await client.hset("rssripple:job:replaced", mapping={"job_id": "new", "status": "running"})
        await client.set("rssripple:active:replaced", "new")
        assert await cleanup_dispatch_reservations(database.engine, queue) == 3
        async with database.async_session_factory() as db:
            survivors = set(
                await db.scalars(select(DownloadDispatch.job_key).where(DownloadDispatch.job_key.is_not(None)))
            )
            assert survivors == {"running", "queued"}
            racing = row("race")
            db.add(racing)
            await db.commit()
        await client.hset("rssripple:job:race", mapping={"job_id": "old", "status": "done"})
        observed_lock = False

        async def writer():
            async with AsyncSession(database.engine) as db, db.begin():
                await persist_dispatch_result(
                    db,
                    DispatchReservation(racing.operation_key, racing.task_id, False),
                    {
                        "file_resource_id": resource_id,
                        "downloader_id": downloader_id,
                        "download_dir": "/downloads",
                        "status": "downloading",
                    },
                )
                waiting.set()
                await release.wait()

        async def observe_lock():
            nonlocal observed_lock
            try:
                async with asyncio.timeout(5):
                    while not observed_lock:
                        async with database.engine.connect() as conn:
                            observed_lock = await conn.scalar(
                                text(
                                    "SELECT EXISTS (SELECT 1 FROM pg_stat_activity WHERE wait_event_type = 'Lock' "
                                    "AND query LIKE 'DELETE FROM download_dispatches%')"
                                )
                            )
                        if not observed_lock:
                            await asyncio.sleep(0.02)
            finally:
                release.set()

        original = queue.job_is_retired

        async def interleave(key, job_id):
            result = await original(key, job_id)
            if key == "race":
                children.append(asyncio.create_task(writer()))
                await asyncio.wait_for(waiting.wait(), 5)
                children.append(asyncio.create_task(observe_lock()))
            return result

        with patch.object(queue, "job_is_retired", interleave):
            deleted = await cleanup_dispatch_reservations(database.engine, queue)
        await asyncio.gather(*children)
        assert observed_lock and deleted == 0
        async with database.async_session_factory() as db:
            assert await db.get(DownloadTask, racing.task_id) is not None
            assert (await db.get(DownloadDispatch, racing.id)).settled is True
        Path(os.environ["PROBE_RESULT_PATH"]).write_text(
            json.dumps(
                {
                    "backend": "actual PostgreSQL and Redis",
                    "data": "synthetic; no RPC",
                    "retired_orphans_deleted": 3,
                    "active_orphans_retained": sorted(survivors),
                    "delete_waited_on_writer_lock": observed_lock,
                    "concurrent_task_and_reservation_preserved": True,
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        release.set()
        if children:
            await asyncio.gather(*children, return_exceptions=True)
        await client.aclose()
        await database.engine.dispose()


asyncio.run(main())
