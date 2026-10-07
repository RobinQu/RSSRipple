"""Isolated crash child: actual DB/Redis lifecycle, deliberately blocked metadata."""

import asyncio
import json
import os
from pathlib import Path

import app.models  # noqa: F401
from app import job_handlers
from app.database import async_session_factory
from app.services import fetch_service, task_queue
from app.services.resource_reparse_requests import create_request, wake_request


async def main():
    assert "/reparse_" in os.environ["DATABASE_URL"], "Dedicated scratch DB required"
    rid = os.environ["REPARSE_RESOURCE_ID"]
    channel_id = os.environ["REPARSE_CHANNEL_ID"]
    mode = os.environ["REPARSE_CRASH_MODE"]
    marker = Path(os.environ["REPARSE_CRASH_MARKER"])
    async with async_session_factory() as db:
        request = await create_request(db, rid, channel_id)
        await db.commit()
    if mode == "before_enqueue":
        marker.write_text(json.dumps({"request_id": request.id, "phase": "committed"}))
        os._exit(97)  # No finally/queue call after the actual commit.
    task_queue.CONSUMER_LEASE_SECONDS = 2
    task_queue.CONSUMER_HEARTBEAT_SECONDS = 0.25
    worker = task_queue.RedisQueue(redis_url=os.environ["REPARSE_REDIS_URL"])
    task_queue.task_queue = worker

    async def refresh():
        pass

    async def metadata(*args, **kwargs):
        await task_queue.require_execution_ownership()
        marker.write_text(json.dumps({
            "request_id": request.id, "phase": "running",
            "job_identity": task_queue.current_job_identity(), "consumer_key": worker._consumer_key,
        }))
        await asyncio.Event().wait()

    job_handlers._refresh_runtime_config = refresh
    fetch_service._process_resource_metadata = metadata
    worker.register("reprocess_resource_metadata", job_handlers._handle_reprocess_resource_metadata)
    await worker.start()
    await wake_request(request)
    await asyncio.Event().wait()  # Parent SIGKILL prevents handler finally.


if __name__ == "__main__":
    asyncio.run(main())
