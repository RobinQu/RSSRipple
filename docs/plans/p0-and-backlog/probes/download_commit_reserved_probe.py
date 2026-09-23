"""Real Redis/PG/Transmission, captured torrent, acceptance-before-commit takeover."""

import asyncio
import json
import os
import subprocess
import uuid
from pathlib import Path
from unittest.mock import patch

PROJECT = "rssripple-v14-commit-mf"


def address(service):
    [state] = json.loads(subprocess.check_output(["docker", "inspect", f"{PROJECT}-{service}-1"]))
    assert state["Config"]["Labels"]["com.docker.compose.project"] == PROJECT
    assert state["State"]["Running"]
    return state["NetworkSettings"]["Networks"][PROJECT + "_isolated"]["IPAddress"]


os.environ["DATABASE_URL"] = f"postgresql+asyncpg://probe:probe@{address('postgres')}:5432/probe"
REDIS_URL = f"redis://{address('redis')}:6379/0"
RPC_URL = f"http://{address('transmission')}:9091/transmission/rpc"

import redis.asyncio as redis  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

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
    async with database.engine.begin() as conn:
        await conn.execute(text("CREATE TABLE probe_allowed_dirs (path VARCHAR(1024) PRIMARY KEY)"))
        await conn.execute(text("INSERT INTO probe_allowed_dirs VALUES ('/downloads')"))
        await conn.execute(
            text(
                "ALTER TABLE download_tasks ADD CONSTRAINT probe_deferred_dir "
                "FOREIGN KEY (download_dir) REFERENCES probe_allowed_dirs(path) "
                "DEFERRABLE INITIALLY DEFERRED"
            )
        )
    queue = RedisQueue(redis_url=REDIS_URL)
    done = asyncio.Event()
    accepted_ids, proposed_ids, failures = [], [], []
    original = TransmissionWrapper.add_torrent

    async def tracked_rpc(self, *args, **kwargs):
        result = await original(self, *args, **kwargs)
        accepted_ids.append(result["torrent_id"])
        return result

    async def handler(payload):
        try:
            for attempt in range(2):
                async with database.async_session_factory() as db:
                    resource = await db.get(FileResource, resource_id)
                    downloader = await db.get(DownloaderInstance, downloader_id)
                    task = await create_and_submit_task(resource, downloader, db, download_dir="/downloads")
                    assert task.status == "downloading", task.error_message
                    proposed_ids.append(task.id)
                    if attempt == 0:
                        task.download_dir = "/rejected-at-commit"
                        await db.flush()  # Deferred FK allows the real SQL write.
                        try:
                            await db.commit()
                        except IntegrityError as exc:
                            assert "probe_deferred_dir" in str(exc)
                            failures.append("actual deferred FK failure at COMMIT")
                            await db.rollback()
                        else:
                            raise AssertionError("COMMIT should fail")
                    else:
                        await db.commit()
                if attempt == 0:
                    async with database.async_session_factory() as check:
                        assert (await check.scalars(select(DownloadTask))).all() == []
                        [reservation] = (await check.scalars(select(DownloadDispatch))).all()
                        assert reservation.settled is False
                        assert reservation.task_id == proposed_ids[0]
            return {"task_id": proposed_ids[-1]}
        finally:
            done.set()

    queue.register("commit-retry", handler)
    try:
        with patch.object(TransmissionWrapper, "add_torrent", tracked_rpc):
            await queue.start()
            await queue.enqueue("commit-retry", "captured-commit", {})
            await asyncio.wait_for(done.wait(), 30)
            async with asyncio.timeout(5):
                while (await queue.status("captured-commit"))["status"] == "running":
                    await asyncio.sleep(0.01)
            assert (await queue.status("captured-commit"))["status"] == "done"
        async with database.async_session_factory() as db:
            [task] = (await db.scalars(select(DownloadTask))).all()
            [reservation] = (await db.scalars(select(DownloadDispatch))).all()
            assert reservation.settled is True
            assert task.id == reservation.task_id == proposed_ids[0] == proposed_ids[1]
        [live] = await wrapper.list_torrents()
        assert accepted_ids[0] == accepted_ids[1] == task.transmission_torrent_id
        assert live["peers_connected"] == 0 and live["have_valid"] == 0
        Path(os.environ["PROBE_RESULT_PATH"]).write_text(
            json.dumps(
                {
                    "case_id": case_id,
                    "torrent_sha256": torrent.stem,
                    "rpc_ids": accepted_ids,
                    "proposed_task_ids": proposed_ids,
                    "persisted_tasks": 1,
                    "daemon_torrents": 1,
                    "failure": failures,
                    "reservation_survived_rollback": True,
                    "received_media_bytes": live["have_valid"],
                    "peers_connected": live["peers_connected"],
                    "retry_scope": "same logical Redis handler; explicit fresh transaction after actual COMMIT failure",
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        await queue.stop()
        await observer.aclose()
        await database.engine.dispose()


asyncio.run(main())
