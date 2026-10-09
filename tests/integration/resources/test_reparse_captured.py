"""Recorded title/torrent through the real reparse pipeline; identity provider is offline."""

import hashlib
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI
from sqlalchemy import select

from app.api.v1.dashboard import _page_pending_confirmations
from app.api.v1.resources import router
from app.job_handlers import _handle_reprocess_resource_metadata
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_publication import ResourcePublication
from app.models.resource_reparse_request import ResourceReparseRequest
from tests.metadata_corpus.dataset import ROOT, asset, load_corpus

TORRENT_SHA = "6936d731675641e065b7af4c3780b3c36c27b2782e65fc3989a6557b0cff75c3"


async def _captured_reparse(db_pair, monkeypatch, tmp_path, record_property):
    _, factory = db_pair
    _, corpus, _ = load_corpus(ROOT)
    case = next(c for c in corpus["cases"] if TORRENT_SHA in (c["evidence"]["torrent"] or ""))
    source = asset(ROOT, case["evidence"]["torrent"])
    torrent = tmp_path / "recorded.torrent"
    torrent.write_bytes(source.read_bytes())
    assert hashlib.sha256(torrent.read_bytes()).hexdigest() == TORRENT_SHA
    rid = str(uuid.uuid4())
    async with factory() as db:
        channel = Channel(name="Recorded reparse input", type="rss_feed", url=f"https://example.invalid/feed/{str(uuid.uuid4())}",
                          field_mapping={}, metadata_agent_enabled=True)
        db.add(channel)
        await db.flush()
        channel_id = channel.id
        db.add(FileResource(id=rid, channel_id=channel_id, guid=rid,
                            title_raw=case["input"]["title_raw"], torrent_url="https://example.invalid/recorded.torrent",
                            torrent_file=str(torrent)))
        await db.flush()
        from app.services.resource_publication import publish_resource

        await publish_resource(db, rid, kind="created")
        await db.commit()
    enqueue = AsyncMock(return_value={"job_id": "synthetic delivery"})
    identity_provider = AsyncMock()  # Deliberately unresolved; no invented work identity.
    monkeypatch.setattr("app.services.task_queue.task_queue.enqueue", enqueue)
    monkeypatch.setattr("app.services.metadata_agent.get_agent", lambda: SimpleNamespace(process=identity_provider))
    monkeypatch.setattr("app.job_handlers._refresh_runtime_config", AsyncMock())
    from app.services import runtime_config

    monkeypatch.setitem(runtime_config._overrides, "llm_api_key", "")
    # A cache replay must never fetch or overwrite the recorded torrent.
    fetch = AsyncMock(side_effect=AssertionError("Recorded cache must be reused"))
    monkeypatch.setattr("app.services.torrent_inspect.fetch_torrent_file", fetch)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://synthetic") as client:
        response = await client.post(f"/api/v1/resources/{rid}/reparse-metadata")
    assert response.status_code == 200
    payload = enqueue.call_args.args[2]
    # Do not replace _process_resource_metadata, torrent inspection, binding,
    # publication or commit: the production pipeline executes all of them.
    assert await _handle_reprocess_resource_metadata(payload) == {"status": "done"}
    identity_provider.assert_awaited_once()
    assert identity_provider.call_args.kwargs["force_refresh"] is True
    fetch.assert_not_awaited()
    async with factory() as db:
        saved = await db.get(FileResource, rid)
        assert saved.title_raw == case["input"]["title_raw"]
        assert saved.is_batch and saved.batch_scope == "season"
        assert (saved.episode_start, saved.episode_end) == (1, 12)
        assignments = list(await db.scalars(select(ResourceFileAssignment).where(
            ResourceFileAssignment.resource_id == rid,
        )))
        assert len(assignments) == 12
        assert sorted(a.episode_start for a in assignments) == list(range(1, 13))
        assert {a.file_path for a in assignments} == {
            a["file_path"] for a in case["candidate_expected"]["assignments"]
        }
        assert all(a.file_size > 0 and a.series_id is None and a.movie_id is None for a in assignments)
        assert await db.get(ResourceReparseRequest, payload["request_id"]) is None
        assert await db.scalar(select(ResourcePublication.id).where(ResourcePublication.resource_id == rid))
        pending, _ = await _page_pending_confirmations(db, 1, 100)
        assert any(item["resource"]["id"] == rid for item in pending)
    assert hashlib.sha256(Path(torrent).read_bytes()).hexdigest() == TORRENT_SHA
    record_property("recorded_case", case["id"])
    record_property("torrent_sha256", TORRENT_SHA)
    record_property("recorded_files_persisted", 12)
    record_property("real_processing_pipeline", True)
    record_property("synthetic_boundary", "queue delivery and unavailable identity provider; no media download")


async def test_postgres_captured_reparse(reparse_database, monkeypatch, tmp_path, record_property):
    await _captured_reparse(reparse_database, monkeypatch, tmp_path, record_property)


async def test_turso_captured_reparse(reparse_turso_database, monkeypatch, tmp_path, record_property):
    await _captured_reparse(reparse_turso_database, monkeypatch, tmp_path, record_property)
