"""A stale queue worker must not start a new destructive file operation."""

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.services import organize_service, task_queue
from tests.unit.test_organize_service import _planned_series_plan


@pytest.mark.parametrize("phase", ["before_lock", "after_validation"])
async def test_expired_queue_owner_cannot_move_files(db_session, tmp_path, monkeypatch, phase):
    plan, downloads = await _planned_series_plan(db_session, tmp_path)
    source = downloads / "Show.S01/ep04.mkv"
    original = source.read_bytes()
    expired = phase == "before_lock"
    validate = organize_service.validate_execution_destinations

    def validate_then_expire(*args):
        nonlocal expired
        validate(*args)
        expired = True

    async def lost():
        if expired:
            raise task_queue.ExecutionOwnershipLostError("Queue owner expired before organize")

    monkeypatch.setattr(task_queue, "require_execution_ownership", lost)
    monkeypatch.setattr(organize_service, "validate_execution_destinations", validate_then_expire)
    with pytest.raises(task_queue.ExecutionOwnershipLostError):
        await organize_service.execute_plan(db_session, plan.id)
    await db_session.refresh(plan)
    assert plan.status == "pending"
    assert source.read_bytes() == original
    assert not list((tmp_path / "lib").rglob("*.mkv"))
    assert all(not Path(op.dst).exists() for op in plan.ops)


async def test_started_move_retains_plan_lock_through_cleanup_after_queue_loss(
    db_session, db_engine, tmp_path, monkeypatch,
):
    plan, downloads = await _planned_series_plan(db_session, tmp_path)
    source = downloads / "Show.S01/ep04.mkv"
    original = source.read_bytes()
    expired = False
    entered, release = asyncio.Event(), asyncio.Event()
    execute = organize_service.run_execution
    cleanup = organize_service.delete_task_after_organize
    cleanup_calls = []

    async def guard():
        if expired:
            raise task_queue.ExecutionOwnershipLostError("Queue expired after file execution started")

    def execute_then_expire(*args, **kwargs):
        nonlocal expired
        result = execute(*args, **kwargs)
        expired = True
        return result

    async def held_cleanup(db, task_id):
        cleanup_calls.append(task_id)
        entered.set()
        await release.wait()
        return await cleanup(db, task_id)

    monkeypatch.setattr(task_queue, "require_execution_ownership", guard)
    monkeypatch.setattr(organize_service, "run_execution", execute_then_expire)
    monkeypatch.setattr(organize_service, "delete_task_after_organize", held_cleanup)
    operation = asyncio.create_task(organize_service.execute_plan(db_session, plan.id))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        assert expired and not operation.done()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as contender:
            with pytest.raises(organize_service.OrganizeError, match="正在执行中"):
                await organize_service.execute_plan(contender, plan.id)
    finally:
        release.set()
        completed = await operation
    assert completed.status == "done"
    assert len(cleanup_calls) == 1
    assert not source.exists()
    assert len(completed.ops) == 1
    assert Path(completed.ops[0].dst).read_bytes() == original
