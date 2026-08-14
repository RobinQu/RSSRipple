"""In-process integration tests for DB-backed metadata services.

Reuses ``tests/unit/conftest.py`` DB fixtures (same pattern as
``tests/integration/organize``): the ``db_engine`` fixture installs a fresh
per-test Turso engine as the global ``app.database`` factory, so service
functions exercising ORM relationships run without the docker app stack.
"""

from __future__ import annotations

from tests.unit import conftest as _unit_conftest

db_engine = _unit_conftest.db_engine
db_session = _unit_conftest.db_session
