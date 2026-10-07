"""Recorded identities with synthetic edits through real persistence services."""
import json

import pytest
from sqlalchemy import insert, select, update

from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.series import TVSeries
from app.schemas.file_resource import ResourceAssociationUpdateRequest
from app.services.metadata_dedup import rehome_series_as_movie
from app.services.metadata_repository import _apply_to_resource
from app.services.metadata_resource_meta import ResourceMetadata
from app.services.resource_association import apply_association_update
from tests.integration.resource_work_fk.test_constraint import CORPUS, _seed
from tests.integration.resource_work_fk.test_legacy import _upgrade


async def _exercise(pair, monkeypatch, flow):
    engine, factory = pair
    await _upgrade(engine, monkeypatch)
    base, identities = await _seed(engine)
    series_id, movie_id, audio_id = identities
    async with engine.begin() as conn:
        refs = {'audio_work_id': audio_id} if flow == 'metadata' else {'series_id': series_id}
        await conn.execute(insert(FileResource).values(**base, **refs))
    if flow == 'associations':
        for work_type, work_id, batch in [('movie', movie_id, False), ('series', series_id, False), ('series', series_id, True)]:
            async with factory() as db:
                resource = await db.get(FileResource, base['id'])
                body = ResourceAssociationUpdateRequest(
                    is_batch=batch, works=[{'work_type':work_type, 'work_id':work_id}],
                    collection_id=base['collection_id'] if batch else None,
                    season=1 if work_type == 'series' else None,
                )
                await apply_association_update(db, resource, body)
                await db.commit()
            async with engine.connect() as conn:
                row = (await conn.execute(select(FileResource.__table__))).mappings().one()
            assert row[f'{work_type}_id'] == work_id and row['audio_work_id'] is None
            assert sum(row[key] is not None for key in ('series_id', 'movie_id', 'audio_work_id')) == 1
            assert row['collection_id'] == (base['collection_id'] if batch else None)
    elif flow == 'metadata':
        recorded = json.loads(CORPUS.read_bytes())['tables']['movies'][0]
        entity = {key:recorded[key] for key in ('external_id', 'external_source', 'title_cn', 'title_en')}
        async with engine.begin() as conn:
            await conn.execute(update(Movie).where(Movie.id == movie_id).values(**entity))
        async with factory() as db:
            resource = await db.get(FileResource, base['id'])
            channel = await db.get(Channel, base['channel_id'])
            meta = ResourceMetadata(clean_title=recorded['title_cn'], content_type='movie', matched_entity=entity)
            await _apply_to_resource(meta, resource, channel, db)
            await db.commit()
        async with engine.connect() as conn:
            row = (await conn.execute(select(FileResource.__table__))).mappings().one()
        assert row['movie_id'] == movie_id and row['series_id'] is None and row['audio_work_id'] is None
    else:
        # Synthetic wrong-table classification; the recorded work is not
        # claimed to have been misclassified in production.
        async with factory() as db:
            series = await db.get(TVSeries, series_id)
            movie = await db.get(Movie, movie_id)
            series.content_type = 'movie'
            await rehome_series_as_movie(db, series, movie)
            await db.commit()
        async with engine.connect() as conn:
            row = (await conn.execute(select(FileResource.__table__))).mappings().one()
            assert await conn.scalar(select(TVSeries.id).where(TVSeries.id == series_id)) is None
        assert row['series_id'] is None and row['movie_id'] == movie_id and row['audio_work_id'] is None
        assert row['collection_id'] == base['collection_id']
    assert row['guid'] == base['guid'] and row['title_raw'] == base['title_raw']


@pytest.mark.parametrize('work_fk_postgres', [None, 'legacy'], indirect=True, ids=['fresh', 'upgraded'])
@pytest.mark.parametrize('flow', ['associations', 'metadata', 'rehome'])
async def test_postgres_service_transitions(work_fk_postgres, monkeypatch, flow):
    await _exercise(work_fk_postgres, monkeypatch, flow)


@pytest.mark.parametrize('work_fk_turso', [None, 'legacy'], indirect=True, ids=['fresh', 'upgraded'])
@pytest.mark.parametrize('flow', ['associations', 'metadata', 'rehome'])
async def test_turso_service_transitions(work_fk_turso, monkeypatch, flow):
    await _exercise(work_fk_turso, monkeypatch, flow)
