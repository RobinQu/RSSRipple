"""Pause a real organize owner while a Redis replacement meets its file lock."""

import asyncio
import json
import os
import signal
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

database_url = os.environ["QUEUE_RECOVERY_DATABASE_URL"]
assert urlsplit(database_url).path.startswith("/queue_recovery_")
os.environ["DATABASE_URL"] = database_url

import redis.asyncio as redis  # noqa: E402
from sqlalchemy import inspect  # noqa: E402

from app import database  # noqa: E402
from app.config import settings  # noqa: E402
from app.models.organize_plan import OrganizePlan  # noqa: E402
from app.services import organize_service  # noqa: E402
from app.services.task_queue import RedisQueue  # noqa: E402

URL = os.environ["QUEUE_RECOVERY_REDIS_URL"]
KEY = "organize-takeover"


async def child():
    root = Path(os.environ["ORGANIZE_PROBE_ROOT"])
    settings.organize_lock_dir = str(root / "locks")
    queue = RedisQueue(redis_url=URL)
    finished = asyncio.Event()
    original = organize_service.delete_task_after_organize

    async def hold_cleanup(db, task_id):
        (root / "cleanup-called").write_text(task_id)
        print(json.dumps({"lease_key": queue._consumer_key}), flush=True)
        async with asyncio.timeout(50):
            while not (root / "release").exists():
                await asyncio.sleep(0.02)
        return await original(db, task_id)

    organize_service.delete_task_after_organize = hold_cleanup

    async def handler(payload):
        try:
            async with database.async_session_factory() as db:
                plan = await organize_service.execute_plan(db, payload["plan_id"])
                assert plan.status == "done"
            return {"owner": "old"}
        finally:
            finished.set()

    queue.register(KEY, handler)
    try:
        await queue.start()
        await asyncio.wait_for(finished.wait(), 65)
        # Let the queue's conditional completion run before graceful shutdown.
        await asyncio.sleep(0.1)
    finally:
        await queue.stop()
        await database.engine.dispose()


async def main():
    from tests.unit.test_organize_service import _planned_series_plan

    observer = redis.from_url(URL, decode_responses=True)
    queue = RedisQueue(redis_url=URL)
    process = None
    owns_redis = False
    outcomes = []

    async def replacement(payload):
        async with database.async_session_factory() as db:
            try:
                plan = await organize_service.execute_plan(db, payload["plan_id"])
            except organize_service.OrganizeError as exc:
                assert "正在执行中" in str(exc)
                outcomes.append("busy")
                return {"owner": "replacement", "busy": True}
            assert plan.status == "done"
            outcomes.append("done")
            return {"owner": "replacement", "busy": False}

    queue.register(KEY, replacement)
    try:
        assert await observer.dbsize() == 0
        owns_redis = True
        async with database.engine.begin() as conn:
            assert not await conn.run_sync(lambda sync: inspect(sync).has_table("organize_plans"))
            await conn.run_sync(database.Base.metadata.create_all)
        with tempfile.TemporaryDirectory(prefix="rssripple-organize-takeover-") as directory:
            root = Path(directory)
            settings.organize_lock_dir = str(root / "locks")
            async with database.async_session_factory() as db:
                plan, downloads = await _planned_series_plan(db, root)
                plan_id = plan.id
                source = downloads / "Show.S01/ep04.mkv"
                content = source.read_bytes()
            await queue.start(consume=False)
            job = await queue.enqueue(KEY, KEY, {"plan_id": plan_id})
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "tests.integration.queue_recovery.organize_takeover_driver", "child",
                stdout=asyncio.subprocess.PIPE,
                env=dict(os.environ, ORGANIZE_PROBE_ROOT=directory),
            )
            ready = json.loads(await asyncio.wait_for(process.stdout.readline(), 15))
            assert not source.exists(), "old owner must have completed the move"
            os.kill(process.pid, signal.SIGSTOP)
            async with asyncio.timeout(25):
                while await observer.exists(ready["lease_key"]):
                    await asyncio.sleep(0.2)
            await queue.start()
            async with asyncio.timeout(10):
                while (await queue.status(KEY))["status"] != "done":
                    await asyncio.sleep(0.05)
            assert outcomes == ["busy"]
            replacement_state = await queue.status(KEY)
            (root / "release").touch()
            os.kill(process.pid, signal.SIGCONT)
            assert await asyncio.wait_for(process.wait(), 15) == 0
            assert await queue.status(KEY) == replacement_state, "old completion changed replacement result"
            async with database.async_session_factory() as db:
                completed = await db.get(OrganizePlan, plan_id)
                await db.refresh(completed, ["ops"])
                assert completed.status == "done" and len(completed.ops) == 1
                assert Path(completed.ops[0].dst).read_bytes() == content
            replay = await queue.enqueue(KEY, KEY, {"plan_id": plan_id})
            assert replay["job_id"] != job["job_id"]
            async with asyncio.timeout(10):
                while (await queue.status(KEY))["status"] != "done":
                    await asyncio.sleep(0.05)
            assert outcomes == ["busy", "done"]
            Path(os.environ["QUEUE_RECOVERY_RESULT"]).write_text(json.dumps({
                "mode": "organize_takeover", "child_exit": process.returncode,
                "outcomes": outcomes, "old_completion_rejected": True,
                "target_bytes_preserved": True, "distinct_replay_job": True,
                "data": "synthetic media; real PG/Redis/file locks; SIGSTOP/SIGCONT; natural lease expiry",
            }, indent=2) + "\n")
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
        await queue.stop()
        if owns_redis:
            await observer.flushdb()
        await observer.aclose()
        await database.engine.dispose()


asyncio.run(child() if len(sys.argv) > 1 else main())
