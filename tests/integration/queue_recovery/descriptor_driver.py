"""Real Redis descriptor preservation with a controlled recovery interleaving."""

import asyncio
import json
import os
from pathlib import Path

import pytest
import redis.asyncio as redis

from app.services.task_queue import RedisQueue


async def main():
    client = redis.from_url(os.environ["QUEUE_RECOVERY_REDIS_URL"], decode_responses=True)
    resumed_client = redis.from_url(os.environ["QUEUE_RECOVERY_REDIS_URL"], decode_responses=True)
    try:
        assert await client.dbsize() == 0, "Dedicated empty Redis database required"
        scanner = RedisQueue(redis_client=client)
        processing = "rssripple:processing:resumed-test-worker"
        stale = json.dumps({"key": "old", "job_id": "old", "job_type": "synthetic"})
        fresh = json.dumps({"key": "fresh", "job_id": "fresh", "job_type": "synthetic"})
        await client.rpush(processing, stale)
        original_lrem = client.lrem
        original_delete = client.delete
        resumed = False

        async def remove_then_resume(key, count, value):
            nonlocal resumed
            removed = await original_lrem(key, count, value)
            if key == processing and value == stale:
                assert not await resumed_client.exists(processing), "Redis must remove the empty list"
                resumed = True
                await resumed_client.set("rssripple:consumer:resumed-test-worker", "1", ex=15)
                await resumed_client.rpush(processing, fresh)
            return removed

        async def no_whole_list_delete(*keys):
            assert processing not in keys, "Recovery may only remove checked descriptors"
            return await original_delete(*keys)

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(client, "lrem", remove_then_resume)
            patch.setattr(client, "delete", no_whole_list_delete)
            await scanner._recover_orphaned_jobs()
        assert resumed
        assert await resumed_client.lrange(processing, 0, -1) == [fresh]
        Path(os.environ["QUEUE_RECOVERY_RESULT"]).write_text(json.dumps({
            "backend": "redis", "resumed_descriptor_preserved": True,
            "empty_list_removed_automatically": True,
            "boundary": "Two real Redis connections; controlled command interleaving, no process pause",
        }) + "\n")
        await client.delete(processing, "rssripple:consumer:resumed-test-worker")
    finally:
        await client.aclose()
        await resumed_client.aclose()


asyncio.run(main())
