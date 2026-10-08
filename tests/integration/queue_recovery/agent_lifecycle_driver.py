"""Real Redis consumers and Agent handler, with a controlled processing boundary.

Recorded resource text/URL, synthetic identities and process interleavings.
No downloader/network side effect is requested by the unrecognized resource.
"""

import asyncio
import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

database_url = os.environ["QUEUE_RECOVERY_DATABASE_URL"]
assert urlsplit(database_url).path.startswith("/queue_recovery_")
os.environ["DATABASE_URL"] = database_url
redis_url = os.environ["QUEUE_RECOVERY_REDIS_URL"]
mode = os.environ["QUEUE_RECOVERY_MODE"]
assert mode in {"agent_lifecycle_kill", "agent_lifecycle_pause"}

import redis.asyncio as redis  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import database  # noqa: E402
from app.job_handlers import _handle_run_agent  # noqa: E402
from app.models.agent import Agent  # noqa: E402
from app.models.agent_run import AgentRun  # noqa: E402
from app.models.agent_run_lease import AgentRunLease  # noqa: E402
from app.models.channel import Channel  # noqa: E402
from app.models.downloader import DownloaderInstance  # noqa: E402
from app.models.file_resource import FileResource  # noqa: E402
from app.services.agent_run_lifecycle import reap_expired_runs  # noqa: E402
from app.services.task_queue import _JOB_PFX, ExecutionOwnershipLostError, RedisQueue  # noqa: E402


async def child():
    from app.services import agent_service

    queue = RedisQueue(redis_url=redis_url)
    finished = asyncio.Event()
    lost = []
    if sys.argv[1] == "A":
        original = agent_service.process_resources

        async def held_processing(*args, **kwargs):
            reader, writer = await asyncio.open_connection("127.0.0.1", int(os.environ["BOUNDARY_PORT"]))
            try:
                writer.write(b"ready\n")
                await writer.drain()
                assert await reader.readline() == b"continue\n"
                return await original(*args, **kwargs)
            finally:
                writer.close()
                await writer.wait_closed()

        agent_service.process_resources = held_processing

    async def handler(payload):
        try:
            ownership = await queue._redis.hgetall(_JOB_PFX + os.environ["AGENT_JOB_KEY"])
            assert ownership["execution_token"] and ownership["status"] == "running"
            Path(os.environ["QUEUE_RECOVERY_RESULT"] + "." + sys.argv[1] + "-owner.json").write_text(json.dumps(ownership))
            result = await _handle_run_agent(payload)
            assert result["total_resources"] == result["unrecognized"] == 1, result
            assert result["dispatched"] == 0, result
            return result
        except ExecutionOwnershipLostError:
            lost.append(True)
            raise
        finally:
            finished.set()

    queue.register("run_agent", handler)
    try:
        await queue.start()
        await asyncio.wait_for(finished.wait(), 70)
        async with asyncio.timeout(5):
            while (await queue.status(os.environ["AGENT_JOB_KEY"]))["status"] == "running":
                await asyncio.sleep(0.01)
        assert (await queue.status(os.environ["AGENT_JOB_KEY"]))["status"] == "done"
        assert bool(lost) == (sys.argv[1] == "A")
    finally:
        await queue.stop()
        await database.engine.dispose()


async def snapshot(agent_id):
    async with database.async_session_factory() as db:
        agent = await db.get(Agent, agent_id)
        rows = list(await db.scalars(select(AgentRun).where(AgentRun.agent_id == agent_id)
                                    .order_by(AgentRun.started_at, AgentRun.id)))
        leases = list(await db.scalars(select(AgentRunLease)))
        return {
            "summary": {"status": agent.last_run_status, "at": str(agent.last_run_at),
                        "token": agent.current_run_token, "watermark": str(agent.last_consumed_at)},
            "runs": [{"id": row.id, "status": row.status, "finished": row.finished_at is not None,
                      "total": row.total_resources, "unrecognized": row.unrecognized,
                      "dispatched": row.dispatched, "errors": row.errors} for row in rows],
            "leases": [{"run_id": row.run_id, "token": row.token} for row in leases],
        }


