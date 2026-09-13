"""Validate the migrated schema and both cascades in the dedicated probe DB."""

import asyncio
import json
import os
import uuid

from sqlalchemy import delete, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.models.agent import Agent
from app.models.agent_resource_request import AgentResourceRequest
from app.models.file_resource import FileResource
from app.services.agent_resource_requests import request_resources


async def main():
    url = make_url(os.environ["DATABASE_URL"])
    assert url.host == "127.0.0.1" and url.database == url.username == url.password == "organize_test"
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.connect() as connection:
            fks = await connection.run_sync(lambda c: inspect(c).get_foreign_keys("agent_resource_requests"))
            assert {(tuple(f["constrained_columns"]), f["options"].get("ondelete")) for f in fks} == {
                (("agent_id",), "CASCADE"),
                (("resource_id",), "CASCADE"),
            }
            constraints = await connection.run_sync(
                lambda c: inspect(c).get_unique_constraints("agent_resource_requests")
            )
            assert any(c["column_names"] == ["agent_id", "resource_id"] for c in constraints)
            columns = await connection.run_sync(lambda c: inspect(c).get_columns("agent_resource_requests"))
            assert all(not c["nullable"] for c in columns if c["name"] in {"agent_id", "resource_id", "revision"})
        async with factory() as db:
            [agent] = (await db.scalars(select(Agent))).all()
            [resource] = (await db.scalars(select(FileResource))).all()
            await request_resources(db, [agent.id], [resource.id])
            await db.commit()
            await db.execute(delete(FileResource).where(FileResource.id == resource.id))
            await db.commit()
            assert await db.scalar(select(AgentResourceRequest.id)) is None
            replacement = FileResource(
                id=str(uuid.uuid4()),
                channel_id=agent.channel_id,
                guid=str(uuid.uuid4()),
                title_raw="synthetic cascade verification",
                torrent_url="https://example.invalid",
            )
            db.add(replacement)
            await db.flush()
            await request_resources(db, [agent.id], [replacement.id])
            await db.commit()
            await db.execute(delete(Agent).where(Agent.id == agent.id))
            await db.commit()
            assert await db.scalar(select(AgentResourceRequest.id)) is None
            assert await db.get(FileResource, replacement.id) is not None
        print(
            json.dumps(
                {
                    "schema": "actual upgraded PostgreSQL",
                    "nonnullable_key": True,
                    "pair_unique": True,
                    "resource_delete_cascade": True,
                    "agent_delete_cascade": True,
                }
            )
        )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
