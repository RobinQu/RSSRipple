"""Unit tests for the Redis-backed submission guard and the dispatching facade."""

from __future__ import annotations

import asyncio
import json

import fakeredis
import pytest

import app.services.task_queue as tq_mod
from app.services.submission_guard import (
    _FORM_TOKEN_PFX,
    RedisSubmissionGuard,
    SubmissionGuard,
    _DispatchingSubmissionGuard,
)
from app.services.task_queue import MemoryQueue, RedisQueue


def _redis(server=None):
    return fakeredis.FakeAsyncRedis(server=server, decode_responses=True)


async def _elapse(seconds: float) -> None:
    """Sleep real wall-clock time. tests/unit/conftest.py stubs out
    asyncio.sleep for delays >= 1s, so sleep in sub-second chunks."""
    remaining = seconds
    while remaining > 0:
        step = min(remaining, 0.5)
        await asyncio.sleep(step)
        remaining -= step


@pytest.mark.asyncio
async def test_issue_and_consume():
    sg = RedisSubmissionGuard(_redis())
    t = await sg.issue()
    assert isinstance(t, str)
    assert await sg.consume(t) is True
    # Rapid repeat consume is rejected (double-click / retry storm).
    assert await sg.consume(t) is False


@pytest.mark.asyncio
async def test_consume_unknown_token_returns_false():
    sg = RedisSubmissionGuard(_redis())
    assert await sg.consume("no-such-token") is False


@pytest.mark.asyncio
async def test_retry_after_duplicate_window_accepted():
    """A fix-and-resubmit retry after the duplicate window is accepted again,
    and its own rapid duplicate is rejected — same semantics as the
    in-process backend."""
    sg = RedisSubmissionGuard(_redis())
    sg.DUPLICATE_WINDOW_SECONDS = 0.2
    t = await sg.issue()
    assert await sg.consume(t) is True
    assert await sg.consume(t) is False
    await _elapse(0.3)
    assert await sg.consume(t) is True
    assert await sg.consume(t) is False


@pytest.mark.asyncio
async def test_token_expires_after_ttl():
    sg = RedisSubmissionGuard(_redis())
    sg.TTL_SECONDS = 1
    t = await sg.issue()
    await _elapse(1.2)
    assert await sg.consume(t) is False


@pytest.mark.asyncio
async def test_consumed_token_still_expires_after_ttl():
    """consume() preserves the remaining TTL: a consumed token is reclaimed
    at the same moment it would have expired anyway."""
    sg = RedisSubmissionGuard(_redis())
    sg.TTL_SECONDS = 1
    t = await sg.issue()
    assert await sg.consume(t) is True
    ttl_ms = await sg._redis.pttl(_FORM_TOKEN_PFX + t)
    assert 0 < ttl_ms <= 1000
    await _elapse(1.2)
    assert await sg.consume(t) is False


@pytest.mark.asyncio
async def test_concurrent_double_consume_exactly_one_wins():
    """Two consumes racing on the same token must not both pass: the WATCH
    transaction retries the loser, which then observes the fresh consumed_at
    inside the duplicate window."""
    sg = RedisSubmissionGuard(_redis())
    t = await sg.issue()
    results = await asyncio.gather(*[sg.consume(t) for _ in range(10)])
    assert results.count(True) == 1


@pytest.mark.asyncio
async def test_shared_across_clients():
    """Two guards on separate connections to the same server (the multi-worker
    shape) share token state: issue on one, consume + duplicate-reject via
    the other."""
    server = fakeredis.FakeServer()
    worker_a = RedisSubmissionGuard(_redis(server))
    worker_b = RedisSubmissionGuard(_redis(server))
    t = await worker_a.issue()
    assert await worker_b.consume(t) is True
    assert await worker_a.consume(t) is False
    assert await worker_b.consume(t) is False


@pytest.mark.asyncio
async def test_consume_preserves_issue_expiry_not_sliding():
    """A late consume inside the TTL must not extend the token's lifetime."""
    sg = RedisSubmissionGuard(_redis())
    sg.TTL_SECONDS = 2
    sg.DUPLICATE_WINDOW_SECONDS = 0.2
    t = await sg.issue()
    await _elapse(1.0)
    assert await sg.consume(t) is True
    await _elapse(0.3)  # window passed, 0.7s of TTL left
    assert await sg.consume(t) is True
    ttl_ms = await sg._redis.pttl(_FORM_TOKEN_PFX + t)
    assert 0 < ttl_ms <= 800


@pytest.mark.asyncio
async def test_facade_uses_in_process_guard_for_memory_queue(monkeypatch):
    monkeypatch.setattr(tq_mod, "task_queue", MemoryQueue())
    facade = _DispatchingSubmissionGuard()
    t = await facade.issue()
    assert await facade.consume(t) is True
    assert await facade.consume(t) is False
    # In-process path: nothing was written to any Redis.
    assert facade._redis_guard is None


@pytest.mark.asyncio
async def test_facade_routes_to_redis_for_redis_queue(monkeypatch):
    redis_client = _redis()
    queue = RedisQueue(redis_client=redis_client)
    monkeypatch.setattr(tq_mod, "task_queue", queue)
    facade = _DispatchingSubmissionGuard()
    t = await facade.issue()
    raw = await redis_client.get(_FORM_TOKEN_PFX + t)
    assert json.loads(raw) == {"consumed_at": None}
    assert await facade.consume(t) is True
    assert await facade.consume(t) is False
    assert json.loads(await redis_client.get(_FORM_TOKEN_PFX + t))["consumed_at"] is not None


@pytest.mark.asyncio
async def test_facade_falls_back_to_local_before_redis_queue_starts(monkeypatch):
    """A RedisQueue whose start() has not run has no client yet; the facade
    must use the in-process guard rather than crash."""
    queue = RedisQueue(redis_url="redis://localhost:6379/0")
    monkeypatch.setattr(tq_mod, "task_queue", queue)
    facade = _DispatchingSubmissionGuard()
    t = await facade.issue()
    assert await facade.consume(t) is True
    assert facade._redis_guard is None
    assert isinstance(facade._local, SubmissionGuard)
