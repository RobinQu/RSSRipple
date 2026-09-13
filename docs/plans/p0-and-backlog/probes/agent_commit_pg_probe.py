"""Disposable PostgreSQL only: real deferred FK failure at COMMIT."""

import asyncio
import json
import os
import uuid
from unittest.mock import patch

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.database import Base
from app.models.agent import Agent
from app.models.channel import Channel
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.agent_service import process_resources
from app.utils.time import utcnow
from tests.unit.test_agent_service import TEST_FIELD_MAPPING, _make_resource


def uid():
    return str(uuid.uuid4())


async def main():
    url = make_url(os.environ["DATABASE_URL"])
    assert url.host == "127.0.0.1" and url.database == url.username == url.password == "organize_test"
    engine = create_async_engine(url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP TABLE IF EXISTS commit_probe"))
            await connection.execute(text("DROP TABLE IF EXISTS commit_probe_parent"))
            await connection.run_sync(Base.metadata.drop_all)
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(text("CREATE TABLE commit_probe_parent (id integer PRIMARY KEY)"))
            await connection.execute(
                text(
                    "CREATE TABLE commit_probe (parent_id integer REFERENCES commit_probe_parent(id) "
                    "DEFERRABLE INITIALLY DEFERRED)"
                )
            )
        async with factory() as db:
            channel = Channel(
                id=uid(),
                name="synthetic commit probe",
                type="rss_feed",
                url="https://example.invalid/feed",
                metadata_agent_enabled=False,
                field_mapping=TEST_FIELD_MAPPING,
            )
            downloader = DownloaderInstance(
                id=uid(), name="no RPC", type="transmission", url="http://example.invalid", download_dir="/synthetic"
            )
            collection = WorkCollection(id=uid(), title_cn="synthetic commit fixture")
            series = TVSeries(
                id=uid(),
                title_cn="synthetic commit fixture",
                content_type="tv",
                collection_id=collection.id,
                season_number=1,
            )
            db.add_all([channel, downloader, collection, series])
            await db.flush()
            agent = Agent(
                id=uid(),
                name="commit recovery",
                channel_id=channel.id,
                downloader_id=downloader.id,
                status="active",
                scope_channel_wide=True,
                conflict_resolution="ask",
            )
            resources = [
                _make_resource(channel.id, series_id=series.id, episode=i, parsed_at=utcnow(), published_at=utcnow())
                for i in (1, 2)
            ]
            db.add_all([agent, *resources])
            await db.commit()
            await db.refresh(agent)
            attempts, flushed = [], []

            async def dispatch(agent, resource, unit):
                attempts.append(resource.id)
                task = DownloadTask(
                    id=uid(),
                    agent_id=agent.id,
                    file_resource_id=resource.id,
                    downloader_id=downloader.id,
                    status="downloading",
                    download_dir="/synthetic",
                )
                unit.add(task)
                await unit.flush()
                if len(attempts) == 1:
                    await unit.execute(text("INSERT INTO commit_probe VALUES (999)"))
                flushed.append(resource.id)
                return task

            with patch("app.services.agent_service.dispatch_download", dispatch):
                result = await process_resources(agent, resources, db, autocommit=True)
            assert flushed == [r.id for r in resources], flushed
            assert result.dispatched == 1 and len(result.errors) == 1, result
            assert "ForeignKeyViolationError" in result.errors[0], result.errors
            assert agent.name == "commit recovery"
            async with factory() as observer:
                tasks = (await observer.scalars(select(DownloadTask))).all()
                assert len(tasks) == 1 and tasks[0].file_resource_id == resources[1].id
                assert await observer.scalar(text("SELECT count(*) FROM commit_probe")) == 0
            print(
                json.dumps(
                    {
                        "backend": "PostgreSQL",
                        "fault": "deferred FK at COMMIT",
                        "flushes_succeeded": 2,
                        "persisted_tasks": 1,
                        "dispatched_counter": result.dispatched,
                        "errors": len(result.errors),
                        "parent_object_usable": True,
                        "fixture": "synthetic, no RPC or media",
                    }
                )
            )
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
