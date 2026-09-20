"""Actual PostgreSQL legacy-schema guards and rollback; synthetic table rows."""

import asyncio
import json
import os
from pathlib import Path

from app.services.decision_schema import install_decision_constraints
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine


async def main():
    url = make_url(os.environ["DATABASE_URL"])
    assert url.drivername == "postgresql+asyncpg" and url.host == "127.0.0.1"
    assert (url.username, url.password, url.database) == ("organize_test",) * 3
    engine = create_async_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.execute(text("DROP TABLE IF EXISTS pending_decisions"))
            await conn.execute(
                text(
                    "CREATE TABLE pending_decisions "
                    "(id TEXT PRIMARY KEY,agent_id TEXT,status TEXT,candidates JSON,legacy_note TEXT)"
                )
            )
            await conn.execute(text("INSERT INTO pending_decisions VALUES ('history','agent','decided','[]','keep')"))
        try:
            async with engine.begin() as conn:
                await install_decision_constraints(conn)
                raise RuntimeError("fault after DDL")
        except RuntimeError:
            pass
        async with engine.begin() as conn:
            columns = await conn.run_sync(
                lambda sync: [c["name"] for c in inspect(sync).get_columns("pending_decisions")]
            )
            assert "decision_key" not in columns
            await install_decision_constraints(conn)
            await install_decision_constraints(conn)
            for stmt in [
                "INSERT INTO pending_decisions(id,agent_id,status) VALUES ('bad','agent','pending')",
                "UPDATE pending_decisions SET status='pending' WHERE id='history'",
            ]:
                try:
                    async with conn.begin_nested():
                        await conn.execute(text(stmt))
                except IntegrityError:
                    pass
                else:
                    raise AssertionError("NULL pending key accepted")
            row = (await conn.execute(text("SELECT status,legacy_note,decision_key FROM pending_decisions"))).one()
            assert tuple(row) == ("decided", "keep", None)
            await conn.execute(text("DROP INDEX uq_pending_decisions_agent_key"))
            await conn.execute(
                text("CREATE INDEX uq_pending_decisions_agent_key ON pending_decisions(agent_id,decision_key)")
            )
            try:
                await install_decision_constraints(conn)
            except ValueError as exc:
                assert "Unexpected" in str(exc)
            else:
                raise AssertionError("Wrong index accepted")
        result = dict(
            ddl_rollback=True,
            idempotent_install=True,
            insert_update_null_rejected=True,
            historical_columns_preserved=True,
            wrong_index_rejected=True,
        )
        Path("/tmp/rssripple-v11-schema-da-result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result))
    finally:
        await engine.dispose()


asyncio.run(main())
