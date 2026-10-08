"""Commit history and existing B9/B7 acknowledgements under one run lease."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import case, or_, select, update

from app import database
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.services import agent_publication_progress as publications
from app.services import agent_resource_requests as requests
from app.services import task_queue
from app.services.agent_publication_progress import PublicationSnapshot
from app.services.agent_resource_requests import RequestSnapshot
from app.services.agent_run_lifecycle import RunOwnership, claim_completion
from app.services.task_queue import ExecutionOwnershipLostError
from app.utils.time import utcnow

if TYPE_CHECKING:
    from app.services.agent_service import RunResult

_COUNTERS = ("total_resources", "matched", "dispatched", "pending_decisions",
             "filter_failed", "duplicates_skipped", "unrecognized")


@dataclass(frozen=True)
class RunCompletion:
    owner: RunOwnership
    agent_id: str
    requests: list[RequestSnapshot]
    publication: PublicationSnapshot | None
    window: PublicationSnapshot | None
    advance_to: datetime | None
    scan_since: datetime | None


async def finish_run(completion: RunCompletion, result: RunResult) -> dict:
    async def attempt():
        await task_queue.require_execution_ownership()
        async with database.committed_session() as db:
            if not await claim_completion(db, completion.owner):
                raise ExecutionOwnershipLostError("Run cannot publish after lease expiry or retirement")
            progress_confirmed = False
            if result.errors:
                await requests.defer_requests(db, completion.requests, "; ".join(result.errors))
            else:
                await requests.acknowledge_requests(db, completion.requests)
                if completion.publication is not None:
                    progress_confirmed = await publications.acknowledge_publications(db, completion.publication)
                elif completion.window is not None and completion.advance_to is not None:
                    progress_confirmed = await publications.acknowledge_window(
                        db, completion.window, completion.scan_since,
                    )
            status = ("failed" if result.errors else
                      "pending_decisions" if result.dispatched == 0 and result.pending_decisions > 0 else "success")
            finished = utcnow()
            counts = {field: getattr(result, field) for field in _COUNTERS}
            updated = await db.scalar(update(AgentRun).where(
                AgentRun.id == completion.owner.run_id, AgentRun.status == "running",
            ).values(status=status, finished_at=finished, **counts,
                     matched_resource_ids=list(result.matched_resource_ids), errors=list(result.errors))
                .returning(AgentRun.id).execution_options(synchronize_session=False))
            if updated is None:
                raise ExecutionOwnershipLostError("Run history is no longer running")
            summary = {"last_run_status": status, "last_run_at": finished}
            if completion.advance_to is not None and not result.errors and progress_confirmed:
                summary["last_consumed_at"] = case(
                    (or_(Agent.last_consumed_at.is_(None), Agent.last_consumed_at < completion.advance_to),
                     completion.advance_to), else_=Agent.last_consumed_at,
                )
            await db.execute(update(Agent).where(
                Agent.id == completion.agent_id, Agent.current_run_token == completion.owner.token,
            ).values(**summary).execution_options(synchronize_session=False))
            await task_queue.require_execution_ownership()
        return {"agent_id": completion.agent_id, "run_id": completion.owner.run_id,
                **counts, "errors": list(result.errors)}
    return await database.retry_on_lock(attempt)


async def fail_run(completion: RunCompletion, error: BaseException) -> None:
    async def attempt():
        await task_queue.require_execution_ownership()
        async with database.committed_session() as db:
            if not await claim_completion(db, completion.owner):
                return
            message = f"Run interrupted ({type(error).__name__}); counters may be incomplete."
            previous = await db.scalar(select(AgentRun.errors).where(AgentRun.id == completion.owner.run_id))
            finished = utcnow()
            await db.execute(update(AgentRun).where(
                AgentRun.id == completion.owner.run_id, AgentRun.status == "running",
            ).values(status="failed", finished_at=finished, errors=[*(previous or []), message])
                .execution_options(synchronize_session=False))
            await db.execute(update(Agent).where(
                Agent.id == completion.agent_id, Agent.current_run_token == completion.owner.token,
            ).values(last_run_status="failed", last_run_at=finished).execution_options(synchronize_session=False))
            if isinstance(error, Exception) and not isinstance(error, ExecutionOwnershipLostError):
                # Preserve B9's existing retry diagnostic; history gets its own
                # interruption marker rather than changing the request contract.
                await requests.defer_requests(db, completion.requests, str(error))
            await task_queue.require_execution_ownership()
    await database.retry_on_lock(attempt)
