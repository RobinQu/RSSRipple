"""Explicit snapshot review, bounded export and atomic retirement on both DBs."""

import asyncio
import copy
import json
import os
import subprocess
import sys
from datetime import timedelta

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app import database
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.agent_run_lease import AgentRunLease
from app.services.agent_run_lifecycle import create_lease
from app.utils.time import utcnow
from scripts.review_legacy_agent_runs import apply_review, export_review


@pytest.fixture
async def review_seed(lifecycle_db):
    engine, factory, owner, agent_id, resource_id = lifecycle_db
    async with factory() as db:
        rows = [AgentRun(agent_id=agent_id, status="running", total_resources=7, dispatched=2,
                         matched_resource_ids=[resource_id], errors=["Preserved synthetic error"])
                for _ in range(3)]
        db.add_all(rows)
        await db.execute(update(Agent).where(Agent.id == agent_id).values(last_run_status="success"))
        await db.commit()
        ids = sorted(row.id for row in rows)
    return engine, factory, owner, agent_id, ids


async def approved(factory, ids):
    async with factory() as db:
        report = await export_review(db)
    report["approved_fingerprint"] = report["fingerprint"]
    report["selected_ids"] = ids
    return report


async def test_bounded_export_preserves_recorded_evidence(review_seed):
    _, factory, owner, _, ids = review_seed
    seen, after = [], ""
    async with factory() as db:
        while True:
            page = await export_review(db, after_id=after, limit=1)
            assert len(page["records"]) == 1
            row = page["records"][0]
            assert row["total_resources"] == 7 and row["dispatched"] == 2
            assert row["errors"] == ["Preserved synthetic error"] and row["matched_resource_ids"]
            seen.append(row["id"])
            if page["next_after_id"] is None:
                break
            after = page["next_after_id"]
        assert seen == ids and owner.run_id not in seen
        assert (await export_review(db, after_id=ids[-1]))["records"] == []
        for limit in [0, 1001, True]:
            with pytest.raises(ValueError):
                await export_review(db, limit=limit)
        assert (await db.get(AgentRun, ids[0])).status == "running"


async def test_selected_retirement_rolls_back_then_replays_idempotently(review_seed):
    _, factory, owner, agent_id, ids = review_seed
    report = await approved(factory, ids[:2])
    async with factory() as db:
        result = await apply_review(db, report)
        assert result["retired_ids"] == ids[:2]
        await db.rollback()
    async with factory() as db:
        assert (await db.get(AgentRun, ids[0])).status == "running"
    async with factory() as db:
        await apply_review(db, report)
        await db.commit()
    async with factory() as db:
        assert await apply_review(db, report) == {"retired_ids": [], "already_retired_ids": ids[:2]}
        await db.commit()
    async with factory() as db:
        for run_id in ids[:2]:
            run = await db.get(AgentRun, run_id)
            assert run.status == "failed" and run.finished_at is not None
            assert run.total_resources == 7 and run.dispatched == 2 and len(run.errors) == 2
            assert run.errors[0] == "Preserved synthetic error"
        assert (await db.get(AgentRun, ids[2])).status == "running"
        assert (await db.get(AgentRun, owner.run_id)).status == "running"
        assert list(await db.scalars(select(AgentRunLease.token))) == [owner.token]
        assert (await db.get(Agent, agent_id)).last_run_status == "success"


@pytest.mark.parametrize("drift", ["count", "status", "error", "lease", "deleted"])
async def test_changed_selection_rejects_entire_batch(review_seed, drift):
    _, factory, _, _, ids = review_seed
    report = await approved(factory, ids[:2])
    async with factory() as db:
        if drift == "lease":
            await create_lease(db, ids[1])
        elif drift == "deleted":
            await db.execute(delete(AgentRun).where(AgentRun.id == ids[1]))
        else:
            field, value = {"count": ("dispatched", 3), "status": ("status", "success"),
                            "error": ("errors", ["Changed synthetic error"])}[drift]
            await db.execute(update(AgentRun).where(AgentRun.id == ids[1]).values(**{field: value}))
        await db.commit()
    async with factory() as db:
        with pytest.raises(ValueError):
            await apply_review(db, report)
        # Even an erroneous caller commit cannot retire earlier selections:
        # all snapshots are checked before the first UPDATE.
        await db.commit()
    async with factory() as db:
        assert (await db.get(AgentRun, ids[0])).status == "running"


