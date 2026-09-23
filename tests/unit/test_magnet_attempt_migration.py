"""Upgrade an actual legacy schema without changing existing resolution state."""

import uuid

from sqlalchemy import MetaData, inspect, select, text
from sqlalchemy.ext.asyncio import create_async_engine

from app.database import Base, _apply_light_migrations, normalize_database_url


async def test_magnet_attempt_upgrade_preserves_legacy_rows(tmp_path):
    legacy = MetaData()
    for table in Base.metadata.sorted_tables:
        table.to_metadata(legacy)
    resources = legacy.tables["file_resources"]
    resources._columns.remove(resources.c.magnet_resolve_attempt_id)
    engine = create_async_engine(normalize_database_url(f"sqlite+aioturso:///{tmp_path / 'legacy.db'}"))
    channel_id = str(uuid.uuid4())
    try:
        async with engine.begin() as conn:
            await conn.run_sync(legacy.create_all)
            await conn.execute(text("PRAGMA journal_mode='mvcc'"))
            assert "magnet_resolve_attempt_id" not in await conn.run_sync(lambda db: {
                col["name"] for col in inspect(db).get_columns("file_resources")
            })
            await conn.execute(legacy.tables["channels"].insert().values(
                id=channel_id, name="Synthetic", type="rss_feed", url="https://example.invalid", field_mapping={},
            ))
            for index, status in enumerate((None, "running", "done")):
                await conn.execute(resources.insert().values(
                    id=str(uuid.uuid4()), channel_id=channel_id, guid=str(index), title_raw="Synthetic",
                    torrent_url="magnet:?xt=synthetic", magnet_resolve_status=status,
                ))
        for _ in range(2):
            async with engine.begin() as conn:
                await _apply_light_migrations(conn)
        current = Base.metadata.tables["file_resources"]
        async with engine.connect() as conn:
            rows = (await conn.execute(select(
                current.c.magnet_resolve_status, current.c.magnet_resolve_attempt_id,
            ).order_by(current.c.guid))).all()
            assert rows == [(None, None), ("running", None), ("done", None)]
    finally:
        await engine.dispose()
