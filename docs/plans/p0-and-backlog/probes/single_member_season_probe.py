"""Synthetic partial collection: one stored season is not single-season evidence."""
import asyncio
import os
from pathlib import Path
from urllib.parse import urlsplit

assert Path(urlsplit(os.environ['DATABASE_URL']).path).resolve().name.startswith('single_member_')
assert Path(urlsplit(os.environ['DATABASE_URL']).path).resolve().parent == Path('/tmp')

from sqlalchemy import text  # noqa: E402

from app import database  # noqa: E402
from app.models.series import TVSeries  # noqa: E402
from app.models.work_collection import WorkCollection  # noqa: E402
from app.services.metadata_service import create_or_update_series_from_external  # noqa: E402


async def main():
    try:
        async with database.engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.create_all)
            await conn.execute(text("PRAGMA journal_mode='mvcc'"))
        async with database.async_session_factory() as db:
            collection = WorkCollection(title_cn='Synthetic incomplete collection')
            db.add(collection)
            await db.flush()
            member = TVSeries(title_cn='Synthetic incomplete collection', season_number=3,
                              collection_id=collection.id, is_anime=False)
            db.add(member)
            await db.commit()
            result = await create_or_update_series_from_external(db, {
                'title_cn': collection.title_cn, 'content_type': 'tv',
                'external_source': 'tmdb', 'is_anime': False,
            })
            await db.commit()
            print({'stored_member_season': 3, 'season_hint': None,
                   'verified_season_count': None,
                   'returned_season': result.season_number if result else None}, flush=True)
            assert result is None, 'Single stored season was treated as season evidence'
    finally:
        await database.engine.dispose()


asyncio.run(main())
