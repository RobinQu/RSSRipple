"""Regression tests for bounded windowed-run selection and batched processing.

A manual windowed agent run with ``scan_since=None`` ("no limit") covers the
channel's entire history. Selection and processing must stay bounded:
ids are fetched in keyset pages (``_select_window_resource_ids``) and
resources are materialised/processed in batches
(``_process_selected_resources``) instead of one unbounded query + list.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.job_handlers import _process_selected_resources, _select_window_resource_ids


def _uuid() -> str:
    return str(uuid.uuid4())


async def _seed_resources(count: int, *, channel_id: str | None = None,
                          created_at: datetime | None = None, step: timedelta = timedelta(minutes=1)):
    """Seed a Channel plus ``count`` FileResources; return (channel_id, ids).

    Ids come back in ``(created_at, id)`` order — the order the windowed
    selection must produce.
    """
    from app.database import async_session_factory
    from app.models.channel import Channel
    from app.models.file_resource import FileResource

    channel_id = channel_id or _uuid()
    base = created_at or datetime(2026, 1, 1)
    resources = [
        FileResource(
            id=_uuid(), channel_id=channel_id, guid=_uuid(),
            title_raw=f"[G] Show {i}", search_title="Show",
            torrent_url=f"http://x/{channel_id}-{i}.torrent",
            created_at=base + step * i,
        )
        for i in range(count)
    ]
    async with async_session_factory() as session:
        session.add(Channel(
            id=channel_id, name="ch", type="rss_feed",
            url=f"https://x/rss-{channel_id}",  # channels.url is unique
            field_mapping={"list_locator": {"source": "entries"}},
            metadata_agent_enabled=False,
        ))
        session.add_all(resources)
        await session.commit()
    ids = [r.id for r in sorted(resources, key=lambda r: (r.created_at, r.id))]
    return channel_id, ids


@pytest.mark.asyncio
async def test_window_selection_paginates_full_history(db_engine, monkeypatch):
    """scan_since=None selects the whole channel history in bounded pages."""
    from app.database import async_session_factory

    channel_id, ids = await _seed_resources(5)
    monkeypatch.setattr("app.job_handlers._RUN_AGENT_SCAN_PAGE_SIZE", 2)

    async with async_session_factory() as session:
        selected, advance_to = await _select_window_resource_ids(
            session, channel_id, None
        )

    # 5 rows with a page size of 2 can only be correct if keyset pagination
    # fetched 3 pages; one unbounded query is exactly what this guards against.
    assert selected == ids
    assert advance_to == datetime(2026, 1, 1) + timedelta(minutes=4)


@pytest.mark.asyncio
async def test_window_selection_respects_scan_since(db_engine, monkeypatch):
    from app.database import async_session_factory

    channel_id, ids = await _seed_resources(5)
    monkeypatch.setattr("app.job_handlers._RUN_AGENT_SCAN_PAGE_SIZE", 1)
    cutoff = datetime(2026, 1, 1) + timedelta(minutes=2)

    async with async_session_factory() as session:
        selected, advance_to = await _select_window_resource_ids(
            session, channel_id, cutoff
        )

    assert selected == ids[3:]  # strictly after the cutoff
    assert advance_to == datetime(2026, 1, 1) + timedelta(minutes=4)


@pytest.mark.asyncio
async def test_window_selection_keyset_tolerates_equal_created_at(db_engine, monkeypatch):
    """Rows sharing a created_at must not be skipped or duplicated by paging."""
    from app.database import async_session_factory

    same_instant = datetime(2026, 2, 1)
    channel_id, ids = await _seed_resources(
        4, created_at=same_instant, step=timedelta(0)
    )
    monkeypatch.setattr("app.job_handlers._RUN_AGENT_SCAN_PAGE_SIZE", 3)

    async with async_session_factory() as session:
        selected, advance_to = await _select_window_resource_ids(
            session, channel_id, None
        )

    assert selected == ids
    assert advance_to == same_instant


@pytest.mark.asyncio
async def test_window_selection_empty_channel(db_engine):
    from app.database import async_session_factory

    channel_id, _ = await _seed_resources(0)
    async with async_session_factory() as session:
        selected, advance_to = await _select_window_resource_ids(
            session, channel_id, None
        )
    assert selected == []
    assert advance_to is None


@pytest.mark.asyncio
async def test_process_selected_resources_batches_materialisation(db_engine, monkeypatch):
    """Five ids with a batch size of 2 → three process_resources calls."""
    from app.database import async_session_factory
    from app.services.agent_service import RunResult

    channel_id, ids = await _seed_resources(5)
    monkeypatch.setattr("app.job_handlers._RUN_AGENT_PROCESS_BATCH_SIZE", 2)

    calls: list[list[str]] = []

    def _result_for(agent, resources, *args, **kwargs):
        calls.append([r.id for r in resources])
        return RunResult(total_resources=len(resources), matched=len(resources), dispatched=1)

    process = AsyncMock(side_effect=_result_for)
    monkeypatch.setattr("app.services.agent_service.process_resources", process)

    agent = MagicMock()
    snapshot = object()
    async with async_session_factory() as session:
        merged = await _process_selected_resources(
            session, agent, ids,
            consumption_snapshot=snapshot,
            required_metadata_fields=["title"],
        )

    assert [len(c) for c in calls] == [2, 2, 1]
    # Batches follow the selection order; each batch is created_at-sorted,
    # which for this fixture equals the chunk of the selection.
    assert [r for call in calls for r in call] == ids
    for call in process.await_args_list:
        assert call.kwargs["autocommit"] is True
        assert call.kwargs["consumption_snapshot"] is snapshot
        assert call.kwargs["required_metadata_fields"] == ["title"]
    assert merged.total_resources == 5
    assert merged.matched == 5
    assert merged.dispatched == 3  # one per batch


@pytest.mark.asyncio
async def test_process_selected_resources_empty_selection_single_call(db_engine, monkeypatch):
    """An empty selection keeps the old single-call behaviour (legacy decision
    retirement / suggestion persistence still run inside process_resources)."""
    from app.database import async_session_factory
    from app.services.agent_service import RunResult

    process = AsyncMock(return_value=RunResult())
    monkeypatch.setattr("app.services.agent_service.process_resources", process)

    async with async_session_factory() as session:
        merged = await _process_selected_resources(
            session, MagicMock(), [],
            consumption_snapshot=None, required_metadata_fields=None,
        )

    process.assert_awaited_once()
    assert process.await_args.args[1] == []
    assert merged.total_resources == 0


@pytest.mark.asyncio
async def test_process_selected_resources_merges_errors_and_suggestions(db_engine, monkeypatch):
    from app.database import async_session_factory
    from app.services.agent_service import RunResult

    channel_id, ids = await _seed_resources(2)
    monkeypatch.setattr("app.job_handlers._RUN_AGENT_PROCESS_BATCH_SIZE", 1)

    results = [
        RunResult(total_resources=1, errors=["boom"], matched_resource_ids=[ids[0]]),
        RunResult(total_resources=1, suggestions=[{"k": "v"}], pending_decisions=1),
    ]
    process = AsyncMock(side_effect=results)
    monkeypatch.setattr("app.services.agent_service.process_resources", process)

    async with async_session_factory() as session:
        merged = await _process_selected_resources(
            session, MagicMock(), ids,
            consumption_snapshot=None, required_metadata_fields=None,
        )

    assert merged.errors == ["boom"]
    assert merged.suggestions == [{"k": "v"}]
    assert merged.matched_resource_ids == [ids[0]]
    assert merged.pending_decisions == 1
    assert merged.total_resources == 2
