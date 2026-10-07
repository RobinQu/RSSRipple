"""Recorded resource identities with synthetic retry/cancellation ordering."""
import pytest

from tests.unit.test_metadata_retry_fairness import (
    test_retry_finishes_before_waiting_resource_takes_its_slot as _case,
)


@pytest.mark.parametrize('separate_channel', [False, True])
@pytest.mark.parametrize('capacity', [1, 4])
@pytest.mark.parametrize('cancel_during_backoff', [False, True])
async def test_metadata_retry_postgres(dedup_postgres, monkeypatch, cancel_during_backoff, capacity, separate_channel):
    _, factory = dedup_postgres
    async with factory() as db:
        await _case(db, monkeypatch, cancel_during_backoff, capacity, separate_channel)


@pytest.mark.parametrize('separate_channel', [False, True])
@pytest.mark.parametrize('capacity', [1, 4])
@pytest.mark.parametrize('cancel_during_backoff', [False, True])
async def test_metadata_retry_turso(dedup_turso, monkeypatch, cancel_during_backoff, capacity, separate_channel):
    _, factory = dedup_turso
    async with factory() as db:
        await _case(db, monkeypatch, cancel_during_backoff, capacity, separate_channel)
