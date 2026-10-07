"""Actual child-process death and Redis descriptor recovery, no fake queue."""

import asyncio
import json
import os
import sys
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock
from urllib.parse import urlsplit, urlunsplit

import pytest
from sqlalchemy import update

from app.job_handlers import _handle_reprocess_resource_metadata
from app.models.file_resource import FileResource
from app.models.resource_reparse_request import ResourceReparseRequest
from app.services import task_queue
from app.services.resource_reparse_requests import dispatch_pending_reparses
from app.utils.time import utcnow


@pytest.mark.parametrize("mode", ["before_enqueue", "during_handler"])
async def test_process_crash_preserves_and_recovers_reparse_request(
    reparse_database, sample_channel, monkeypatch, tmp_path, mode, record_property,
):
    engine, factory = reparse_database
    url = os.environ.get("REPARSE_TEST_REDIS_URL") or os.environ.get("QUEUE_RECOVERY_REDIS_URL")
    if not url:
        if os.environ.get("QUEUE_RECOVERY_REQUIRED") == "1":
            pytest.fail("Crash gate requires dedicated Redis")
        pytest.skip("Use isolated integration Redis")
    url = urlunsplit(urlsplit(url)._replace(path="/14"))
    rid, ignored_at = str(uuid.uuid4()), utcnow()
    async with factory() as db:
        db.add(FileResource(id=rid, channel_id=sample_channel.id, guid=rid, title_raw="Synthetic crash",
                            torrent_url="magnet:?xt=urn:btih:synthetic", confirmation_ignored_at=ignored_at))
        await db.commit()
    marker = tmp_path / "child.json"
    env = dict(os.environ, DATABASE_URL=engine.url.render_as_string(hide_password=False),
               REPARSE_RESOURCE_ID=rid, REPARSE_CHANNEL_ID=sample_channel.id, REPARSE_CRASH_MODE=mode,
               REPARSE_CRASH_MARKER=str(marker), REPARSE_REDIS_URL=url)
    log = (tmp_path / "worker.log").open("wb")
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "tests.integration.resources.reparse_worker_driver",
        cwd=Path(__file__).resolve().parents[3], env=env, stdout=log, stderr=log,
    )
    worker = task_queue.RedisQueue(redis_url=url)
    producer = task_queue.RedisQueue(redis_url=url)
    await producer.start(consume=False)
    monkeypatch.setattr(task_queue, "task_queue", producer)
    monkeypatch.setattr("app.job_handlers._refresh_runtime_config", AsyncMock())
    replay = AsyncMock()
    monkeypatch.setattr("app.services.fetch_service._process_resource_metadata", replay)
    worker.register("reprocess_resource_metadata", _handle_reprocess_resource_metadata)
    observed = None
    try:
        async with asyncio.timeout(15):
            while not marker.exists():
                assert child.returncode is None, (tmp_path / "worker.log").read_text()
                await asyncio.sleep(0.02)
        observed = json.loads(marker.read_text())
        if mode == "during_handler":
            child.kill()
        code = await asyncio.wait_for(child.wait(), 5)
        assert code == (-9 if mode == "during_handler" else 97)
        async with factory() as db:
            assert await db.get(ResourceReparseRequest, observed["request_id"])
            assert (await db.get(FileResource, rid)).confirmation_ignored_at == ignored_at
        if mode == "before_enqueue":
            assert await producer.status(f"reprocess-resource:{rid}") is None
            async with factory() as db:
                await db.execute(update(ResourceReparseRequest).where(
                    ResourceReparseRequest.id == observed["request_id"],
                ).values(next_attempt_at=utcnow() - timedelta(seconds=1)))
                await db.commit()
            await dispatch_pending_reparses()
        else:
            async with asyncio.timeout(10):
                while await producer._redis.exists(observed["consumer_key"]):
                    await asyncio.sleep(0.05)
        await worker.start()
        async with asyncio.timeout(15):
            while True:
                state = await producer.status(f"reprocess-resource:{rid}")
                if state and state["status"] in {"done", "failed"}:
                    break
                await asyncio.sleep(0.02)
        assert state["status"] == "done", state
        replay.assert_awaited_once()
        if mode == "during_handler":
            assert state["job_id"] == observed["job_identity"][1]
        async with factory() as db:
            assert await db.get(ResourceReparseRequest, observed["request_id"]) is None
            assert (await db.get(FileResource, rid)).confirmation_ignored_at == ignored_at
        record_property("child_exit", code)
        record_property("crash_phase", mode)
        record_property("recovered_actual_redis_job", True)
        record_property("metadata_provider", "synthetic; first process killed while blocked")
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
        log.close()
        await worker.stop()
        keys = [f"rssripple:job:reprocess-resource:{rid}", f"rssripple:active:reprocess-resource:{rid}",
                worker._processing_key]
        if observed and observed.get("consumer_key"):
            keys.append(observed["consumer_key"].replace("rssripple:consumer:", "rssripple:processing:"))
        await producer._redis.delete(*keys)
        await producer.stop()
