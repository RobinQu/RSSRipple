"""A candidate discovered before an edit must be revalidated under its lock."""
from datetime import date

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.api.v1 import works
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services import metadata_dedup as dedup


@pytest.mark.parametrize('case', ['movie_title', 'movie_year', 'series_season', 'series_year', 'cross_title', 'cross_identity', 'api_season'])
async def test_committed_group_change_prevents_stale_merge(dedup_postgres, monkeypatch, case):
    _, factory = dedup_postgres
    async with factory() as seed:
        collection = WorkCollection(title_cn='合成分组合集')
        seed.add(collection)
        await seed.flush()
        if case in {'movie_title', 'movie_year'}:
            rows = [Movie(title_en='Synthetic old group'), Movie(title_en='Synthetic old group')]
        elif case in {'cross_title', 'cross_identity'}:
            rows = [Movie(title_en='Synthetic old group'), TVSeries(title_en='Synthetic old group', collection_id=collection.id)]
        else:
            other = WorkCollection(title_cn='合成另一合集')
            seed.add(other)
            await seed.flush()
            rows = [TVSeries(title_en='Synthetic old group', collection_id=c.id, season_number=1)
                    for c in [collection, other]]
        if case.endswith('year'):
            for row in rows:
                setattr(row, 'release_date' if isinstance(row, Movie) else 'start_date', date(2000, 1, 1))
        if case == 'cross_identity':
            for index, row in enumerate(rows):
                row.title_en = f'Synthetic identity-only candidate {index}'
                row.external_source = 'wikipedia'
                row.external_id = 'wikipedia:en:123456'
        seed.add_all(rows)
        await seed.commit()
        first_id, changed_id = [row.id for row in rows]
        changed_model = type(rows[1])
    original = dedup.lock_merge_works
    edited = False

    async def concurrent_edit(db, targets):
        nonlocal edited
        if not edited:
            edited = True
            async with factory() as editor:
                changed = await editor.get(changed_model, changed_id)
                if case.endswith('title'):
                    changed.title_en = 'Different work after correction'
                    changed.manually_edited_fields = ['title_en']
                elif case.endswith('year'):
                    field = 'release_date' if isinstance(changed, Movie) else 'start_date'
                    setattr(changed, field, date(2008, 1, 1))
                    changed.manually_edited_fields = [field]
                elif case == 'cross_identity':
                    changed.external_id = 'wikipedia:en:654321'
                    changed.manually_edited_fields = ['external_id']
                else:
                    changed.season_number = 2
                await editor.commit()
        await original(db, targets)

    monkeypatch.setattr(dedup, 'lock_merge_works', concurrent_edit)
    if case == 'api_season':
        app = FastAPI()
        app.include_router(works.router, prefix='/api/v1')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://synthetic') as client:
            response = await client.post('/api/v1/works/merge', json={
                'survivor_type': 'series', 'survivor_id': first_id,
                'duplicate_ids': [changed_id], 'confirm': True,
            })
        assert response.status_code == 409, response.text
    else:
        async with factory() as db:
            merge = {'movie_title': dedup.merge_duplicate_movies,
                     'movie_year': dedup.merge_duplicate_movies,
                     'series_year': dedup.merge_duplicate_series,
                     'series_season': dedup.merge_duplicate_series,
                     'cross_identity': dedup.merge_cross_type_duplicates,
                     'cross_title': dedup.merge_cross_type_duplicates}[case]
            report = await merge(db)
            await db.commit()
            assert report.movies_removed + report.series_removed + report.cross_type_merges == 0
    assert edited
    async with factory() as check:
        assert await check.get(type(rows[0]), first_id) is not None
        assert await check.get(changed_model, changed_id) is not None
        if not case.startswith('cross_'):
            assert len((await check.execute(select(changed_model))).scalars().all()) == 2
