"""Real startup upgrades, historical conflicts, drift, and atomic DDL rollback."""
import pytest
from sqlalchemy import event, inspect, select, text, update

from app.config import settings
from app.database import create_tables
from app.models.file_resource import RESOURCE_WORK_FK_CONSTRAINT, FileResource
from app.models.movie import Movie
from app.models.series import TVSeries
from tests.integration.resource_work_fk.test_constraint import COMBINATIONS, _case


async def _upgrade(engine, monkeypatch):
    monkeypatch.setattr("app.database.engine", engine)
    monkeypatch.setattr(settings, "database_url", engine.url.render_as_string(hide_password=False))
    await create_tables()


async def _guards(engine):
    async with engine.connect() as conn:
        if engine.dialect.name == 'postgresql':
            constraints = await conn.run_sync(lambda sync: inspect(sync).get_check_constraints('file_resources'))
            return [item for item in constraints if item['name'] == RESOURCE_WORK_FK_CONSTRAINT]
        return list((await conn.execute(text(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'ck_file_resources_work_fk_%' ORDER BY name"
        ))).all())


async def _negative(engine, monkeypatch, failure):
    await _case(engine, (False, False, False), True, 'insert')
    async with engine.begin() as conn:
        if engine.dialect.name == 'sqlite':
            await conn.execute(text('BEGIN'))
        if failure == 'dirty':
            series = await conn.scalar(select(TVSeries.id))
            movie = await conn.scalar(select(Movie.id))
            await conn.execute(update(FileResource).values(series_id=series, movie_id=movie))
        elif failure == 'drift':
            ddl = (f'ALTER TABLE file_resources ADD CONSTRAINT {RESOURCE_WORK_FK_CONSTRAINT} CHECK (1=1)'
                   if engine.dialect.name == 'postgresql' else
                   f"CREATE TRIGGER {RESOURCE_WORK_FK_CONSTRAINT}_insert BEFORE INSERT ON file_resources BEGIN SELECT 1; END")
            await conn.execute(text(ddl))
    async with engine.connect() as conn:
        before = (await conn.execute(select(FileResource.__table__))).mappings().one()
    guards_before = await _guards(engine)
    installed = []

    def completed(conn, cursor, statement, parameters, context, executemany):
        if RESOURCE_WORK_FK_CONSTRAINT in statement and ('ADD CONSTRAINT' in statement or 'CREATE TRIGGER' in statement):
            installed.append(statement)

    def inject(conn, cursor, statement, parameters, context, executemany):
        if failure == 'interrupt' and RESOURCE_WORK_FK_CONSTRAINT in statement:
            if 'VALIDATE CONSTRAINT' in statement or ('CREATE TRIGGER' in statement and 'BEFORE UPDATE' in statement):
                assert installed, 'First guard DDL must actually complete before the injected failure'
                raise RuntimeError('Injected second guard installation failure')

    event.listen(engine.sync_engine, 'after_cursor_execute', completed)
    event.listen(engine.sync_engine, 'before_cursor_execute', inject)
    try:
        error = RuntimeError if failure == 'interrupt' else ValueError
        match = {'interrupt':'Injected second guard', 'dirty':'sample IDs: ' + before['id'], 'drift':'Unexpected resource work FK'}[failure]
        with pytest.raises(error, match=match):
            await _upgrade(engine, monkeypatch)
    finally:
        event.remove(engine.sync_engine, 'after_cursor_execute', completed)
        event.remove(engine.sync_engine, 'before_cursor_execute', inject)
    async with engine.connect() as conn:
        after = (await conn.execute(select(FileResource.__table__))).mappings().one()
    assert dict(after) == dict(before), 'Migration failure must preserve historical resource values'
    assert await _guards(engine) == guards_before
    if failure == 'interrupt':
        assert len(installed) == 1
        await _upgrade(engine, monkeypatch)
        assert await _guards(engine)


@pytest.mark.parametrize('work_fk_postgres', ['legacy'], indirect=True)
@pytest.mark.parametrize('bits', COMBINATIONS)
@pytest.mark.parametrize('has_collection', [False, True])
@pytest.mark.parametrize('operation', ['insert', 'update'])
async def test_legacy_postgres_upgrade(work_fk_postgres, monkeypatch, bits, has_collection, operation):
    engine, _ = work_fk_postgres
    await _upgrade(engine, monkeypatch)
    await _upgrade(engine, monkeypatch)
    await _case(engine, bits, has_collection, operation)


