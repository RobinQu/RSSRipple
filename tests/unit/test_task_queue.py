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

import pytest
import fakeredis

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