@pytest.mark.parametrize("invalid", ["unapproved", "fingerprint", "unknown", "duplicate", "tampered"])
async def test_invalid_review_cannot_change_history(review_seed, invalid):
    _, factory, owner, _, ids = review_seed
    report = copy.deepcopy(await approved(factory, ids[:1]))
    if invalid == "unapproved":
        report.pop("approved_fingerprint")
    elif invalid == "fingerprint":
        report["approved_fingerprint"] = "wrong"
    elif invalid == "unknown":
        report["selected_ids"] = [owner.run_id]
    elif invalid == "duplicate":
        report["selected_ids"] *= 2
    else:
        report["records"][0]["dispatched"] = 99
    async with factory() as db:
        with pytest.raises(ValueError):
            await apply_review(db, report)
        await db.rollback()
        assert (await db.get(AgentRun, ids[0])).status == "running"


@pytest.mark.parametrize("field", ["finished_at", "dispatched"])
async def test_replay_rejects_changed_retirement(review_seed, field):
    _, factory, _, _, ids = review_seed
    report = await approved(factory, ids[:1])
    async with factory() as db:
        await apply_review(db, report)
        await db.commit()
    async with factory() as db:
        value = utcnow() + timedelta(days=1) if field == "finished_at" else 99
        await db.execute(update(AgentRun).where(AgentRun.id == ids[0]).values(**{field: value}))
        await db.commit()
    async with factory() as db:
        with pytest.raises(ValueError):
            await apply_review(db, report)


_SEED_SEPARATE_FILE = """
import asyncio, json, sys
from datetime import datetime
from sqlalchemy import DateTime, text
import app.models
from app import database

async def main():
    rows = json.load(sys.stdin)
    try:
        async with database.engine.begin() as connection:
            await connection.run_sync(database.Base.metadata.create_all)
            await connection.execute(text("PRAGMA journal_mode='mvcc'"))
            for name, values in rows.items():
                table = database.Base.metadata.tables[name]
                for row in values:
                    for column in table.columns:
                        if isinstance(column.type, DateTime) and row.get(column.name) is not None:
                            row[column.name] = datetime.fromisoformat(row[column.name])
                if values:
                    await connection.execute(table.insert(), values)
    finally:
        await database.engine.dispose()

asyncio.run(main())
"""


async def test_real_cli_requires_review_and_refuses_overwriting_export(review_seed, tmp_path):
    engine, factory, _, _, ids = review_seed
    url = engine.url.render_as_string(hide_password=False)
    if engine.dialect.name == "sqlite":
        # Native Turso retains file ownership at process scope. Seed a separate
        # actual DB in a child that exits before any CLI process opens it.
        rows = {}
        async with factory() as db:
            for name in ["channels", "downloader_instances", "agents", "file_resources", "agent_runs", "agent_run_leases"]:
                table = database.Base.metadata.tables[name]
                rows[name] = [dict(row) for row in (await db.execute(select(table))).mappings()]
        url = f"sqlite+aioturso:///{tmp_path / 'cli.db'}"
        seeded = await asyncio.to_thread(
            subprocess.run, [sys.executable, "-c", _SEED_SEPARATE_FILE],
            env=dict(os.environ, DATABASE_URL=url), input=json.dumps(rows, default=str),
            capture_output=True, text=True, timeout=30,
        )
        assert seeded.returncode == 0, seeded.stderr
    env = dict(os.environ, DATABASE_URL=url)
    path = tmp_path / "review.json"

    async def cli(*args):
        return await asyncio.to_thread(subprocess.run, [sys.executable, "-m", "scripts.review_legacy_agent_runs", *args],
                                       env=env, capture_output=True, text=True, timeout=30)

    result = await cli("--export", str(path), "--limit", "1")
    assert result.returncode == 0, result.stderr
    original = path.read_bytes()
    assert (await cli("--export", str(path))).returncode == 1
    assert path.read_bytes() == original
    assert (await cli("--apply-review", str(path))).returncode == 2
    report = json.loads(original)
    report["approved_fingerprint"] = report["fingerprint"]
    report["selected_ids"] = [ids[0]]
    path.write_text(json.dumps(report))
    result = await cli("--apply-review", str(path), "--writers-stopped", "--backup-confirmed")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["retired_ids"] == [ids[0]]
    replay = await cli("--apply-review", str(path), "--writers-stopped", "--backup-confirmed")
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout)["already_retired_ids"] == [ids[0]]
    observer = create_async_engine(database.normalize_database_url(url))
    try:
        async with async_sessionmaker(observer)() as db:
            assert (await db.get(AgentRun, ids[0])).status == "failed"
            assert (await db.get(AgentRun, ids[1])).status == "running"
    finally:
        await observer.dispose()
