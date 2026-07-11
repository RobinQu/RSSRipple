"""System settings API - runtime-configurable LLM + external search source keys.

These settings persist in the ``app_settings`` table (via the runtime_config
layer) and take effect without an app restart. See
:mod:`app.services.runtime_config` for precedence and caching.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas.common import success_response
from app.services.metadata_agent import reset_metadata_agent
from app.services.runtime_config import (
    EXA_EFFORT_LEVELS,
    RUNTIME_SETTING_KEYS,
    is_secret,
    kind_of,
    reload_runtime_config,
    runtime_config,
)
from app.services.settings_service import set_setting

router = APIRouter()

# Display grouping for the frontend (id -> ordered keys).
_GROUPS: list[dict[str, Any]] = [
    {
        "id": "llm",
        "keys": ["llm_api_key", "llm_model", "llm_base_url", "llm_enable_thinking"],
    },
    {
        "id": "sources",
        "keys": [
            "tmdb_api_key", "tmdb_enabled",
            "jina_api_key", "jina_enabled",
            "exa_api_key", "exa_effort_level", "exa_enabled",
            "wikipedia_enabled",
        ],
    },
]

_SECRET_MASK = "••••"


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return _SECRET_MASK
    return f"{_SECRET_MASK}{value[-4:]}"


def _field_value(key: str) -> dict[str, Any]:
    """Build the public descriptor for one setting key."""
    kind = kind_of(key)
    if kind == "bool":
        raw = bool(getattr(runtime_config, key))
        return {"value": raw, "configured": True, "secret": False, "kind": "bool"}
    # str
    raw = getattr(runtime_config, key) or ""
    secret = is_secret(key)
    return {
        "value": _mask_secret(raw) if secret else raw,
        "configured": bool(raw),
        "secret": secret,
        "kind": "str",
    }


def _build_response() -> dict[str, Any]:
    settings_map = {key: _field_value(key) for key in RUNTIME_SETTING_KEYS}
    return {
        "settings": settings_map,
        "groups": _GROUPS,
        "exa_effort_levels": list(EXA_EFFORT_LEVELS),
    }


class SystemSettingsUpdate(BaseModel):
    """Partial update. Only keys present in the body are written.

    Secret (API key) fields are only changed when present in the body: a
    non-empty string overwrites, an empty string / null clears (reverts to the
    env default). Omit a secret field to leave it unchanged.
    """

    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_base_url: str | None = None
    llm_enable_thinking: bool | None = None
    tmdb_api_key: str | None = None
    jina_api_key: str | None = None
    exa_api_key: str | None = None
    exa_effort_level: str | None = None
    exa_enabled: bool | None = None
    jina_enabled: bool | None = None
    tmdb_enabled: bool | None = None
    wikipedia_enabled: bool | None = None


@router.get("/system-settings")
async def get_system_settings(db: AsyncSession = Depends(get_db)):
    """Return all LLM + external-source settings (secrets masked)."""
    # Refresh from DB so the UI always reflects persisted state (the in-process
    # cache may be stale in multi-worker deployments).
    await reload_runtime_config(db)
    return success_response(_build_response())


@router.put("/system-settings")
async def put_system_settings(
    body: SystemSettingsUpdate, db: AsyncSession = Depends(get_db)
):
    """Persist changed settings, reload the runtime cache, and reset the
    metadata agent so new LLM config takes effect immediately.
    """
    payload = body.model_dump(exclude_unset=True)
    if not payload:
        raise HTTPException(status_code=400, detail="no settings fields provided")

    if "exa_effort_level" in payload and payload["exa_effort_level"] is not None:
        level = str(payload["exa_effort_level"]).strip().lower()
        if level not in EXA_EFFORT_LEVELS:
            raise HTTPException(
                status_code=400,
                detail=f"exa_effort_level must be one of {', '.join(EXA_EFFORT_LEVELS)}",
            )
        payload["exa_effort_level"] = level

    for key, value in payload.items():
        if key not in RUNTIME_SETTING_KEYS:
            continue  # ignore unknown keys (forward-compatible)
        if kind_of(key) == "bool":
            stored = "true" if bool(value) else "false"
        else:
            # str field (incl. secrets): None / empty -> clear (revert to env)
            stored = "" if value is None else str(value)
        await set_setting(db, key, stored)

    await db.commit()
    await reload_runtime_config(db)
    reset_metadata_agent()
    return success_response(_build_response())
