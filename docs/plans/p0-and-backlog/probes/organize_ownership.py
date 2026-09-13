"""Bug evidence only: synthetic media, real PG and independent processes.

Assertions characterize the unfixed behavior; these are NOT acceptance tests.
Only downloader post-actions are stubbed. The real file executor is retained.
"""
import asyncio
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

from seed_helpers import _planned_series_plan
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.database import Base
from app.models.organize_audit import OrganizeAuditEntry
from app.models.organize_plan import OrganizePlan
from app.models.organize_rule import OrganizeRule
from app.services import organize_service as service


async def edge_noop(*args, **kwargs):
    return True


service.resume_task_after_organize = edge_noop
service.delete_task_after_organize = edge_noop


async def actor(factory, plan_id, directory, name):
    original = service.run_execution

    def barrier(*args, **kwargs):
        (directory / f"{name}.entered").write_text(str(os.getpid()))
        deadline = time.monotonic() + 45
        while name == "first" and not (directory / "release").exists():
            if time.monotonic() > deadline:
                raise TimeoutError("parent did not release executor")
            time.sleep(0.05)
        return original(*args, **kwargs)

    service.run_execution = barrier
    async with factory() as db:
        plan = await service.execute_plan(db, plan_id)
        assert plan.status == "done", plan.error_message


async def wait_entered(path):
    deadline = time.monotonic() + 30
    while not path.exists():
        if time.monotonic() > deadline:
            raise TimeoutError(str(path))
        await asyncio.sleep(0.05)


async def main():
    url = make_url(os.environ["DATABASE_URL"])
    if not (
        url.drivername == "postgresql+asyncpg"
        and url.host == "127.0.0.1"
        and url.port is not None
        and url.username == url.password == url.database == "organize_test"
    ):
        raise ValueError("Only the dedicated loopback organize_test database is allowed")
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        if len(sys.argv) > 1:
            await actor(factory, sys.argv[1], Path(sys.argv[2]), sys.argv[3])
            return
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        directory = Path(f"evidence-{uuid.uuid4().hex[:8]}").resolve()
        directory.mkdir()
        async with factory() as db:
            plan, downloads = await _planned_series_plan(db, directory / "overlap", file_op="hardlink")
            plan_id = plan.id
        processes = []
        try:
            for name in ("first", "second"):
                processes.append(await asyncio.create_subprocess_exec(
                    sys.executable, __file__, plan_id, str(directory), name,
                ))
                await wait_entered(directory / f"{name}.entered")
            assert await asyncio.wait_for(processes[1].wait(), 30) == 0
        finally:
            (directory / "release").touch()
            for process in processes:
                try:
                    await asyncio.wait_for(process.wait(), 30)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        assert all(p.returncode == 0 for p in processes)
        async with factory() as db:
            row = await db.get(OrganizePlan, plan_id)
            audits = (await db.execute(select(OrganizeAuditEntry).where(
                OrganizeAuditEntry.plan_id == plan_id,
                OrganizeAuditEntry.action == "execute",
            ))).scalars().all()
            assert row.status == "done" and len(audits) == 2
            assert (downloads / "Show.S01/ep04.mkv").exists()
            overlap = {"plan_id": plan_id, "completed_executions": len(audits),
                       "actor_pids": [int((directory / f"{n}.entered").read_text()) for n in ("first", "second")]}
        (directory / "overlap.json").write_text(json.dumps(overlap, indent=2))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as db:
            plan, downloads = await _planned_series_plan(db, directory / "mode", file_op="hardlink")
            before = [(op.id, op.src, op.dst) for op in plan.ops]
            rule = await db.get(OrganizeRule, plan.rule_id)
            rule.file_op = "move"
            await db.commit()  # exact API commit-before-replan window
            await service.execute_plan(db, plan.id)
            assert plan.status == "done"
            assert not (downloads / "Show.S01/ep04.mkv").exists()
            assert before == [(op.id, op.src, op.dst) for op in plan.ops]
            mode = {"plan_id": plan.id, "planned_mode": "hardlink", "executed_mode": "move",
                    "same_operation_rows": True, "source_deleted": True}
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        async with factory() as db:
            plan, downloads = await _planned_series_plan(db, directory / "replan", file_op="hardlink")
            plan_id = plan.id
        entered = threading.Event()
        release = threading.Event()
        original_collect = service._collect_and_plan

        def pause_replan(*args, **kwargs):
            result = original_collect(*args, **kwargs)
            entered.set()
            if not release.wait(30):
                raise TimeoutError("replan was not released")
            return result

        service._collect_and_plan = pause_replan
        async with factory() as replan_db:
            rebuild = asyncio.create_task(service.replan_open_plans(replan_db, reason="race probe"))
            try:
                assert await asyncio.to_thread(entered.wait, 30)
                async with factory() as execute_db:
                    executed = await service.execute_plan(execute_db, plan_id)
                    assert executed.status == "done"
            finally:
                release.set()
            await rebuild
        service._collect_and_plan = original_collect
        async with factory() as db:
            plan = await db.get(OrganizePlan, plan_id)
            # SQLAlchemy sees pending -> pending as unchanged; only replaced
            # ops are written, leaving the concurrently committed done status.
            assert plan.status == "done"
            assert all(op.status == "pending" for op in plan.ops)
            stale = {"plan_id": plan_id, "completed_before_rebuild_commit": True,
                     "final_plan_status": plan.status, "final_op_statuses": [op.status for op in plan.ops]}
        report = {"backend": "PostgreSQL 16", "data": "synthetic 300-byte media; no real media claim",
                  "O5": overlap, "O2": mode, "O1": stale}
        (directory / "result.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
    finally:
        await engine.dispose()


asyncio.run(main())
