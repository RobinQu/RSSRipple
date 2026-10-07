"""Control an actual renewal transaction while the enclosing task stops."""

import asyncio

import pytest
from sqlalchemy import select

from app.models.agent_run_lease import AgentRunLease
from app.services import agent_run_execution as execution


@pytest.mark.parametrize("mode", ["normal", "cancel", "repeat_cancel"])
async def test_scope_drains_inflight_renewal_before_returning(lifecycle_db, monkeypatch, mode):
    _, factory, owner, _, _ = lifecycle_db
    async with factory() as db:
        initial = await db.scalar(select(AgentRunLease.expires_at_epoch).where(AgentRunLease.run_id == owner.run_id))
    monkeypatch.setattr(execution, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(execution, "LEASE_SECONDS", 60)
    transaction_ready, release, body_ready, body_done = (asyncio.Event() for _ in range(4))
    heartbeat_done = asyncio.Event()
    stop_events, background_errors = [], []
    renewal_cancelled = False
    real_renew, real_heartbeat = execution.renew_lease, execution._heartbeat
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    def record_background_error(loop, context):
        background_errors.append(context)
        if previous_handler:
            previous_handler(loop, context)
        else:
            loop.default_exception_handler(context)

    async def controlled_renew(*args, **kwargs):
        nonlocal renewal_cancelled
        result = await real_renew(*args, **kwargs)
        transaction_ready.set()  # Real UPDATE completed; transaction not committed.
        try:
            await release.wait()
        except asyncio.CancelledError:
            renewal_cancelled = True
            raise
        return result

    async def observed_heartbeat(owner, stop):
        stop_events.append(stop)
        try:
            await real_heartbeat(owner, stop)
        finally:
            heartbeat_done.set()

    async def managed_scope():
        try:
            async with execution.maintain_run_lease(owner):
                body_ready.set()
                await body_done.wait()
            return "normal"
        except asyncio.CancelledError:
            return "cancelled"

    loop.set_exception_handler(record_background_error)
    monkeypatch.setattr(execution, "renew_lease", controlled_renew)
    monkeypatch.setattr(execution, "_heartbeat", observed_heartbeat)
    task = asyncio.create_task(managed_scope())
    try:
        await asyncio.wait_for(body_ready.wait(), timeout=10)
        await asyncio.wait_for(transaction_ready.wait(), timeout=10)
        if mode == "normal":
            body_done.set()
        else:
            task.cancel()
        assert len(stop_events) == 1
        await asyncio.wait_for(stop_events[0].wait(), timeout=10)
        if mode == "repeat_cancel":
            task.cancel()
        # Scope must remain responsible for its in-flight transaction even if
        # another cancellation arrives during shielded cleanup.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
        assert not renewal_cancelled and not heartbeat_done.is_set()
        release.set()
        assert await asyncio.wait_for(task, timeout=10) == ("normal" if mode == "normal" else "cancelled")
        assert heartbeat_done.is_set()
        async with factory() as db:
            final = await db.scalar(select(AgentRunLease.expires_at_epoch).where(AgentRunLease.run_id == owner.run_id))
        assert final >= initial + 25
        await asyncio.sleep(0)
        assert background_errors == [] and not renewal_cancelled
    finally:
        release.set()
        body_done.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=10)
        await asyncio.wait_for(heartbeat_done.wait(), timeout=10)
        loop.set_exception_handler(previous_handler)
