"""Top-level pytest config shared across unit/api/integration suites.

Keeps the runtime_config DB-override map hermetic: a test that persists a
setting via the system-settings API reloads the override map in-process, and
without a reset that override would leak into later tests in the same process.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Hermetic test env — must run BEFORE any `app.*` import, because
# `app.config.Settings` reads os.environ + the repo-root `.env` at import time.
# A developer's local `.env` carries real secret keys, which breaks tests that
# assert "no secret configured by default" (CI uses an empty .env). Env vars
# take precedence over .env in pydantic-settings, so forcing them empty here
# neutralizes the leak.
for _key in ("LLM_API_KEY", "TMDB_API_KEY", "JINA_API_KEY", "EXA_API_KEY"):
    os.environ[_key] = ""

# The repo-root data/ dir may be owned by root after docker runs; point the
# poster cache at a writable location so importing app.main never fails.
os.environ.setdefault(
    "POSTER_CACHE_DIR", str(Path(tempfile.gettempdir()) / "rssripple-test-posters")
)

from app.services.runtime_config import reset_to_env_defaults  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_runtime_config_overrides():
    reset_to_env_defaults()
    yield
    reset_to_env_defaults()
