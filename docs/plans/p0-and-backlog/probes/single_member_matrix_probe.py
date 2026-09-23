"""Real Turso upsert matrix; all work/source rows are synthetic."""
import asyncio
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

assert Path(urlsplit(os.environ['DATABASE_URL']).path).resolve().name.startswith('single_member_')
assert Path(urlsplit(os.environ['DATABASE_URL']).path).resolve().parent == Path('/tmp')

from sqlalchemy import func, select, text  # noqa: E402

from app import database  # noqa: E402
from app.models.series import TVSeries  # noqa: E402
from app.models.work_collection import WorkCollection  # noqa: E402
from app.services.metadata_service import create_or_update_series_from_external  # noqa: E402


async def main():
    failures = []
    try:
        async with database.engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.create_all)
            await conn.execute(text("PRAGMA journal_mode='mvcc'"))
        cases = [
            ('only_special_unknown', [0], None, None, None),
            ('only_first_unknown', [1], None, None, None),
            ('only_third_unknown', [3], None, None, None),
            ('only_first_multiseason', [1], None, 3, None),
            ('empty_unknown', [], None, None, None),
            ('multiple_unknown', [1, 3], None, None, None),
            ('explicit_third', [3], 3, None, 3),
            ('verified_single', [1], None, 1, 1),
        ]
        for label, members, hint, count, expected in cases:
            async with database.async_session_factory() as db:
                title = 'Synthetic family ' + uuid.uuid4().hex
                collection = WorkCollection(title_cn=title)
                db.add(collection)
                await db.flush()
                cid = collection.id
                for season in members:
                    db.add(TVSeries(title_cn=title, season_number=season,
                                    collection_id=cid, is_anime=False))
                await db.commit()
                entity = {'title_cn': title, 'content_type': 'tv', 'external_source': 'tmdb', 'is_anime': False}
                if count is not None:
                    entity['number_of_seasons'] = count
                result = await create_or_update_series_from_external(db, entity, season_hint=hint)
                actual = result.season_number if result else None
                await db.commit()
            async with database.async_session_factory() as observer:
                rows = await observer.scalar(
                    select(func.count()).select_from(TVSeries).where(TVSeries.collection_id == cid)
                )
            passed = actual == expected and rows == len(members)
            record = {'case': label, 'members': members, 'hint': hint, 'source_count': count,
                      'expected_season': expected, 'actual_season': actual, 'rows_after_commit': rows, 'passed': passed}
            print(record, flush=True)
            if not passed:
                failures.append(label)
        assert not failures, failures
    finally:
        await database.engine.dispose()


asyncio.run(main())
