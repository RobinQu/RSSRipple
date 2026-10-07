"""Historical work-FK repair must retain the actual concurrent reference barrier."""
import pytest
from sqlalchemy import text

from tests.integration.dedup.test_late_writers import _late_writer
from tests.integration.resource_work_fk.test_legacy import _missing_audio


@pytest.mark.parametrize('work_fk_turso', ['legacy_no_audio'], indirect=True)
@pytest.mark.parametrize('action', ['new_reference', 'wizard_reference'])
async def test_rebuilt_resource_table_preserves_work_reference_protection(
    work_fk_turso, monkeypatch, action,
):
    engine, _ = work_fk_turso
    # Real startup adds the historical missing column and rebuilds the table
    # to restore its FK. It must preserve both constraint and parent guards.
    await _missing_audio(engine, monkeypatch)
    async with engine.connect() as connection:
        names = set(await connection.scalars(text(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )))
    assert {'ck_file_resources_work_fk_insert', 'ck_file_resources_work_fk_update'} <= names
    for table in ('file_resources', 'resource_work_links'):
        for operation in ('insert', 'update'):
            assert f'trg_work_parent_{table}_movie_id_{operation}' in names
    # Catalog presence alone is insufficient: a real writer arriving after
    # merge locks must not leave an association pointing at a deleted work.
    await _late_writer(work_fk_turso, monkeypatch, action)
