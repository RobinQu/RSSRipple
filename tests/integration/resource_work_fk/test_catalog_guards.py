"""Existing guard names must prove their definition and validation state."""
import pytest
from sqlalchemy import insert, inspect, select, text, update
from sqlalchemy.exc import DBAPIError

from app.models.file_resource import RESOURCE_WORK_FK_CHECK, RESOURCE_WORK_FK_CONSTRAINT, FileResource
from app.services.resource_work_schema import ensure_resource_work_fk_guard
from tests.integration.resource_work_fk.test_constraint import _seed
from tests.integration.resource_work_fk.test_legacy import _upgrade


async def _checks(engine):
    async with engine.connect() as conn:
        return await conn.run_sync(lambda sync: inspect(sync).get_check_constraints('file_resources'))


async def _drift(engine, monkeypatch):
    base, identities = await _seed(engine)
    async with engine.begin() as conn:
        # Both deliberately wrong definitions actually permit a dual FK.
        await conn.execute(insert(FileResource).values(**base, series_id=identities[0], movie_id=identities[1]))
    before = await _checks(engine)
    with pytest.raises(ValueError, match='Unexpected resource work FK CHECK definition'):
        await _upgrade(engine, monkeypatch)
    assert await _checks(engine) == before
    async with engine.connect() as conn:
        row = (await conn.execute(select(FileResource.__table__))).mappings().one()
        if engine.dialect.name == 'sqlite':
            assert await conn.scalar(text("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'ck_file_resources_work_fk_%'")) == 0
    assert row['series_id'] == identities[0] and row['movie_id'] == identities[1]
    assert row['guid'] == base['guid'] and row['title_raw'] == base['title_raw']


@pytest.mark.parametrize('work_fk_postgres', ['drift_threshold'], indirect=True)
async def test_postgres_native_check_drift(work_fk_postgres, monkeypatch):
    await _drift(work_fk_postgres[0], monkeypatch)


@pytest.mark.parametrize('work_fk_turso', ['drift_threshold', 'drift_parentheses'], indirect=True)
async def test_turso_native_check_drift(work_fk_turso, monkeypatch):
    await _drift(work_fk_turso[0], monkeypatch)


@pytest.mark.parametrize('work_fk_postgres', ['legacy'], indirect=True)
@pytest.mark.parametrize('dirty', [False, True])
async def test_postgres_not_valid_history(work_fk_postgres, monkeypatch, dirty):
    engine, _ = work_fk_postgres
    base, identities = await _seed(engine)
    async with engine.begin() as conn:
        await conn.execute(insert(FileResource).values(**base, series_id=identities[0], movie_id=identities[1] if dirty else None))
        await conn.execute(text(f'ALTER TABLE file_resources ADD CONSTRAINT {RESOURCE_WORK_FK_CONSTRAINT} CHECK ({RESOURCE_WORK_FK_CHECK}) NOT VALID'))
    query = text("SELECT convalidated FROM pg_constraint WHERE conrelid='file_resources'::regclass AND conname=:name")
    async with engine.connect() as conn:
        assert await conn.scalar(query, {'name': RESOURCE_WORK_FK_CONSTRAINT}) is False
        before = (await conn.execute(select(FileResource.__table__))).mappings().one()
    if dirty:
        with pytest.raises(ValueError, match='sample IDs: ' + base['id']):
            await _upgrade(engine, monkeypatch)
    else:
        await _upgrade(engine, monkeypatch)
        await _upgrade(engine, monkeypatch)
    async with engine.connect() as conn:
        assert await conn.scalar(query, {'name': RESOURCE_WORK_FK_CONSTRAINT}) is (not dirty)
        after = (await conn.execute(select(FileResource.__table__))).mappings().one()
    assert dict(after) == dict(before)
    if not dirty:
        with pytest.raises(DBAPIError, match=RESOURCE_WORK_FK_CONSTRAINT):
            async with engine.begin() as conn:
                await conn.execute(update(FileResource).values(movie_id=identities[1]))


@pytest.mark.parametrize('work_fk_postgres', ['legacy'], indirect=True)
async def test_postgres_guard_closes_scan_write_gap(work_fk_postgres):
    engine, _ = work_fk_postgres
    base, identities = await _seed(engine)
    async with engine.begin() as conn:
        await conn.execute(insert(FileResource).values(**base, series_id=identities[0]))
    async with engine.begin() as migrator:
        await ensure_resource_work_fk_guard(migrator)
        # A separate physical connection cannot commit an invalid write
        # between the historical scan and the guard transaction's commit.
        with pytest.raises(DBAPIError, match='lock timeout'):
            async with engine.begin() as writer:
                await writer.execute(text("SET LOCAL lock_timeout='100ms'"))
                await writer.execute(update(FileResource).values(movie_id=identities[1]))
    with pytest.raises(DBAPIError, match=RESOURCE_WORK_FK_CONSTRAINT):
        async with engine.begin() as writer:
            await writer.execute(update(FileResource).values(movie_id=identities[1]))
    async with engine.connect() as conn:
        row = (await conn.execute(select(FileResource.__table__))).mappings().one()
    assert row['series_id'] == identities[0] and row['movie_id'] is None


@pytest.mark.parametrize('work_fk_turso', ['legacy'], indirect=True)
async def test_turso_old_snapshot_cannot_bypass_committed_guard(work_fk_turso, record_property):
    from app.database import _is_retryable_lock_error
    from app.services.resource_work_schema import upgrade_sqlite_resource_work_fk

    engine, _ = work_fk_turso
    base, identities = await _seed(engine)
    async with engine.begin() as conn:
        await conn.execute(insert(FileResource).values(**base, series_id=identities[0]))
    installed = False
    async with engine.connect() as writer:
        transaction = await writer.begin()
        # Force a physical CONCURRENT transaction before reading the old
        # catalog; SQLAlchemy's logical transaction alone is insufficient.
        await writer.execute(text('UPDATE file_resources SET id=id WHERE 1=0'))
        assert await writer.scalar(select(FileResource.series_id)) == identities[0]
        try:
            try:
                await upgrade_sqlite_resource_work_fk(engine)
                installed = True
            except DBAPIError as error:
                assert _is_retryable_lock_error(error), str(error)
            record_property('guard_installed_with_old_snapshot', installed)
            if installed:
                with pytest.raises(DBAPIError) as rejected:
                    await writer.execute(update(FileResource).values(movie_id=identities[1]))
                    await transaction.commit()
                record_property('old_snapshot_rejection', str(rejected.value.orig))
                assert ('Database schema conflict' in str(rejected.value.orig)
                        or RESOURCE_WORK_FK_CONSTRAINT in str(rejected.value.orig)), str(rejected.value)
        finally:
            if transaction.is_active:
                await transaction.rollback()
    if not installed:
        await upgrade_sqlite_resource_work_fk(engine)
    with pytest.raises(DBAPIError, match=RESOURCE_WORK_FK_CONSTRAINT):
        async with engine.begin() as writer:
            await writer.execute(update(FileResource).values(movie_id=identities[1]))
    async with engine.connect() as conn:
        row = (await conn.execute(select(FileResource.__table__))).mappings().one()
    assert row['series_id'] == identities[0] and row['movie_id'] is None
