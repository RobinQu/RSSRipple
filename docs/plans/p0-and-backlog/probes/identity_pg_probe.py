"""Run the identity persistence matrix on a disposable, explicitly selected PostgreSQL."""
import asyncio
import json
import os
import tempfile
from pathlib import Path

from sqlalchemy.engine import make_url

pg_url = os.environ['IDENTITY_PROBE_DATABASE_URL']
url = make_url(pg_url)
assert url.drivername == 'postgresql+asyncpg'
assert url.host in {'localhost', '127.0.0.1'}
assert (url.username, url.password, url.database) == ('organize_test',) * 3
os.environ['DATABASE_URL'] = pg_url
os.environ['POSTER_CACHE_DIR'] = str(Path(tempfile.mkdtemp(prefix='rssripple-identity-pg-')) / 'posters')

import pytest  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

import app.database as database  # noqa: E402
from app.config import settings  # noqa: E402
from tests.integration.metadata.test_identity_persistence import (  # noqa: E402
    test_process_persists_only_evidenced_identity,
)


async def main():
    # Test helper imports set an environment default; pin the explicitly
    # guarded backend before any application session is used.
    settings.database_url = pg_url
    engine = create_async_engine(pg_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    database.engine, database.async_session_factory = engine, factory
    results = []
    try:
        for source in ('wikipedia', 'wikipedia_react', 'tmdb'):
            for variant in ('valid', 'invalid_primary', 'forged_alias', 'trusted_alias', 'old_cache'):
                async with engine.begin() as connection:
                    await connection.run_sync(database.Base.metadata.drop_all)
                    await connection.run_sync(database.Base.metadata.create_all)
                with pytest.MonkeyPatch.context() as monkeypatch:
                    async with factory() as session:
                        await test_process_persists_only_evidenced_identity(session, monkeypatch, variant, source)
                results.append({'source': source, 'variant': variant, 'passed': True})
        print(json.dumps({'backend': 'PostgreSQL', 'passed': len(results), 'cases': results,
                          'fixture': 'synthetic source/LLM; real process/upsert/cache/identity bag'}))
    finally:
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
