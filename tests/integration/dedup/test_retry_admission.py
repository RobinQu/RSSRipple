"""Recorded backlog, real counter transactions, and controlled newcomer pressure."""
from tests.unit.test_metadata_retry_admission import (
    test_captured_backlog_commits_all_selected_resources_under_newcomer_contention as _case,
)


async def test_backlog_admission_postgres(dedup_postgres, monkeypatch, record_property):
    _, factory = dedup_postgres
    async with factory() as db:
        await _case(db, monkeypatch, record_property)


async def test_backlog_admission_turso(dedup_turso, monkeypatch, record_property):
    _, factory = dedup_turso
    async with factory() as db:
        await _case(db, monkeypatch, record_property)
