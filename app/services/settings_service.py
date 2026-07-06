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


async def resolve_default_metadata_source(
    db: AsyncSession, explicit: str | None = None
) -> str:
    """Resolve the metadata source to use for the works-page refresh action.

    Priority: an explicit per-action override → the stored default setting →
    the agent's hardcoded default. The result is always a supported source.
    """
    from app.services.metadata_agent import (
        DEFAULT_METADATA_SOURCE,
        SUPPORTED_METADATA_SOURCES,
    )

    if explicit:
        v = explicit.strip().lower()
        if v in SUPPORTED_METADATA_SOURCES:
            return v

    stored = await get_setting(db, SETTING_DEFAULT_METADATA_SOURCE)
    if stored:
        v = stored.strip().lower()
        if v in SUPPORTED_METADATA_SOURCES:
            return v

    return DEFAULT_METADATA_SOURCE
