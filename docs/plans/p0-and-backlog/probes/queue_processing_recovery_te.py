import json

import fakeredis
import pytest

from app.services.task_queue import RedisQueue


@pytest.mark.asyncio
async def test_recovery_cannot_delete_resumed_consumers_new_descriptor(monkeypatch):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    scanner = RedisQueue(redis_client=client)
    processing = "rssripple:processing:resumed-test-worker"
    old = json.dumps({"key": "old", "job_id": "old", "job_type": "synthetic"})
    fresh = json.dumps({"key": "fresh", "job_id": "fresh", "job_type": "synthetic"})
    await client.rpush(processing, old)
    original_llen = client.llen
    observed = False

    async def racing_llen(key):
        nonlocal observed
        size = await original_llen(key)
        if key == processing and size == 0:
            observed = True
            await client.set("rssripple:consumer:resumed-test-worker", "1", ex=15)
            await client.rpush(processing, fresh)
        return size

    monkeypatch.setattr(client, "llen", racing_llen)
    await scanner._recover_orphaned_jobs()
    assert observed, "race injection must execute"
    assert await client.lrange(processing, 0, -1) == [fresh]
