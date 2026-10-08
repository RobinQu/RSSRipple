"""Time helpers — naive UTC for PostgreSQL compatibility."""

from datetime import UTC, datetime


def naive_utc(value: datetime) -> datetime:
    """Preserve an aware instant as naive UTC; naive input already means UTC."""
    if value.tzinfo is None:
        return value
    try:
        return value.astimezone(UTC).replace(tzinfo=None)
    except OverflowError as exc:
        raise ValueError("UTC timestamp is outside the supported datetime range") from exc


def utc_isoformat(value: datetime | None) -> str | None:
    """Serialize a known timestamp as ISO 8601 UTC, retaining microseconds."""
    return naive_utc(value).isoformat() + "Z" if value is not None else None


def utcnow() -> datetime:
    """Return current UTC time as a naive datetime.

    PostgreSQL TIMESTAMP WITHOUT TIME ZONE columns reject
    timezone-aware values when mixed with naive values in the
    same INSERT statement.
    """
    return datetime.now(UTC).replace(tzinfo=None)
