"""Real batch cache writer with concurrent sessions and recorded retry evidence."""
import asyncio

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1 import resources
from app.database import _is_retryable_lock_error
from app.models.metadata_cache import MetadataCache
from tests.integration.dedup.conftest import dedup_postgres as dedup_postgres
from tests.integration.dedup.conftest import dedup_turso as dedup_turso


async def check(pair, monkeypatch):
    engine, factory = pair
    attempts, conflicts = [], []
    class ObservedSession(AsyncSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            attempts.append(self)
        async def commit(self):
            try:
                return await super().commit()
            except Exception as exc:
                if _is_retryable_lock_error(exc):
                    conflicts.append(str(exc))
                raise
    monkeypatch.setattr(resources, 'async_session_factory', async_sessionmaker(
        engine, class_=ObservedSession, expire_on_commit=False))
    results = await asyncio.wait_for(asyncio.gather(*[
        resources._store_batch_analysis('same fingerprint', {'suggestion': label})
        for label in ['first', 'second']
    ], return_exceptions=True), 20)
    assert results == [None, None], results
    async with factory() as db:
        rows = (await db.scalars(select(MetadataCache))).all()
        assert len(rows) == 1
        assert rows[0].title == 'same fingerprint'
        assert rows[0].source == resources._BATCH_ANALYSIS_SOURCE
        assert rows[0].metadata_json in [{'suggestion': 'first'}, {'suggestion': 'second'}]
    assert len(attempts) >= 2
    assert len({id(session) for session in attempts}) == len(attempts)
    print(f'backend={engine.dialect.name} fresh_sessions={len(attempts)} commit_conflicts={len(conflicts)}')


async def test_postgres(dedup_postgres, monkeypatch):
    await check(dedup_postgres, monkeypatch)


async def test_turso(dedup_turso, monkeypatch):
    await check(dedup_turso, monkeypatch)
