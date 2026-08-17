"""In-process RedisQueue integration coverage.

The single-node HTTP suite runs MemoryQueue only (Redis backends exercise
their own dedup/status paths in the distributed stack, which has no coverage
instrumentation). These tests drive the RedisQueue directly against
fakeredis so the fan-out/worker/dedup code paths count toward the
integration coverage gate.

Mirrors tests/unit/test_task_queue.py::TestRedisQueue but lives under
tests/integration/ so the docker test-runner's ``--cov=app`` run collects it.
"""

from __future__ import annotations

import asyncio

import fakeredis
import pytest

from app.services.task_queue import (
    BaseQueue,
    JobStatus,
    MemoryQueue,
    RedisQueue,
    create_queue,
)


async def _wait_done(queue: BaseQueue, key: str, timeout: float = 5.0) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        state = await queue.status(key)
        if state and state["status"] in (JobStatus.DONE, JobStatus.FAILED):
            return state
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"Job {key!r} did not finish; last state={state}")
        await asyncio.sleep(0.02)


def _make_redis():
    return fakeredis.FakeAsyncRedis(decode_responses=True)


class TestRedisQueue:
    @pytest.fixture
    async def redis_client(self):
        client = _make_redis()
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
        assert job is not None and job["status"] == JobStatus.QUEUED
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
        assert "No handler" in state["error"]

    async def test_dedup_then_reenqueue(self, queue):
        gate = asyncio.Event()

        async def slow(payload):
            await gate.wait()

        queue.register("slow", slow)
        await queue.start()
        job1 = await queue.enqueue("slow", "rk4", {})
        assert job1 is not None
        await asyncio.sleep(0.05)
        assert await queue.enqueue("slow", "rk4", {}) is None
        gate.set()
        await _wait_done(queue, "rk4")
        job3 = await queue.enqueue("slow", "rk4", {})
        assert job3 is not None
        gate.set()
        await _wait_done(queue, "rk4")

    async def test_status_before_enqueue_and_metadata(self, queue):
        await queue.start()
        assert await queue.status("never") is None
        async def handler(payload):
            return payload
        queue.register("meta", handler)
        job = await queue.enqueue("meta", "rk6", {"a": 1, "b": "two"})
        assert job["job_type"] == "meta" and job["key"] == "rk6"
        assert job["queued_at"] is not None
        state = await _wait_done(queue, "rk6")
        assert state["result"] == {"a": 1, "b": "two"}
        assert state["started_at"] is not None and state["finished_at"] is not None

    async def test_consume_false_enqueues_without_consuming(self, queue, redis_client):
        ran = []

        async def handler(payload):
            ran.append(payload)

        queue.register("echo", handler)
        await queue.start(consume=False)
        job = await queue.enqueue("echo", "rnc1", {"x": 1})
        assert job is not None and job["status"] == JobStatus.QUEUED
        await asyncio.sleep(0.1)
        assert ran == []
        assert await redis_client.llen("rssripple:jobs") == 1
        state = await queue.status("rnc1")
        assert state["status"] == JobStatus.QUEUED

    async def test_throttle_first_tick_wins(self, queue):
        await queue.start()
        assert await queue.throttle("sync_progress", 60) is True
        assert await queue.throttle("sync_progress", 60) is False
        assert await queue.throttle("fts_drain", 60) is True
        # Before start (no client) throttle is permissive.
        q = RedisQueue(redis_client=None)
        assert await q.throttle("x", 60) is True

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

    async def test_clear_and_result_serialization(self, queue):
        async def handler(payload):
            return {"nested": [1, 2]}
        queue.register("ser", handler)
        await queue.start()
        await queue.enqueue("ser", "rk7", {})
        state = await _wait_done(queue, "rk7")
        assert state["result"] == {"nested": [1, 2]}
        await queue.clear("rk7")
        assert await queue.status("rk7") is None


class TestCreateQueue:
    def test_memory_backend(self):
        assert isinstance(create_queue("memory"), MemoryQueue)

    def test_redis_backend(self):
        q = create_queue("redis", redis_client=_make_redis())
        assert isinstance(q, RedisQueue)

    def test_default_is_memory(self):
        assert isinstance(create_queue(), MemoryQueue)

    async def test_redis_full_lifecycle(self):
        fake = _make_redis()
        q = create_queue("redis", redis_client=fake)

        async def handler(payload):
            return {"ok": True}

        q.register("ping", handler)
        await q.start()
        await q.enqueue("ping", "rfactory", {})
        state = await _wait_done(q, "rfactory")
        assert state["status"] == JobStatus.DONE
        await q.stop()
        await fake.aclose()
