"""Upgrade the actual old table shape and preserve unknown historical modes."""
import uuid

from sqlalchemy import MetaData, inspect, select, text, update
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401
from app.database import Base, _apply_light_migrations, normalize_database_url
from app.models.organize_configuration import CONFIGURATION_ID, OrganizeConfiguration
from app.models.organize_plan import OrganizePlan

NEW_COLUMNS = {
    "file_op", "needs_category", "manual_destination", "revision", "config_revision", "owner_token",
}


async def check_upgrade(engine):
    legacy = MetaData()
    for table in Base.metadata.sorted_tables:
        if table.name != "organize_configuration":
            table.to_metadata(legacy)
    old_plan = legacy.tables["organize_plans"]
    for name in NEW_COLUMNS:
        old_plan._columns.remove(old_plan.c[name])
    ids = {name: str(uuid.uuid4()) for name in ("channel", "downloader", "resource", "task", "notification", "plan")}
    async with engine.begin() as conn:
        await conn.run_sync(legacy.create_all)
        columns = await conn.run_sync(lambda sync: {
            column["name"] for column in inspect(sync).get_columns("organize_plans")
        })
        assert not (columns & NEW_COLUMNS), "upgrade test requires a genuinely old schema"
        assert not await conn.run_sync(lambda sync: inspect(sync).has_table("organize_configuration"))
        if conn.dialect.name == "sqlite":
            await conn.execute(text("PRAGMA journal_mode='mvcc'"))
        await conn.execute(legacy.tables["channels"].insert().values(
            id=ids["channel"], name="migration", type="rss_feed", url="https://example.invalid/feed", field_mapping={},
        ))
        await conn.execute(legacy.tables["downloader_instances"].insert().values(
            id=ids["downloader"], name="migration", type="mock", url="", download_dir="/downloads",
        ))
        await conn.execute(legacy.tables["file_resources"].insert().values(
            id=ids["resource"], channel_id=ids["channel"], guid="migration", title_raw="synthetic migration fixture",
            torrent_url="magnet:?xt=urn:btih:synthetic",
        ))
        await conn.execute(legacy.tables["download_tasks"].insert().values(
            id=ids["task"], file_resource_id=ids["resource"], downloader_id=ids["downloader"],
            download_dir="/downloads", status="completed",
        ))
        await conn.execute(legacy.tables["download_notifications"].insert().values(
            id=ids["notification"], download_task_id=ids["task"], payload={"migration_sentinel": True},
        ))
        await conn.execute(old_plan.insert().values(
            id=ids["plan"], notification_id=ids["notification"], status="running",
            payload={"migration_sentinel": True},
        ))
    for attempt in range(2):
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            await _apply_light_migrations(conn)
            row = (await conn.execute(select(OrganizePlan.__table__).where(
                OrganizePlan.id == ids["plan"],
            ))).mappings().one()
            assert row["status"] == "running"
            assert row["payload"] == {"migration_sentinel": True}
            assert row["file_op"] is None and row["config_revision"] is None
            assert row["owner_token"] is None and row["revision"] == 0
            assert row["needs_category"] is False and row["manual_destination"] is False
            revision = await conn.scalar(select(OrganizeConfiguration.revision))
            assert revision == (0 if attempt == 0 else 77)
            await conn.execute(update(OrganizeConfiguration).where(
                OrganizeConfiguration.id == CONFIGURATION_ID,
            ).values(revision=77))


async def test_old_organize_plan_upgrade_is_idempotent(tmp_path, monkeypatch):
    from app.config import settings

    url = normalize_database_url(f"sqlite+aioturso:///{tmp_path / 'upgrade.db'}")
    monkeypatch.setattr(settings, "database_url", url)
    engine = create_async_engine(url)
    try:
        await check_upgrade(engine)
    finally:
        await engine.dispose()
