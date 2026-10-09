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
import socket
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from contextvars import Context, ContextVar, copy_context
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.utils.time import utc_isoformat, utcnow

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _ExecutionOwnership:
    queue: "RedisQueue"
    key: str
    job_id: str
    token: str


_execution_ownership: ContextVar[_ExecutionOwnership | None] = ContextVar("queue_execution_ownership", default=None)


def independent_execution_context() -> Context:
    """Detach queue ownership only after another durable owner has been claimed."""
    context = copy_context()
    context.run(_execution_ownership.set, None)
    return context


def current_job_identity() -> tuple[str, str] | None:
    owner = _execution_ownership.get()
    return (owner.key, owner.job_id) if owner is not None else None


class ExecutionOwnershipLostError(RuntimeError):
    """The current queued execution no longer owns its job."""


async def require_execution_ownership() -> None:
    """Reject stale Redis handlers before starting a business operation.

    Direct API calls and MemoryQueue have no Redis execution context. This
    check does not fence an external operation after it has already started.
    Redis failure propagates: unknown ownership is not permission to proceed.
    """
    owner = _execution_ownership.get()
    if owner is None:
        return
    queue = owner.queue
    async with queue._redis.pipeline(transaction=True) as pipe:
        pipe.hgetall(f"{_JOB_PFX}{owner.key}")
        pipe.get(f"{_ACTIVE_PFX}{owner.key}")
        pipe.exists(queue._consumer_key)
        state, active, leased = await pipe.execute()
    if (state.get("job_id") != owner.job_id or
            state.get("status") != JobStatus.RUNNING or
            state.get("execution_token") != owner.token or
            state.get("consumer_id") != queue._consumer_id or
            active != owner.job_id or not leased):
        raise ExecutionOwnershipLostError(f"Queue execution lost ownership: {owner.key}")


MAX_CONCURRENT = 4
JOB_TTL_SECONDS = 86_400  # 24 h — how long Redis keeps job state after completion
# MemoryQueue has no TTL expiry; bound its terminal-state retention instead.
# Queued/running entries are never evicted — only done/failed ones, oldest
# finish first (mirrors the Redis backend's JOB_TTL_SECONDS expiry semantics).
MEMORY_TERMINAL_JOB_RETENTION = 1000

# Redis key prefixes
_QUEUE_LIST = "rssripple:jobs"
# Delayed retries: score = epoch seconds when the descriptor becomes due.
# Shared (not per-consumer) and durable, so a crashed worker's scheduled
# retry is promoted by whichever consumer notices it first.
_DELAYED_ZSET = "rssripple:jobs:delayed"
_ACTIVE_PFX = "rssripple:active:"
_TICK_PFX = "rssripple:tick:"
_JOB_PFX = "rssripple:job:"
_PROCESSING_PFX = "rssripple:processing:"
_CONSUMER_PFX = "rssripple:consumer:"
_RECOVERY_LOCK = "rssripple:recovery-lock"

CONSUMER_LEASE_SECONDS = 15
CONSUMER_HEARTBEAT_SECONDS = 5

# ---------------------------------------------------------------------------
# Automatic retry policy
# ---------------------------------------------------------------------------

# Job types whose handlers are verified idempotent and therefore eligible for
# bounded automatic retry. Every other type runs exactly once (failure is
# terminal) — see the per-type rationale in docs/design/business-logic.md.
_RETRYABLE_JOB_TYPES = frozenset({
    "fetch_channel",             # re-fetching the RSS feed upserts resources
    "refresh_works_metadata",    # per-work refresh re-applies the same metadata
    "refresh_channel_works",     # same pipeline, channel-scoped
    "backfill_metadata",         # global re-scan of retry-eligible resources
    "sync_progress",             # read-only RPC + status sync
    "daily_cleanup",             # idempotent expiry/deletion sweep
    "daily_dedup",               # idempotent metadata merge
    "check_downloaders",         # connectivity probe
    "fts_drain",                 # outbox replay is idempotent
    "fts_reconcile",             # full diff/rewrite of shadow tables
    "magnet_resolve_sweep",      # re-claims only NULL-status rows
    "refresh_resource_organize",  # rebuilds the notification snapshot from current state
})

# Deliberately NOT retried at the queue level (kept explicit so new job types
# must take a position; the test suite asserts this covers every registered
# handler):
_NON_RETRYABLE_JOB_TYPES = frozenset({
    # Own durable recovery: on failure the watermark is not advanced and
    # requests are deferred with their own backoff; a queue retry would pile
    # duplicate AgentRun history rows onto the same incident.
    "run_agent",
    # The durable ResourceReparseRequest is acknowledged in the handler's
    # finally before the failure propagates, so a retry would immediately
    # no-op as "superseded".
    "reprocess_resource_metadata",
    # Interactive LLM job: a failure must surface to the polling user
    # promptly (manual re-run with force=true), not be hidden behind backoff.
    "analyze_batch_files",
    # Delivery retries live in the notification backoff state machine; the
    # tick handler already self-contains its failures.
    "download_notifications",
    # Magnet resolution has its own attempt budget + backoff and claims the
    # DB row; a re-launched queue job no-ops on the already-claimed row.
    "resolve_magnet_torrent",
})

RETRY_BACKOFF_CAP_SECONDS = 3600.0


