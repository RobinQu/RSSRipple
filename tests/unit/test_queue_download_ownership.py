"""Stale queue executions must stop before initiating a downloader operation."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis
import pytest
from sqlalchemy import select

from app.models.channel import Channel
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.services.agent_service import create_and_submit_task
from app.services.task_queue import ExecutionOwnershipLostError, RedisQueue


@pytest.mark.parametrize("rpc_failed", [False, True])
async def test_download_losing_ownership_during_rpc_leaves_reservation_unsettled(
    monkeypatch, db_session, rpc_failed,
):
    from app.models.download_dispatch import DownloadDispatch

    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisQueue(redis_client=client)
    channel = Channel(name="rpc-loss", type="rss_feed", url="https://example.invalid", field_mapping={})
    downloader = DownloaderInstance(name="rpc-loss", type="mock", url="http://example.invalid", download_dir="/downloads")
    db_session.add_all([channel, downloader])
    await db_session.flush()
    resource = FileResource(channel_id=channel.id, guid="rpc-loss", title_raw="Synthetic", torrent_url="magnet:?xt=synthetic")
    db_session.add(resource)
    await db_session.commit()
    finished = asyncio.Event()
    errors = []

    async def add_torrent(*args, **kwargs):
        await client.hset("rssripple:job:rpc-loss", "execution_token", "replacement")
        if rpc_failed:
            raise RuntimeError("Old RPC failed after takeover")
        return {"torrent_id": 1}

    wrapper = SimpleNamespace(add_torrent=AsyncMock(side_effect=add_torrent))
    monkeypatch.setattr("app.clients.downloader.get_downloader_client", lambda _: wrapper)

    async def handler(payload):
        try:
            await create_and_submit_task(resource, downloader, db_session)
            await db_session.commit()
        except Exception as exc:
            await db_session.rollback()
            errors.append(exc)
        finally:
            finished.set()

    queue.register("synthetic", handler)
    await queue.start()
    try:
        await queue.enqueue("synthetic", "rpc-loss", {})
        await asyncio.wait_for(finished.wait(), 5)
        wrapper.add_torrent.assert_awaited_once()
        assert len(errors) == 1 and isinstance(errors[0], ExecutionOwnershipLostError)
        assert (await db_session.scalars(select(DownloadTask))).all() == []
        reservation = (await db_session.scalars(select(DownloadDispatch))).one()
        assert reservation.settled is False
    finally:
        await queue.stop()


@pytest.mark.parametrize("condition", ["token", "lease", "active", "job_id", "status", "consumer", "redis_failure", "current"])
async def test_download_requires_current_execution(monkeypatch, condition, db_session):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisQueue(redis_client=client)
    entered, resume, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
    wrapper = SimpleNamespace(add_torrent=AsyncMock(return_value={"torrent_id": 1}))
    monkeypatch.setattr("app.clients.downloader.get_downloader_client", lambda _: wrapper)
    db = db_session
    channel = Channel(name="ownership", type="rss_feed", url="https://example.invalid", field_mapping={})
    downloader = DownloaderInstance(name="ownership", type="mock", url="http://example.invalid", download_dir="/downloads")
    db.add_all([channel, downloader])
    await db.flush()
    resource = FileResource(channel_id=channel.id, guid="ownership", title_raw="Synthetic", torrent_url="magnet:?xt=synthetic")
    db.add(resource)
    await db.commit()

    errors = []

    async def handler(payload):
        entered.set()
        await resume.wait()
        try:
            await create_and_submit_task(resource, downloader, db)
        except Exception as exc:
            errors.append(exc)
            raise
        finally:
            finished.set()

    queue.register("synthetic", handler)
    await queue.start()
    try:
        await queue.enqueue("synthetic", "download-ownership", {})
        await asyncio.wait_for(entered.wait(), 2)
        job_key = "rssripple:job:download-ownership"
        if condition in {"token", "job_id", "status", "consumer"}:
            field = {"token": "execution_token", "job_id": "job_id", "status": "status", "consumer": "consumer_id"}[condition]
            await client.hset(job_key, field, "replacement")
        elif condition == "lease":
            await client.delete(queue._consumer_key)
        elif condition == "active":
            await client.set("rssripple:active:download-ownership", "replacement")
        elif condition == "redis_failure":
            pipeline_class = type(client.pipeline())
            execute = pipeline_class.execute
            failed = False

            async def fail_guard_once(self, *args, **kwargs):
                nonlocal failed
                if not failed:
                    failed = True
                    raise ConnectionError("Synthetic ownership read failure")
                return await execute(self, *args, **kwargs)

            monkeypatch.setattr(pipeline_class, "execute", fail_guard_once)
        resume.set()
        await asyncio.wait_for(finished.wait(), 2)
        if condition == "current":
            wrapper.add_torrent.assert_awaited_once()
            assert len((await db.scalars(select(DownloadTask))).all()) == 1
            assert errors == []
        else:
            wrapper.add_torrent.assert_not_awaited()
            assert (await db.scalars(select(DownloadTask))).all() == []
            assert len(errors) == 1
            assert isinstance(errors[0], ConnectionError if condition == "redis_failure" else ExecutionOwnershipLostError)
    finally:
        resume.set()
        await queue.stop()


@pytest.mark.parametrize("changed", ["directory", "payload", "downloader"])
async def test_same_dispatch_rejects_changed_parameters_before_rpc(monkeypatch, db_session, changed):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisQueue(redis_client=client)
    wrapper = SimpleNamespace(add_torrent=AsyncMock(return_value={"torrent_id": 1}))
    monkeypatch.setattr("app.clients.downloader.get_downloader_client", lambda _: wrapper)
    channel = Channel(name="drift", type="rss_feed", url="https://example.invalid", field_mapping={})
    downloader = DownloaderInstance(name="drift", type="mock", url="http://example.invalid", download_dir="/downloads")
    db_session.add_all([channel, downloader])
    await db_session.flush()
    resource = FileResource(channel_id=channel.id, guid="drift", title_raw="Synthetic", torrent_url="magnet:?xt=original")
    db_session.add(resource)
    await db_session.commit()
    finished = asyncio.Event()
    errors = []

    async def handler(payload):
        try:
            await create_and_submit_task(resource, downloader, db_session, download_dir="/downloads")
            await db_session.commit()
            directory = "/elsewhere" if changed == "directory" else "/downloads"
            selected_downloader = SimpleNamespace(id="another-downloader") if changed == "downloader" else downloader
            if changed == "payload":
                resource.torrent_url = "magnet:?xt=replacement"
            await create_and_submit_task(resource, selected_downloader, db_session, download_dir=directory)
        except Exception as exc:
            errors.append(exc)
        finally:
            finished.set()

    queue.register("synthetic", handler)
    await queue.start()
    try:
        await queue.enqueue("synthetic", "drift", {})
        await asyncio.wait_for(finished.wait(), 5)
        assert len(errors) == 1 and "parameters changed" in str(errors[0])
        wrapper.add_torrent.assert_awaited_once()
        assert len((await db_session.scalars(select(DownloadTask))).all()) == 1
    finally:
        await queue.stop()


@pytest.mark.parametrize("scope", ["different_agents", "new_jobs"])
async def test_distinct_dispatch_operations_keep_distinct_tasks(monkeypatch, db_session, scope):
    from app.models.agent import Agent

    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisQueue(redis_client=client)
    wrapper = SimpleNamespace(add_torrent=AsyncMock(return_value={"torrent_id": 1}))
    monkeypatch.setattr("app.clients.downloader.get_downloader_client", lambda _: wrapper)
    channel = Channel(name="scope", type="rss_feed", url="https://example.invalid", field_mapping={})
    downloader = DownloaderInstance(name="scope", type="mock", url="http://example.invalid", download_dir="/downloads")
    db_session.add_all([channel, downloader])
    await db_session.flush()
    agents = [Agent(name=f"scope-{i}", channel_id=channel.id, downloader_id=downloader.id) for i in range(2)]
    resource = FileResource(channel_id=channel.id, guid="scope", title_raw="Synthetic", torrent_url="magnet:?xt=synthetic")
    db_session.add_all([resource, *agents])
    await db_session.commit()

    async def handler(payload):
        for agent_id in payload["agent_ids"]:
            await create_and_submit_task(resource, downloader, db_session, agent_id=agent_id, download_dir="/downloads")
            await db_session.commit()
        return {"done": True}

    queue.register("synthetic", handler)
    await queue.start()
    try:
        payloads = [[agents[0].id, agents[1].id]] if scope == "different_agents" else [[agents[0].id], [agents[0].id]]
        job_ids = []
        for ids in payloads:
            job = await queue.enqueue("synthetic", "scope", {"agent_ids": ids})
            assert job is not None
            job_ids.append(job["job_id"])
            async with asyncio.timeout(5):
                while (await queue.status("scope"))["status"] in {"queued", "running"}:
                    await asyncio.sleep(0.01)
            assert (await queue.status("scope"))["status"] == "done"
        tasks = (await db_session.scalars(select(DownloadTask))).all()
        assert len(tasks) == 2 and tasks[0].id != tasks[1].id
        assert {task.transmission_torrent_id for task in tasks} == {1}
        assert {task.agent_id for task in tasks} == ({a.id for a in agents} if scope == "different_agents" else {agents[0].id})
        assert len(set(job_ids)) == len(payloads)
        assert all(len(identifier) == 32 for identifier in job_ids)
        assert wrapper.add_torrent.await_count == 2
    finally:
        await queue.stop()
