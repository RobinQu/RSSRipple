"""Retention must not remove a dispatch that can still run or has a task."""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock

import fakeredis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.download_dispatch import DownloadDispatch
from app.services import download_dispatch_cleanup as cleanup
from app.services.task_queue import RedisQueue
from app.utils.time import utcnow


def reservation(name, *, age=8, known=True):
    return DownloadDispatch(
        operation_key=uuid.uuid4().hex * 2, task_id=str(uuid.uuid4()),
        job_key=name if known else None, job_id="old" if known else None,
        parameters={}, created_at=utcnow() - timedelta(days=age),
    )


async def test_cleanup_preserves_recoverable_and_unknown_jobs(db_session, monkeypatch):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisQueue(redis_client=client)
    names = ["running", "queued", "absent", "done", "failed", "replaced", "active-orphan", "unknown", "young", "anonymous"]
    rows = [reservation(name, age=1 if name == "young" else 8, known=name != "anonymous") for name in names]
    db_session.add_all(rows)
    await db_session.commit()
    for name in ["running", "queued", "done", "failed", "unknown"]:
        await client.hset("rssripple:job:" + name, mapping={"job_id": "old", "status": name})
    await client.hset("rssripple:job:replaced", mapping={"job_id": "new", "status": "running"})
    await client.set("rssripple:active:replaced", "new")
    await client.set("rssripple:active:active-orphan", "old")
    monkeypatch.setattr(cleanup, "PAGE_SIZE", 2)
    assert await cleanup.cleanup_dispatch_reservations(db_session.bind, queue) == 4
    async with AsyncSession(db_session.bind) as check:
        remaining = set(await check.scalars(select(DownloadDispatch.job_key)))
    assert remaining == {"running", "queued", "active-orphan", "unknown", "young", None}
    await client.aclose()


async def test_cleanup_skips_when_queue_read_fails(db_session):
    row = reservation("outage")
    db_session.add(row)
    await db_session.commit()
    queue = AsyncMock()
    queue.job_is_retired.side_effect = ConnectionError("Synthetic Redis failure")
    assert await cleanup.cleanup_dispatch_reservations(db_session.bind, queue) == 0
    async with AsyncSession(db_session.bind) as check:
        assert await check.get(DownloadDispatch, row.id) is not None


async def test_cleanup_rechecks_settlement_after_queue_read(db_session):
    row = reservation("racing-settlement")
    db_session.add(row)
    await db_session.commit()

    async def settle_before_delete(key, job_id):
        async with AsyncSession(db_session.bind) as other, other.begin():
            await other.execute(update(DownloadDispatch).where(DownloadDispatch.id == row.id).values(settled=True))
        return True

    queue = AsyncMock()
    queue.job_is_retired.side_effect = settle_before_delete
    assert await cleanup.cleanup_dispatch_reservations(db_session.bind, queue) == 0
    async with AsyncSession(db_session.bind) as check:
        assert (await check.get(DownloadDispatch, row.id)).settled is True


async def test_cleanup_retains_reservation_with_existing_task(db_session):
    from app.models.channel import Channel
    from app.models.download_task import DownloadTask
    from app.models.downloader import DownloaderInstance
    from app.models.file_resource import FileResource

    channel = Channel(name="retained", type="rss_feed", url="https://example.invalid", field_mapping={})
    downloader = DownloaderInstance(name="retained", type="mock", url="http://example.invalid", download_dir="/downloads")
    db_session.add_all([channel, downloader])
    await db_session.flush()
    resource = FileResource(channel_id=channel.id, guid="retained", title_raw="Synthetic", torrent_url="magnet:?xt=synthetic")
    db_session.add(resource)
    await db_session.flush()
    row = reservation("retired-with-task")
    row.settled = True
    db_session.add_all([row, DownloadTask(
        id=row.task_id, file_resource_id=resource.id, downloader_id=downloader.id,
        download_dir="/downloads", status="completed",
    )])
    await db_session.commit()
    queue = AsyncMock()
    queue.job_is_retired.return_value = True
    assert await cleanup.cleanup_dispatch_reservations(db_session.bind, queue) == 0
    queue.job_is_retired.assert_not_awaited()
    async with AsyncSession(db_session.bind) as check:
        assert await check.get(DownloadDispatch, row.id) is not None
