"""Time helpers — naive UTC for PostgreSQL compatibility."""

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return current UTC time as a naive datetime.

    PostgreSQL TIMESTAMP WITHOUT TIME ZONE columns reject
    timezone-aware values when mixed with naive values in the
    same INSERT statement.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)
