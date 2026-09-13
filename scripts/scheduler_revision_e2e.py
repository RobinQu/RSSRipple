"""P0-2 regression in the disposable PostgreSQL/Redis/three-worker stack.

Captured title is provenance, not a semantic oracle. Work links are deliberately
absent: the production worker must consume the revision, persist its run and
reject dispatch via the actual metadata gate. The driver never enqueues work.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import time
import uuid
from datetime import timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401 — register ORM relationships
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.utils.time import utcnow
from scripts.scheduler_e2e import request


async def check() -> None:
    url = os.environ["DATABASE_URL"]
    parsed = make_url(url)
    if parsed.database != "scheduler_test" or parsed.host != "postgres":
        raise RuntimeError("refusing any database outside the dedicated scheduler test stack")
    root = Path("/app/tests/fixtures/metadata_corpus_v2")
    manifest = json.loads((root / "manifest.json").read_text())
    raw = (root / "candidates.json.gz").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == manifest["candidate_sha256"]
    cases = json.loads(gzip.decompress(raw))["cases"]
    case = next(case for case in cases if case["id"] == "f79ef2eb-02d5-42d3-80dc-70dd3c1d733b")
    channel = request("/channels", {
        "name": "revision-e2e", "type": "rss_feed", "url": "http://feed:8080/feed",
        "status": "inactive", "metadata_agent_enabled": False,
        "field_mapping": {"list_locator": {"source": "entries"},
                          "field_mappings": {"torrent_url": {"source": "link"}}},
    }, "POST")["data"]
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    agent_id, resource_id, downloader_id = [str(uuid.uuid4()) for _ in range(3)]
    watermark = utcnow()
    try:
        async with sessions.begin() as session:
            session.add(DownloaderInstance(id=downloader_id, name="revision-mock", type="mock",
                                           url="http://unused.invalid", download_dir="/tmp/downloads"))
            session.add(Agent(id=agent_id, name="revision-agent", channel_id=channel["id"],
                              downloader_id=downloader_id, scope_channel_wide=True, status="active",
                              last_consumed_at=watermark))
            session.add(FileResource(id=resource_id, channel_id=channel["id"], guid=resource_id,
                                     title_raw=case["input"]["title_raw"],
                                     torrent_url="magnet:?xt=urn:btih:1111111111111111111111111111111111111111",
                                     created_at=watermark - timedelta(days=2)))
        for expected_runs, resolution in enumerate(["1080p", "2160p", "1080p"], 1):
            response = request(f"/resources/{resource_id}", {"resolution": resolution}, "PATCH")
            assert response["data"]["resolution"] == resolution
            deadline = time.monotonic() + 30
            while True:
                async with sessions() as session:
                    statement = select(AgentRun).where(AgentRun.agent_id == agent_id)
                    runs = (await session.execute(statement)).scalars().all()
                    finished = [run for run in runs if run.finished_at is not None]
                    if len(finished) == expected_runs:
                        assert len(runs) == expected_runs
                        assert all(run.status == "success" and run.total_resources == 1
                                   and run.unrecognized == 1 and not run.errors for run in runs)
                        agent = await session.get(Agent, agent_id)
                        resource = await session.get(FileResource, resource_id)
                        assert agent.last_consumed_at == watermark
                        assert resource.resolution == resolution
                        assert resource.series_id is None and resource.movie_id is None
                        assert not await session.scalar(select(func.count()).select_from(DownloadTask).where(
                            DownloadTask.file_resource_id == resource_id))
                        break
                if time.monotonic() >= deadline:
                    raise AssertionError(f"revision {expected_runs} was not consumed and committed by a worker")
                await asyncio.sleep(0.25)
            print(f"PASS: Redis worker persisted revision {expected_runs}; "
                  "old resource consumed, watermark unchanged", flush=True)
        print(f"PROVENANCE: case={case['id']} captured_at={manifest['captured_at']} "
              "work_links=deliberately_absent", flush=True)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(check())
