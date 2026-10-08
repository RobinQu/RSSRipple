"""Actual Redis connection loss inside a production consumer ownership context."""

import asyncio
import os
from urllib.parse import urlsplit, urlunsplit

import pytest
import redis.asyncio as redis
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select

from app.models.agent_run_lease import AgentRunLease
from app.services import agent_run_execution as execution
from app.services.task_queue import RedisQueue
from tests.integration.agent_run_lifecycle.test_database_disconnect import DatabaseRoute


@pytest.mark.parametrize("lifecycle_backend", ["work_fk_postgres"], indirect=True)
async def test_redis_route_loss_denies_checkpoint_and_renewal(lifecycle_db, monkeypatch):
    _, factory, owner, _, _ = lifecycle_db
    url = os.environ.get("QUEUE_RECOVERY_REDIS_URL")
    if not url:
        if os.environ.get("QUEUE_RECOVERY_REQUIRED") == "1":
            pytest.fail("Redis disconnect test requires the isolated broker")
        pytest.skip("Set QUEUE_RECOVERY_REDIS_URL to the isolated broker")
    parts = urlsplit(url)
    route = DatabaseRoute(parts.hostname, parts.port or 6379)
    port = await route.start()
    proxy_url = urlunsplit(parts._replace(netloc=f"127.0.0.1:{port}"))
    proxied = redis.from_url(proxy_url, decode_responses=True, socket_connect_timeout=1, socket_timeout=1)
    observer = redis.from_url(url, decode_responses=True)
    queue = RedisQueue(redis_client=proxied, max_concurrent=1)
    ready, cut, checked, release, begin_heartbeat, stopped = (asyncio.Event() for _ in range(6))
    outcomes = []
    real_heartbeat = execution._heartbeat
    owned_broker = False

    async def observe_heartbeat(*args):
        try:
            await begin_heartbeat.wait()
            return await real_heartbeat(*args)
        finally:
            stopped.set()

    monkeypatch.setattr(execution, "_heartbeat", observe_heartbeat)
    monkeypatch.setattr(execution, "HEARTBEAT_SECONDS", 0.05)

    async def handler(payload):
        async with execution.maintain_run_lease(owner):
            ready.set()
            try:
                await cut.wait()
                try:
                    await execution.require_agent_execution_ownership()
                except RedisConnectionError:
                    outcomes.append("unknown ownership rejected")
                else:
                    outcomes.append("incorrectly allowed progress")
                checked.set()
                await release.wait()
            finally:
                begin_heartbeat.set()

    queue.register("synthetic-network-check", handler)
    try:
        assert await observer.dbsize() == 0
        owned_broker = True
        async with factory() as db:
            initial = await db.scalar(select(AgentRunLease.expires_at_epoch).where(
                AgentRunLease.run_id == owner.run_id,
            ))
        await queue.start()
        await queue.enqueue("synthetic-network-check", "synthetic-network-check", {})
        await asyncio.wait_for(ready.wait(), 5)
        await route.close()
        begin_heartbeat.set()
        cut.set()
        await asyncio.wait_for(asyncio.gather(checked.wait(), stopped.wait()), 5)
        assert outcomes == ["unknown ownership rejected"]
        # Observer has an unaffected route to Redis and DB: there was no
        # takeover or lease deletion to masquerade as a transport failure.
        assert (await observer.ping()) is True
        async with factory() as db:
            lease = await db.scalar(select(AgentRunLease).where(AgentRunLease.run_id == owner.run_id))
            assert lease.token == owner.token and lease.expires_at_epoch == initial
    finally:
        # Restore only this fixture's client so production queue shutdown and
        # terminal bookkeeping can finish without a busy reconnect loop.
        queue._redis = redis.from_url(url, decode_responses=True)
        begin_heartbeat.set()
        cut.set()
        release.set()
        await queue.stop()
        await proxied.aclose()
        if owned_broker:
            await observer.flushdb()
        await observer.aclose()
        await route.close()
