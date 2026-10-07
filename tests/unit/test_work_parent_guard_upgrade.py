"""Legacy Turso startup must install work barriers before accepting new writes."""
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import create_tables
from tests.integration.dedup.test_late_writers import _late_writer


@pytest.mark.parametrize("action", ["new_reference", "wizard_reference"])
async def test_existing_database_gets_work_barriers(db_engine, monkeypatch, action):
    # Represent the accepted pre-V25 schema: resource guards exist, work
    # guards do not. Exercise startup twice, then actual concurrent writes.
    async with db_engine.begin() as conn:
        names = (await conn.scalars(text(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'trg_work_parent_%'"
        ))).all()
        assert names
        quote = conn.dialect.identifier_preparer.quote
        for name in names:
            await conn.execute(text(f"DROP TRIGGER {quote(name)}"))
    await create_tables()
    await create_tables()
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    await _late_writer((db_engine, factory), monkeypatch, action)
