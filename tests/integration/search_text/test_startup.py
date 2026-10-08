"""Production startup upgrades legacy columns before backfill and serves writes."""
import pytest
from sqlalchemy import event, select, text

from app import database
from app.config import settings
from app.models.movie import Movie
from app.services.search_text_schema import ensure_search_text_columns
from tests.integration.search_text.test_storage import MODELS, input_values, seed

TABLES = ('audio_works', 'movies', 'tv_series', 'work_collections')


async def column_types(conn):
    return dict((await conn.execute(text(
        "SELECT table_name, data_type FROM information_schema.columns "
        "WHERE table_schema=current_schema() AND column_name='search_text'"
    ))).all())


@pytest.mark.parametrize('legacy', ['fresh', 'varchar', 'text', 'missing'])
async def test_postgres_actual_startup(dedup_postgres, monkeypatch, legacy):
    engine, factory = dedup_postgres
    monkeypatch.setattr(settings, 'database_url', engine.url.render_as_string(hide_password=False))
    monkeypatch.setattr(database, 'engine', engine)
    identities = {}
    if legacy != 'fresh':
        async with factory() as db:
            for model in MODELS:
                title, _, _ = input_values(model, 4095)
                identities[model] = await seed(db, model, title, ['original alias'])
        async with engine.begin() as conn:
            for table in TABLES:
                if legacy == 'missing':
                    await conn.execute(text(f'ALTER TABLE {table} DROP COLUMN search_text'))
                elif legacy == 'varchar':
                    await conn.execute(text(f'ALTER TABLE {table} ALTER COLUMN search_text TYPE VARCHAR(4096)'))
    # Production upgrade starts in a new process, without cached old statements.
    await engine.dispose()
    await database.create_tables()
    async with engine.connect() as conn:
        assert await column_types(conn) == {table: 'text' for table in TABLES}
    async with factory() as db:
        for model, identity in identities.items():
            row = await db.get(model, identity)
            assert row.aliases == ['original alias']
            assert row.search_text is not None, model.__tablename__
            assert row.search_text.endswith(' original alias')
    statements = []

    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(engine.sync_engine, 'before_cursor_execute', capture)
    try:
        await database.create_tables()
    finally:
        event.remove(engine.sync_engine, 'before_cursor_execute', capture)
    assert not any(s.lstrip().upper().startswith('ALTER TABLE') and 'search_text' in s for s in statements)
    # A distinct post-startup session stores and queries a tail past the old bound.
    title, alias, expected = input_values(Movie, 8192)
    async with factory() as db:
        identity = await seed(db, Movie, title, [alias])
    async with factory() as db:
        row = await db.get(Movie, identity)
        assert row.search_text == expected
        assert list(await db.scalars(select(Movie.id).where(Movie.search_text.contains('tailmarker')))) == [identity]


@pytest.mark.parametrize('drift', ['integer', 'domain', 'missing'])
async def test_postgres_rejects_unknown_schema_before_alter(dedup_postgres, drift):
    engine, _ = dedup_postgres
    async with engine.begin() as conn:
        for table in TABLES:
            await conn.execute(text(f'ALTER TABLE {table} ALTER COLUMN search_text TYPE VARCHAR(4096)'))
        if drift == 'integer':
            await conn.execute(text('ALTER TABLE work_collections ALTER COLUMN search_text TYPE INTEGER USING NULL'))
        elif drift == 'domain':
            await conn.execute(text('CREATE DOMAIN search_text_domain AS TEXT CHECK(length(VALUE)<100)'))
            await conn.execute(text('ALTER TABLE work_collections ALTER COLUMN search_text TYPE search_text_domain'))
        else:
            await conn.execute(text('ALTER TABLE work_collections DROP COLUMN search_text'))
    async with engine.begin() as conn:
        before = await column_types(conn)
        with pytest.raises(RuntimeError, match='Unexpected search text schema'):
            await ensure_search_text_columns(conn)
        assert await column_types(conn) == before


