"""Shared fixtures for the per-season-works integration gate (P6).

Every test module in this package runs against its own module-scoped Turso
file DB pre-loaded with the production fixture
(``tests/fixtures/prod_works_v1.json``) through the ORM loader.

Scope trade-off: loading ~8k object-graph rows through the ORM (hooks,
search_text, fts_outbox) takes seconds, and the season-split migration is
stateful by nature — the tests in a module deliberately build on each other
(fixture dependency order, e.g. baseline captured before ``migrated`` runs).
A fresh module-scoped DB per test module keeps modules isolated while making
the suite fast; each test still gets a fresh session (``db``) so Turso MVCC
snapshots never leak stale reads across phases.

All modules must set ``pytestmark = pytest.mark.asyncio(loop_scope="module")``
so the module-scoped async fixtures share the module's event loop (Turso
connections are bound to the loop that created them).

Modules that need TWO loaded databases (before/after migration comparisons)
build them with :func:`open_fixture_db` directly.
"""

from __future__ import annotations

import contextlib
import io
from types import SimpleNamespace

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Importing the unit-suite conftest performs the shared environment setup:
# DATABASE_URL fallback (must precede app imports), app.models registration,
# fast asyncio.sleep for retry/backoff paths.
from tests.unit import conftest as _unit_conftest  # noqa: F401

from .loader import assert_fixture_loaded, load_fixture, load_fixture_data


@contextlib.asynccontextmanager
async def open_fixture_db(db_path):
    """Create a fresh Turso DB at ``db_path``, load the fixture, install it as
    the ``app.database`` globals (restored on exit).

    NOTE: the global engine/factory point at the most recently opened DB —
    callers holding two of these at once (before/after comparisons) must use
    the returned ``factory`` explicitly and only run global-factory-driven
    code (``run_full_migration``) while the intended DB is the active one.
    """
    import app.database as db_mod
    from app.database import apply_db_pragmas, normalize_database_url

    engine = create_async_engine(
        normalize_database_url(f"sqlite+aioturso:///{db_path}"), echo=False
    )
    apply_db_pragmas(engine)
    async with engine.begin() as conn:
        from sqlalchemy import text

        await conn.run_sync(db_mod.Base.metadata.create_all)
        # MVCC is persistent per file; required by isolation_level=CONCURRENT.
        await conn.execute(text("PRAGMA journal_mode='mvcc'"))
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    saved_engine = db_mod.engine
    saved_factory = db_mod.async_session_factory
    db_mod.engine = engine
    db_mod.async_session_factory = factory

    data = load_fixture_data()
    await load_fixture(engine, data)
    async with factory() as session:
        await assert_fixture_loaded(session, data)

    try:
        yield SimpleNamespace(engine=engine, factory=factory, data=data, path=db_path)
    finally:
        db_mod.engine = saved_engine
        db_mod.async_session_factory = saved_factory
        await engine.dispose()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def fixture_db(tmp_path_factory):
    """Module-scoped Turso DB pre-loaded with the production fixture."""
    db_path = tmp_path_factory.mktemp("season-model") / "fixture.db"
    async with open_fixture_db(db_path) as handle:
        yield handle


@pytest_asyncio.fixture(loop_scope="module")
async def db(fixture_db):
    """Fresh session on the module's fixture DB; rolled back at test end."""
    async with fixture_db.factory() as session:
        yield session
        await session.rollback()


async def run_full_migration():
    """Apply the season-split migration in-process (stdout suppressed).

    Uses the module-global ``app.database.async_session_factory``, so it runs
    against the caller module's fixture DB. Returns the per-series reports.
    """
    from scripts.season_split_migration import run_migration

    with contextlib.redirect_stdout(io.StringIO()):
        return await run_migration(apply=True, limit=None)


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def migrated_db(fixture_db):
    """Fixture DB after a full applied migration. Returns the reports."""
    reports = await run_full_migration()
    return SimpleNamespace(
        engine=fixture_db.engine,
        factory=fixture_db.factory,
        data=fixture_db.data,
        reports=reports,
    )
