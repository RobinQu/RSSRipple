"""Real Redis/PG/Transmission, captured torrent, acceptance-before-commit takeover."""

import asyncio
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

PROJECT = "rssripple-v14-crash-mg"


def address(service):
    [state] = json.loads(subprocess.check_output(["docker", "inspect", f"{PROJECT}-{service}-1"]))
    assert state["Config"]["Labels"]["com.docker.compose.project"] == PROJECT
    assert state["State"]["Running"]
    return state["NetworkSettings"]["Networks"][PROJECT + "_isolated"]["IPAddress"]


os.environ["DATABASE_URL"] = f"postgresql+asyncpg://probe:probe@{address('postgres')}:5432/probe"
REDIS_URL = f"redis://{address('redis')}:6379/0"
RPC_URL = f"http://{address('transmission')}:9091/transmission/rpc"

import redis.asyncio as redis  # noqa: E402
from sqlalchemy import select  # noqa: E402

import app.database as database  # noqa: E402
import app.models  # noqa: E402,F401
from app.clients.transmission import TransmissionWrapper  # noqa: E402
from app.models.channel import Channel  # noqa: E402
from app.models.download_dispatch import DownloadDispatch  # noqa: E402
from app.models.download_task import DownloadTask  # noqa: E402
from app.models.downloader import DownloaderInstance  # noqa: E402
from app.models.file_resource import FileResource  # noqa: E402
from app.services.agent_service import create_and_submit_task  # noqa: E402
from app.services.task_queue import RedisQueue  # noqa: E402
from tests.metadata_corpus.dataset import ROOT, asset, digest, load_corpus  # noqa: E402


async def child(role):
    observer = redis.from_url(REDIS_URL, decode_responses=True)
    queue = RedisQueue(redis_url=REDIS_URL)
    finished = asyncio.Event()
    original = TransmissionWrapper.add_torrent

    async def tracked_rpc(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        await observer.rpush("probe:rpc_ids", str(result["torrent_id"]))
        if role == "A":
            await observer.set("probe:accepted_consumer", queue._consumer_key)
            await asyncio.Future()  # SIGKILL after real acceptance, before task persistence.
        return result

    async def handler(payload):
        async with database.async_session_factory() as db:
            resource = await db.get(FileResource, payload["resource_id"])
            downloader = await db.get(DownloaderInstance, payload["downloader_id"])
            task = await create_and_submit_task(resource, downloader, db, download_dir="/downloads")
            assert task.status == "downloading", task.error_message
            await db.commit()
        finished.set()
        return {"task_id": task.id}

    queue.register("crash-download", handler)
    try:
        with patch.object(TransmissionWrapper, "add_torrent", tracked_rpc):
            await queue.start()
            await asyncio.wait_for(finished.wait(), 45)
            async with asyncio.timeout(5):
                while (await queue.status("captured-crash"))["status"] == "running":
                    await asyncio.sleep(0.01)
            assert (await queue.status("captured-crash"))["status"] == "done"
    finally:
        await queue.stop()
        await observer.aclose()
        await database.engine.dispose()


async def main():
    observer = redis.from_url(REDIS_URL, decode_responses=True)
    wrapper = TransmissionWrapper(RPC_URL)
    assert await observer.dbsize() == 0
    assert await wrapper.list_torrents() == []
    _, corpus, reviews = load_corpus(ROOT)
    case_id = "f79ef2eb-02d5-42d3-80dc-70dd3c1d733b"
    case = next(row for row in corpus["cases"] if row["id"] == case_id)
    assert reviews[case_id]["status"] == "confirmed"
    torrent = asset(ROOT, case["evidence"]["torrent"])
    assert digest(torrent.read_bytes()) == torrent.stem
    async with database.engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    async with database.async_session_factory() as db:
        channel = Channel(
            id=str(uuid.uuid4()),
            name="captured takeover",
            field_mapping={},
            type="rss_feed",
            url="https://example.invalid/feed",
        )
        downloader = DownloaderInstance(
            id=str(uuid.uuid4()), name="isolated", type="transmission", url=RPC_URL, download_dir="/downloads"
        )
        db.add_all([channel, downloader])
        await db.flush()
        resource = FileResource(
            id=str(uuid.uuid4()),
            channel_id=channel.id,
            guid=str(uuid.uuid4()),
            title_raw=case["input"]["title_raw"],
            torrent_url="https://example.invalid/captured.torrent",
            torrent_file=str(torrent),
        )
        db.add(resource)
        await db.commit()
        resource_id, downloader_id = resource.id, downloader.id
    queue = RedisQueue(redis_url=REDIS_URL)
    children = []
    try:
        await queue.start(consume=False)
        original_job = await queue.enqueue(
            "crash-download",
            "captured-crash",
            {
                "resource_id": resource_id,
                "downloader_id": downloader_id,
            },
        )
        a = subprocess.Popen([sys.executable, __file__, "A"])
        children.append(a)
        async with asyncio.timeout(30):
            while not await observer.get("probe:accepted_consumer"):
                assert a.poll() is None, "First worker exited before acceptance"
                await asyncio.sleep(0.05)
        async with database.async_session_factory() as db:
            assert (await db.scalars(select(DownloadTask))).all() == []
            [reservation] = (await db.scalars(select(DownloadDispatch))).all()
            reserved_id = reservation.task_id
            assert reservation.settled is False
        a.kill()
        assert await asyncio.to_thread(a.wait, 5) == -9
        b = subprocess.Popen([sys.executable, __file__, "B"])
        children.append(b)
        async with asyncio.timeout(45):
            while b.poll() is None:
                await asyncio.sleep(0.1)
        assert b.returncode == 0
        state = await queue.status("captured-crash")
        assert state["job_id"] == original_job["job_id"] and state["status"] == "done"
        async with database.async_session_factory() as db:
            [task] = (await db.scalars(select(DownloadTask))).all()
            [reservation] = (await db.scalars(select(DownloadDispatch))).all()
            assert task.id == reservation.task_id == reserved_id
            assert reservation.settled is True
        rpc_ids = await observer.lrange("probe:rpc_ids", 0, -1)
        assert len(rpc_ids) == 2 and rpc_ids[0] == rpc_ids[1] == str(task.transmission_torrent_id)
        [live] = await wrapper.list_torrents()
        assert live["peers_connected"] == 0 and live["have_valid"] == 0
        Path(os.environ["PROBE_RESULT_PATH"]).write_text(
            json.dumps(
                {
                    "case_id": case_id,
                    "torrent_sha256": torrent.stem,
                    "worker_exit_codes": [a.returncode, b.returncode],
                    "rpc_ids": rpc_ids,
                    "persisted_tasks": 1,
                    "daemon_torrents": 1,
                    "reserved_task_id_preserved": True,
                    "logical_job_id_preserved": True,
                    "lease": "natural expiry with default 15-second lease and 5-second heartbeat",
                    "received_media_bytes": live["have_valid"],
                    "peers_connected": live["peers_connected"],
                    "scope": (
                        "production RedisQueue and create_and_submit_task; synthetic handler around captured torrent"
                    ),
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        for process in children:
            if process.poll() is None:
                process.terminate()
                await asyncio.to_thread(process.wait, 5)
        await queue.stop()
        await observer.aclose()
        await database.engine.dispose()


asyncio.run(child(sys.argv[1]) if len(sys.argv) > 1 else main())
