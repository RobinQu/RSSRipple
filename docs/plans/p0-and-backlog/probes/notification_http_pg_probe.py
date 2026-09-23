"""Real loopback HTTP interleaving against a dedicated PostgreSQL fixture."""

import asyncio
import json
import os
import subprocess
import uuid
from pathlib import Path

project = os.environ["PROBE_POSTGRES_PROJECT"]
assert project.startswith("rssripple-v14-schema-")
[state] = json.loads(subprocess.check_output(["docker", "inspect", f"{project}-postgres-1"]))
assert state["Config"]["Labels"]["com.docker.compose.project"] == project
address = state["NetworkSettings"]["Networks"][project + "_isolated"]["IPAddress"]
os.environ["DATABASE_URL"] = f"postgresql+asyncpg://probe:probe@{address}:5432/probe"

from sqlalchemy import select  # noqa: E402

from app import database  # noqa: E402
from app.config import settings  # noqa: E402
from app.models.agent import Agent  # noqa: E402
from app.models.agent_webhook import AgentWebhook  # noqa: E402
from app.models.channel import Channel  # noqa: E402
from app.models.download_notification import DownloadNotification  # noqa: E402
from app.models.download_task import DownloadTask  # noqa: E402
from app.models.downloader import DownloaderInstance  # noqa: E402
from app.models.file_resource import FileResource  # noqa: E402
from app.models.webhook_delivery import WebhookDelivery  # noqa: E402
from app.services.notify_service import deliver_due_deliveries  # noqa: E402


def uid():
    return str(uuid.uuid4())


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
                await release_first.wait()
            status = "500 Internal Server Error" if index == 1 else "200 OK"
            writer.write(f"HTTP/1.1 {status}\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{{}}".encode())
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            handlers.discard(asyncio.current_task())

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    tasks = []
    try:
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
        settings.notify_max_attempts = 1

        async def deliver():
            async with database.async_session_factory() as db:
                return await deliver_due_deliveries(db)

        old = asyncio.create_task(deliver())
        tasks.append(old)
        await asyncio.wait_for(first_received.wait(), 10)
        async with database.async_session_factory() as db:
            first_token = (await db.get(WebhookDelivery, delivery_id)).attempt_token
            assert first_token
        newer = await asyncio.wait_for(deliver(), 10)
        async with database.async_session_factory() as db:
            row = await db.get(WebhookDelivery, delivery_id)
            assert row.status == "done" and row.attempt_token != first_token
            winning_token = row.attempt_token
        release_first.set()
        older = await asyncio.wait_for(old, 10)
        async with database.async_session_factory() as db:
            row = await db.get(WebhookDelivery, delivery_id)
            assert row.status == "done" and row.attempt_count == 0 and row.error_message is None
            assert row.attempt_token == winning_token
        assert len(requests) == 2 and requests[0] == requests[1]
        assert newer == {"delivered": 1, "failed": 0, "skipped": 0}
        assert older == {"delivered": 0, "failed": 0, "skipped": 1}
        Path(os.environ["PROBE_RESULT_PATH"]).write_text(
            json.dumps(
                {
                    "backend": "postgresql",
                    "http_requests": len(requests),
                    "older": older,
                    "newer": newer,
                    "final_status": "done",
                    "attempt_count": 0,
                    "distinct_committed_tokens": True,
                    "data": "synthetic notification payload",
                    "scope": "two independent DB sessions, real loopback HTTP; no Redis or process takeover",
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        release_first.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.gather(*handlers, return_exceptions=True)
        await database.engine.dispose()


asyncio.run(main())