@pytest.mark.parametrize('legacy', ['fresh', 'null'])
async def test_turso_actual_startup(dedup_turso, monkeypatch, legacy):
    engine, factory = dedup_turso
    monkeypatch.setattr(database, 'engine', engine)
    identities = {}
    if legacy == 'null':
        async with factory() as db:
            for model in MODELS:
                title, alias, _ = input_values(model, 8192)
                identities[model] = await seed(db, model, title, [alias])
        async with engine.begin() as conn:
            for table in TABLES:
                await conn.execute(text(f'UPDATE {table} SET search_text=NULL'))
    await database.create_tables()
    await database.create_tables()
    async with factory() as db:
        for model, identity in identities.items():
            row = await db.get(model, identity)
            _, alias, expected = input_values(model, 8192)
            assert row.aliases == [alias]
            assert row.search_text == expected
        for model in MODELS:
            assert list(await db.scalars(select(model.id).where(model.search_text.is_(None)))) == []


async def test_postgres_rollback_and_gin_preservation(dedup_postgres):
    engine, factory = dedup_postgres
    title, _, _ = input_values(Movie, 4095)
    async with factory() as db:
        identity = await seed(db, Movie, title, ['original alias'])
    async with engine.begin() as conn:
        for table in TABLES:
            await conn.execute(text(f'ALTER TABLE {table} ALTER COLUMN search_text TYPE VARCHAR(4096)'))
        await database._ensure_pg_trgm_indexes(conn)
        original = await column_types(conn)
        query = text("SELECT indexname, indexdef FROM pg_indexes WHERE schemaname=current_schema() "
                     "AND indexname LIKE '%search_text_trgm' ORDER BY indexname")
        indexes = (await conn.execute(query)).all()
        assert len(indexes) == 3
    with pytest.raises(RuntimeError, match='synthetic rollback'):
        async with engine.begin() as conn:
            await ensure_search_text_columns(conn)
            assert all(value == 'text' for value in (await column_types(conn)).values())
            raise RuntimeError('synthetic rollback')
    async with engine.begin() as conn:
        assert await column_types(conn) == original
        await ensure_search_text_columns(conn)
    await engine.dispose()
    async with engine.connect() as conn:
        assert (await conn.execute(query)).all() == indexes
    async with factory() as db:
        row = await db.get(Movie, identity)
        assert row.title_cn == title and row.aliases == ['original alias']
        assert row.search_text.endswith(' original alias')


async def test_postgres_two_startups_wait_for_shared_ddl_lock(dedup_postgres, monkeypatch):
    import asyncio

    engine, _ = dedup_postgres
    monkeypatch.setattr(database, 'engine', engine)
    monkeypatch.setattr(settings, 'database_url', engine.url.render_as_string(hide_password=False))
    async with engine.begin() as conn:
        for table in TABLES:
            await conn.execute(text(f'ALTER TABLE {table} ALTER COLUMN search_text TYPE VARCHAR(4096)'))
    await engine.dispose()
    statements = []

    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement)

    event.listen(engine.sync_engine, 'before_cursor_execute', capture)
    tasks = []
    try:
        async with engine.connect() as blocker:
            await blocker.execute(text('SELECT pg_advisory_xact_lock(72057594037927937)'))
            tasks = [asyncio.create_task(database.create_tables()) for _ in range(2)]
            waiting = 0
            async with engine.connect() as observer:
                for _ in range(120):
                    waiting = await observer.scalar(text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND wait_event='advisory'"
                    ))
                    if waiting == 2:
                        break
                    assert not any(task.done() for task in tasks)
                    await asyncio.sleep(0.025)
            assert waiting == 2, 'Both actual startups must be observed waiting on the DDL lock'
            await blocker.rollback()
        await asyncio.wait_for(asyncio.gather(*tasks), 15)
        alters = [s for s in statements if s.startswith('ALTER TABLE') and 'ALTER COLUMN search_text TYPE TEXT' in s]
        assert len(alters) == 4
        async with engine.connect() as conn:
            assert await column_types(conn) == {table: 'text' for table in TABLES}
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        event.remove(engine.sync_engine, 'before_cursor_execute', capture)