@pytest.mark.parametrize('work_fk_turso', ['legacy'], indirect=True)
@pytest.mark.parametrize('bits', COMBINATIONS)
@pytest.mark.parametrize('has_collection', [False, True])
@pytest.mark.parametrize('operation', ['insert', 'update'])
async def test_legacy_turso_upgrade(work_fk_turso, monkeypatch, bits, has_collection, operation):
    engine, _ = work_fk_turso
    await _upgrade(engine, monkeypatch)
    await _upgrade(engine, monkeypatch)
    await _case(engine, bits, has_collection, operation)


@pytest.mark.parametrize('work_fk_postgres', ['legacy'], indirect=True)
@pytest.mark.parametrize('failure', ['dirty', 'drift', 'interrupt'])
async def test_legacy_postgres_failure(work_fk_postgres, monkeypatch, failure):
    await _negative(work_fk_postgres[0], monkeypatch, failure)


@pytest.mark.parametrize('work_fk_turso', ['legacy'], indirect=True)
@pytest.mark.parametrize('failure', ['dirty', 'drift', 'interrupt'])
async def test_legacy_turso_failure(work_fk_turso, monkeypatch, failure):
    await _negative(work_fk_turso[0], monkeypatch, failure)


async def _missing_audio(engine, monkeypatch):
    from sqlalchemy import delete, insert
    from sqlalchemy.exc import DBAPIError

    from app.models.audio_work import AudioWork
    from tests.integration.resource_work_fk.test_constraint import _seed

    base, identities = await _seed(engine)
    async with engine.begin() as conn:
        columns = await conn.run_sync(lambda sync: {c['name'] for c in inspect(sync).get_columns('file_resources')})
        assert 'audio_work_id' not in columns
        await conn.execute(insert(FileResource.__table__).values(**base, series_id=identities[0]))
        before = (await conn.execute(text('SELECT id, guid, title_raw, series_id, movie_id, collection_id FROM file_resources'))).mappings().one()
    await _upgrade(engine, monkeypatch)
    await _upgrade(engine, monkeypatch)
    async with engine.connect() as conn:
        after = (await conn.execute(select(FileResource.__table__))).mappings().one()
        foreign_keys = await conn.run_sync(lambda sync: inspect(sync).get_foreign_keys('file_resources'))
    assert {key: after[key] for key in before} == dict(before)
    assert after['audio_work_id'] is None
    assert any(fk['constrained_columns'] == ['audio_work_id'] and fk['referred_table'] == 'audio_works'
               and fk['options']['ondelete'] == 'SET NULL' for fk in foreign_keys)
    assert await _guards(engine), 'Turso table rebuild must retain the write guards'
    # Cross-type change is one UPDATE; parent deletion must still clear its FK.
    async with engine.begin() as conn:
        await conn.execute(update(FileResource).values(series_id=None, audio_work_id=identities[2]))
    async with engine.connect() as conn:
        assert await conn.scalar(select(FileResource.audio_work_id)) == identities[2]
    async with engine.begin() as conn:
        await conn.execute(delete(AudioWork).where(AudioWork.id == identities[2]))
    async with engine.connect() as conn:
        assert await conn.scalar(select(FileResource.audio_work_id)) is None
    with pytest.raises(DBAPIError, match='ck_file_resources_work_fk'):
        async with engine.begin() as conn:
            await conn.execute(update(FileResource).values(series_id=identities[0], movie_id=identities[1]))
    async with engine.connect() as conn:
        row = (await conn.execute(select(FileResource.__table__))).mappings().one()
    assert row['series_id'] is None and row['movie_id'] is None and row['audio_work_id'] is None
    assert row['collection_id'] == base['collection_id'] and row['title_raw'] == base['title_raw']


@pytest.mark.parametrize('work_fk_postgres', ['legacy_no_audio'], indirect=True)
async def test_legacy_postgres_missing_audio(work_fk_postgres, monkeypatch):
    await _missing_audio(work_fk_postgres[0], monkeypatch)


@pytest.mark.parametrize('work_fk_turso', ['legacy_no_audio'], indirect=True)
async def test_legacy_turso_missing_audio(work_fk_turso, monkeypatch):
    await _missing_audio(work_fk_turso[0], monkeypatch)
