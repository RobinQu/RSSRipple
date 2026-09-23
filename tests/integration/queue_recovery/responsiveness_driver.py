"""A real queue lease must survive slow offloaded torrent analysis."""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

import redis.asyncio as redis

from app.services.task_queue import CONSUMER_LEASE_SECONDS, RedisQueue

URL = os.environ["QUEUE_RECOVERY_REDIS_URL"]
KEY = "torrent-responsiveness"


async def child():
    from app.services import torrent_inspect as ti
    from tests.unit.test_torrent_inspect import _resource

    queue = RedisQueue(redis_url=URL)
    finished = asyncio.Event()
    original = ti.analyze_torrent_files

    def slow_analysis(files):
        time.sleep(18)
        return original(files)

    ti.analyze_torrent_files = slow_analysis

    async def handler(payload):
        await queue._redis.incr("probe:executions")
        print(json.dumps({"lease_key": queue._consumer_key}), flush=True)
        try:
            source = "tests/fixtures/metadata_corpus_v1/torrents/987a72c09d5b0c2e934fa5016cc4dda6427a80dc5a4594e284a06ccf966acdb2.torrent"
            await ti.maybe_inspect_torrent(None, _resource(torrent_file=source))
            return {"analyzed": True}
        finally:
            finished.set()

    queue.register(KEY, handler)
    try:
        await queue.start()
        await asyncio.wait_for(finished.wait(), 40)
        async with asyncio.timeout(5):
            while (await queue.status(KEY))["status"] == "running":
                await asyncio.sleep(0.01)
        assert (await queue.status(KEY))["status"] == "done"
    finally:
        await queue.stop()


async def main():
    observer = redis.from_url(URL, decode_responses=True)
    competitor = RedisQueue(redis_url=URL)
    process = None
    owns_database = False

    async def duplicate(payload):
        await competitor._redis.incr("probe:executions")
        return {"unexpected_takeover": True}

    competitor.register(KEY, duplicate)
    try:
        assert await observer.dbsize() == 0, "Refuse a nonempty test Redis database"
        owns_database = True
        assert CONSUMER_LEASE_SECONDS == 15
        await competitor.start(consume=False)
        await competitor.enqueue(KEY, KEY, {})
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "tests.integration.queue_recovery.responsiveness_driver", "child",
            stdout=asyncio.subprocess.PIPE,
        )
        started = json.loads(await asyncio.wait_for(process.stdout.readline(), 15))
        await competitor.start()
        samples = []
        for _ in range(17):
            samples.append(await observer.pttl(started["lease_key"]))
            await asyncio.sleep(1)
        assert all(value > 0 for value in samples), samples
        renewals = sum(after > before + 1000 for before, after in zip(samples, samples[1:]))
        assert renewals >= 2, samples
        assert await asyncio.wait_for(process.wait(), 15) == 0
        assert await observer.get("probe:executions") == "1"
        status = await competitor.status(KEY)
        assert status["status"] == "done"
        Path(os.environ["QUEUE_RECOVERY_RESULT"]).write_text(json.dumps({
            "mode": "responsiveness", "executions": 1, "child_exit": process.returncode,
            "lease_seconds": CONSUMER_LEASE_SECONDS, "blocked_thread_seconds": 18,
            "lease_samples_ms": samples, "renewals": renewals,
            "data": "recorded torrent; real Redis and independent process; controlled slow analysis",
        }, indent=2) + "\n")
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        await competitor.stop()
        if owns_database:
            await observer.flushdb()
        await observer.aclose()


asyncio.run(child() if len(sys.argv) > 1 else main())
