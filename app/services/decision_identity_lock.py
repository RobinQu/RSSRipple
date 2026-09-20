"""Transaction coordination for pending creation versus work identity changes."""
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

# Different namespace from database startup. Shared creators remain concurrent;
# rare work merges take exclusive ownership before discovering pending rows.
_IDENTITY_LOCK = 72057594037927938


async def lock_decision_identity(db, *, changing=False):
    """Fail fast so callers can roll back before retrying the whole transaction.

    Callers can already own resource/Agent locks from the surrounding operation;
    waiting here would add an inverse lock order. PostgreSQL transaction locks
    release at commit/rollback. Turso relies on its write-conflict detection.
    """
    if db.get_bind().dialect.name != "postgresql":
        return
    function = "pg_try_advisory_xact_lock" if changing else "pg_try_advisory_xact_lock_shared"
    with db.no_autoflush:
        acquired = await db.scalar(text(f"SELECT {function}(:identity)"), {"identity": _IDENTITY_LOCK})
    if not acquired:
        raise OperationalError(
            "decision identity coordination", {},
            RuntimeError("database is locked: concurrent decision identity change; retry whole transaction"),
        )
