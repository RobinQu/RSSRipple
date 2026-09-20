"""Real legacy table rebuilds must retain downloads and organize associations."""

import re
import uuid

import pytest
from sqlalchemy import event, insert, select, text
from sqlalchemy.schema import CreateTable

from app.database import Base, create_tables


@pytest.mark.parametrize("inject_failure", [False, True])
async def test_rebuild_preserves_downloads_and_organize_links(db_engine, inject_failure):
    tables = Base.metadata.tables
    legacy = {
        "file_resources": ("audio_work_id", "collection_id"),
        "downloader_instances": ("volume_id",),
        "libraries": ("media_server_id", "volume_id"),
    }
    async with db_engine.begin() as conn:
        for table, columns in legacy.items():
            ddl = str(CreateTable(tables[table]).compile(dialect=db_engine.dialect))
            ddl = "\n".join(line for line in ddl.splitlines() if not any(f"FOREIGN KEY({c})" in line for c in columns))
            ddl = re.sub(r",\s*\)", "\n)", ddl)
            await conn.execute(text(f"DROP TABLE {table}"))
            await conn.execute(text(ddl))

        async def add(table, **values):
            identity = str(uuid.uuid4())
            await conn.execute(insert(tables[table]).values(id=identity, **values))
            return identity

        channel = await add("channels", name="Synthetic", url="https://example.invalid/rss", field_mapping={})
        volume = await add("storage_volumes", name="Synthetic", mount_path="/synthetic")
        server = await add("media_server_instances", name="Synthetic", type="plex", url="https://example.invalid")
        audio = await add("audio_works", title_cn="Synthetic audio")
        downloader = await add(
            "downloader_instances",
            name="Synthetic",
            url="https://example.invalid",
            download_dir="/synthetic",
            volume_id=volume,
        )
        resource = await add(
            "file_resources",
            channel_id=channel,
            guid="synthetic-fk-links",
            title_raw="Synthetic audio",
            torrent_url="https://example.invalid/torrent",
            audio_work_id=audio,
        )
        library = await add(
            "libraries",
            name="Synthetic",
            media_server_id=server,
            volume_id=volume,
            root_subpath="protected",
            section_key="1",
            server_path="/synthetic/protected",
        )
        task = await add(
            "download_tasks",
            file_resource_id=resource,
            downloader_id=downloader,
            download_dir="/synthetic",
            status="completed",
        )
        notification = await add(
            "download_notifications", download_task_id=task, payload={"version": 2, "sentinel": "preserve"}
        )
        rule = await add(
            "organize_rules",
            name="Synthetic protected rule",
            library_id=library,
            path_template="{title}",
            file_op="copy",
        )
        await add(
            "organize_plans",
            notification_id=notification,
            rule_id=rule,
            library_id=library,
            payload={"version": 2, "sentinel": "frozen"},
            file_op="copy",
            manual_destination=True,
        )
        watched = [*legacy, "download_tasks", "download_notifications", "organize_rules", "organize_plans"]
        before = {
            table: (await conn.execute(select(tables[table]).order_by(tables[table].c.id))).all() for table in watched
        }

    def fail_after_all_swaps(conn, cursor, statement, parameters, context, executemany):
        if "__fk_repair_libraries" in statement and "RENAME TO" in statement:
            raise RuntimeError("injected after final swap")

    if inject_failure:
        event.listen(db_engine.sync_engine, "after_cursor_execute", fail_after_all_swaps)
        try:
            with pytest.raises(RuntimeError, match="injected after final swap"):
                await create_tables()
        finally:
            event.remove(db_engine.sync_engine, "after_cursor_execute", fail_after_all_swaps)
        async with db_engine.connect() as conn:
            for table in watched:
                assert (await conn.execute(select(tables[table]).order_by(tables[table].c.id))).all() == before[table]
            for table, columns in legacy.items():
                fks = (await conn.execute(text(f"PRAGMA foreign_key_list({table})"))).all()
                assert not any(row[3] in columns for row in fks)
            assert await conn.scalar(text("PRAGMA foreign_keys")) == 1
    await create_tables()
    await create_tables()
    async with db_engine.connect() as conn:
        for table in watched:
            assert (await conn.execute(select(tables[table]).order_by(tables[table].c.id))).all() == before[table]
        for table, columns in legacy.items():
            fks = (await conn.execute(text(f"PRAGMA foreign_key_list({table})"))).all()
            assert all(any(row[3] == column for row in fks) for column in columns)
