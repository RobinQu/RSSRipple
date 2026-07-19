"""Async task queue abstraction.

Two backends are provided:
- MemoryQueue  — asyncio-based, suitable for single-process deployments (default)
- RedisQueue   — redis.asyncio-based, suitable for multi-instance deployments

Both backends share the same public interface (BaseQueue):
  queue.register("job_type", async_handler_fn)
  job_dict = await queue.enqueue("job_type", key, payload_dict)
  state_dict = await queue.status(key)

Handlers are plain async functions (payload: dict) -> Any and must be registered
before start() is called.  Dedup is enforced per-key: only one job for a given
key can be active at a time; a second enqueue() for the same key returns None.
"""

import asyncio
import json
import logging
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any

from app.utils.time import utcnow

logger = logging.getLogger(__name__)

MAX_CONCURRENT = 4
JOB_TTL_SECONDS = 86_400  # 24 h — how long Redis keeps job state after completion

# Redis key prefixes
_QUEUE_LIST = "rssripple:jobs"
_ACTIVE_PFX = "rssripple:active:"
_JOB_PFX = "rssripple:job:"


# ---------------------------------------------------------------------------
# Status constants
# ---------------------------------------------------------------------------

class JobStatus:
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class BaseQueue(ABC):
    """Common interface for in-process and Redis-backed task queues."""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[dict], Awaitable[Any]]] = {}

    def register(self, job_type: str, handler: Callable[[dict], Awaitable[Any]]) -> None:
        """Register an async handler for a job_type. Call before start()."""
        self._handlers[job_type] = handler
        logger.debug("Registered handler: job_type=%s", job_type)

    @abstractmethod
    async def start(self) -> None:
        """Start background worker(s). Must be awaited inside a running event loop."""

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully shut down the queue."""

    @abstractmethod
    async def enqueue(self, job_type: str, key: str, payload: dict) -> dict | None:
        """Enqueue a job.

        Returns job state dict on success.
        Returns None if a job for *key* is already active (dedup).
        If no handler is registered, the job is enqueued but will fail at
        execution time.
        """

    @abstractmethod
    async def status(self, key: str) -> dict | None:
        """Return the latest job state for *key*, or None if no job ever queued."""

    @abstractmethod
    async def clear(self, key: str) -> None:
        """Drop any stored job state for *key*.

        Callers use this after a terminal job has been observed so a new job
        with the same key can be enqueued. Implementations must be idempotent.
        """


# ---------------------------------------------------------------------------
# In-process asyncio implementation
# ---------------------------------------------------------------------------

class _MemJob:
    __slots__ = (
        "job_id", "job_type", "key", "payload",
        "status", "result", "error",
        "queued_at", "started_at", "finished_at",
    )

    def __init__(self, job_id: str, job_type: str, key: str, payload: dict) -> None:
        self.job_id = job_id
        self.job_type = job_type
        self.key = key
        self.payload = payload
        self.status = JobStatus.QUEUED
        self.result: Any = None
        self.error: str | None = None
        self.queued_at = utcnow()
        self.started_at: datetime | None = None
        self.finished_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "job_type": self.job_type,
            "key": self.key,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "queued_at": self.queued_at.isoformat(),
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


class MemoryQueue(BaseQueue):
    """asyncio-based task queue. Works in a single process; state is not shared
    across multiple processes or instances."""

    def __init__(self, max_concurrent: int = 1) -> None:
        super().__init__()
        self._max_concurrent = max_concurrent
        self._queue: asyncio.Queue[_MemJob] = asyncio.Queue()
        self._active_keys: set[str] = set()
        self._jobs_by_key: dict[str, _MemJob] = {}
        self._sem: asyncio.Semaphore | None = None
        self._dispatcher: asyncio.Task | None = None

    async def start(self) -> None:
        self._sem = asyncio.Semaphore(self._max_concurrent)
        self._dispatcher = asyncio.create_task(self._dispatch_loop())
        logger.info("MemoryQueue started (max_concurrent=%d)", self._max_concurrent)

    async def stop(self) -> None:
        if self._dispatcher:
            self._dispatcher.cancel()
            try:
                await self._dispatcher
            except asyncio.CancelledError:
                pass
        logger.info("MemoryQueue stopped")

    async def enqueue(self, job_type: str, key: str, payload: dict) -> dict | None:
        if key in self._active_keys:
            return None
        job = _MemJob(job_id=uuid.uuid4().hex[:8], job_type=job_type, key=key, payload=payload)
        self._active_keys.add(key)
        self._jobs_by_key[key] = job
        self._queue.put_nowait(job)
        logger.info("Enqueued %s/%s (job=%s)", job_type, key[:16], job.job_id)
        return job.to_dict()

    async def status(self, key: str) -> dict | None:
        job = self._jobs_by_key.get(key)
        return job.to_dict() if job else None

    async def clear(self, key: str) -> None:
        self._jobs_by_key.pop(key, None)

    async def _dispatch_loop(self) -> None:
        while True:
            try:
                job = await self._queue.get()
                asyncio.create_task(self._run(job))
            except asyncio.CancelledError:
                break

    async def _run(self, job: _MemJob) -> None:
        async with self._sem:
            job.status = JobStatus.RUNNING
            job.started_at = utcnow()
            logger.info("Running %s/%s (job=%s)", job.job_type, job.key[:16], job.job_id)
            try:
                handler = self._handlers.get(job.job_type)
                if handler is None:
                    raise RuntimeError(f"No handler registered for job_type={job.job_type!r}")
                job.result = await handler(job.payload)
                job.status = JobStatus.DONE
                logger.info("Done %s/%s", job.job_type, job.key[:16])
            except Exception as exc:
                job.status = JobStatus.FAILED
                job.error = str(exc)
                logger.error("Failed %s/%s: %s", job.job_type, job.key[:16], exc)
            finally:
                job.finished_at = utcnow()
                self._active_keys.discard(job.key)
                self._queue.task_done()


# ---------------------------------------------------------------------------
# Redis-backed implementation
# ---------------------------------------------------------------------------

class RedisQueue(BaseQueue):
    """redis.asyncio-backed task queue. Job descriptors and state are stored in
    Redis so that multiple app instances share the same queue and status store.

    Each instance that calls start() runs a local worker loop that pops job
    descriptors from a Redis list and executes the corresponding registered
    handler in-process.  All instances should register the same handlers; an
    instance without a handler for a given job_type will fail that job and move
    on.

    Args:
        redis_client: Pre-connected redis.asyncio client. When provided the
            queue uses it directly (useful for testing with fakeredis). When
            None a new client is created from redis_url on start().
        redis_url: Redis connection URL, used when redis_client is None.
        max_concurrent: Max simultaneous in-flight jobs per process.
        ttl: Seconds to retain job state in Redis after completion (default 24 h).
    """

    def __init__(
        self,
        redis_client=None,
        redis_url: str = "redis://localhost:6379/0",
        max_concurrent: int = MAX_CONCURRENT,
        ttl: int = JOB_TTL_SECONDS,
    ) -> None:
        super().__init__()
        self._redis_url = redis_url
        self._redis = redis_client
        self._max_concurrent = max_concurrent
        self._ttl = ttl
        self._sem: asyncio.Semaphore | None = None
        self._worker: asyncio.Task | None = None

    async def start(self) -> None:
        if self._redis is None:
            import redis.asyncio as aioredis  # lazy — not installed for MemoryQueue setups
            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
        self._sem = asyncio.Semaphore(self._max_concurrent)
        self._worker = asyncio.create_task(self._worker_loop())
        logger.info("RedisQueue started (url=%s, max_concurrent=%d)", self._redis_url, self._max_concurrent)

    async def stop(self) -> None:
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        if self._redis is not None:
            await self._redis.aclose()
        logger.info("RedisQueue stopped")

    async def enqueue(self, job_type: str, key: str, payload: dict) -> dict | None:
        active_key = f"{_ACTIVE_PFX}{key}"
        job_id = uuid.uuid4().hex[:8]
        now = utcnow().isoformat()

        # Atomic SETNX — only one active job per key across all instances
        acquired = await self._redis.set(active_key, job_id, nx=True, ex=self._ttl)
        if not acquired:
            return None

        job_hash = {
            "job_id": job_id,
            "job_type": job_type,
            "key": key,
            "status": JobStatus.QUEUED,
            "result": "",
            "error": "",
            "queued_at": now,
            "started_at": "",
            "finished_at": "",
        }
        redis_key = f"{_JOB_PFX}{key}"
        await self._redis.hset(redis_key, mapping=job_hash)
        await self._redis.expire(redis_key, self._ttl)

        msg = json.dumps({"job_id": job_id, "job_type": job_type, "key": key, "payload": payload})
        await self._redis.rpush(_QUEUE_LIST, msg)
        logger.info("Enqueued %s/%s (job=%s) → Redis", job_type, key[:16], job_id)
        return self._deserialize(job_hash)

    async def status(self, key: str) -> dict | None:
        raw = await self._redis.hgetall(f"{_JOB_PFX}{key}")
        return self._deserialize(raw) if raw else None

    async def clear(self, key: str) -> None:
        redis_key = f"{_JOB_PFX}{key}"
        active_key = f"{_ACTIVE_PFX}{key}"
        await self._redis.delete(redis_key, active_key)

    async def _worker_loop(self) -> None:
        while True:
            try:
                item = await self._redis.blpop(_QUEUE_LIST, timeout=1)
                if item is None:
                    continue
                _, raw = item
                asyncio.create_task(self._run(json.loads(raw)))
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("RedisQueue worker error: %s", exc)

    async def _run(self, msg: dict) -> None:
        job_type: str = msg["job_type"]
        key: str = msg["key"]
        payload: dict = msg["payload"]
        redis_key = f"{_JOB_PFX}{key}"
        active_key = f"{_ACTIVE_PFX}{key}"

        handler = self._handlers.get(job_type)
        if handler is None:
            logger.warning("No handler for job_type=%s — failing job", job_type)
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.hset(redis_key, mapping={
                    "status": JobStatus.FAILED,
                    "error": f"No handler registered for job_type={job_type!r}",
                    "finished_at": utcnow().isoformat(),
                })
                pipe.delete(active_key)
                pipe.expire(redis_key, self._ttl)
                await pipe.execute()
            return

        async with self._sem:
            await self._redis.hset(redis_key, mapping={
                "status": JobStatus.RUNNING,
                "started_at": utcnow().isoformat(),
            })
            logger.info("Running %s/%s", job_type, key[:16])

            # Build final state before the atomic pipeline so no await
            # separates setting the terminal status from releasing the dedup key.
            finish_status = JobStatus.FAILED
            finish_extra: dict = {}
            try:
                result = await handler(payload)
                finish_status = JobStatus.DONE
                finish_extra = {"result": json.dumps(result) if result is not None else ""}
                logger.info("Done %s/%s", job_type, key[:16])
            except Exception as exc:
                finish_extra = {"error": str(exc)}
                logger.error("Failed %s/%s: %s", job_type, key[:16], exc)

            # Atomic pipeline: write terminal state + delete dedup key together.
            # This prevents a window where status=DONE is visible but the active
            # key still blocks a re-enqueue.
            async with self._redis.pipeline(transaction=True) as pipe:
                pipe.hset(redis_key, mapping={
                    "status": finish_status,
                    "finished_at": utcnow().isoformat(),
                    **finish_extra,
                })
                pipe.delete(active_key)
                pipe.expire(redis_key, self._ttl)
                await pipe.execute()

    @staticmethod
    def _deserialize(raw: dict) -> dict:
        """Convert Redis hash string values back to a typed job state dict."""
        result_raw = raw.get("result", "")
        try:
            result = json.loads(result_raw) if result_raw else None
        except (json.JSONDecodeError, ValueError):
            result = result_raw or None

        return {
            "job_id": raw.get("job_id", ""),
            "job_type": raw.get("job_type", ""),
            "key": raw.get("key", ""),
            "status": raw.get("status", JobStatus.QUEUED),
            "result": result,
            "error": raw.get("error") or None,
            "queued_at": raw.get("queued_at") or None,
            "started_at": raw.get("started_at") or None,
            "finished_at": raw.get("finished_at") or None,
        }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def create_queue(backend: str = "memory", **kwargs) -> BaseQueue:
    """Instantiate a queue for the given backend.

    backend="memory"  → MemoryQueue  (default, no external deps)
    backend="redis"   → RedisQueue   (requires redis package + a Redis server)

    Only kwargs accepted by the target backend constructor are forwarded; the
    rest are silently ignored (e.g. redis_url is ignored for MemoryQueue).
    """
    if backend == "redis":
        redis_kwargs = {k: v for k, v in kwargs.items()
                        if k in ("redis_client", "redis_url", "max_concurrent", "ttl")}
        return RedisQueue(**redis_kwargs)
    memory_kwargs = {k: v for k, v in kwargs.items() if k in ("max_concurrent",)}
    return MemoryQueue(**memory_kwargs)


# ---------------------------------------------------------------------------
# Process-level singleton — replaced during app lifespan (see app/main.py)
# ---------------------------------------------------------------------------

task_queue: BaseQueue = MemoryQueue()
