"""Top-level pytest config shared across unit/api/integration suites.

Keeps the runtime_config DB-override map hermetic: a test that persists a
setting via the system-settings API reloads the override map in-process, and
without a reset that override would leak into later tests in the same process.
"""

from __future__ import annotations

import pytest

from app.services.runtime_config import reset_to_env_defaults


@pytest.fixture(autouse=True)
def _isolate_runtime_config_overrides():
    reset_to_env_defaults()
    yield
    reset_to_env_defaults()
