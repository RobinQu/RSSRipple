"""Reserve an OTP attempt before any secret verification, in a short transaction."""

import asyncio
import hashlib
import math
import uuid
from datetime import datetime, timedelta
from weakref import WeakKeyDictionary

from sqlalchemy import case, delete, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app import database
from app.models.auth_rate_limit import AuthRateLimitBucket
from app.utils.time import utcnow

PEER_LIMIT = 5
GLOBAL_LIMIT = 30
WINDOW_SECONDS = 60

# Turso is single-process; serialize its local budget writers to avoid a
# retry storm on the global row. Counts remain authoritative in the database.
_turso_locks: WeakKeyDictionary[AsyncEngine, asyncio.Lock] = WeakKeyDictionary()


async def _reserve(db: AsyncSession, key: str, limit: int, now: datetime) -> int:
    """Return zero for a reservation, or seconds until the current budget resets."""
    bucket = AuthRateLimitBucket
    insert = pg_insert if db.get_bind().dialect.name == "postgresql" else sqlite_insert
    expired = bucket.resets_at <= now
    deadline = now + timedelta(seconds=WINDOW_SECONDS)
    statement = insert(bucket).values(
        id=str(uuid.uuid4()), bucket_key=key, attempts=1, resets_at=deadline,
    ).on_conflict_do_update(
        index_elements=[bucket.bucket_key],
        set_={
            "attempts": case((expired, 1), else_=bucket.attempts + 1),
            "resets_at": case((expired, deadline), else_=bucket.resets_at),
        },
        where=or_(expired, bucket.attempts < limit),
    ).returning(bucket.id)
    if (await db.execute(statement)).scalar_one_or_none() is not None:
        return 0
    reset = (await db.execute(
        select(bucket.resets_at).where(bucket.bucket_key == key)
    )).scalar_one()
    return max(1, math.ceil((reset - now).total_seconds()))


async def reserve_otp_attempt(peer: str) -> int:
    """Commit budgets even when the later OTP verification fails.

    Every validated request consumes global budget, including peer denials.
    Global denial creates no peer row; cleanup is bounded and never removes
    active windows. Database errors propagate so verification fails closed.
    """
    key = "peer:" + hashlib.sha256(peer.encode()).hexdigest()

    async def reserve_transaction() -> int:
        now = utcnow()
        async with database.async_session_factory() as db, db.begin():
            retry_after = await _reserve(db, "global", GLOBAL_LIMIT, now)
            if retry_after:
                return retry_after
            expired_ids = select(AuthRateLimitBucket.id).where(
                AuthRateLimitBucket.resets_at <= now,
                AuthRateLimitBucket.bucket_key != "global",
            ).order_by(AuthRateLimitBucket.resets_at, AuthRateLimitBucket.id).limit(100)
            await db.execute(delete(AuthRateLimitBucket).where(
                AuthRateLimitBucket.id.in_(expired_ids),
                AuthRateLimitBucket.resets_at <= now,
            ))
            return await _reserve(db, key, PEER_LIMIT, now)

    engine = database.engine
    if database.is_turso_url(str(engine.url)):
        lock = _turso_locks.get(engine)
        if lock is None:
            lock = _turso_locks[engine] = asyncio.Lock()
        async with lock:
            return await database.retry_on_lock(reserve_transaction)
    return await database.retry_on_lock(reserve_transaction)