async def main():
    ready, release = asyncio.Event(), asyncio.Event()
    connections = set()

    async def boundary(reader, writer):
        connections.add(asyncio.current_task())
        try:
            assert await reader.readline() == b"ready\n"
            ready.set()
            await release.wait()
            writer.write(b"continue\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            connections.discard(asyncio.current_task())

    server = await asyncio.start_server(boundary, "127.0.0.1", 0)
    children = []
    observer = redis.from_url(redis_url, decode_responses=True)
    queue = RedisQueue(redis_url=redis_url)
    owns_redis = False
    try:
        assert await observer.dbsize() == 0
        owns_redis = True
        raw = Path("tests/fixtures/prod_works_v1.json").read_bytes()
        assert hashlib.sha256(raw).hexdigest() == "d11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32"
        recorded = json.loads(raw)["tables"]["file_resources"][0]
        async with database.engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.create_all)
        async with database.async_session_factory() as db:
            channel = Channel(name="Synthetic lifecycle", type="rss_feed", url="https://example.invalid", field_mapping={})
            downloader = DownloaderInstance(name="Synthetic", type="mock", url="mock://local", download_dir="/tmp/no-media")
            db.add_all([channel, downloader])
            await db.flush()
            agent = Agent(name="Synthetic", channel_id=channel.id, downloader_id=downloader.id,
                          scope_channel_wide=True, llm_enabled=False)
            resource = FileResource(channel_id=channel.id, guid=recorded["guid"],
                                    title_raw=recorded["title_raw"], torrent_url=recorded["torrent_url"])
            db.add_all([agent, resource])
            await db.commit()
            agent_id, resource_id = agent.id, resource.id
        key = "agent:" + agent_id
        await queue.start(consume=False)
        job = await queue.enqueue("run_agent", key, {"agent_id": agent_id, "resource_ids": [resource_id]})
        env = dict(os.environ, BOUNDARY_PORT=str(server.sockets[0].getsockname()[1]), AGENT_JOB_KEY=key)
        module = "tests.integration.queue_recovery.agent_lifecycle_driver"
        first = subprocess.Popen([sys.executable, "-m", module, "A"], env=env)
        children.append(first)
        await asyncio.wait_for(ready.wait(), 15)
        before = await snapshot(agent_id)
        assert len(before["runs"]) == len(before["leases"]) == 1
        assert before["runs"][0]["status"] == "running"
        old_id = before["runs"][0]["id"]
        first_queue_token = (await observer.hgetall(_JOB_PFX + key))["execution_token"]
        if mode.endswith("kill"):
            first.kill()
            assert await asyncio.to_thread(first.wait, 5) == -9
        else:
            os.kill(first.pid, signal.SIGSTOP)
            assert first.poll() is None
        second = subprocess.Popen([sys.executable, "-m", module, "B"], env=env)
        children.append(second)
        async with asyncio.timeout(55):
            while second.poll() is None:
                await asyncio.sleep(0.1)
        assert second.returncode == 0
        state = await queue.status(key)
        assert state["status"] == "done" and state["job_id"] == job["job_id"]
        replacement = json.loads(Path(os.environ["QUEUE_RECOVERY_RESULT"] + ".B-owner.json").read_text())
        assert replacement["execution_token"] and replacement["execution_token"] != first_queue_token
        assert replacement["job_id"] == job["job_id"]
        # A separate observer calls the real bounded reaper after natural DB
        # expiry. No timestamp/lease mutation or age-based history update.
        async with asyncio.timeout(40):
            while old_id not in await reap_expired_runs():
                await asyncio.sleep(0.2)
        after_reap = await snapshot(agent_id)
        assert sorted(row["status"] for row in after_reap["runs"]) == ["failed", "success"]
        assert all(row["finished"] for row in after_reap["runs"])
        assert after_reap["leases"] == [] and after_reap["summary"]["status"] == "success"
        assert before["summary"]["token"] != after_reap["summary"]["token"]
        if mode.endswith("pause"):
            os.kill(first.pid, signal.SIGCONT)
            release.set()
            assert await asyncio.to_thread(first.wait, 10) == 0
        final = await snapshot(agent_id)
        assert final == after_reap, "Late worker changed completed history or Agent summary"
        success = next(row for row in final["runs"] if row["status"] == "success")
        assert success["total"] == success["unrecognized"] == 1 and success["dispatched"] == 0
        Path(os.environ["QUEUE_RECOVERY_RESULT"]).write_text(json.dumps({
            "backend": "postgresql", "mode": mode, "before": before, "after": final,
            "first_worker_exit": first.returncode, "second_worker_exit": second.returncode,
            "same_job_id": True, "distinct_queue_tokens": True, "late_snapshot_unchanged": True,
            "default_agent_lease_seconds": 30, "default_queue_lease_seconds": 15,
            "scope": "Independent production RedisQueue consumers and real Agent handler; controlled processing boundary; observer invokes real reaper, not scheduler boot.",
        }, indent=2) + "\n")
    finally:
        release.set()
        for process in children:
            if process.poll() is None:
                process.kill()
                await asyncio.to_thread(process.wait, 5)
        await queue.stop()
        if owns_redis:
            await observer.flushdb()
        await observer.aclose()
        server.close()
        await server.wait_closed()
        if connections:
            await asyncio.gather(*connections, return_exceptions=True)
        await database.engine.dispose()


asyncio.run(child() if len(sys.argv) > 1 else main())