def _retry_delay(backoff_seconds: float, failed_attempt: int) -> float:
    """Exponential backoff after the Nth failed attempt (1-based), capped."""
    return min(backoff_seconds * (2 ** (failed_attempt - 1)), RETRY_BACKOFF_CAP_SECONDS)


def resolve_retry_policy(
    job_type: str,
    max_attempts: int | None = None,
    backoff_seconds: float | None = None,
) -> tuple[int, float]:
    """Resolve (max_attempts, backoff_seconds) for an enqueue.

    Explicit enqueue arguments win. Otherwise a retryable job type uses the
    configured defaults (QUEUE_JOB_MAX_ATTEMPTS / QUEUE_JOB_RETRY_BACKOFF_SECONDS);
    every other type runs exactly once.
    """
    from app.config import settings

    if max_attempts is None:
        max_attempts = (
            settings.queue_job_max_attempts if job_type in _RETRYABLE_JOB_TYPES else 1
        )
    if backoff_seconds is None:
        backoff_seconds = settings.queue_job_retry_backoff_seconds
    return max(1, int(max_attempts)), max(0.0, float(backoff_seconds))

# Operational reconciliation ticks must not sit behind a large backlog of
# slow metadata/LLM work. They are short, idempotent jobs whose freshness is
# user-visible (download progress, completion, notifications, connectivity).
_PRIORITY_JOB_TYPES = {
    "sync_progress",
    "download_notifications",
    "check_downloaders",
}


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

    @property
    def redis_client(self):
        """Connected redis.asyncio client, or None for non-Redis backends.

        Lets other cross-process services (e.g. submission_guard) reuse the
        deployment's Redis connection instead of opening their own."""
        return None

    def register(self, job_type: str, handler: Callable[[dict], Awaitable[Any]]) -> None:
        """Register an async handler for a job_type. Call before start()."""
        self._handlers[job_type] = handler
        logger.debug("Registered handler: job_type=%s", job_type)

    @abstractmethod
    async def start(self, consume: bool = True) -> None:
        """Start background worker(s). Must be awaited inside a running event loop.

        With ``consume=False`` the queue only enqueues/tracks jobs — no
        dispatcher/worker loop is started (used by the web role in a split
        web/worker deployment, where a separate worker process consumes).
        """

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully shut down the queue."""

    @abstractmethod
    async def enqueue(
        self,
        job_type: str,
        key: str,
        payload: dict,
        *,
        max_attempts: int | None = None,
        backoff_seconds: float | None = None,
    ) -> dict | None:
        """Enqueue a job.

        Returns job state dict on success.
        Returns None if a job for *key* is already active (dedup).
        If no handler is registered, the job is enqueued but will fail at
        execution time.

        Retry: when the handler fails and the job has attempts remaining, it
        is re-queued after an exponential backoff instead of going straight to
        FAILED. ``max_attempts`` counts the first run; ``None`` resolves the
        per-type policy (see :func:`resolve_retry_policy`). The dedup key and
        crash-recovery semantics are held across retries.
        """

    async def job_is_retired(self, key: str, job_id: str) -> bool:
        """Only durable backends can prove a historical execution is retired."""
        return False

    @abstractmethod
    async def status(self, key: str) -> dict | None:
        """Return the latest job state for *key*, or None if no job ever queued."""

    @abstractmethod
    async def list_jobs(self) -> list[dict]:
        """Return a snapshot of every job state known to the backend.

        Same dict shape as ``status()``. This is a live, best-effort view:
        MemoryQueue covers jobs since process start, RedisQueue covers jobs
        whose state hash has not expired yet (``JOB_TTL_SECONDS``).
        """

    @abstractmethod
    async def clear(self, key: str) -> None:
        """Drop any stored job state for *key*.

        Callers use this after a terminal job has been observed so a new job
        with the same key can be enqueued. Implementations must be idempotent.
        """

    @abstractmethod
    async def update_progress(self, key: str, result: dict) -> None:
        """Replace a queued/running job's intermediate result payload."""

    async def throttle(self, key: str, ttl: int) -> bool:
        """Cross-process tick throttle: True if the caller may proceed.

        The first caller within ``ttl`` seconds wins; everyone else gets
        False. Used by periodic scheduler ticks so that N worker processes
        each running their own scheduler collapse to one enqueue per
        interval (the queue's active-key dedup alone only covers
        *concurrent* duplicates — staggered ticks each complete before the
        next fires). The default implementation always returns True:
        single-process backends have exactly one scheduler.
        """
        return True

    async def release_throttle(self, key: str) -> None:
        """Release a tick key claimed by :meth:`throttle`.

        Called when the follow-up enqueue failed after throttle() won: the
        interval must not be burned, so the next scheduler tick (on any
        worker) may retry instead of waiting out the throttle TTL. The
        default implementation is a no-op — in-process backends never store
        tick keys, so there is nothing to release.
        """


# ---------------------------------------------------------------------------
# In-process asyncio implementation
# ---------------------------------------------------------------------------

class _MemJob:
    __slots__ = (
        "job_id", "job_type", "key", "payload",
        "status", "result", "error",
        "queued_at", "started_at", "finished_at",
        "attempt", "max_attempts", "backoff_seconds", "next_retry_at",
    )

    def __init__(
        self,
        job_id: str,
        job_type: str,
        key: str,
        payload: dict,
        max_attempts: int = 1,
        backoff_seconds: float = 0.0,
    ) -> None:
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
        self.attempt = 0
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.next_retry_at: datetime | None = None

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "job_type": self.job_type,
            "key": self.key,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "queued_at": utc_isoformat(self.queued_at),
            "started_at": utc_isoformat(self.started_at),
            "finished_at": utc_isoformat(self.finished_at),
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "backoff_seconds": self.backoff_seconds,
            "next_retry_at": utc_isoformat(self.next_retry_at),
        }


