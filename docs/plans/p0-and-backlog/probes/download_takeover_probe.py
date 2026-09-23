"""Real Redis/PG/Transmission, captured torrent, acceptance-before-commit takeover."""
import asyncio
import json
import os
import subprocess
import uuid
from pathlib import Path
from unittest.mock import patch

PROJECT = "rssripple-v14-download-lv"

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
from app.models.downloader import DownloaderInstance  # noqa: E402
from app.models.file_resource import FileResource  # noqa: E402
from app.models.download_task import DownloadTask  # noqa: E402
from app.services.agent_service import create_and_submit_task  # noqa: E402
from app.services.task_queue import RedisQueue  # noqa: E402
from tests.metadata_corpus.dataset import ROOT, asset, digest, load_corpus  # noqa: E402

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
        channel = Channel(id=str(uuid.uuid4()), name="captured takeover", field_mapping={}, type="rss_feed", url="https://example.invalid/feed")
        downloader = DownloaderInstance(id=str(uuid.uuid4()), name="isolated", type="transmission", url=RPC_URL, download_dir="/downloads")
        db.add_all([channel, downloader])
        await db.flush()
        resource = FileResource(id=str(uuid.uuid4()), channel_id=channel.id, guid=str(uuid.uuid4()), title_raw=case["input"]["title_raw"], torrent_url="https://example.invalid/captured.torrent", torrent_file=str(torrent))
        db.add(resource)
        await db.commit()
        resource_id, downloader_id = resource.id, downloader.id
    a, b = RedisQueue(redis_url=REDIS_URL), RedisQueue(redis_url=REDIS_URL)
    accepted, release, b_committed, a_committed = [asyncio.Event() for _ in range(4)]
    accepted_ids = []
    original = TransmissionWrapper.add_torrent

    async def delay_after_acceptance(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        accepted_ids.append(result["torrent_id"])
        if len(accepted_ids) == 1:
            accepted.set()
            await release.wait()
        return result

    def handler(done):
        async def run(payload):
            async with database.async_session_factory() as db:
                resource = await db.get(FileResource, resource_id)
                downloader = await db.get(DownloaderInstance, downloader_id)
                task = await create_and_submit_task(resource, downloader, db, download_dir="/downloads")
                assert task.status == "downloading", task.error_message
                await db.commit()
                done.set()
                return {"task_id": task.id}
        return run

    a.register("takeover", handler(a_committed))
    b.register("takeover", handler(b_committed))
    try:
        with patch.object(TransmissionWrapper, "add_torrent", delay_after_acceptance):
            await a.start()
            await a.enqueue("takeover", "captured-download", {})
            await asyncio.wait_for(accepted.wait(), 30)
            # Force a missed lease after actual acceptance; keep A's handler alive.
            a._heartbeat.cancel()
            await a._heartbeat
            a._worker.cancel()
            await a._worker
            await observer.delete(a._consumer_key)
            await b.start()
            await asyncio.wait_for(b_committed.wait(), 30)
            release.set()
            await asyncio.wait_for(a_committed.wait(), 30)
        async with database.async_session_factory() as db:
            tasks = (await db.scalars(select(DownloadTask))).all()
        [live] = await wrapper.list_torrents()
        result = {
            "case_id": case_id, "torrent_sha256": torrent.stem,
            "rpc_ids": accepted_ids, "daemon_torrents": 1,
            "task_ids": [task.id for task in tasks], "persisted_tasks": len(tasks),
            "task_torrent_ids": [task.transmission_torrent_id for task in tasks],
            "peers_connected": live["peers_connected"], "received_media_bytes": live["have_valid"],
            "boundary": "actual RPC acceptance held before local task flush/commit; forced lease loss",
        }
        Path(os.environ["PROBE_RESULT_PATH"]).write_text(json.dumps(result, indent=2) + "\n")
        assert accepted_ids[0] == accepted_ids[1]
        assert len(tasks) == 2, "Defect probe expects duplicate persisted tasks"
        assert live["peers_connected"] == 0 and live["have_valid"] == 0
    finally:
        release.set()
        await a.stop()
        await b.stop()
        await observer.aclose()
        await database.engine.dispose()

asyncio.run(main())
