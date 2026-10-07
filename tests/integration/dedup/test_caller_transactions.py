"""Both real backends must roll back caught failures in the fetch pipeline.

Discovery responses are synthetic. The production repository, dedup, resource
references, publication, and outer commit are exercised without mocks.
"""
import pytest

from tests.unit.test_dedup_caller_transactions import (
    test_fetch_franchise_merge_is_atomic as _franchise_case,
)
from tests.unit.test_dedup_caller_transactions import (
    test_fetch_online_rehome_commits_only_complete_graph as _rehome_case,
)
from tests.unit.test_queue_metadata_ownership import (
    test_resource_metadata_loss_rolls_back_flushed_fields_and_publication as _loss_case,
)


@pytest.mark.parametrize("conflict", [True, False])
async def test_fetch_rehome_postgres(dedup_postgres, monkeypatch, conflict):
    _, factory = dedup_postgres
    async with factory() as db:
        await _rehome_case(db, monkeypatch, conflict)


@pytest.mark.parametrize("fail_after_repoint", [True, False])
async def test_fetch_franchise_postgres(dedup_postgres, monkeypatch, fail_after_repoint):
    _, factory = dedup_postgres
    async with factory() as db:
        await _franchise_case(db, monkeypatch, fail_after_repoint)


@pytest.mark.parametrize("conflict", [True, False])
async def test_fetch_rehome_turso(dedup_turso, monkeypatch, conflict):
    _, factory = dedup_turso
    async with factory() as db:
        await _rehome_case(db, monkeypatch, conflict)


@pytest.mark.parametrize("fail_after_repoint", [True, False])
async def test_fetch_franchise_turso(dedup_turso, monkeypatch, fail_after_repoint):
    _, factory = dedup_turso
    async with factory() as db:
        await _franchise_case(db, monkeypatch, fail_after_repoint)


@pytest.mark.parametrize("agent_enabled", [False, True])
@pytest.mark.parametrize("raises_loss", [False, True])
async def test_savepoint_ownership_loss_postgres(
    dedup_postgres, monkeypatch, agent_enabled, raises_loss,
):
    _, factory = dedup_postgres
    async with factory() as db:
        await _loss_case(db, monkeypatch, agent_enabled, raises_loss)


@pytest.mark.parametrize("agent_enabled", [False, True])
@pytest.mark.parametrize("raises_loss", [False, True])
async def test_savepoint_ownership_loss_turso(
    dedup_turso, monkeypatch, agent_enabled, raises_loss,
):
    _, factory = dedup_turso
    async with factory() as db:
        await _loss_case(db, monkeypatch, agent_enabled, raises_loss)


