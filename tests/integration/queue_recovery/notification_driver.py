"""Real loopback HTTP interleaving against a dedicated PostgreSQL fixture."""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

mode = os.environ["QUEUE_RECOVERY_MODE"]
assert mode in {"kill", "pause"}
database_url = os.environ["QUEUE_RECOVERY_DATABASE_URL"]
assert urlsplit(database_url).path.startswith("/queue_recovery_")
os.environ["DATABASE_URL"] = database_url
redis_url = os.environ["QUEUE_RECOVERY_REDIS_URL"]

import redis.asyncio as redis  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import database  # noqa: E402
from app.models.agent import Agent  # noqa: E402
from app.models.agent_webhook import AgentWebhook  # noqa: E402
from app.models.channel import Channel  # noqa: E402
from app.models.download_notification import DownloadNotification  # noqa: E402
from app.models.download_task import DownloadTask  # noqa: E402
from app.models.downloader import DownloaderInstance  # noqa: E402
from app.models.file_resource import FileResource  # noqa: E402
from app.models.webhook_delivery import WebhookDelivery  # noqa: E402
from app.services.notify_service import deliver_due_deliveries  # noqa: E402
from app.services.task_queue import ExecutionOwnershipLostError, RedisQueue  # noqa: E402


def uid():
    return str(uuid.uuid4())


async def child():
    queue = RedisQueue(redis_url=redis_url)
    finished = asyncio.Event()
    lost = []

    async def handler(payload):
        try:
            async with database.async_session_factory() as db:
                stats = await deliver_due_deliveries(db)
            assert stats == {"delivered": 1, "failed": 0, "skipped": 0}, stats
            return stats
        except ExecutionOwnershipLostError:
            lost.append(True)
            raise
        finally:
            finished.set()

    queue.register("notification-crash", handler)
    try:
        await queue.start()
        await asyncio.wait_for(finished.wait(), 50)
        async with asyncio.timeout(5):
            while (await queue.status("notification-crash"))["status"] == "running":
                await asyncio.sleep(0.01)
        assert (await queue.status("notification-crash"))["status"] == "done"
        assert bool(lost) == (sys.argv[1] == "A")
    finally:
        await queue.stop()
        await database.engine.dispose()


async def main():
    first_received, release_first = asyncio.Event(), asyncio.Event()
    requests = []
    handlers = set()

    async def handle(reader, writer):
        handlers.add(asyncio.current_task())
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            length = next(
                int(line.split(b":", 1)[1])
                for line in header.split(b"\r\n")
                if line.lower().startswith(b"content-length:")
            )
            requests.append(json.loads(await reader.readexactly(length)))
            index = len(requests)
            if index == 1:
                first_received.set()
                await (reader.read() if mode == "kill" else release_first.wait())
                if mode == "kill":
                    return
            status = "500 Internal Server Error" if index == 1 else "200 OK"
            writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{{}}".encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(asyncio.current_task())

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    children = []
    observer = redis.from_url(redis_url, decode_responses=True)
    queue = RedisQueue(redis_url=redis_url)
    owns_redis = False
    try:
        assert await observer.dbsize() == 0
        owns_redis = True
        async with database.engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.create_all)
        async with database.async_session_factory() as db:
            assert not (await db.scalars(select(WebhookDelivery))).all()
            channel = Channel(
                id=uid(),
                name="notification-race",
                type="rss_feed",
                url="https://example.invalid/rss",
                field_mapping={
                    "list_locator": {"source": "entries"},
                    "field_mappings": {"torrent_url": {"source": "link"}},
                },
            )
            downloader = DownloaderInstance(
                id=uid(), name="fixture", type="mock", url="mock://local", download_dir="/downloads"
            )
            db.add_all([channel, downloader])
            await db.flush()
            agent = Agent(id=uid(), name="fixture", channel_id=channel.id, downloader_id=downloader.id)
            resource = FileResource(
                id=uid(),
                channel_id=channel.id,
                guid=uid(),
                title_raw="synthetic notification race",
                torrent_url="https://example.invalid/test.torrent",
            )
            db.add_all([agent, resource])
            await db.flush()
            task = DownloadTask(
                id=uid(),
                agent_id=agent.id,
                file_resource_id=resource.id,
                downloader_id=downloader.id,
                download_dir="/downloads",
                status="completed",
            )
            hook = AgentWebhook(id=uid(), agent_id=agent.id, url=f"http://127.0.0.1:{port}/webhook")
            db.add_all([task, hook])
            await db.flush()
            notification = DownloadNotification(
                id=uid(),
                agent_id=agent.id,
                download_task_id=task.id,
                payload={"version": 2, "notification_id": uid(), "fixture": "synthetic state-race only"},
            )
            db.add(notification)
            await db.flush()
            delivery = WebhookDelivery(id=uid(), notification_id=notification.id, webhook_id=hook.id)
            db.add(delivery)
            await db.commit()
            delivery_id = delivery.id
        await queue.start(consume=False)
        job = await queue.enqueue("notification-crash", "notification-crash", {})
        first = subprocess.Popen([sys.executable, "-m", "tests.integration.queue_recovery.notification_driver", "A"])
        children.append(first)
        await asyncio.wait_for(first_received.wait(), 15)
        assert first.poll() is None
        async with database.async_session_factory() as db:
            row = await db.get(WebhookDelivery, delivery_id)
            first_token = row.attempt_token
            assert first_token and row.status == "pending"
        if mode == "kill":
            first.kill()
            assert await asyncio.to_thread(first.wait, 5) == -9
        else:
            os.kill(first.pid, signal.SIGSTOP)
            assert first.poll() is None
        started = time.monotonic()
        second = subprocess.Popen([sys.executable, "-m", "tests.integration.queue_recovery.notification_driver", "B"])
        children.append(second)
        async with asyncio.timeout(50):
            while second.poll() is None:
                await asyncio.sleep(0.1)
        elapsed = time.monotonic() - started
        assert second.returncode == 0
        if mode == "pause":
            assert first.poll() is None
            os.kill(first.pid, signal.SIGCONT)
            release_first.set()
            assert await asyncio.to_thread(first.wait, 10) == 0
        state = await queue.status("notification-crash")
        assert state["status"] == "done" and state["job_id"] == job["job_id"]
        async with database.async_session_factory() as db:
            row = await db.get(WebhookDelivery, delivery_id)
            assert row.status == "done" and row.attempt_count == 0 and row.error_message is None
            assert row.attempt_token and row.attempt_token != first_token
        assert len(requests) == 2 and requests[0] == requests[1]
        Path(os.environ["QUEUE_RECOVERY_RESULT"]).write_text(
            json.dumps(
                {
                    "backend": "postgresql",
                    "http_requests": len(requests),
                    "first_worker_exit": first.returncode,
                    "second_worker_exit": second.returncode,
                    "same_job_id": state["job_id"] == job["job_id"],
                    "recovery_seconds": elapsed,
                    "default_lease_seconds": 15,
                    "default_heartbeat_seconds": 5,
                    "final_status": "done",
                    "attempt_count": 0,
                    "distinct_committed_tokens": True,
                    "data": "synthetic notification payload",
                    "mode": mode,
                    "scope": "independent workers, natural lease recovery, real Redis/PG/HTTP",
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        release_first.set()
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
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
        await database.engine.dispose()


asyncio.run(child() if len(sys.argv) > 1 else main())
