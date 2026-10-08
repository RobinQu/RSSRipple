"""Agent-specific execution checkpoints and heartbeat scope."""

import asyncio
import logging
from contextlib import asynccontextmanager
from contextvars import ContextVar

from sqlalchemy import select

from app import database
from app.models.agent_run_lease import AgentRunLease
from app.services import task_queue
from app.services.agent_run_lifecycle import RunOwnership, _clock, renew_lease
from app.services.task_queue import ExecutionOwnershipLostError

LEASE_SECONDS = 30
HEARTBEAT_SECONDS = 10
_current_run: ContextVar[RunOwnership | None] = ContextVar("agent_run_ownership", default=None)
logger = logging.getLogger(__name__)


async def require_agent_execution_ownership() -> None:
    """Unknown DB/queue ownership denies progress; direct API keeps its scope."""
    await task_queue.require_execution_ownership()
    owner = _current_run.get()
    if owner is None:
        return
    async with database.async_session_factory() as db:
        active = await db.scalar(select(AgentRunLease.id).where(
            AgentRunLease.run_id == owner.run_id, AgentRunLease.token == owner.token,
            AgentRunLease.expires_at_epoch > _clock(db),
        ))
    if active is None:
        raise ExecutionOwnershipLostError(f"Agent execution lease revoked: {owner.run_id}")


async def _heartbeat(owner: RunOwnership, stop: asyncio.Event) -> None:
    try:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=HEARTBEAT_SECONDS)
                return
            except TimeoutError:
                pass
            await task_queue.require_execution_ownership()

            async def renew():
                async with database.committed_session() as db:
                    return await renew_lease(db, owner, seconds=LEASE_SECONDS)

            if not await database.retry_on_lock(renew):
                return
    except Exception:
        # No renewal on unknown ownership. Future checkpoints fail closed;
        # an interrupted attempt is eventually retired by the DB lease reaper.
        logger.exception("Agent lease heartbeat stopped for run %s", owner.run_id)


async def _drain_heartbeat(heartbeat: asyncio.Task) -> None:
    """Defer repeated cancellation until the owned DB operation has finished."""
    cancellation = None
    while not heartbeat.done():
        try:
            await asyncio.shield(heartbeat)
        except asyncio.CancelledError as error:
            cancellation = error
    heartbeat.result()
    if cancellation is not None:
        raise cancellation


@asynccontextmanager
async def maintain_run_lease(owner: RunOwnership):
    token = _current_run.set(owner)
    stop = asyncio.Event()
    heartbeat = asyncio.create_task(_heartbeat(owner, stop))
    try:
        await require_agent_execution_ownership()
        yield
    finally:
        try:
            # Finish any short DB operation instead of cancelling the native
            # driver's pending callback. Never start another renewal afterward.
            stop.set()
            await _drain_heartbeat(heartbeat)
        finally:
            _current_run.reset(token)
