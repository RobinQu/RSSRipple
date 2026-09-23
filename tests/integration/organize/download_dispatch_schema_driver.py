"""Upgrade a legacy schema through production startup, then exercise raw SQL."""

import asyncio
import json
import os
import subprocess
import uuid
from pathlib import Path

probe_root = Path(os.environ["PROBE_ROOT"]).resolve()
assert probe_root.is_relative_to(Path("/tmp"))
project = os.environ.get("PROBE_POSTGRES_PROJECT")
if project:
    assert project.startswith("rssripple-v14-schema-")
    [state] = json.loads(subprocess.check_output(["docker", "inspect", f"{project}-postgres-1"]))
    assert state["Config"]["Labels"]["com.docker.compose.project"] == project
    address = state["NetworkSettings"]["Networks"][project + "_isolated"]["IPAddress"]
    os.environ["DATABASE_URL"] = f"postgresql+asyncpg://probe:probe@{address}:5432/probe"
else:
    assert os.environ["DATABASE_URL"] == f"sqlite+aioturso:///{probe_root / 'legacy.db'}"

from sqlalchemy import inspect, select, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

import app.database as database  # noqa: E402
import app.models  # noqa: E402,F401
from app.models.app_setting import AppSetting  # noqa: E402
from app.models.download_dispatch import DownloadDispatch  # noqa: E402

INSERT = (
    "INSERT INTO download_dispatches (id, operation_key, task_id, parameters) VALUES (:id, :key, :task, :parameters)"
)


async def main():
    try:
        async with database.engine.begin() as conn:
            if not project:
                await conn.execute(text("PRAGMA journal_mode='mvcc'"))
            assert not await conn.run_sync(lambda sync: inspect(sync).has_table("app_settings"))
            legacy_tables = [t for t in database.Base.metadata.sorted_tables if t.name != "download_dispatches"]
            await conn.run_sync(lambda sync: database.Base.metadata.create_all(sync, tables=legacy_tables))
            await conn.execute(text("ALTER TABLE webhook_deliveries DROP COLUMN attempt_token"))
            await conn.execute(text("INSERT INTO app_settings (key, value) VALUES ('probe-legacy', 'preserved')"))
            assert not await conn.run_sync(lambda sync: inspect(sync).has_table("download_dispatches"))
        await database.create_tables()
        async with database.engine.connect() as conn:
            columns = await conn.run_sync(lambda sync: inspect(sync).get_columns("webhook_deliveries"))
            assert any(c["name"] == "attempt_token" and c["nullable"] for c in columns)
        row = {"id": str(uuid.uuid4()), "key": "a" * 64, "task": str(uuid.uuid4()), "parameters": "{}"}
        async with database.engine.begin() as conn:
            await conn.execute(text(INSERT), row)
        rejected = []
        for duplicate in ["operation", "task"]:
            other = {**row, "id": str(uuid.uuid4())}
            if duplicate == "operation":
                other["task"] = str(uuid.uuid4())
            else:
                other["key"] = "b" * 64
            try:
                async with database.engine.begin() as conn:
                    await conn.execute(text(INSERT), other)
            except IntegrityError:
                rejected.append(duplicate)
            else:
                raise AssertionError(f"Duplicate {duplicate} was accepted")
        try:
            async with database.engine.begin() as conn:
                await conn.execute(text("UPDATE download_dispatches SET settled = NULL WHERE id = :id"), row)
        except IntegrityError:
            rejected.append("null-settled")
        else:
            raise AssertionError("NULL settled was accepted")
        await database.create_tables()
        async with database.async_session_factory() as db:
            assert (await db.get(AppSetting, "probe-legacy")).value == "preserved"
            [dispatch] = (await db.scalars(select(DownloadDispatch))).all()
            assert dispatch.task_id == row["task"] and dispatch.settled is False
        Path(os.environ["PROBE_RESULT_PATH"]).write_text(
            json.dumps(
                {
                    "backend": "postgresql" if project else "turso",
                    "legacy_row_preserved": True,
                    "startup_calls": 2,
                    "delivery_attempt_column_restored": True,
                    "reservation_row_preserved": True,
                    "raw_default_settled": False,
                    "rejected": rejected,
                    "data": "synthetic schema fixture, no RPC",
                },
                indent=2,
            )
            + "\n"
        )
    finally:
        from app.services import fts

        if fts._FTS_ENGINE is not None:
            await fts._FTS_ENGINE.dispose()
        await database.engine.dispose()


asyncio.run(main())
