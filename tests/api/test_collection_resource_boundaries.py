"""Real API/DB coverage of collection cleanup across resource pages and media."""
import uuid

import pytest
from sqlalchemy import select

from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection


@pytest.mark.parametrize('operation', ['delete', 'detach'])
@pytest.mark.parametrize('kind', ['series', 'movie'])
@pytest.mark.parametrize('binding', ['direct', 'work_link', 'file_assignment'])
async def test_collection_resource_pages(
    client, db_session_factory, sample_channel, operation, kind, binding,
):
    model = TVSeries if kind == 'series' else Movie
    key = 'series_id' if kind == 'series' else 'movie_id'
    async with db_session_factory() as db:
        old = WorkCollection(title_cn='Synthetic old group')
        other = WorkCollection(title_cn='Unrelated control group')
        db.add_all([old, other])
        await db.flush()
        work = model(title_en='Synthetic paginated work', collection_id=old.id)
        if kind == 'series':
            work.season_number = 2
        db.add(work)
        await db.flush()
        old_id, other_id, work_id = old.id, other.id, work.id
        ids = [str(uuid.uuid4()) for _ in range(103)]
        stray_id, control_id = str(uuid.uuid4()), str(uuid.uuid4())
        rows = []
        for identity in ids + [stray_id, control_id]:
            fields = {key: work_id} if binding == 'direct' and identity in ids else {}
            rows.append(FileResource(
                id=identity, guid=identity, channel_id=sample_channel.id,
                title_raw='Synthetic resource boundary', torrent_url='https://example.invalid/data.torrent',
                collection_id=other_id if identity == control_id else old_id, **fields,
            ))
        db.add_all(rows)
        await db.flush()
        if binding != 'direct':
            for identity in ids:
                fields = {key: work_id}
                if binding == 'work_link':
                    db.add(ResourceWorkLink(resource_id=identity, source='manual', **fields))
                else:
                    db.add(ResourceFileAssignment(resource_id=identity, file_path='episode.mkv',
                                                  season=2 if kind == 'series' else None,
                                                  source='manual', **fields))
        await db.commit()
    if operation == 'delete':
        response = await client.delete(f'/api/v1/collections/{old_id}')
    else:
        response = await client.delete(f'/api/v1/collections/{old_id}/works/{work_id}',
                                       params={'work_type': kind})
    assert response.status_code == 200, response.text
    async with db_session_factory() as db:
        work = await db.get(model, work_id)
        expected = work.collection_id
        if kind == 'series':
            assert expected not in (None, old_id)
            assert work.season_number == 2
            assert await db.get(WorkCollection, expected) is not None
        else:
            assert expected is None
        resources = list(await db.scalars(select(FileResource).where(FileResource.id.in_(ids))))
        assert len(resources) == 103
        assert all(resource.collection_id == expected for resource in resources)
        if binding == 'direct':
            assert all(getattr(resource, key) == work_id for resource in resources)
        else:
            mapping_model = ResourceWorkLink if binding == 'work_link' else ResourceFileAssignment
            mappings = list(await db.scalars(select(mapping_model).where(mapping_model.resource_id.in_(ids))))
            assert len(mappings) == 103
            assert all(getattr(mapping, key) == work_id and mapping.source == 'manual' for mapping in mappings)
        assert (await db.get(FileResource, control_id)).collection_id == other_id
        assert (await db.get(FileResource, stray_id)).collection_id == (None if operation == 'delete' else old_id)
        assert (await db.get(WorkCollection, old_id) is None) == (operation == 'delete')
