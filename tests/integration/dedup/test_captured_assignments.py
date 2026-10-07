"""Captured Nisekoi 24-file subgraph, with explicitly synthetic merge/edit history.

The pre-season-split export binds S1 and specials to one row. This fixture
separates its explicitly recorded S0/S1 placements into synthetic season
containers; file identity, sizes, episode evidence and provenance stay intact.
It does not assert that the historic graph was already single-season compliant.
"""
import hashlib
import json
from pathlib import Path

from sqlalchemy import select

from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.models.work_external_id import WorkExternalId
from app.services.metadata_dedup import DedupReport, _merge_series_group

CORPUS = Path(__file__).parents[2] / 'fixtures' / 'prod_works_v1.json'
CORPUS_HASH = 'd11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32'
WORK_ID = 'd008e00f-5e44-4747-ae2d-1d0ed5958837'
RESOURCE_ID = 'e15c597b-0f4a-4e8e-8c9d-0f8fea2222fd'


async def test_captured_assignments_postgres(dedup_postgres):
    await _replay(dedup_postgres)


async def test_captured_assignments_turso(dedup_turso):
    await _replay(dedup_turso)


def _file_values(row):
    fields = ['id', 'resource_id', 'file_path', 'file_size', 'work_title_hint',
              'season', 'episode_start', 'episode_end', 'source']
    return {name: row[name] for name in fields}


async def _replay(database):
    assert hashlib.sha256(CORPUS.read_bytes()).hexdigest() == CORPUS_HASH
    data = json.loads(CORPUS.read_text())['tables']
    work = next(row for row in data['tv_series'] if row['id'] == WORK_ID)
    resource = next(row for row in data['file_resources'] if row['id'] == RESOURCE_ID)
    assignments = [row for row in data['resource_file_assignments'] if row['resource_id'] == RESOURCE_ID]
    assert len(assignments) == 24
    assert {row['season'] for row in assignments} == {0, 1}
    assert all(row['series_id'] == WORK_ID for row in assignments)
    _, factory = database
    async with factory() as db:
        channel = Channel(name='Synthetic captured dedup channel', type='rss_feed', url='https://example.invalid', field_mapping={})
        collections = [WorkCollection(title_cn='合成季容器'), WorkCollection(title_cn='合成重复容器')]
        db.add_all([channel, *collections])
        await db.flush()
        fields = {name: work[name] for name in ['title_cn', 'title_en', 'original_title', 'aliases', 'external_id', 'external_source']}
        original = TVSeries(id=WORK_ID, **fields, season_number=1, collection_id=collections[0].id)
        special = TVSeries(title_cn=work['title_cn'], season_number=0, collection_id=collections[0].id)
        target = TVSeries(title_cn=work['title_cn'], season_number=1, collection_id=collections[1].id,
                          description='Synthetic curator correction', manually_edited_fields=['description'])
        db.add_all([original, special, target])
        await db.flush()
        target_id, special_id = target.id, special.id
        copied = {name: resource[name] for name in ['id', 'guid', 'title_raw', 'torrent_url', 'is_batch', 'batch_scope', 'file_size']}
        db.add(FileResource(**copied, channel_id=channel.id, series_id=original.id))
        await db.flush()
        for row in assignments:
            db.add(ResourceFileAssignment(**_file_values(row), series_id=original.id if row['season'] == 1 else special.id))
        links = [row for row in data['resource_work_links'] if row['resource_id'] == RESOURCE_ID and row.get('series_id') == WORK_ID]
        assert links
        for row in links:
            db.add(ResourceWorkLink(id=row['id'], resource_id=RESOURCE_ID, series_id=WORK_ID, source=row['source']))
        bags = [row for row in data['work_external_ids'] if row['work_id'] == WORK_ID and row['work_type'] == 'series']
        assert bags
        for row in bags:
            db.add(WorkExternalId(id=row['id'], work_id=WORK_ID, work_type='series', source=row['source'], external_id=row['external_id']))
        await db.commit()
        await _merge_series_group(db, [original, target], DedupReport(), survivor=target)
        await db.commit()
    async with factory() as check:
        result = await check.get(TVSeries, target_id)
        assert result.description == 'Synthetic curator correction'
        assert result.manually_edited_fields == ['description']
        assert await check.get(TVSeries, WORK_ID) is None
        persisted = (await check.execute(select(ResourceFileAssignment))).scalars().all()
        assert len(persisted) == 24
        expected = {row['id']: _file_values(row) for row in assignments}
        for row in persisted:
            assert {name: getattr(row, name) for name in expected[row.id]} == expected[row.id]
            assert row.series_id == (target_id if row.season == 1 else special_id)
        res = await check.get(FileResource, RESOURCE_ID)
        assert res.title_raw == resource['title_raw'] and res.series_id == target_id
        actual_links = (await check.execute(select(ResourceWorkLink))).scalars().all()
        assert {(row.id, row.source, row.series_id) for row in actual_links} == {(row['id'], row['source'], target_id) for row in links}
        actual_bags = (await check.execute(select(WorkExternalId))).scalars().all()
        assert {(row.source, row.external_id, row.work_id) for row in actual_bags} >= {(row['source'], row['external_id'], target_id) for row in bags}
    assert hashlib.sha256(CORPUS.read_bytes()).hexdigest() == CORPUS_HASH
