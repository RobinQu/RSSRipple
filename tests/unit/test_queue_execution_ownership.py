"""Deterministic ownership races; fakeredis transport, production queue methods."""

import asyncio

import fakeredis
import pytest

from app.services.task_queue import RedisQueue


@pytest.mark.parametrize("replacement", ["recover", "new_job"])
@pytest.mark.parametrize("ending", ["success", "failure", "cancel", "progress"])
async def test_old_execution_cannot_change_current_owner(replacement, ending):
    server = fakeredis.FakeServer()
    clients = [fakeredis.aioredis.FakeRedis(server=server, decode_responses=True) for _ in range(2)]
    old, current = [RedisQueue(redis_client=client, max_concurrent=1) for client in clients]
    old_started, current_started = asyncio.Event(), asyncio.Event()
    old_release, current_release = asyncio.Event(), asyncio.Event()

    async def old_handler(payload):
        old_started.set()
        await old_release.wait()
        if ending == "progress":
            await old.update_progress("ownership", {"owner": "stale progress"})
        if ending == "failure":
            raise RuntimeError("Synthetic stale failure")
        return {"owner": "old"}

    async def current_handler(payload):
        current_started.set()
        await current_release.wait()
        return {"owner": "current"}

    old.register("synthetic", old_handler)
    current.register("synthetic", current_handler)
    await old.start()
    try:
        original = await old.enqueue("synthetic", "ownership", {})
        await asyncio.wait_for(old_started.wait(), 2)
        stale_task = next(iter(old._run_tasks))
        # Stop claiming new descriptors while leaving the original handler live.
        old._worker.cancel()
        await old._worker
        if replacement == "recover":
            await clients[0].delete(old._consumer_key)
        else:
            await old.clear("ownership")
            fresh = await old.enqueue("synthetic", "ownership", {})
            assert fresh["job_id"] != original["job_id"]
        await current.start()
        await asyncio.wait_for(current_started.wait(), 2)
        before = await current.status("ownership")
        assert before["status"] == "running"
        if ending == "cancel":
            stale_task.cancel()
        else:
            old_release.set()
        await asyncio.wait_for(asyncio.gather(stale_task, return_exceptions=True), 2)
        after = await current.status("ownership")
        assert after == before, "Stale completion/failure/cancel changed the current execution"
        assert await clients[1].get("rssripple:active:ownership") == before["job_id"]
        assert await clients[1].llen("rssripple:jobs") == 0, "Stale cancel requeued the old execution"
    finally:
        old_release.set()
        current_release.set()
        await old.stop()
        await current.stop()



async def test_recovery_rechecks_lease_renewal_atomically(monkeypatch):
    server = fakeredis.FakeServer()
    clients = [fakeredis.aioredis.FakeRedis(server=server, decode_responses=True) for _ in range(2)]
    owner, scanner = [RedisQueue(redis_client=client, max_concurrent=1) for client in clients]
    started, release = asyncio.Event(), asyncio.Event()

    async def handler(payload):
        started.set()
        await release.wait()

    owner.register("synthetic", handler)
    await owner.start()
    await scanner.start(consume=False)
    try:
        await owner.enqueue("synthetic", "renewed", {})
        await asyncio.wait_for(started.wait(), 2)
        await clients[0].delete(owner._consumer_key)
        original_read = clients[1].hgetall
        renewed = False

        async def renew_during_read(key):
            nonlocal renewed
            state = await original_read(key)
            if key == "rssripple:job:renewed" and not renewed:
                renewed = True
                await clients[0].set(owner._consumer_key, "1", ex=15)
            return state

        monkeypatch.setattr(clients[1], "hgetall", renew_during_read)
        await scanner._recover_orphaned_jobs()
        assert renewed, "The renewal race must actually execute"
        assert (await owner.status("renewed"))["status"] == "running"
        assert await clients[0].llen("rssripple:jobs") == 0
    finally:
        release.set()
        await owner.stop()
        await scanner.stop()


async def test_failed_enqueue_does_not_leave_active_key(monkeypatch):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisQueue(redis_client=client)
    await queue.start(consume=False)
    try:
        async def abort_transaction(self, *args, **kwargs):
            raise RuntimeError("Synthetic failure before transaction execution")

        with monkeypatch.context() as patcher:
            patcher.setattr(type(client.pipeline()), "execute", abort_transaction)
            with pytest.raises(RuntimeError, match="Synthetic failure"):
                await queue.enqueue("synthetic", "atomic-enqueue", {})
        assert await client.get("rssripple:active:atomic-enqueue") is None
        assert await queue.status("atomic-enqueue") is None
        assert await client.llen("rssripple:jobs") == 0
        assert await queue.enqueue("synthetic", "atomic-enqueue", {}) is not None
    finally:
        await queue.stop()


async def test_recovery_release_preserves_successor_lock(monkeypatch):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisQueue(redis_client=client)
    from app.services.task_queue import _RECOVERY_LOCK
    lock = _RECOVERY_LOCK
    replaced = False

    def racing_get(original):
        async def get(self, key, *args, **kwargs):
            nonlocal replaced
            value = await original(self, key, *args, **kwargs)
            if key == lock and value is not None and not replaced:
                replaced = True
                # Expiration and a successor acquiring the lock after our read.
                await client.set(lock, "successor-token", ex=30)
            return value
        return get

    for cls in [type(client), type(client.pipeline())]:
        monkeypatch.setattr(cls, "get", racing_get(cls.get))
    await queue._recover_orphaned_jobs()
    assert replaced, "The lock replacement race must execute"
    assert await client.get(lock) == "successor-token"
    await client.aclose()


async def test_legacy_recovery_cannot_fail_replacement_job(monkeypatch):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    queue = RedisQueue(redis_client=client)
    key = "rssripple:job:legacy-replaced"
    active = "rssripple:active:legacy-replaced"
    await client.hset(key, mapping={
        "job_id": "old", "key": "legacy-replaced", "job_type": "synthetic",
        "status": "running",
    })
    await client.set(active, "old")
    original = client.hgetall
    replaced = False

    async def replace_after_read(name):
        nonlocal replaced
        state = await original(name)
        if name == key and not replaced:
            replaced = True
            await client.hset(key, mapping={
                "job_id": "new", "status": "queued", "message": "new-descriptor",
            })
            await client.set(active, "new")
        return state

    monkeypatch.setattr(client, "hgetall", replace_after_read)
    await queue._recover_orphaned_jobs(reconcile_legacy=True)
    assert replaced
    state = await original(key)
    assert state["job_id"] == "new"
    assert state["status"] == "queued"
    assert await client.get(active) == "new"
    await client.aclose()


async def test_recovery_preserves_descriptor_claimed_by_resumed_consumer(monkeypatch):
    import json

    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
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
            resumed = True
            await client.set("rssripple:consumer:resumed-test-worker", "1", ex=15)
            await client.rpush(processing, fresh)
        return removed

    async def no_whole_list_delete(*keys):
        assert processing not in keys, "Recovery must only remove checked descriptors"
        return await original_delete(*keys)

    monkeypatch.setattr(client, "lrem", remove_then_resume)
    monkeypatch.setattr(client, "delete", no_whole_list_delete)
    await scanner._recover_orphaned_jobs()
    assert resumed
    assert await client.lrange(processing, 0, -1) == [fresh]
