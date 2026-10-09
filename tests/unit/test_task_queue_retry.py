"""Unit tests for bounded automatic retry in the task queue backends.

Covers, for both MemoryQueue and RedisQueue (fakeredis):
- failure → exponential-backoff retry → success
- attempts exhausted → FAILED terminal state (existing terminal semantics)
- backoff interval actually elapses before the next attempt
- dedup and ownership survive the retry window
- crash recovery coexists with scheduled retries
Plus the per-job-type retry classification and config wiring.
"""

import asyncio
import json
import time

import fakeredis
import pytest

from app.job_handlers import register_all_handlers
from app.services.task_queue import (
    _DELAYED_ZSET,
    _NON_RETRYABLE_JOB_TYPES,
    _RETRYABLE_JOB_TYPES,
    JobStatus,
    MemoryQueue,
    RedisQueue,
    _retry_delay,
    resolve_retry_policy,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _wait_terminal(queue, key: str, timeout: float = 3.0) -> dict:
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        state = await queue.status(key)
        if state and state["status"] in (JobStatus.DONE, JobStatus.FAILED):
            return state
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"Job {key!r} did not finish; last state={state}")
        await asyncio.sleep(0.02)


async def _wait_retry_pending(queue, key: str, timeout: float = 2.0) -> dict:
    """Wait until the first attempt failed and the job sits in backoff."""
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        state = await queue.status(key)
        if (state and state["status"] == JobStatus.QUEUED
                and state["attempt"] >= 1 and state["next_retry_at"]):
            return state
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError(f"Job {key!r} never entered backoff; last state={state}")
        await asyncio.sleep(0.01)


def make_fake_redis(server=None):
    return fakeredis.FakeAsyncRedis(server=server, decode_responses=True)


