"""Selected primary identity is indexed even when it was absent from the bag."""
import pytest

from tests.unit.test_external_ids import test_dedup_merge_unions_bags as _case


@pytest.mark.parametrize("kind", ["series", "movie"])
async def test_selected_primary_postgres(dedup_postgres, kind):
    _, factory = dedup_postgres
    async with factory() as db:
        await _case(db, kind)


@pytest.mark.parametrize("kind", ["series", "movie"])
async def test_selected_primary_turso(dedup_turso, kind):
    _, factory = dedup_turso
    async with factory() as db:
        await _case(db, kind)
