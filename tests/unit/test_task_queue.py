"""Unit tests for task queue backends (MemoryQueue and RedisQueue).

Tests are grouped by backend and cover:
- register + enqueue + handler execution (done path)
- failed handler → job status == failed
- per-key dedup (second enqueue returns None while first is active)
- status() before any enqueue → None
- status() reflects transitions queued → running → done/failed
- bounded concurrency via max_concurrent
- stop() cancels the worker cleanly
"""

import asyncio
import json

import fakeredis
import pytest

from app.services.task_queue import (
    BaseQueue,
    JobStatus,
    MemoryQueue,
    RedisQueue,
    create_queue,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _wait_done(queue: BaseQueue, key: str, timeout: float = 2.0) -> dict:
    """Poll queue.status(key) until the job reaches a terminal state."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        state = await queue.status(key)
        if state and state["status"] in (JobStatus.DONE, JobStatus.FAILED):
            return state
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"Job {key!r} did not finish within {timeout}s; last state={state}")
        await asyncio.sleep(0.02)


def make_fake_redis():
    """Return a fakeredis async client configured to match real redis.asyncio behaviour."""
    return fakeredis.FakeAsyncRedis(decode_responses=True)


# ---------------------------------------------------------------------------
# MemoryQueue
# ---------------------------------------------------------------------------

class TestMemoryQueue:
    @pytest.fixture
    async def queue(self):
        q = MemoryQueue()
        yield q
        await q.stop()

    async def test_register_and_run_handler(self, queue):
        results = []

        async def handler(payload):
            results.append(payload["x"])
            return {"doubled": payload["x"] * 2}

        queue.register("echo", handler)
        await queue.start()

        job = await queue.enqueue("echo", "k1", {"x": 21})
        assert job is not None
        assert job["key"] == "k1"
        assert job["job_type"] == "echo"
        assert job["status"] == JobStatus.QUEUED

        state = await _wait_done(queue, "k1")
        assert state["status"] == JobStatus.DONE
        assert state["result"] == {"doubled": 42}
        assert results == [21]

    async def test_failed_handler(self, queue):
        async def bad_handler(payload):
            raise ValueError("boom")

        queue.register("bad", bad_handler)
        await queue.start()

        await queue.enqueue("bad", "k2", {})
        state = await _wait_done(queue, "k2")
        assert state["status"] == JobStatus.FAILED
        assert "boom" in state["error"]

    async def test_no_handler_fails_job(self, queue):
        await queue.start()
        # No handler registered for "unregistered"
        await queue.enqueue("unregistered", "k3", {})
        state = await _wait_done(queue, "k3")
        assert state["status"] == JobStatus.FAILED
        assert "No handler" in state["error"]

    async def test_dedup_returns_none(self, queue):
        gate = asyncio.Event()

        async def slow_handler(payload):
            await gate.wait()

        queue.register("slow", slow_handler)
        await queue.start()

        job1 = await queue.enqueue("slow", "k4", {})
        assert job1 is not None

        # Second enqueue while first is still active should be rejected
        job2 = await queue.enqueue("slow", "k4", {})
        assert job2 is None

        gate.set()
        await _wait_done(queue, "k4")

        # After completion, a new enqueue for the same key is allowed
        job3 = await queue.enqueue("slow", "k4", {})
        assert job3 is not None
        gate.set()  # let it finish
        await _wait_done(queue, "k4")

    async def test_status_before_enqueue(self, queue):
        await queue.start()
        assert await queue.status("never_enqueued") is None

    async def test_update_progress_is_visible_before_completion(self, queue):
        await queue.enqueue("slow", "progress-memory", {})
        await queue.update_progress("progress-memory", {"message": "working", "output": "abc"})
        state = await queue.status("progress-memory")
        assert state["result"] == {"message": "working", "output": "abc"}

    async def test_run_tasks_survive_gc(self, queue):
        """Regression: the dispatcher's fire-and-forget create_task held no
        strong reference, so a run task could be garbage-collected mid-flight —
        the job never ran and its dedup key leaked in _active_keys."""
        import gc

        started = asyncio.Event()

        async def handler(payload):
            started.set()
            await asyncio.sleep(0.05)
            return {"ok": True}

        queue.register("gc-job", handler)
        await queue.start()
        await queue.enqueue("gc-job", "k-gc", {})
        # Hammer the GC while the job is queued/running; the run task must
        # stay alive and release the dedup key afterwards.
        for _ in range(20):
            gc.collect()
            await asyncio.sleep(0.01)
        state = await _wait_done(queue, "k-gc")
        assert state["status"] == JobStatus.DONE
        assert started.is_set()
        # Dedup key released: a fresh enqueue for the same key succeeds.
        job = await queue.enqueue("gc-job", "k-gc", {})
        assert job is not None
        await _wait_done(queue, "k-gc")

    async def test_status_transitions(self, queue):
        started = asyncio.Event()
        done = asyncio.Event()

        async def handler(payload):
            started.set()
            await done.wait()
            return {}

        queue.register("track", handler)
        await queue.start()

        await queue.enqueue("track", "k5", {})
        await asyncio.wait_for(started.wait(), timeout=1.0)

        state = await queue.status("k5")
        assert state["status"] == JobStatus.RUNNING
        assert state["started_at"] is not None

        done.set()
        final = await _wait_done(queue, "k5")
        assert final["status"] == JobStatus.DONE
        assert final["finished_at"] is not None

    async def test_bounded_concurrency(self, queue):
        """max_concurrent=2 should limit in-flight jobs."""
        max_concurrent = 2
        q = MemoryQueue(max_concurrent=max_concurrent)
        running = asyncio.Queue()
        gate = asyncio.Event()

        async def handler(payload):
            running.put_nowait(1)
            await gate.wait()
            running.get_nowait()

        q.register("bounded", handler)
        await q.start()

        # Enqueue 4 jobs
        for i in range(4):
            await q.enqueue("bounded", f"bc{i}", {})

        # Wait until exactly max_concurrent are running
        await asyncio.sleep(0.1)
        assert running.qsize() <= max_concurrent

        gate.set()
        for i in range(4):
            await _wait_done(q, f"bc{i}")

        await q.stop()

    async def test_stop_is_idempotent(self, queue):
        await queue.start()
        await queue.stop()
        await queue.stop()  # second stop should not raise

    async def test_consume_false_enqueues_without_consuming(self, queue):
        """start(consume=False) (web role): enqueue/status work but no
        dispatcher runs, so the handler is never invoked."""
        ran = []

        async def handler(payload):
            ran.append(payload)

        queue.register("echo", handler)
        await queue.start(consume=False)

        job = await queue.enqueue("echo", "nc1", {"x": 1})
        assert job is not None
        assert job["status"] == JobStatus.QUEUED

        await asyncio.sleep(0.1)
        assert ran == []
        state = await queue.status("nc1")
        assert state["status"] == JobStatus.QUEUED

        await queue.stop()  # safe without a dispatcher
        await queue.stop()

    async def test_throttle_always_true_single_process(self, queue):
        """MemoryQueue (single process, one scheduler) never throttles."""
        await queue.start()
        assert await queue.throttle("sync_progress", 60) is True
        assert await queue.throttle("sync_progress", 60) is True

    async def test_multiple_different_keys(self, queue):
        counts = {}

        async def handler(payload):
            counts[payload["id"]] = True
            return {"id": payload["id"]}

        queue.register("multi", handler)
        await queue.start()

        for i in range(5):
            await queue.enqueue("multi", f"m{i}", {"id": i})

        for i in range(5):
            state = await _wait_done(queue, f"m{i}")
            assert state["status"] == JobStatus.DONE

        assert len(counts) == 5

    async def test_list_jobs_snapshot(self, queue):
        async def handler(payload):
            return {"ok": True}

        queue.register("echo", handler)
        await queue.start()
        await queue.enqueue("echo", "lj1", {})
        await queue.enqueue("echo", "lj2", {})
        await _wait_done(queue, "lj1")
        await _wait_done(queue, "lj2")

        jobs = await queue.list_jobs()
        assert {job["key"] for job in jobs} == {"lj1", "lj2"}
        assert all(job["status"] == JobStatus.DONE for job in jobs)
        assert all(job["queued_at"] and job["finished_at"] for job in jobs)

        await queue.clear("lj1")
        jobs = await queue.list_jobs()
        assert [job["key"] for job in jobs] == ["lj2"]


# ---------------------------------------------------------------------------
# RedisQueue
# ---------------------------------------------------------------------------

class TestRedisQueue:
    @pytest.fixture
    async def redis_client(self):
        client = make_fake_redis()
        yield client
        await client.aclose()

    @pytest.fixture
    async def queue(self, redis_client):
        q = RedisQueue(redis_client=redis_client)
        yield q
        await q.stop()

    async def test_register_and_run_handler(self, queue):
        async def handler(payload):
            return {"echo": payload["msg"]}

        queue.register("echo", handler)
        await queue.start()

        job = await queue.enqueue("echo", "rk1", {"msg": "hello"})
        assert job is not None
        assert job["status"] == JobStatus.QUEUED

        state = await _wait_done(queue, "rk1")
        assert state["status"] == JobStatus.DONE
        assert state["result"] == {"echo": "hello"}

    async def test_failed_handler(self, queue):
        async def bad(payload):
            raise RuntimeError("redis-boom")

        queue.register("bad", bad)
        await queue.start()

        await queue.enqueue("bad", "rk2", {})
        state = await _wait_done(queue, "rk2")
        assert state["status"] == JobStatus.FAILED
        assert "redis-boom" in state["error"]

    async def test_no_handler_fails_job(self, queue):
        await queue.start()
        await queue.enqueue("unregistered", "rk3", {})
        state = await _wait_done(queue, "rk3")
        assert state["status"] == JobStatus.FAILED

    async def test_dedup_returns_none(self, queue):
        gate = asyncio.Event()

        async def slow(payload):
            await gate.wait()

        queue.register("slow", slow)
        await queue.start()

        job1 = await queue.enqueue("slow", "rk4", {})
        assert job1 is not None

        # Give the worker a moment to start running (and consume the active key)
        await asyncio.sleep(0.05)
        job2 = await queue.enqueue("slow", "rk4", {})
        assert job2 is None

        gate.set()
        await _wait_done(queue, "rk4")

        # After completion, re-enqueue should succeed
        job3 = await queue.enqueue("slow", "rk4", {})
        assert job3 is not None
        gate.set()
        await _wait_done(queue, "rk4")

    async def test_status_before_enqueue(self, queue):
        await queue.start()
        assert await queue.status("never") is None

    async def test_list_jobs_scans_state_hashes(self, queue, redis_client):
        async def handler(payload):
            return {"ok": True}

        queue.register("echo", handler)
        await queue.start()
        await queue.enqueue("echo", "rlj1", {})
        state = await _wait_done(queue, "rlj1")
        assert state["status"] == JobStatus.DONE

        jobs = await queue.list_jobs()
        assert [job["key"] for job in jobs] == ["rlj1"]
        assert jobs[0]["job_type"] == "echo"
        assert jobs[0]["status"] == JobStatus.DONE

        # A malformed hash is skipped instead of sinking the whole snapshot.
        await redis_client.hset("rssripple:job:broken", mapping={"garbage": "1"})
        original = RedisQueue._deserialize

        def flaky_deserialize(raw):
            if raw.get("garbage"):
                raise ValueError("bad hash")
            return original(raw)

        queue._deserialize = flaky_deserialize
        try:
            jobs = await queue.list_jobs()
        finally:
            queue._deserialize = original
        assert [job["key"] for job in jobs] == ["rlj1"]

    async def test_update_progress_is_shared_in_redis(self, queue):
        await queue.start(consume=False)
        await queue.enqueue("slow", "progress-redis", {})
        await queue.update_progress("progress-redis", {"message": "working", "output": "abc"})
        state = await queue.status("progress-redis")
        assert state["result"] == {"message": "working", "output": "abc"}

    async def test_consume_false_enqueues_without_consuming(self, queue, redis_client):
        """start(consume=False) (web role): the job lands in the Redis list
        and its state hash is readable, but no worker loop pops it."""
        ran = []

        async def handler(payload):
            ran.append(payload)

        queue.register("echo", handler)
        await queue.start(consume=False)

        job = await queue.enqueue("echo", "rnc1", {"x": 1})
        assert job is not None
        assert job["status"] == JobStatus.QUEUED

        await asyncio.sleep(0.1)
        assert ran == []
        assert await redis_client.llen("rssripple:jobs") == 1
        state = await queue.status("rnc1")
        assert state["status"] == JobStatus.QUEUED

        await queue.stop()  # safe without a worker loop

    async def test_throttle_first_tick_wins(self, queue):
        """throttle(): first caller within the TTL wins; everyone else is
        told to skip — this collapses N worker schedulers to one tick."""
        await queue.start()
        assert await queue.throttle("sync_progress", 60) is True
        assert await queue.throttle("sync_progress", 60) is False
        # A different key is independent.
        assert await queue.throttle("fts_drain", 60) is True

    async def test_status_transitions(self, queue):
        started = asyncio.Event()
        done = asyncio.Event()

        async def handler(payload):
            started.set()
            await done.wait()

        queue.register("track", handler)
        await queue.start()

        await queue.enqueue("track", "rk5", {})
        await asyncio.wait_for(started.wait(), timeout=1.0)

        state = await queue.status("rk5")
        assert state["status"] == JobStatus.RUNNING

        done.set()
        final = await _wait_done(queue, "rk5")
        assert final["status"] == JobStatus.DONE

    async def test_backlog_stays_in_redis_until_execution_slot_is_free(self, redis_client):
        """Slow handlers must not prefetch the durable Redis backlog."""
        queue = RedisQueue(redis_client=redis_client, max_concurrent=1)
        gate = asyncio.Event()
        started = asyncio.Event()

        async def slow(payload):
            started.set()
            await gate.wait()

        queue.register("slow", slow)
        await queue.start()
        try:
            await queue.enqueue("slow", "first", {})
            await queue.enqueue("slow", "second", {})
            await asyncio.wait_for(started.wait(), timeout=1)
            await asyncio.sleep(0.05)

            assert await redis_client.llen("rssripple:jobs") == 1
            assert (await queue.status("second"))["status"] == JobStatus.QUEUED

            gate.set()
            assert (await _wait_done(queue, "first"))["status"] == JobStatus.DONE
            assert (await _wait_done(queue, "second"))["status"] == JobStatus.DONE
        finally:
            gate.set()
            await queue.stop()

    async def test_recovers_job_from_dead_consumer(self, redis_client):
        """A descriptor claimed by a crashed worker is executed after restart."""
        msg = json.dumps({
            "job_id": "crashed1", "job_type": "echo", "key": "recover-me",
            "payload": {"value": 42},
        })
        await redis_client.hset("rssripple:job:recover-me", mapping={
            "job_id": "crashed1", "job_type": "echo", "key": "recover-me",
            "status": JobStatus.RUNNING, "result": "", "error": "",
            "queued_at": "2026-01-01T00:00:00", "started_at": "2026-01-01T00:00:01",
            "finished_at": "", "message": msg,
        })
        await redis_client.set("rssripple:active:recover-me", "crashed1")
        await redis_client.rpush("rssripple:processing:dead-worker", msg)

        queue = RedisQueue(redis_client=redis_client)
        queue.register("echo", lambda payload: asyncio.sleep(0, result=payload))
        await queue.start()
        try:
            state = await _wait_done(queue, "recover-me")
            assert state["status"] == JobStatus.DONE
            assert state["result"] == {"value": 42}
            assert await redis_client.llen("rssripple:processing:dead-worker") == 0
        finally:
            await queue.stop()

    async def test_does_not_steal_job_from_live_consumer(self, redis_client):
        msg = json.dumps({
            "job_id": "live1", "job_type": "echo", "key": "still-running",
            "payload": {},
        })
        await redis_client.hset("rssripple:job:still-running", mapping={
            "job_id": "live1", "job_type": "echo", "key": "still-running",
            "status": JobStatus.RUNNING, "message": msg,
        })
        await redis_client.rpush("rssripple:processing:healthy-worker", msg)
        await redis_client.set("rssripple:consumer:healthy-worker", "1", ex=60)

        queue = RedisQueue(redis_client=redis_client)
        await queue.start()
        try:
            await asyncio.sleep(0.05)
            assert await redis_client.llen("rssripple:processing:healthy-worker") == 1
            assert await redis_client.llen("rssripple:jobs") == 0
            assert (await queue.status("still-running"))["status"] == JobStatus.RUNNING
        finally:
            await queue.stop()

    async def test_releases_unrecoverable_legacy_running_lock(self, redis_client):
        await redis_client.hset("rssripple:job:legacy-zombie", mapping={
            "job_id": "old1", "job_type": "fetch_channel", "key": "legacy-zombie",
            "status": JobStatus.RUNNING,
        })
        await redis_client.set("rssripple:active:legacy-zombie", "old1")

        queue = RedisQueue(redis_client=redis_client)
        await queue.start()
        try:
            state = await queue.status("legacy-zombie")
            assert state["status"] == JobStatus.FAILED
            assert "durable recovery" in state["error"]
            assert not await redis_client.exists("rssripple:active:legacy-zombie")
        finally:
            await queue.stop()

    async def test_operational_sync_runs_ahead_of_normal_backlog(self, redis_client):
        queue = RedisQueue(redis_client=redis_client, max_concurrent=1)
        gate = asyncio.Event()
        first_started = asyncio.Event()
        order: list[str] = []

        async def normal(payload):
            order.append(payload["name"])
            if payload["name"] == "first":
                first_started.set()
                await gate.wait()

        async def sync(payload):
            order.append("sync")

        queue.register("normal", normal)
        queue.register("sync_progress", sync)
        await queue.start()
        try:
            await queue.enqueue("normal", "first-priority-test", {"name": "first"})
            await asyncio.wait_for(first_started.wait(), timeout=1)
            await queue.enqueue("normal", "second-priority-test", {"name": "second"})
            await queue.enqueue("sync_progress", "sync-priority-test", {})
            gate.set()

            await _wait_done(queue, "first-priority-test")
            await _wait_done(queue, "sync-priority-test")
            await _wait_done(queue, "second-priority-test")
            assert order == ["first", "sync", "second"]
        finally:
            gate.set()
            await queue.stop()

    async def test_job_metadata_preserved(self, queue):
        async def handler(payload):
            return payload

        queue.register("meta", handler)
        await queue.start()

        job = await queue.enqueue("meta", "rk6", {"a": 1, "b": "two"})
        assert job["job_type"] == "meta"
        assert job["key"] == "rk6"
        assert job["queued_at"] is not None

        state = await _wait_done(queue, "rk6")
        assert state["result"] == {"a": 1, "b": "two"}
        assert state["started_at"] is not None
        assert state["finished_at"] is not None


# ---------------------------------------------------------------------------
# create_queue factory
# ---------------------------------------------------------------------------

class TestCreateQueue:
    def test_memory_backend(self):
        q = create_queue("memory")
        assert isinstance(q, MemoryQueue)

    def test_redis_backend(self):
        fake = make_fake_redis()
        q = create_queue("redis", redis_client=fake)
        assert isinstance(q, RedisQueue)

    def test_default_is_memory(self):
        q = create_queue()
        assert isinstance(q, MemoryQueue)

    def test_base_queue_interface(self):
        q = create_queue("memory")
        assert isinstance(q, BaseQueue)

    async def test_memory_queue_full_lifecycle(self):
        q = create_queue("memory")

        async def handler(payload):
            return {"ok": True}

        q.register("ping", handler)
        await q.start()

        job = await q.enqueue("ping", "factory_key", {})
        assert job is not None

        state = await _wait_done(q, "factory_key")
        assert state["status"] == JobStatus.DONE

        await q.stop()

    async def test_redis_queue_full_lifecycle(self):
        fake = make_fake_redis()
        q = create_queue("redis", redis_client=fake)

        async def handler(payload):
            return {"ok": True}

        q.register("ping", handler)
        await q.start()

        job = await q.enqueue("ping", "rfactory_key", {})
        assert job is not None

        state = await _wait_done(q, "rfactory_key")
        assert state["status"] == JobStatus.DONE

        await q.stop()
        await fake.aclose()


# ---------------------------------------------------------------------------
# RedisQueue edge cases: lazy client creation, throttle before start, clear,
# heartbeat loop, worker error/cancellation, recovery corner cases.
# ---------------------------------------------------------------------------


class _WrappedRedis:
    """Delegates every attribute to the inner redis client but exposes a
    non-fakeredis type, so RedisQueue enables its heartbeat loop."""

    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)


class TestRedisQueueEdgeCases:
    @pytest.fixture
    async def redis_client(self):
        client = make_fake_redis()
        yield client
        await client.aclose()

    async def test_start_lazily_creates_client_from_url(self, monkeypatch):
        fake = make_fake_redis()
        import redis.asyncio as aioredis

        monkeypatch.setattr(aioredis, "from_url", lambda *a, **k: fake)
        q = RedisQueue(redis_url="redis://example:6379/0")
        await q.start(consume=False)
        try:
            assert q._redis is fake
        finally:
            await q.stop()

    async def test_throttle_true_when_not_started(self):
        q = RedisQueue()
        assert await q.throttle("sync_progress", 60) is True

    async def test_list_jobs_empty_before_any_enqueue(self, redis_client):
        q = RedisQueue(redis_client=redis_client)
        assert await q.list_jobs() == []

    async def test_clear_removes_job_state_and_active_lock(self, redis_client):
        q = RedisQueue(redis_client=redis_client)
        await q.start(consume=False)
        try:
            job = await q.enqueue("noop", "clear-me", {})
            assert job is not None
            assert await q.status("clear-me") is not None

            await q.clear("clear-me")
            assert await q.status("clear-me") is None
            assert not await redis_client.exists("rssripple:job:clear-me")
            assert not await redis_client.exists("rssripple:active:clear-me")
        finally:
            await q.stop()

    async def test_worker_releases_slot_when_cancelled_while_claiming(self, redis_client):
        """Cancelling the worker while it holds a semaphore slot (inside the
        claim) must release the slot — otherwise the sem leaks capacity."""
        entered = asyncio.Event()
        blocking = asyncio.Event()

        async def blocking_lmove(*args, **kwargs):
            entered.set()
            await blocking.wait()
            return None

        redis_client.lmove = blocking_lmove
        q = RedisQueue(redis_client=redis_client, max_concurrent=1)
        await q.start()
        try:
            await asyncio.wait_for(entered.wait(), timeout=1.0)
            await q.stop()
            assert q._sem._value == 1
        finally:
            await q.stop()

    async def test_worker_survives_transient_lmove_error(self, redis_client):
        inner = redis_client
        calls = {"n": 0}
        real_lmove = inner.lmove

        async def flaky_lmove(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient redis error")
            return await real_lmove(*args, **kwargs)

        inner.lmove = flaky_lmove
        q = RedisQueue(redis_client=inner)
        await q.start()
        try:
            await asyncio.sleep(0.35)
            # the loop logged the error and kept polling (later claims succeeded)
            assert calls["n"] >= 3
        finally:
            await q.stop()

    async def test_heartbeat_loop_refreshes_lease(self, redis_client, monkeypatch):
        import app.services.task_queue as tq

        monkeypatch.setattr(tq, "CONSUMER_HEARTBEAT_SECONDS", 0.001)
        q = RedisQueue(redis_client=_WrappedRedis(redis_client))
        try:
            await q.start()
            assert q._heartbeat is not None
            assert q._worker is not None

            await redis_client.delete(q._consumer_key)
            await asyncio.sleep(0.05)
            # the heartbeat loop re-armed the consumer lease
            assert await redis_client.exists(q._consumer_key)
        finally:
            await q.stop()

    async def test_heartbeat_logs_and_continues_after_error(self, redis_client, monkeypatch):
        import app.services.task_queue as tq

        monkeypatch.setattr(tq, "CONSUMER_HEARTBEAT_SECONDS", 0.001)
        inner = redis_client
        real_set = inner.set
        calls = {"n": 0}

        async def flaky_set(name, *a, **k):
            calls["n"] += 1
            if calls["n"] >= 3:
                await asyncio.sleep(0)
                raise RuntimeError("redis down")
            return await real_set(name, *a, **k)

        inner.set = flaky_set
        q = RedisQueue(redis_client=_WrappedRedis(inner))
        q._sem = asyncio.Semaphore(4)
        q._heartbeat = asyncio.create_task(q._heartbeat_loop())
        await asyncio.sleep(0.05)
        try:
            # the loop kept running after the exceptions instead of dying
            assert not q._heartbeat.done()
        finally:
            await q.stop()

    async def test_stop_handles_heartbeat_cancelled_before_first_run(self, redis_client):
        """stop() tolerates a heartbeat task that was cancelled before it ever
        ran a loop iteration (the CancelledError escapes the loop's handler)."""
        q = RedisQueue(redis_client=redis_client)
        q._sem = asyncio.Semaphore(4)
        q._heartbeat = asyncio.create_task(q._heartbeat_loop())
        q._heartbeat.cancel()
        await q.stop()

    async def test_recovery_skips_when_lock_held(self, redis_client):
        await redis_client.set("rssripple:recovery-lock", "some-other-worker")
        q = RedisQueue(redis_client=redis_client)
        await q._recover_orphaned_jobs()
        assert await redis_client.get("rssripple:recovery-lock") == "some-other-worker"

    async def test_recovery_discards_malformed_processing_message(self, redis_client):
        await redis_client.rpush("rssripple:processing:dead-worker", "not-json{")
        q = RedisQueue(redis_client=redis_client)
        await q._recover_orphaned_jobs()
        assert not await redis_client.exists("rssripple:processing:dead-worker")

    async def test_recovery_requeues_priority_job(self, redis_client):
        msg = json.dumps({
            "job_id": "crashed-prio", "job_type": "sync_progress", "key": "prio-key",
            "payload": {"x": 1},
        })
        await redis_client.hset("rssripple:job:prio-key", mapping={
            "job_id": "crashed-prio", "job_type": "sync_progress", "key": "prio-key",
            "status": JobStatus.RUNNING, "result": "", "error": "",
            "queued_at": "2026-01-01T00:00:00", "started_at": "2026-01-01T00:00:01",
            "finished_at": "", "message": msg,
        })
        await redis_client.set("rssripple:active:prio-key", "crashed-prio")
        await redis_client.rpush("rssripple:processing:dead-worker", msg)

        q = RedisQueue(redis_client=redis_client)
        await q._recover_orphaned_jobs()

        assert (await q.status("prio-key"))["status"] == JobStatus.QUEUED
        head = (await redis_client.lrange("rssripple:jobs", 0, 0))[0]
        assert json.loads(head)["job_type"] == "sync_progress"
        assert not await redis_client.exists("rssripple:processing:dead-worker")

    async def test_recovery_drops_message_without_matching_state(self, redis_client):
        msg = json.dumps({
            "job_id": "orphan-job", "job_type": "echo", "key": "gone-key",
            "payload": {},
        })
        await redis_client.rpush("rssripple:processing:dead-worker", msg)
        # No job hash exists for "gone-key" — the descriptor is discarded.
        q = RedisQueue(redis_client=redis_client)
        await q._recover_orphaned_jobs()
        assert not await redis_client.exists("rssripple:processing:dead-worker")
        assert await redis_client.llen("rssripple:jobs") == 0

    async def test_stop_requeues_inflight_job(self):
        """A running job cancelled by stop() is put back on the durable queue
        (with its dedup lock intact) instead of being lost."""
        server = fakeredis.FakeServer()
        redis = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
        probe = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
        gate = asyncio.Event()
        started = asyncio.Event()

        async def slow(payload):
            started.set()
            await gate.wait()

        q = RedisQueue(redis_client=redis)
        q.register("slow", slow)
        await q.start()
        try:
            await q.enqueue("slow", "inflight-1", {})
            await asyncio.wait_for(started.wait(), timeout=1.0)
            await q.stop()

            assert (await probe.hget("rssripple:job:inflight-1", "status")) == JobStatus.QUEUED
            assert await probe.llen("rssripple:jobs") == 1
            assert await probe.llen(q._processing_key) == 0
            assert await probe.exists("rssripple:active:inflight-1")
        finally:
            await q.stop()
            await probe.aclose()

    async def test_stop_requeues_priority_job(self):
        server = fakeredis.FakeServer()
        redis = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
        probe = fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
        gate = asyncio.Event()
        started = asyncio.Event()

        async def slow(payload):
            started.set()
            await gate.wait()

        q = RedisQueue(redis_client=redis)
        q.register("sync_progress", slow)
        await q.start()
        try:
            await q.enqueue("sync_progress", "inflight-prio", {})
            await asyncio.wait_for(started.wait(), timeout=1.0)
            await q.stop()

            assert (await probe.hget("rssripple:job:inflight-prio", "status")) == JobStatus.QUEUED
            assert await probe.llen("rssripple:jobs") == 1
            assert await probe.llen(q._processing_key) == 0
        finally:
            await q.stop()
            await probe.aclose()

    def test_deserialize_parses_json_result(self):
        state = RedisQueue._deserialize({
            "job_id": "1", "job_type": "t", "key": "k", "status": JobStatus.DONE,
            "result": '{"ok": 1}', "error": "", "queued_at": "", "started_at": "",
            "finished_at": "",
        })
        assert state["result"] == {"ok": 1}

    def test_deserialize_gracefully_handles_bad_result_json(self):
        state = RedisQueue._deserialize({
            "job_id": "1", "job_type": "t", "key": "k", "status": JobStatus.DONE,
            "result": "{not-json", "error": "", "queued_at": "", "started_at": "",
            "finished_at": "",
        })
        assert state["result"] == "{not-json"


# ---------------------------------------------------------------------------
# create_queue kwargs forwarding
# ---------------------------------------------------------------------------


class TestCreateQueueKwargs:
    def test_memory_backend_forwards_max_concurrent_only(self):
        q = create_queue("memory", redis_url="redis://x:6379/0", max_concurrent=3)
        assert isinstance(q, MemoryQueue)
        assert q._max_concurrent == 3

    def test_redis_backend_forwards_known_kwargs(self):
        fake = make_fake_redis()
        q = create_queue("redis", redis_client=fake, ttl=100, max_concurrent=2, bogus=1)
        assert isinstance(q, RedisQueue)
        assert q._redis is fake
        assert q._ttl == 100
        assert q._max_concurrent == 2