class _FlakyHandler:
    """Fails ``fail_times`` times, then succeeds."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls: list[float] = []

    async def __call__(self, payload):
        self.calls.append(asyncio.get_event_loop().time())
        if len(self.calls) <= self.fail_times:
            raise RuntimeError(f"boom-{len(self.calls)}")
        return {"ok": True, "calls": len(self.calls)}


# ---------------------------------------------------------------------------
# MemoryQueue retry
# ---------------------------------------------------------------------------

class TestMemoryQueueRetry:
    @pytest.fixture
    async def queue(self):
        q = MemoryQueue()
        yield q
        await q.stop()

    async def test_retry_succeeds_after_transient_failure(self, queue):
        handler = _FlakyHandler(fail_times=1)
        queue.register("flaky", handler)
        await queue.start()

        await queue.enqueue("flaky", "mr1", {}, max_attempts=3, backoff_seconds=0.02)
        state = await _wait_terminal(queue, "mr1")
        assert state["status"] == JobStatus.DONE
        assert state["attempt"] == 2
        assert state["max_attempts"] == 3
        assert state["result"] == {"ok": True, "calls": 2}
        assert state["error"] is None  # previous attempt's failure is cleared
        assert state["next_retry_at"] is None
        assert len(handler.calls) == 2

    async def test_attempts_exhausted_marks_failed(self, queue):
        handler = _FlakyHandler(fail_times=99)
        queue.register("flaky", handler)
        await queue.start()

        await queue.enqueue("flaky", "mr2", {}, max_attempts=3, backoff_seconds=0.01)
        state = await _wait_terminal(queue, "mr2")
        assert state["status"] == JobStatus.FAILED
        assert state["attempt"] == 3
        assert "boom-3" in state["error"]
        assert state["finished_at"] is not None
        assert len(handler.calls) == 3
        # Terminal state releases the dedup key as before.
        assert await queue.enqueue("flaky", "mr2", {}, max_attempts=1) is not None

    async def test_backoff_delay_elapses_between_attempts(self, queue):
        handler = _FlakyHandler(fail_times=2)
        queue.register("flaky", handler)
        await queue.start()

        await queue.enqueue("flaky", "mr3", {}, max_attempts=3, backoff_seconds=0.08)
        # First retry waits backoff * 2^0, the second backoff * 2^1.
        state = await _wait_terminal(queue, "mr3")
        assert state["status"] == JobStatus.DONE
        assert len(handler.calls) == 3
        assert handler.calls[1] - handler.calls[0] >= 0.07
        assert handler.calls[2] - handler.calls[1] >= 0.15

    async def test_dedup_key_held_during_retry_backoff(self, queue):
        handler = _FlakyHandler(fail_times=99)
        queue.register("flaky", handler)
        await queue.start()

        await queue.enqueue("flaky", "mr4", {}, max_attempts=2, backoff_seconds=0.3)
        state = await _wait_retry_pending(queue, "mr4")
        assert state["attempt"] == 1
        assert state["error"] == "boom-1"  # last failure visible during backoff
        # Same key cannot be re-enqueued while a retry is pending.
        assert await queue.enqueue("flaky", "mr4", {}) is None

        state = await _wait_terminal(queue, "mr4")
        assert state["status"] == JobStatus.FAILED
        assert await queue.enqueue("flaky", "mr4", {}, max_attempts=1) is not None

    async def test_clear_during_retry_backoff_cancels_retry(self, queue):
        handler = _FlakyHandler(fail_times=99)
        queue.register("flaky", handler)
        await queue.start()

        await queue.enqueue("flaky", "mr5", {}, max_attempts=3, backoff_seconds=0.05)
        await _wait_retry_pending(queue, "mr5")
        await queue.clear("mr5")
        assert await queue.status("mr5") is None

        await asyncio.sleep(0.15)  # past the backoff window
        assert len(handler.calls) == 1  # cleared job never re-ran
        assert await queue.status("mr5") is None  # and did not resurrect
        # The dedup key went with the clear.
        assert await queue.enqueue("flaky", "mr5", {}, max_attempts=1) is not None

    async def test_stop_during_retry_backoff_fails_job(self):
        q = MemoryQueue()
        handler = _FlakyHandler(fail_times=99)
        q.register("flaky", handler)
        await q.start()

        # Backoff must stay below 1s: the unit conftest short-circuits
        # asyncio.sleep delays >= 1s.
        await q.enqueue("flaky", "mr6", {}, max_attempts=3, backoff_seconds=0.5)
        await _wait_retry_pending(q, "mr6")
        await q.stop()

        state = await q.status("mr6")
        assert state["status"] == JobStatus.FAILED
        assert "stopped during retry backoff" in state["error"]
        assert state["next_retry_at"] is None
        assert state["finished_at"] is not None
        assert not q._active_keys
        assert not q._retry_tasks

    async def test_unknown_type_defaults_to_single_attempt(self, queue):
        handler = _FlakyHandler(fail_times=99)
        queue.register("custom", handler)
        await queue.start()

        job = await queue.enqueue("custom", "mr7", {})
        assert job["max_attempts"] == 1
        state = await _wait_terminal(queue, "mr7")
        assert state["status"] == JobStatus.FAILED
        assert state["attempt"] == 1
        assert len(handler.calls) == 1


# ---------------------------------------------------------------------------
# RedisQueue retry
# ---------------------------------------------------------------------------

class TestRedisQueueRetry:
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

    async def test_retry_succeeds_after_transient_failure(self, queue):
        handler = _FlakyHandler(fail_times=1)
        queue.register("flaky", handler)
        await queue.start()

        job = await queue.enqueue("flaky", "rr1", {}, max_attempts=3, backoff_seconds=0.02)
        assert job["attempt"] == 0
        assert job["max_attempts"] == 3
        state = await _wait_terminal(queue, "rr1")
        assert state["status"] == JobStatus.DONE
        assert state["attempt"] == 2
        assert state["error"] is None
        assert state["next_retry_at"] is None
        assert len(handler.calls) == 2

    async def test_attempts_exhausted_marks_failed(self, queue):
        handler = _FlakyHandler(fail_times=99)
        queue.register("flaky", handler)
        await queue.start()

        await queue.enqueue("flaky", "rr2", {}, max_attempts=2, backoff_seconds=0.01)
        state = await _wait_terminal(queue, "rr2")
        assert state["status"] == JobStatus.FAILED
        assert state["attempt"] == 2
        assert "boom-2" in state["error"]
        assert len(handler.calls) == 2

    async def test_retry_waits_in_delayed_zset(self, queue, redis_client):
        handler = _FlakyHandler(fail_times=1)
        queue.register("flaky", handler)
        await queue.start()

        await queue.enqueue("flaky", "rr3", {}, max_attempts=2, backoff_seconds=0.3)
        state = await _wait_retry_pending(queue, "rr3")
        assert state["attempt"] == 1
        assert state["error"] == "boom-1"

        # The descriptor sits in the shared delayed zset with a future score,
        # not in the immediately-consumable backlog.
        pending = await redis_client.zrangebyscore(_DELAYED_ZSET, 0, time.time() + 60)
        assert len(pending) == 1
        score = (await redis_client.zscore(_DELAYED_ZSET, pending[0]))
        assert score > time.time() + 0.1
        assert await redis_client.llen("rssripple:jobs") == 0
        # Ownership lock is held across the backoff.
        assert await redis_client.get("rssripple:active:rr3") == state["job_id"]

        state = await _wait_terminal(queue, "rr3")
        assert state["status"] == JobStatus.DONE
        assert await redis_client.zcard(_DELAYED_ZSET) == 0
        assert len(handler.calls) == 2

    async def test_backoff_delay_elapses_between_attempts(self, queue):
        handler = _FlakyHandler(fail_times=1)
        queue.register("flaky", handler)
        await queue.start()

        await queue.enqueue("flaky", "rr4", {}, max_attempts=2, backoff_seconds=0.15)
        state = await _wait_terminal(queue, "rr4")
        assert state["status"] == JobStatus.DONE
        assert handler.calls[1] - handler.calls[0] >= 0.14

    async def test_dedup_key_held_during_retry_backoff(self, queue):
        handler = _FlakyHandler(fail_times=99)
        queue.register("flaky", handler)
        await queue.start()

        await queue.enqueue("flaky", "rr5", {}, max_attempts=2, backoff_seconds=0.3)
        await _wait_retry_pending(queue, "rr5")
        assert await queue.enqueue("flaky", "rr5", {}) is None
        state = await _wait_terminal(queue, "rr5")
        assert state["status"] == JobStatus.FAILED
        assert await queue.enqueue("flaky", "rr5", {}, max_attempts=1) is not None

    async def test_retry_survives_consumer_restart(self):
        """A retry scheduled by a consumer that then dies is promoted and
        executed by a new consumer: the delayed zset is durable and shared."""
        server = fakeredis.FakeServer()
        redis_a = make_fake_redis(server)
        redis_b = make_fake_redis(server)
        handler_a = _FlakyHandler(fail_times=99)
        handler_b = _FlakyHandler(fail_times=0)

        queue_a = RedisQueue(redis_client=redis_a)
        queue_a.register("flaky", handler_a)
        await queue_a.start()
        try:
            await queue_a.enqueue("flaky", "rr6", {}, max_attempts=2, backoff_seconds=0.2)
            await _wait_retry_pending(queue_a, "rr6")
        finally:
            await queue_a.stop()  # "crash" during the backoff window

        queue_b = RedisQueue(redis_client=redis_b)
        queue_b.register("flaky", handler_b)
        await queue_b.start()
        try:
            state = await _wait_terminal(queue_b, "rr6")
            assert state["status"] == JobStatus.DONE
            assert state["attempt"] == 2
            assert len(handler_b.calls) == 1
        finally:
            await queue_b.stop()

    async def test_clear_during_retry_backoff_cancels_retry(self, queue, redis_client):
        handler = _FlakyHandler(fail_times=99)
        queue.register("flaky", handler)
        await queue.start()

        await queue.enqueue("flaky", "rr7", {}, max_attempts=3, backoff_seconds=0.05)
        await _wait_retry_pending(queue, "rr7")
        await queue.clear("rr7")
        assert await queue.status("rr7") is None

        # The promoted descriptor fails the ownership check at claim time.
        await asyncio.sleep(0.4)
        assert len(handler.calls) == 1
        assert await queue.status("rr7") is None
        assert await redis_client.zcard(_DELAYED_ZSET) == 0
        # Recovery garbage-collects the unclaimable descriptor; the backlog
        # never executes it.
        await queue._recover_orphaned_jobs()
        assert await redis_client.llen("rssripple:jobs") == 0

    async def test_crash_recovery_requeues_running_retry_job(self, redis_client):
        """Crash recovery and retry stay orthogonal: a recovered descriptor
        keeps its remaining attempts instead of being failed outright."""
        msg = json.dumps({
            "job_id": "crash-r1", "job_type": "flaky", "key": "recover-retry",
            "payload": {},
        })
        await redis_client.hset("rssripple:job:recover-retry", mapping={
            "job_id": "crash-r1", "job_type": "flaky", "key": "recover-retry",
            "status": JobStatus.RUNNING, "result": "", "error": "",
            "queued_at": "2026-01-01T00:00:00", "started_at": "2026-01-01T00:00:01",
            "finished_at": "", "message": msg,
            "attempt": "1", "max_attempts": "2", "backoff_seconds": "0.01",
            "next_retry_at": "",
        })
        await redis_client.set("rssripple:active:recover-retry", "crash-r1")
        await redis_client.rpush("rssripple:processing:dead-worker", msg)

        handler = _FlakyHandler(fail_times=0)
        queue = RedisQueue(redis_client=redis_client)
        queue.register("flaky", handler)
        await queue.start()
        try:
            state = await _wait_terminal(queue, "recover-retry")
            assert state["status"] == JobStatus.DONE
            assert state["attempt"] == 2  # recovery preserved the spent attempt
            assert len(handler.calls) == 1
        finally:
            await queue.stop()

    async def test_deserialize_defaults_for_pre_retry_hashes(self):
        state = RedisQueue._deserialize({
            "job_id": "1", "job_type": "t", "key": "k", "status": JobStatus.FAILED,
            "result": "", "error": "x", "queued_at": "", "started_at": "",
            "finished_at": "",
        })
        assert state["attempt"] == 0
        assert state["max_attempts"] == 1
        assert state["backoff_seconds"] == 0.0
        assert state["next_retry_at"] is None


# ---------------------------------------------------------------------------
# Retry policy classification & config
# ---------------------------------------------------------------------------

class TestRetryPolicy:
    def test_retry_delay_grows_exponentially_and_is_capped(self):
        assert _retry_delay(30.0, 1) == 30.0
        assert _retry_delay(30.0, 2) == 60.0
        assert _retry_delay(30.0, 3) == 120.0
        assert _retry_delay(30.0, 20) == 3600.0

    def test_classification_covers_every_registered_handler(self):
        registered: list[str] = []

        class _Stub:
            def register(self, job_type, handler):
                registered.append(job_type)

        register_all_handlers(_Stub())
        assert set(registered) == set(_RETRYABLE_JOB_TYPES | _NON_RETRYABLE_JOB_TYPES)
        assert not (_RETRYABLE_JOB_TYPES & _NON_RETRYABLE_JOB_TYPES)

    def test_expected_classification(self):
        # Idempotent handlers are retried.
        assert {
            "fetch_channel", "refresh_works_metadata", "refresh_channel_works",
            "backfill_metadata", "sync_progress", "daily_cleanup", "daily_dedup",
            "check_downloaders", "fts_drain", "fts_reconcile",
            "magnet_resolve_sweep", "refresh_resource_organize",
        } == _RETRYABLE_JOB_TYPES
        # Own durable recovery / interactive / own-backoff handlers are not.
        assert {
            "run_agent", "reprocess_resource_metadata", "analyze_batch_files",
            "download_notifications", "resolve_magnet_torrent",
        } == _NON_RETRYABLE_JOB_TYPES

    def test_resolve_policy_defaults(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "queue_job_max_attempts", 5)
        monkeypatch.setattr(settings, "queue_job_retry_backoff_seconds", 7.5)
        assert resolve_retry_policy("fetch_channel") == (5, 7.5)
        assert resolve_retry_policy("run_agent") == (1, 7.5)
        assert resolve_retry_policy("never-heard-of") == (1, 7.5)

    def test_resolve_policy_explicit_overrides_win(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "queue_job_max_attempts", 5)
        # Explicit override even for a non-retryable type (escape hatch).
        assert resolve_retry_policy("run_agent", max_attempts=2, backoff_seconds=1.0) == (2, 1.0)
        # Clamped to sane minimums.
        assert resolve_retry_policy("fetch_channel", max_attempts=0, backoff_seconds=-1) == (1, 0.0)

    async def test_config_drives_enqueue_defaults(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "queue_job_max_attempts", 2)
        monkeypatch.setattr(settings, "queue_job_retry_backoff_seconds", 0.01)

        handler = _FlakyHandler(fail_times=99)
        q = MemoryQueue()
        q.register("fetch_channel", handler)
        await q.start()
        try:
            # No explicit retry args: the retryable type picks up the config.
            job = await q.enqueue("fetch_channel", "cfg-ch", {"channel_id": "x"})
            assert job["max_attempts"] == 2
            state = await _wait_terminal(q, "cfg-ch")
            assert state["status"] == JobStatus.FAILED
            assert state["attempt"] == 2
            assert len(handler.calls) == 2
        finally:
            await q.stop()

    async def test_non_retryable_type_fails_terminally_by_default(self, monkeypatch):
        from app.config import settings

        monkeypatch.setattr(settings, "queue_job_max_attempts", 5)
        monkeypatch.setattr(settings, "queue_job_retry_backoff_seconds", 0.01)

        handler = _FlakyHandler(fail_times=99)
        q = MemoryQueue()
        q.register("run_agent", handler)
        await q.start()
        try:
            job = await q.enqueue("run_agent", "cfg-agent", {"agent_id": "x"})
            assert job["max_attempts"] == 1  # config default must not leak in
            state = await _wait_terminal(q, "cfg-agent")
            assert state["status"] == JobStatus.FAILED
            assert len(handler.calls) == 1
        finally:
            await q.stop()
