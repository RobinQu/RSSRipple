"""Actual startup on pre-lease schemas, including required-column DDL failure."""

import hashlib
import json
import uuid
from pathlib import Path

import pytest
from sqlalchemy import MetaData, event, inspect, select

from app import database
from app.config import settings
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_lease import AgentRunLease
from app.models.file_resource import FileResource
from app.services.agent_run_lifecycle import create_lease


@pytest.fixture(params=["work_fk_turso", "work_fk_postgres"], ids=["turso", "postgresql"])
def legacy_backend(request, monkeypatch):
    legacy = MetaData()
    for table in database.Base.metadata.sorted_tables:
        table.to_metadata(legacy)
    legacy.remove(legacy.tables["agent_run_leases"])
    agent = legacy.tables["agents"]
    agent._columns.remove(agent.c.current_run_token)
    monkeypatch.setattr("tests.integration.resource_work_fk.conftest._schema", lambda request: legacy)
    engine, factory = request.getfixturevalue(request.param)
    return engine, factory, legacy


@pytest.fixture
async def old_history(legacy_backend, monkeypatch):
    engine, factory, legacy = legacy_backend
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(settings, "database_url", engine.url.render_as_string(hide_password=False))
    raw = Path("tests/fixtures/prod_works_v1.json").read_bytes()
    assert hashlib.sha256(raw).hexdigest() == "d11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32"
    recorded = json.loads(raw)["tables"]["file_resources"][0]
    ids = {key: str(uuid.uuid4()) for key in ["channel", "downloader", "agent", "run", "resource"]}
    async with engine.begin() as conn:
        await conn.execute(legacy.tables["channels"].insert(), dict(
            id=ids["channel"], name="Synthetic legacy", type="rss_feed", url="https://example.invalid", field_mapping={},
        ))
        await conn.execute(legacy.tables["downloader_instances"].insert(), dict(
            id=ids["downloader"], name="Synthetic", type="mock", url="mock://local", download_dir="/tmp/no-media",
        ))
        await conn.execute(legacy.tables["agents"].insert(), dict(
            id=ids["agent"], name="Synthetic legacy", channel_id=ids["channel"], downloader_id=ids["downloader"],
        ))
        await conn.execute(legacy.tables["file_resources"].insert(), dict(
            id=ids["resource"], channel_id=ids["channel"], title_raw=recorded["title_raw"],
            guid=recorded["guid"], torrent_url=recorded["torrent_url"],
        ))
        await conn.execute(legacy.tables["agent_runs"].insert(), dict(
            id=ids["run"], agent_id=ids["agent"], status="running", total_resources=7, dispatched=2,
            matched_resource_ids=[ids["resource"]], errors=["Synthetic preserved error"],
        ))
    return engine, factory, ids, recorded


async def test_startup_upgrades_and_preserves_unowned_history(old_history):
    engine, factory, ids, recorded = old_history
    await database.create_tables()
    async with factory() as db:
        assert (await db.get(Agent, ids["agent"])).current_run_token is None
        assert list(await db.scalars(select(AgentRunLease))) == []
        run = AgentRun(agent_id=ids["agent"], status="running")
        db.add(run)
        await db.flush()
        owner = await create_lease(db, run.id)
        await db.commit()
    await database.create_tables()
    async with factory() as db:
        old = await db.get(AgentRun, ids["run"])
        assert old.status == "running" and old.finished_at is None
        assert old.total_resources == 7 and old.dispatched == 2
        assert old.matched_resource_ids == [ids["resource"]]
        assert old.errors == ["Synthetic preserved error"]
        resource = await db.get(FileResource, ids["resource"])
        assert resource.title_raw == recorded["title_raw"] and resource.guid == recorded["guid"]
        assert resource.torrent_url == recorded["torrent_url"]
        assert list(await db.scalars(select(AgentRunLease.token))) == [owner.token]
    async with engine.connect() as conn:
        indexes = await conn.run_sync(lambda sync: inspect(sync).get_indexes("agent_run_leases"))
        assert any(index["column_names"] == ["expires_at_epoch", "id"] for index in indexes)


async def test_required_ownership_column_failure_aborts_startup(old_history):
    engine, factory, ids, _ = old_history

    class OwnershipDDLError(RuntimeError):
        pass

    attempts = []

    def fail_ownership_column(conn, cursor, statement, params, context, many):
        if statement.startswith("ALTER TABLE agents ADD COLUMN current_run_token"):
            attempts.append(statement)
            raise OwnershipDDLError("Synthetic required-column DDL failure")

    event.listen(engine.sync_engine, "before_cursor_execute", fail_ownership_column)
    try:
        with pytest.raises(OwnershipDDLError):
            await database.create_tables()
        assert len(attempts) == 1
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", fail_ownership_column)
    # Removing the failure permits retry; legacy history is never auto-retired.
    await database.create_tables()
    async with factory() as db:
        assert (await db.get(Agent, ids["agent"])).current_run_token is None
        assert (await db.get(AgentRun, ids["run"])).status == "running"
        assert list(await db.scalars(select(AgentRunLease))) == []