class MemoryQueue(BaseQueue):
    """asyncio-based task queue. Works in a single process; state is not shared
    across multiple processes or instances.

    Terminal job state is bounded: only the most recent
    ``MEMORY_TERMINAL_JOB_RETENTION`` done/failed jobs are kept (oldest finish
    evicted first), mirroring the Redis backend's ``JOB_TTL_SECONDS`` expiry.
    Queued/running entries are never evicted.
    """

    def __init__(self, max_concurrent: int = 1) -> None:
        super().__init__()
        self._max_concurrent = max_concurrent
        self._queue: asyncio.Queue[_MemJob] = asyncio.Queue()
        self._active_keys: set[str] = set()
        self._jobs_by_key: dict[str, _MemJob] = {}
        self._sem: asyncio.Semaphore | None = None
        self._dispatcher: asyncio.Task | None = None
        # Strong references to in-flight run tasks. The event loop only holds
        # weak refs to tasks, so a fire-and-forget create_task() can be
        # garbage-collected mid-flight — the job would never run and its
        # dedup key would leak in _active_keys.
        self._run_tasks: set[asyncio.Task] = set()
        # Tasks sleeping out a retry backoff before re-queueing their job.
        self._retry_tasks: set[asyncio.Task] = set()

    async def start(self, consume: bool = True) -> None:
        self._sem = asyncio.Semaphore(self._max_concurrent)
        if consume:
            self._dispatcher = asyncio.create_task(self._dispatch_loop())
        logger.info(
            "MemoryQueue started (max_concurrent=%d, consume=%s)",
            self._max_concurrent, consume,
        )

    async def stop(self) -> None:
        if self._dispatcher:
            self._dispatcher.cancel()
            try:
                await self._dispatcher
            except asyncio.CancelledError:
                pass
            self._dispatcher = None
        # Jobs still queued will never run: fail them instead of silently
        # dropping them (a stopped MemoryQueue has no durable backlog to
        # requeue to, unlike RedisQueue.stop, which requeues in-flight work).
        while True:
            try:
                job = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if self._jobs_by_key.get(job.key) is job and job.status == JobStatus.QUEUED:
                job.status = JobStatus.FAILED
                job.error = "queue stopped before the job ran"
                job.finished_at = utcnow()
                self._active_keys.discard(job.key)
            self._queue.task_done()
        for task in self._run_tasks:
            task.cancel()
        if self._run_tasks:
            await asyncio.gather(*self._run_tasks, return_exceptions=True)
        self._run_tasks.clear()
        # Jobs waiting out a retry backoff are not in the backlog queue, so
        # the drain above never saw them: cancel the sleeps and fail the jobs
        # (a stopped MemoryQueue has no durable backlog to hold the delay).
        for task in self._retry_tasks:
            task.cancel()
        if self._retry_tasks:
            await asyncio.gather(*self._retry_tasks, return_exceptions=True)
        self._retry_tasks.clear()
        for job in list(self._jobs_by_key.values()):
            if job.status == JobStatus.QUEUED and job.next_retry_at is not None:
                job.status = JobStatus.FAILED
                job.error = (
                    f"{job.error}; queue stopped during retry backoff"
                    if job.error else "queue stopped during retry backoff"
                )
                job.next_retry_at = None
                job.finished_at = utcnow()
                self._active_keys.discard(job.key)
        self._evict_terminal_jobs()
        logger.info("MemoryQueue stopped")

    async def enqueue(
        self,
        job_type: str,
        key: str,
        payload: dict,
        *,
        max_attempts: int | None = None,
        backoff_seconds: float | None = None,
    ) -> dict | None:
        if key in self._active_keys:
            return None
        max_attempts, backoff_seconds = resolve_retry_policy(
            job_type, max_attempts, backoff_seconds
        )
        job = _MemJob(
            job_id=uuid.uuid4().hex, job_type=job_type, key=key, payload=payload,
            max_attempts=max_attempts, backoff_seconds=backoff_seconds,
        )
        self._active_keys.add(key)
        self._jobs_by_key[key] = job
        self._queue.put_nowait(job)
        logger.info("Enqueued %s/%s (job=%s)", job_type, key[:16], job.job_id)
        return job.to_dict()

    async def status(self, key: str) -> dict | None:
        job = self._jobs_by_key.get(key)
        return job.to_dict() if job else None

    async def list_jobs(self) -> list[dict]:
        return [job.to_dict() for job in self._jobs_by_key.values()]

    async def clear(self, key: str) -> None:
        job = self._jobs_by_key.pop(key, None)
        if job is not None and job.status != JobStatus.RUNNING:
            # Queued/terminal jobs hold no in-flight execution, so the dedup
            # key goes with the state entry (otherwise a cleared key stayed
            # blocked forever). A running job's key is released by _run's
            # finally when the execution ends.
            self._active_keys.discard(key)

    def _evict_terminal_jobs(self) -> None:
        """Bound _jobs_by_key: drop the oldest terminal jobs past retention."""
        terminal = [
            job for job in self._jobs_by_key.values()
            if job.status in (JobStatus.DONE, JobStatus.FAILED)
        ]
        excess = len(terminal) - MEMORY_TERMINAL_JOB_RETENTION
        if excess <= 0:
            return
        terminal.sort(key=lambda job: job.finished_at or job.queued_at)
        for job in terminal[:excess]:
            self._jobs_by_key.pop(job.key, None)

    async def update_progress(self, key: str, result: dict) -> None:
        job = self._jobs_by_key.get(key)
        if job is not None and job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
            job.result = result

    async def _dispatch_loop(self) -> None:
        while True:
            try:
                job = await self._queue.get()
                task = asyncio.create_task(self._run(job))
                self._run_tasks.add(task)
                task.add_done_callback(self._run_tasks.discard)
            except asyncio.CancelledError:
                break

    def _schedule_retry(self, job: _MemJob, delay: float) -> None:
        """Re-queue *job* after *delay* seconds, keeping its dedup key held."""

        async def _requeue() -> None:
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                return  # stop() fails the job itself
            # Cleared or superseded during the backoff window: never requeue.
            if self._jobs_by_key.get(job.key) is job and job.status == JobStatus.QUEUED:
                job.next_retry_at = None
                self._queue.put_nowait(job)

        task = asyncio.create_task(_requeue())
        self._retry_tasks.add(task)
        task.add_done_callback(self._retry_tasks.discard)

    async def _run(self, job: _MemJob) -> None:
        try:
            async with self._sem:
                if self._jobs_by_key.get(job.key) is not job:
                    # Cleared or superseded while queued: never execute, and
                    # never touch the dedup key — a replacement job that was
                    # enqueued after the clear owns it now.
                    return
                job.status = JobStatus.RUNNING
                job.started_at = utcnow()
                job.attempt += 1
                logger.info(
                    "Running %s/%s (job=%s, attempt %d/%d)",
                    job.job_type, job.key[:16], job.job_id, job.attempt, job.max_attempts,
                )
                try:
                    handler = self._handlers.get(job.job_type)
                    if handler is None:
                        raise RuntimeError(f"No handler registered for job_type={job.job_type!r}")
                    job.result = await handler(job.payload)
                    job.status = JobStatus.DONE
                    job.error = None  # clear the previous attempt's failure
                    logger.info("Done %s/%s", job.job_type, job.key[:16])
                except Exception as exc:
                    job.error = str(exc)
                    if job.attempt < job.max_attempts:
                        delay = _retry_delay(job.backoff_seconds, job.attempt)
                        job.status = JobStatus.QUEUED
                        job.started_at = None
                        job.next_retry_at = utcnow() + timedelta(seconds=delay)
                        self._schedule_retry(job, delay)
                        logger.warning(
                            "Failed %s/%s (attempt %d/%d); retrying in %.1fs: %s",
                            job.job_type, job.key[:16], job.attempt, job.max_attempts,
                            delay, exc,
                        )
                    else:
                        job.status = JobStatus.FAILED
                        logger.error("Failed %s/%s: %s", job.job_type, job.key[:16], exc)
        finally:
            self._queue.task_done()
            current = self._jobs_by_key.get(job.key)
            if current is job:
                if job.status == JobStatus.QUEUED and job.next_retry_at is not None:
                    # A retry is scheduled: keep the state entry and the dedup
                    # key held until the job reaches a terminal state.
                    return
                if job.status in (JobStatus.QUEUED, JobStatus.RUNNING):
                    # Cancelled by stop() before completion: there is no
                    # durable backlog to requeue to, so record a terminal
                    # state instead of leaking a permanently active dedup key.
                    job.status = JobStatus.FAILED
                    job.error = job.error or "queue stopped before the job finished"
                job.finished_at = job.finished_at or utcnow()
                self._active_keys.discard(job.key)
                self._evict_terminal_jobs()
            elif current is None:
                # Cleared mid-run: the state entry is gone, but this run
                # still owned the dedup key and must release it.
                self._active_keys.discard(job.key)
            # else: superseded by a replacement job — it owns the key now.


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
        self._heartbeat: asyncio.Task | None = None
        self._consumer_id = f"{socket.gethostname()}:{uuid.uuid4().hex}"
        self._processing_key = f"{_PROCESSING_PFX}{self._consumer_id}"
        self._consumer_key = f"{_CONSUMER_PFX}{self._consumer_id}"
        # Strong references to in-flight jobs. More importantly, the worker
        # acquires a slot before claiming work so Redis remains the durable backlog;
        # jobs must never be prefetched into unbounded local tasks while all
        # execution slots are occupied by slow handlers.
        self._run_tasks: set[asyncio.Task] = set()

    @property
    def redis_client(self):
        """The connected redis.asyncio client, or None before start()."""
        return self._redis

    async def start(self, consume: bool = True) -> None:
        if self._redis is None:
            import redis.asyncio as aioredis  # lazy — not installed for MemoryQueue setups
            self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
        self._sem = asyncio.Semaphore(self._max_concurrent)
        if consume:
            await self._redis.set(
                self._consumer_key, "1", ex=CONSUMER_LEASE_SECONDS,
            )
            await self._recover_orphaned_jobs(reconcile_legacy=True)
            # fakeredis serialises concurrent commands on one in-memory
            # socket and can deadlock a polling worker against a heartbeat.
            # Startup recovery remains covered there; real Redis runs leases.
            if not type(self._redis).__module__.startswith("fakeredis"):
                self._heartbeat = asyncio.create_task(self._heartbeat_loop())
            self._worker = asyncio.create_task(self._worker_loop())
        logger.info(
            "RedisQueue started (url=%s, max_concurrent=%d, consume=%s)",
            self._redis_url, self._max_concurrent, consume,
        )

    async def stop(self) -> None:
        if self._heartbeat:
            self._heartbeat.cancel()
            try:
                await self._heartbeat
            except asyncio.CancelledError:
                pass
        if self._worker:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
        for task in self._run_tasks:
            task.cancel()
        if self._run_tasks:
            await asyncio.gather(*self._run_tasks, return_exceptions=True)
        self._run_tasks.clear()
        if self._redis is not None:
            await self._redis.delete(self._consumer_key)
        if self._redis is not None:
            await self._redis.aclose()
        logger.info("RedisQueue stopped")

    async def throttle(self, key: str, ttl: int) -> bool:
        """SET NX EX on a tick key — only the first scheduler tick within
        ``ttl`` seconds across all worker processes proceeds."""
        if self._redis is None:
            return True  # not started yet — nothing to throttle against
        return bool(await self._redis.set(f"{_TICK_PFX}{key}", "1", nx=True, ex=ttl))

    async def release_throttle(self, key: str) -> None:
        """Delete the tick key after the follow-up enqueue failed, so the
        interval is retried by the next tick instead of being lost until the
        throttle TTL expires (≈ a full interval — 24h for the daily jobs)."""
        if self._redis is None:
            return
        await self._redis.delete(f"{_TICK_PFX}{key}")

    async def enqueue(
        self,
        job_type: str,
        key: str,
        payload: dict,
        *,
        max_attempts: int | None = None,
        backoff_seconds: float | None = None,
    ) -> dict | None:
        from redis.exceptions import WatchError

        max_attempts, backoff_seconds = resolve_retry_policy(
            job_type, max_attempts, backoff_seconds
        )
        active_key = f"{_ACTIVE_PFX}{key}"
        job_id = uuid.uuid4().hex
        now = utcnow().isoformat()

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
            "execution_token": "",
            "consumer_id": "",
            "attempt": "0",
            "max_attempts": str(max_attempts),
            "backoff_seconds": str(backoff_seconds),
            "next_retry_at": "",
        }
        redis_key = f"{_JOB_PFX}{key}"
        msg = json.dumps({"job_id": job_id, "job_type": job_type, "key": key, "payload": payload})
        job_hash["message"] = msg
        while True:
            async with self._redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(active_key)
                    if await pipe.exists(active_key):
                        return None
                    pipe.multi()
                    pipe.set(active_key, job_id, ex=self._ttl)
                    pipe.hset(redis_key, mapping=job_hash)
                    pipe.expire(redis_key, self._ttl)
                    if job_type in _PRIORITY_JOB_TYPES:
                        pipe.lpush(_QUEUE_LIST, msg)
                    else:
                        pipe.rpush(_QUEUE_LIST, msg)
                    await pipe.execute()
                    break
                except WatchError:
                    continue
        logger.info("Enqueued %s/%s (job=%s) → Redis", job_type, key[:16], job_id)
        return self._deserialize(job_hash)

    async def job_is_retired(self, key: str, job_id: str) -> bool:
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.hgetall(f"{_JOB_PFX}{key}")
            pipe.get(f"{_ACTIVE_PFX}{key}")
            state, active = await pipe.execute()
        if active == job_id:
            return False
        if not state:
            return True
        if not state.get("job_id") or state.get("status") not in {
            JobStatus.QUEUED, JobStatus.RUNNING, JobStatus.DONE, JobStatus.FAILED,
        }:
            return False
        return state["job_id"] != job_id or state["status"] in {JobStatus.DONE, JobStatus.FAILED}

    async def status(self, key: str) -> dict | None:
        raw = await self._redis.hgetall(f"{_JOB_PFX}{key}")
        return self._deserialize(raw) if raw else None

    async def list_jobs(self) -> list[dict]:
        # Collect keys first, then fetch all hashes in one pipeline —
        # sequential HGETALLs cost one round trip per key, which gets slow
        # once per-entity keys (magnet:<id>, refresh_works:<uuid>, …)
        # accumulate within the TTL window.
        redis_keys = [
            key async for key in self._redis.scan_iter(f"{_JOB_PFX}*", count=500)
        ]
        if not redis_keys:
            return []
        async with self._redis.pipeline(transaction=False) as pipe:
            for redis_key in redis_keys:
                pipe.hgetall(redis_key)
            raws = await pipe.execute()
        jobs: list[dict] = []
        for redis_key, raw in zip(redis_keys, raws):
            try:
                if raw:
                    jobs.append(self._deserialize(raw))
            except Exception as exc:
                # One malformed hash must not sink the whole snapshot.
                logger.warning("Skipping malformed job state %s: %s", redis_key, exc)
        return jobs

    async def clear(self, key: str) -> None:
        redis_key = f"{_JOB_PFX}{key}"
        active_key = f"{_ACTIVE_PFX}{key}"
        await self._redis.delete(redis_key, active_key)

    async def update_progress(self, key: str, result: dict) -> None:
        from redis.exceptions import WatchError

        owner = _execution_ownership.get()
        if owner is None or owner.queue is not self or owner.key != key:
            return
        redis_key = f"{_JOB_PFX}{key}"
        active = f"{_ACTIVE_PFX}{key}"
        while True:
            async with self._redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(redis_key, active, self._consumer_key)
                    state = await pipe.hgetall(redis_key)
                    if (state.get("job_id") != owner.job_id or
                            state.get("status") != JobStatus.RUNNING or
                            state.get("execution_token") != owner.token or
                            state.get("consumer_id") != self._consumer_id or
                            await pipe.get(active) != owner.job_id or
                            not await pipe.exists(self._consumer_key)):
                        return
                    pipe.multi()
                    pipe.hset(redis_key, "result", json.dumps(result))
                    pipe.expire(redis_key, self._ttl)
                    await pipe.execute()
                    return
                except WatchError:
                    continue

    async def _promote_due_retries(self) -> None:
        """Move due retry descriptors from the delayed zset to the backlog.

        ZREM decides the winner when several consumers notice the same due
        entry, so a descriptor is never pushed twice. Entries whose job was
        cleared meanwhile fail the ownership check at claim time and are
        garbage-collected by lease recovery.
        """
        due = await self._redis.zrangebyscore(_DELAYED_ZSET, 0, time.time())
        for raw in due:
            if not await self._redis.zrem(_DELAYED_ZSET, raw):
                continue  # another consumer won the promotion
            try:
                job_type = json.loads(raw).get("job_type")
            except (json.JSONDecodeError, AttributeError):
                job_type = None
            if job_type in _PRIORITY_JOB_TYPES:
                await self._redis.lpush(_QUEUE_LIST, raw)
            else:
                await self._redis.rpush(_QUEUE_LIST, raw)

    async def _worker_loop(self) -> None:
        while True:
            slot_acquired = False
            try:
                # Promote due retries before reserving capacity so a saturated
                # semaphore never delays them past their backoff (they wait
                # for a slot in the durable backlog like any queued job).
                await self._promote_due_retries()
                # Reserve execution capacity before removing a durable job
                # from Redis. The old order BLPOP'ed the entire backlog and
                # created local tasks waiting on the semaphore, starving
                # periodic sync jobs and losing prefetched jobs on restart.
                await self._sem.acquire()
                slot_acquired = True
                # LMOVE is the reliable-queue claim: the descriptor changes
                # lists atomically. Polling also keeps fakeredis and older
                # Redis-compatible services from blocking the event loop on
                # an empty BLMOVE.
                raw = await self._redis.lmove(
                    _QUEUE_LIST, self._processing_key, "LEFT", "RIGHT",
                )
                if raw is None:
                    self._sem.release()
                    slot_acquired = False
                    await asyncio.sleep(0.1)
                    continue
                task = asyncio.create_task(self._run(json.loads(raw), raw))
                self._run_tasks.add(task)
                task.add_done_callback(self._run_tasks.discard)
                # _run owns the reserved slot from here.
                slot_acquired = False
            except asyncio.CancelledError:
                if slot_acquired:
                    self._sem.release()
                break
            except Exception as exc:
                if slot_acquired:
                    self._sem.release()
                logger.error("RedisQueue worker error: %s", exc)

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(CONSUMER_HEARTBEAT_SECONDS)
                await self._redis.set(
                    self._consumer_key, "1", ex=CONSUMER_LEASE_SECONDS,
                )
                await self._recover_orphaned_jobs()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("RedisQueue heartbeat failed: %s", exc)

    async def _recover_descriptor(self, processing_key, consumer_id, raw, msg) -> bool:
        """Fence recovery against renewal, completion and another claimant."""
        from redis.exceptions import WatchError

        key = f"{_JOB_PFX}{msg['key']}"
        active = f"{_ACTIVE_PFX}{msg['key']}"
        lease = f"{_CONSUMER_PFX}{consumer_id}"
        while True:
            async with self._redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key, active, lease, processing_key)
                    state = await pipe.hgetall(key)
                    if (await pipe.exists(lease) or
                            state.get("job_id") != msg["job_id"] or
                            state.get("status") not in (JobStatus.QUEUED, JobStatus.RUNNING) or
                            state.get("consumer_id") not in (None, "", consumer_id) or
                            await pipe.get(active) != msg["job_id"] or
                            raw not in await pipe.lrange(processing_key, 0, -1)):
                        return False
                    pipe.multi()
                    pipe.hset(key, mapping={
                        "status": JobStatus.QUEUED, "started_at": "",
                        "finished_at": "", "error": "",
                        "execution_token": "", "consumer_id": "",
                        "next_retry_at": "",
                    })
                    if msg.get("job_type") in _PRIORITY_JOB_TYPES:
                        pipe.lpush(_QUEUE_LIST, raw)
                    else:
                        pipe.rpush(_QUEUE_LIST, raw)
                    pipe.lrem(processing_key, 1, raw)
                    await pipe.execute()
                    return True
                except WatchError:
                    continue

    async def _recover_orphaned_jobs(self, *, reconcile_legacy: bool = False) -> None:
        """Return jobs owned by dead consumers to the durable backlog.

        The short recovery lock makes this safe when several workers start at
        once. A processing list is touched only after its consumer lease has
        expired, so work executing in another healthy process is never stolen.
        """
        recovery_token = uuid.uuid4().hex
        acquired = await self._redis.set(_RECOVERY_LOCK, recovery_token, nx=True, ex=30)
        if not acquired:
            return
        try:
            processing_job_ids: set[str] = set()
            async for processing_key in self._redis.scan_iter(f"{_PROCESSING_PFX}*"):
                consumer_id = processing_key.removeprefix(_PROCESSING_PFX)
                messages = await self._redis.lrange(processing_key, 0, -1)
                for raw in messages:
                    try:
                        msg = json.loads(raw)
                        processing_job_ids.add(msg["job_id"])
                    except (json.JSONDecodeError, KeyError, TypeError):
                        await self._redis.lrem(processing_key, 1, raw)
                        continue
                if await self._redis.exists(f"{_CONSUMER_PFX}{consumer_id}"):
                    continue
                recovered = 0
                for raw in messages:
                    try:
                        msg = json.loads(raw)
                        state = await self._redis.hgetall(f"{_JOB_PFX}{msg['key']}")
                    except (json.JSONDecodeError, KeyError, TypeError):
                        continue
                    if (state.get("job_id") == msg.get("job_id") and
                            state.get("status") in (JobStatus.QUEUED, JobStatus.RUNNING)):
                        if await self._recover_descriptor(processing_key, consumer_id, raw, msg):
                            recovered += 1
                    else:
                        await self._redis.lrem(processing_key, 1, raw)
                # Redis removes empty lists automatically. A separate DELETE
                # could erase a descriptor claimed by a resumed consumer.
                if recovered:
                    logger.warning("Recovered %d jobs from dead consumer %s", recovered, consumer_id)

            if not reconcile_legacy:
                return

            # Upgrade safety: old queue versions removed a job from Redis
            # before executing it, so their running hashes have no recoverable
            # descriptor. Release those locks instead of preserving a zombie.
            async for job_key in self._redis.scan_iter(f"{_JOB_PFX}*"):
                state = await self._redis.hgetall(job_key)
                if (state.get("status") == JobStatus.RUNNING and
                        not state.get("message") and
                        state.get("job_id") not in processing_job_ids):
                    await self._fail_legacy_execution(job_key, state.get("job_id"))
        finally:
            # Do not turn task cancellation into another Redis round trip;
            # the lock has a short TTL and cancellation must remain prompt.
            current = asyncio.current_task()
            if not current or not current.cancelling():
                await self._release_recovery_lock(recovery_token)

    async def _fail_legacy_execution(self, job_key: str, expected_id: str | None) -> None:
        from redis.exceptions import WatchError

        active = f"{_ACTIVE_PFX}{job_key.removeprefix(_JOB_PFX)}"
        while True:
            async with self._redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(job_key, active)
                    state = await pipe.hgetall(job_key)
                    if (not expected_id or state.get("job_id") != expected_id or
                            state.get("status") != JobStatus.RUNNING or
                            state.get("message") or state.get("execution_token")):
                        return
                    owns_active = await pipe.get(active) == expected_id
                    pipe.multi()
                    pipe.hset(job_key, mapping={
                        "status": JobStatus.FAILED,
                        "error": "worker interrupted before durable recovery was available",
                        "finished_at": utcnow().isoformat(),
                    })
                    if owns_active:
                        pipe.delete(active)
                    await pipe.execute()
                    return
                except WatchError:
                    continue

    async def _release_recovery_lock(self, token: str) -> None:
        from redis.exceptions import WatchError

        while True:
            async with self._redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(_RECOVERY_LOCK)
                    if await pipe.get(_RECOVERY_LOCK) != token:
                        return
                    pipe.multi()
                    pipe.delete(_RECOVERY_LOCK)
                    await pipe.execute()
                    return
                except WatchError:
                    continue

    async def _claim_execution(self, msg: dict) -> tuple[str, int, int, float] | None:
        from redis.exceptions import WatchError

        key = f"{_JOB_PFX}{msg['key']}"
        active = f"{_ACTIVE_PFX}{msg['key']}"
        token = uuid.uuid4().hex
        while True:
            async with self._redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key, active, self._consumer_key)
                    state = await pipe.hgetall(key)
                    if (state.get("job_id") != msg["job_id"] or
                            state.get("status") != JobStatus.QUEUED or
                            await pipe.get(active) != msg["job_id"] or
                            not await pipe.exists(self._consumer_key)):
                        return None
                    attempt = int(state.get("attempt") or 0) + 1
                    pipe.multi()
                    pipe.hset(key, mapping={
                        "status": JobStatus.RUNNING,
                        "started_at": utcnow().isoformat(),
                        "execution_token": token,
                        "consumer_id": self._consumer_id,
                        "attempt": str(attempt),
                        "next_retry_at": "",
                    })
                    await pipe.execute()
                    return (
                        token,
                        attempt,
                        int(state.get("max_attempts") or 1),
                        float(state.get("backoff_seconds") or 0.0),
                    )
                except WatchError:
                    continue

    async def _finish_execution(
        self, msg, raw, token, status, extra, *, requeue, retry_delay=None,
    ):
        from redis.exceptions import WatchError

        key = f"{_JOB_PFX}{msg['key']}"
        active = f"{_ACTIVE_PFX}{msg['key']}"
        while True:
            async with self._redis.pipeline(transaction=True) as pipe:
                try:
                    await pipe.watch(key, active, self._consumer_key)
                    state = await pipe.hgetall(key)
                    if (state.get("job_id") != msg["job_id"] or
                            state.get("status") != JobStatus.RUNNING or
                            state.get("execution_token") != token or
                            state.get("consumer_id") != self._consumer_id or
                            await pipe.get(active) != msg["job_id"] or
                            not await pipe.exists(self._consumer_key)):
                        # Retain the descriptor for lease recovery/garbage
                        # collection; never erase the current owner's work.
                        return False
                    pipe.multi()
                    if retry_delay is not None:
                        # Bounded retry: back to QUEUED behind a delayed-zset
                        # entry. The dedup lock stays held for the whole
                        # backoff; the last failure stays visible in `error`.
                        pipe.hset(key, mapping={
                            "status": JobStatus.QUEUED, "started_at": "",
                            "finished_at": "",
                            "next_retry_at": (
                                utcnow() + timedelta(seconds=retry_delay)
                            ).isoformat(),
                            "execution_token": "", "consumer_id": "",
                            **extra,
                        })
                        pipe.zadd(_DELAYED_ZSET, {raw: time.time() + retry_delay})
                        pipe.expire(active, self._ttl)
                    elif requeue:
                        # Cancelled by stop(): not a failure — roll the
                        # attempt counter back so shutdowns cannot burn the
                        # retry budget.
                        pipe.hset(key, mapping={
                            "status": JobStatus.QUEUED, "started_at": "",
                            "finished_at": "", "error": "",
                            "execution_token": "", "consumer_id": "",
                            **extra,
                        })
                        if msg["job_type"] in _PRIORITY_JOB_TYPES:
                            pipe.lpush(_QUEUE_LIST, raw)
                        else:
                            pipe.rpush(_QUEUE_LIST, raw)
                        pipe.expire(active, self._ttl)
                    else:
                        pipe.hset(key, mapping={
                            "status": status, "finished_at": utcnow().isoformat(), **extra,
                        })
                        pipe.delete(active)
                    pipe.lrem(self._processing_key, 1, raw)
                    pipe.expire(key, self._ttl)
                    await pipe.execute()
                    return True
                except WatchError:
                    continue

    async def _run(self, msg: dict, raw: str) -> None:
        context_token = None
        try:
            claimed = await self._claim_execution(msg)
            if claimed is None:
                return
            token, attempt, max_attempts, backoff_seconds = claimed
            context_token = _execution_ownership.set(
                _ExecutionOwnership(self, msg["key"], msg["job_id"], token)
            )
            handler = self._handlers.get(msg["job_type"])
            finish_status = JobStatus.FAILED
            finish_extra: dict = {}
            requeue = False
            retry_delay: float | None = None
            try:
                if handler is None:
                    raise RuntimeError(f"No handler registered for job_type={msg['job_type']!r}")
                result = await handler(msg["payload"])
                finish_status = JobStatus.DONE
                finish_extra = {
                    "result": json.dumps(result) if result is not None else "",
                    "error": "",  # clear the previous attempt's failure
                }
            except asyncio.CancelledError:
                requeue = True
                finish_extra = {"attempt": str(max(0, attempt - 1))}
            except Exception as exc:
                finish_extra = {"error": str(exc)}
                if attempt < max_attempts:
                    retry_delay = _retry_delay(backoff_seconds, attempt)
                    logger.warning(
                        "Failed %s/%s (attempt %d/%d); retrying in %.1fs: %s",
                        msg["job_type"], msg["key"][:16], attempt, max_attempts,
                        retry_delay, exc,
                    )
                else:
                    logger.error("Failed %s/%s: %s", msg["job_type"], msg["key"][:16], exc)
            await self._finish_execution(
                msg, raw, token, finish_status, finish_extra,
                requeue=requeue, retry_delay=retry_delay,
            )
        finally:
            if context_token is not None:
                _execution_ownership.reset(context_token)
            self._sem.release()

    @staticmethod
    def _deserialize(raw: dict) -> dict:
        """Convert Redis hash string values back to a typed job state dict."""
        result_raw = raw.get("result", "")
        try:
            result = json.loads(result_raw) if result_raw else None
        except (json.JSONDecodeError, ValueError):
            result = result_raw or None

        def timestamp(field: str) -> str | None:
            # Old Redis hashes carry naive UTC. Normalize only these owned
            # fields on read; do not rewrite history or arbitrary result text.
            value = raw.get(field)
            if not value:
                return None
            try:
                return utc_isoformat(datetime.fromisoformat(value))
            except (TypeError, ValueError):
                return None

        def integer(field: str, default: int) -> int:
            try:
                return int(raw.get(field) or default)
            except (TypeError, ValueError):
                return default

        try:
            backoff = float(raw.get("backoff_seconds") or 0.0)
        except (TypeError, ValueError):
            backoff = 0.0

        return {
            "job_id": raw.get("job_id", ""),
            "job_type": raw.get("job_type", ""),
            "key": raw.get("key", ""),
            "status": raw.get("status", JobStatus.QUEUED),
            "result": result,
            "error": raw.get("error") or None,
            "queued_at": timestamp("queued_at"),
            "started_at": timestamp("started_at"),
            "finished_at": timestamp("finished_at"),
            # Absent on hashes written before retries existed: one-shot jobs.
            "attempt": integer("attempt", 0),
            "max_attempts": integer("max_attempts", 1),
            "backoff_seconds": backoff,
            "next_retry_at": timestamp("next_retry_at"),
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
# Process-level singleton — replaced during startup (app/main.py lifespan for
# the web/all roles, app/worker.py for the worker role)
# ---------------------------------------------------------------------------

task_queue: BaseQueue = MemoryQueue()
