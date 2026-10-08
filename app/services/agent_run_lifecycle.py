"""Short transactional lease operations; callers own commit and rollback.

A lease is authority to proceed,
not evidence that its process is alive. Expired tokens cannot be renewed.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import BigInteger, cast, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_lease import AgentRunLease


@dataclass(frozen=True)
class RunOwnership:
    run_id: str
    token: str


def _clock(db: AsyncSession):
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        # clock_timestamp, unlike CURRENT_TIMESTAMP, advances within a txn.
        return cast(func.floor(func.extract("epoch", func.clock_timestamp())), BigInteger)
    if dialect == "sqlite":
        return cast(func.strftime("%s", "now"), BigInteger)
    raise ValueError(f"Unsupported run lease database: {dialect}")


def _duration(seconds: int) -> int:
    if isinstance(seconds, bool) or not isinstance(seconds, int) or not 1 <= seconds <= 3600:
        raise ValueError("Run lease duration must be between 1 and 3600 whole seconds")
    return seconds


async def create_lease(db: AsyncSession, run_id: str, *, seconds: int = 30) -> RunOwnership:
    owner = RunOwnership(run_id, str(uuid.uuid4()))
    db.add(AgentRunLease(run_id=run_id, token=owner.token, expires_at_epoch=_clock(db) + _duration(seconds)))
    await db.flush()
    return owner


def _owned(owner: RunOwnership):
    return (AgentRunLease.run_id == owner.run_id, AgentRunLease.token == owner.token)


async def renew_lease(db: AsyncSession, owner: RunOwnership, *, seconds: int = 30) -> bool:
    renewed = await db.scalar(
        update(AgentRunLease)
        .where(*_owned(owner), AgentRunLease.expires_at_epoch > _clock(db))
        .values(expires_at_epoch=_clock(db) + _duration(seconds))
        .returning(AgentRunLease.id)
        .execution_options(synchronize_session=False)
    )
    return renewed is not None


async def claim_completion(db: AsyncSession, owner: RunOwnership) -> bool:
    """Consume live authority in the SAME transaction as history/progress writes.

    A rollback restores authority; after commit, old renew/complete operations
    cannot succeed. Never commit the deletion separately from the run outcome.
    """
    claimed = await db.scalar(
        delete(AgentRunLease)
        .where(*_owned(owner), AgentRunLease.expires_at_epoch > _clock(db))
        .returning(AgentRunLease.id)
        .execution_options(synchronize_session=False)
    )
    return claimed is not None


async def reap_expired(db: AsyncSession, *, limit: int = 100) -> list[str]:
    """Revoke a bounded batch and fail interrupted histories in one transaction.

    Updates the matching Agent summary; preserves request retries, counters and consumption.
    Old histories without a lease require an explicit offline migration.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("Run lease reaper limit must be between 1 and 1000")
    candidates = list(await db.scalars(
        select(AgentRunLease.id)
        .where(AgentRunLease.expires_at_epoch <= _clock(db))
        .order_by(AgentRunLease.expires_at_epoch, AgentRunLease.id)
        .limit(limit)
    ))
    retired = []
    for lease_id in candidates:
        revoked = (await db.execute(
            delete(AgentRunLease)
            .where(AgentRunLease.id == lease_id, AgentRunLease.expires_at_epoch <= _clock(db))
            .returning(AgentRunLease.run_id, AgentRunLease.token)
            .execution_options(synchronize_session=False)
        )).one_or_none()
        if revoked is None:
            continue
        run_id, token = revoked
        previous = await db.scalar(select(AgentRun.errors).where(AgentRun.id == run_id))
        # UTC conversion is explicit on PG, independent of its session TZ.
        finished = (func.timezone("UTC", func.clock_timestamp())
                    if db.get_bind().dialect.name == "postgresql" else func.datetime("now"))
        updated = await db.scalar(
            update(AgentRun)
            .where(AgentRun.id == run_id, AgentRun.status == "running")
            .values(
                status="failed", finished_at=finished,
                errors=[*(previous or []), "Execution lease expired; run interrupted. Counters may be incomplete."],
            )
            .returning(AgentRun.id)
            .execution_options(synchronize_session=False)
        )
        if updated is not None:
            await db.execute(update(Agent).where(
                Agent.id == select(AgentRun.agent_id).where(AgentRun.id == run_id).scalar_subquery(),
                Agent.current_run_token == token,
            ).values(last_run_at=finished, last_run_status="failed").execution_options(synchronize_session=False))
            retired.append(updated)
    return retired


async def reap_expired_runs() -> list[str]:
    """Short durable reconciliation; must not wait behind occupied queue slots."""
    from app import database

    async def attempt():
        async with database.committed_session() as db:
            return await reap_expired(db)
    return await database.retry_on_lock(attempt)
