"""Runtime settings service — key/value settings stored in the ``app_settings``
table.

Unlike ``app.config.Settings`` (env-var driven, read-only), these settings can
be changed by the user at runtime through the UI and persist across restarts.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_setting import AppSetting

# Setting keys ---------------------------------------------------------------
SETTING_DEFAULT_METADATA_SOURCE = "default_metadata_source"
SETTING_METADATA_AUTO_REFRESH_ENABLED = "metadata_auto_refresh_enabled"
SETTING_METADATA_AUTO_REFRESH_INTERVAL_MINUTES = "metadata_auto_refresh_interval_minutes"
DEFAULT_METADATA_AUTO_REFRESH_INTERVAL_MINUTES = 1440
MIN_METADATA_AUTO_REFRESH_INTERVAL_MINUTES = 30
MAX_METADATA_AUTO_REFRESH_INTERVAL_MINUTES = 10080


async def get_setting(db: AsyncSession, key: str) -> str | None:
    """Return the raw value for *key*, or ``None`` if unset."""
    row = await db.get(AppSetting, key)
    return row.value if row else None


async def set_setting(db: AsyncSession, key: str, value: str | None) -> None:
    """Upsert (or delete, when *value* is empty) a setting. Caller commits."""
    row = await db.get(AppSetting, key)
    if not value:
        if row is not None:
            await db.delete(row)
        return
    if row is not None:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))
    await db.flush()


async def get_bool_setting(db: AsyncSession, key: str, default: bool = False) -> bool:
    raw = await get_setting(db, key)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


async def get_int_setting(
    db: AsyncSession,
    key: str,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    raw = await get_setting(db, key)
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


async def resolve_default_metadata_source(
    db: AsyncSession, explicit: str | None = None
) -> str:
    """Resolve the metadata source to use for the works-page refresh action.

    Priority: an explicit per-action override → the stored setting. The stored
    setting is intentionally required so users make one active source choice.
    """
    from app.services.metadata_agent import (
        SUPPORTED_METADATA_SOURCES,
        is_metadata_source_available,
    )

    if explicit:
        v = explicit.strip().lower()
        if v in SUPPORTED_METADATA_SOURCES and is_metadata_source_available(v):
            return v

    stored = await get_setting(db, SETTING_DEFAULT_METADATA_SOURCE)
    if stored:
        v = stored.strip().lower()
        if v in SUPPORTED_METADATA_SOURCES and is_metadata_source_available(v):
            return v

    raise ValueError("metadata source has not been configured")
