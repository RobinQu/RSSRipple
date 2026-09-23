"""Actual Redis and separate processes; synthetic side effect, unchanged 15s lease."""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import redis.asyncio as redis

from app.services.task_queue import RedisQueue, require_execution_ownership

URL = os.environ["PROBE_REDIS_URL"]
assert URL.startswith("redis://127.0.0.1:") and URL.endswith("/0")
PREFIX = "probe:lease-reentry:"


async def child(role):
    client = redis.from_url(URL, decode_responses=True)
    queue = RedisQueue(redis_url=URL)
    done = asyncio.Event()

    async def handler(payload):
        await client.rpush(PREFIX + "starts", role)
        if role == "A":
            await client.set(PREFIX + "consumer", queue._consumer_key)
            # Model synchronous parser/native work starving the event loop.
            time.sleep(22)
        try:
            await require_execution_ownership()
            await client.rpush(PREFIX + "effects", role)
            return {"owner": role}
        finally:
            done.set()

    queue.register("synthetic_lease", handler)
    await queue.start()
    try:
        if role == "A":
            await queue.enqueue("synthetic_lease", PREFIX + "job", {})
        await asyncio.wait_for(done.wait(), 40)
        await asyncio.sleep(0.3)
    finally:
        await queue.stop()
        await client.aclose()


async def main():
    client = redis.from_url(URL, decode_responses=True)
    assert await client.dbsize() == 0, "Dedicated empty Redis required"
    children = []
    try:
        a = subprocess.Popen([sys.executable, __file__, "A"])
        children.append(a)
        async with asyncio.timeout(10):
            while not await client.get(PREFIX + "consumer"):
                await asyncio.sleep(0.05)
        consumer = await client.get(PREFIX + "consumer")
        async with asyncio.timeout(20):
            while await client.exists(consumer):
                await asyncio.sleep(0.05)
        assert a.poll() is None, "Original worker must still be alive"
        b = subprocess.Popen([sys.executable, __file__, "B"])
        children.append(b)
        async with asyncio.timeout(30):
            while any(c.poll() is None for c in children):
                await asyncio.sleep(0.1)
        assert all(c.returncode == 0 for c in children)
        starts = await client.lrange(PREFIX + "starts", 0, -1)
        effects = await client.lrange(PREFIX + "effects", 0, -1)
        queue = RedisQueue(redis_client=client)
        state = await queue.status(PREFIX + "job")
        result = {
            "data": "synthetic handler; actual Redis and processes",
            "original_alive_when_lease_expired": True,
            "starts": starts,
            "effects": effects,
            "final_job_state": state,
        }
        Path(os.environ["PROBE_RESULT_PATH"]).write_text(json.dumps(result, indent=2) + "\n")
        assert starts == ["A", "B"] and effects == ["B"]
        assert state["result"] == {"owner": "B"}, "Recovered completion must survive the stale owner"
    finally:
        for child_process in children:
            if child_process.poll() is None:
                child_process.terminate()
                await asyncio.to_thread(child_process.wait, 5)
        await client.aclose()


asyncio.run(child(sys.argv[1]) if len(sys.argv) > 1 else main())
