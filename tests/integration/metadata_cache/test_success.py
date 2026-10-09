"""Successful real Agent/cache/work upsert with synthetic source identity."""
import copy
import json
import uuid
from pathlib import Path
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.metadata_cache import MetadataCache
from app.models.movie import Movie
from app.services import metadata_service
from app.services.metadata_agent import UnifiedMetadataAgent
from tests.integration.dedup.conftest import dedup_postgres as dedup_postgres
from tests.integration.dedup.conftest import dedup_turso as dedup_turso
from tests.integration.metadata_cache.test_boundaries import data


async def check(pair, monkeypatch):
    _, factory = pair
    recorded, title = data(1024, 'cjk')
    label = json.loads(Path('tests/fixtures/prod_works_v1.json').read_text())['tables']['movies'][0]['title_cn']
    async with factory() as db:
        channel = Channel(name='Synthetic successful cache match', url='https://synthetic.invalid/cache',
                          metadata_source='wikipedia', metadata_fallback_sources=[], field_mapping={})
        db.add(channel)
        await db.flush()
        resources = [FileResource(channel_id=channel.id, guid=str(uuid.uuid4()), title_raw=title,
                                  torrent_url=recorded['torrent_url']) for _ in range(2)]
        db.add_all(resources)
        await db.commit()
        channel_id, ids = channel.id, [r.id for r in resources]
    verdict = {'found': True, 'clean_title': label, 'content_type': 'movie',
               'matched_entity': {'title_cn': label, 'content_type': 'movie',
                                  'external_source': 'wikipedia', 'external_id': 'wikipedia:synthetic-cache-match'}}
    source = AsyncMock(side_effect=lambda *args, **kwargs: (copy.deepcopy(verdict), {'method': 'synthetic'}))
    poster = AsyncMock(return_value=None)
    monkeypatch.setattr(metadata_service, 'download_and_cache_poster', poster)
    agent = UnifiedMetadataAgent()
    monkeypatch.setattr(agent, '_run_search_then_judge', source)
    actual_cache = agent._get_cache
    hits = []
    async def observed_cache(*args):
        result = await actual_cache(*args)
        hits.append(result is not None)
        return result
    monkeypatch.setattr(agent, '_get_cache', observed_cache)
    work_ids = []
    for resource_id in ids:
        async with factory() as db:
            resource = await db.get(FileResource, resource_id)
            channel = await db.get(Channel, channel_id)
            result = await agent.process(resource, channel, db)
            assert result.found
            await db.commit()
            work_ids.append(resource.movie_id)
    assert hits == [False, True]
    assert source.await_count == 1
    assert work_ids[0] is not None and work_ids[0] == work_ids[1]
    async with factory() as db:
        movies = (await db.scalars(select(Movie))).all()
        assert len(movies) == 1 and movies[0].title_cn == label
        cache = (await db.scalars(select(MetadataCache))).one()
        assert cache.title == title and cache.metadata_json['matched_entity']['external_id'] == verdict['matched_entity']['external_id']
        for identity in ids:
            resource = await db.get(FileResource, identity)
            assert resource.movie_id == movies[0].id
            assert resource.series_id is None and resource.metadata_failure_type is None
            assert resource.title_raw == title
    assert all(call.args == (None,) for call in poster.await_args_list)


async def test_postgres(dedup_postgres, monkeypatch):
    await check(dedup_postgres, monkeypatch)


async def test_turso(dedup_turso, monkeypatch):
    await check(dedup_turso, monkeypatch)
