"""Runtime configuration for the LLM + external search data sources.

These settings are user-editable at runtime (no restart) through the system
settings UI and persist in the ``app_settings`` table. They override the
env-var defaults in :mod:`app.config.settings`.

Design:
    Each accessor property returns the DB override (if one is set) and
    otherwise falls back *live* to :class:`app.config.Settings`. So the env
    defaults remain in effect until a user persists a value via the UI, and
    code/tests that patch ``settings.*`` still influence the result when no DB
    override is present.

    :func:`load_runtime_config` (called at app startup and after every settings
    write) populates the in-memory ``_overrides`` map from the ``app_settings``
    table.

Precedence:
    A DB row (if present) wins over the env default. Clearing a field in the UI
    deletes its row, so the value reverts to the env default.

Multi-worker caveat:
    The override map is process-local. The default single-process (memory-queue)
    deployment stays consistent because writes reload the map in-process. In a
    multi-worker deployment each worker keeps its own map and may serve stale
    values until it is restarted - acceptable for this app's default topology.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.app_setting import AppSetting

# Setting definition: key -> (env_attr, kind, is_secret)
#   env_attr : attribute name on :class:`app.config.Settings` used as the default
#   kind     : "str" | "bool"
#   is_secret: masked in API responses / never returned in plaintext
_KIND_STR = "str"
_KIND_BOOL = "bool"
_SETTING_DEFS: dict[str, tuple[str, str, bool]] = {
    # LLM (OpenAI-compatible API)
    "llm_api_key": ("llm_api_key", _KIND_STR, True),
    "llm_model": ("llm_model", _KIND_STR, False),
    "llm_base_url": ("llm_base_url", _KIND_STR, False),
    "llm_enable_thinking": ("llm_enable_thinking", _KIND_BOOL, False),
    # External search data sources
    "tmdb_api_key": ("tmdb_api_key", _KIND_STR, True),
    "jina_api_key": ("jina_api_key", _KIND_STR, True),
    "exa_api_key": ("exa_api_key", _KIND_STR, True),
    "exa_effort_level": ("exa_effort_level", _KIND_STR, False),
    "exa_enabled": ("exa_enabled", _KIND_BOOL, False),
    "jina_enabled": ("jina_enabled", _KIND_BOOL, False),
    "tmdb_enabled": ("tmdb_enabled", _KIND_BOOL, False),
    "wikipedia_enabled": ("wikipedia_enabled", _KIND_BOOL, False),
}

# Allowed values for the Exa agent effort level.
EXA_EFFORT_LEVELS: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh")

# All recognized setting keys (exported for the API layer + tests).
RUNTIME_SETTING_KEYS: tuple[str, ...] = tuple(_SETTING_DEFS.keys())

# DB overrides loaded at startup / after writes. Empty -> pure env defaults.
_overrides: dict[str, str] = {}
_BOOL_TRUE = {"1", "true", "yes", "on"}


def _to_bool(raw: str) -> bool:
    return (raw or "").strip().lower() in _BOOL_TRUE


def _env_str(env_attr: str) -> str:
    raw = getattr(settings, env_attr)
    return "" if raw is None else str(raw)


def _env_bool(env_attr: str) -> bool:
    return bool(getattr(settings, env_attr))


def is_secret(key: str) -> bool:
    return _SETTING_DEFS[key][2]


def kind_of(key: str) -> str:
    return _SETTING_DEFS[key][1]


class _RuntimeConfig:
    """Synchronous read access to the effective (DB-over-env) runtime settings.

    Attribute names mirror :class:`app.config.Settings` so call sites migrate
    with a trivial ``settings.`` -> ``runtime_config.`` rename.
    """

    def _str(self, key: str) -> str:
        return _overrides[key] if key in _overrides else _env_str(_SETTING_DEFS[key][0])

    def _bool(self, key: str) -> bool:
        if key in _overrides:
            return _to_bool(_overrides[key])
        return _env_bool(_SETTING_DEFS[key][0])

    # ── LLM ───────────────────────────────────────────────────────────────
    @property
    def llm_api_key(self) -> str:
        return self._str("llm_api_key")

    @property
    def llm_model(self) -> str:
        return self._str("llm_model")

    @property
    def llm_base_url(self) -> str:
        return self._str("llm_base_url")

    @property
    def llm_enable_thinking(self) -> bool:
        return self._bool("llm_enable_thinking")

    # ── External search data sources ──────────────────────────────────────
    @property
    def tmdb_api_key(self) -> str:
        return self._str("tmdb_api_key")

    @property
    def jina_api_key(self) -> str:
        return self._str("jina_api_key")

    @property
    def exa_api_key(self) -> str:
        return self._str("exa_api_key")

    @property
    def exa_effort_level(self) -> str:
        return self._str("exa_effort_level") or "low"

    @property
    def exa_enabled(self) -> bool:
        return self._bool("exa_enabled")

    @property
    def jina_enabled(self) -> bool:
        return self._bool("jina_enabled")

    @property
    def tmdb_enabled(self) -> bool:
        return self._bool("tmdb_enabled")

    @property
    def wikipedia_enabled(self) -> bool:
        return self._bool("wikipedia_enabled")


runtime_config = _RuntimeConfig()


async def load_runtime_config(db: AsyncSession) -> None:
    """Populate the override map from the ``app_settings`` table.

    Env defaults are not stored here; they are read live by the accessors when
    no override is present. Keys absent from the DB keep using the env default.
    """
    _overrides.clear()
    result = await db.execute(select(AppSetting))
    rows: dict[str, str | None] = {r.key: r.value for r in result.scalars().all()}
    for key in _SETTING_DEFS:
        if key in rows and rows[key] is not None:
            _overrides[key] = rows[key]  # type: ignore[assignment]


async def reload_runtime_config(db: AsyncSession) -> None:
    """Refresh the override map after a settings write. Alias of :func:`load_runtime_config`."""
    await load_runtime_config(db)


def reset_to_env_defaults() -> None:
    """Clear all DB overrides. Used to keep tests hermetic."""
    _overrides.clear()


def get_effective_value(key: str) -> str:
    """Return the effective raw value for *key* as a string (bool -> "true"/"false")."""
    if kind_of(key) == _KIND_BOOL:
        return "true" if getattr(runtime_config, key) else "false"
    return getattr(runtime_config, key)
