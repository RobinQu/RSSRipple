"""Dedicated PG/Redis only: lose queue state, recover with two fresh processes."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse


def guard():
    assert os.environ.get("PROBE_ALLOW_ERASE_ISOLATED_REDIS") == "1", "Dedicated test Redis required"
    from sqlalchemy.engine import make_url

    url = make_url(os.environ["DATABASE_URL"])
    assert url.drivername == "postgresql+asyncpg" and url.host == "127.0.0.1"
    assert (url.username, url.password, url.database) == ("organize_test",) * 3
    redis = urlparse(os.environ["PROBE_REDIS_URL"])
    assert redis.scheme == "redis" and redis.hostname == "127.0.0.1"


async def child(mode):
    guard()
    from datetime import date, timedelta

    from sqlalchemy import select

    import app.database as database
    import app.models  # noqa: F401
    import app.services.task_queue as queue_module
    from app.job_handlers import _handle_run_agent
    from app.models.agent import Agent
    from app.models.download_task import DownloadTask
    from app.models.movie import Movie
    from app.services.publication_dispatch import dispatch_pending_publications
    from app.services.resource_publication import publish_resource
    from app.services.task_queue import RedisQueue
    from app.utils.time import utcnow
    from tests.unit.test_agent_service import _make_resource

    state_path = Path(os.environ["PROBE_STATE"])
    queue = RedisQueue(redis_url=os.environ["PROBE_REDIS_URL"])
    queue.register("run_agent", _handle_run_agent)
    queue_module.task_queue = queue
    if mode == "seed":
        if state_path.exists():
            state = json.loads(state_path.read_text())
        else:
            async with database.async_session_factory() as db:
                agent = (await db.scalars(select(Agent))).one()
                movie = Movie(title_cn="Synthetic queue recovery", release_date=date(2022, 1, 1), is_anime=False)
                db.add(movie)
                await db.flush()
                resource = _make_resource(
                    agent.channel_id, movie_id=movie.id, season=None, episode=None, parsed_at=None
                )
                resource.created_at = utcnow() - timedelta(days=3)
                db.add(resource)
                await db.flush()
                await publish_resource(db, resource.id, kind="created")
                await db.commit()
                state = {"agent_id": agent.id, "resource_id": resource.id}
                state_path.write_text(json.dumps(state))
        await queue.start(consume=False)
        try:
            await dispatch_pending_publications()
            status = await queue.status("agent:" + state["agent_id"])
            assert status and status["status"] == "queued", status
            print(json.dumps({"queued_job_id": status["job_id"]}))
        finally:
            await queue.stop()
    elif mode == "erase":
        import redis.asyncio as aioredis

        client = aioredis.from_url(os.environ["PROBE_REDIS_URL"])
        assert await client.dbsize() > 0
        await client.flushdb()
        assert await client.dbsize() == 0
        await client.aclose()
        print(json.dumps({"isolated_queue_state_erased": True}))
    else:
        state = json.loads(state_path.read_text())
        await queue.start()
        try:
            await dispatch_pending_publications()
            async with asyncio.timeout(20):
                while True:
                    status = await queue.status("agent:" + state["agent_id"])
                    if status and status["status"] in {"done", "failed"}:
                        break
                    await asyncio.sleep(0.05)
            assert status["status"] == "done" and status["result"]["dispatched"] == 1, status
            await dispatch_pending_publications()
            assert (await queue.status("agent:" + state["agent_id"]))["job_id"] == status["job_id"]
            async with database.async_session_factory() as db:
                tasks = list(
                    await db.scalars(select(DownloadTask).where(DownloadTask.file_resource_id == state["resource_id"]))
                )
                assert len(tasks) == 1 and tasks[0].status == "downloading"
            print(json.dumps({"job_id": status["job_id"], "task_count": len(tasks), "result": status["result"]}))
        finally:
            await queue.stop()
    await database.engine.dispose()


def parent():
    guard()

    def run(mode):
        completed = subprocess.run([sys.executable, __file__, mode], capture_output=True, text=True, timeout=35)
        assert completed.returncode == 0, (mode, completed.returncode, completed.stderr)
        return json.loads(completed.stdout)

    seeded = run("seed")
    erased = run("erase")
    processes = [
        subprocess.Popen(
            [sys.executable, __file__, "consume"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for _ in range(2)
    ]
    results = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=35)
            assert process.returncode == 0, (process.returncode, stderr)
            results.append(json.loads(stdout))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    assert results[0]["job_id"] == results[1]["job_id"] != seeded["queued_job_id"]
    print(
        json.dumps(
            {
                "seeded": seeded,
                "erased": erased,
                "fresh_consumers": results,
                "scope": (
                    "actual Redis queue data loss and two new consumer processes; synthetic resource/mock downloader"
                ),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    if len(sys.argv) > 1:
        asyncio.run(child(sys.argv[1]))
    else:
        parent()
