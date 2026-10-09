"""Real startup and UnifiedMetadataAgent; only source/LLM result boundaries synthetic."""
import copy
import uuid
from unittest.mock import AsyncMock

from sqlalchemy import select

from app import database
from app.config import settings
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.metadata_cache import MetadataCache
from app.services.metadata_agent import UnifiedMetadataAgent
from tests.integration.dedup.conftest import dedup_postgres as dedup_postgres
from tests.integration.dedup.conftest import dedup_turso as dedup_turso
from tests.integration.metadata_cache.test_boundaries import data


async def check(pair, monkeypatch):
    engine, factory = pair
    monkeypatch.setattr(database, 'engine', engine)
    monkeypatch.setattr(settings, 'database_url', engine.url.render_as_string(hide_password=False))
    async with engine.begin() as conn:
        if engine.dialect.name == 'sqlite':
            from sqlalchemy import text
            await conn.execute(text('BEGIN'))
        await conn.run_sync(database.Base.metadata.drop_all)
    await engine.dispose()
    await database.create_tables()
    await database.create_tables()
    recorded, title = data(1024, 'cjk')
    async with factory() as db:
        channel = Channel(name='Synthetic cache caller', url='https://synthetic.invalid/cache-caller',
                          metadata_source='wikipedia', metadata_fallback_sources=[], field_mapping={})
        db.add(channel)
        await db.flush()
        resources = [FileResource(channel_id=channel.id, guid=str(uuid.uuid4()), title_raw=title,
                                  torrent_url=recorded['torrent_url']) for _ in range(4)]
        db.add_all(resources)
        await db.commit()
        channel_id, resource_ids = channel.id, [r.id for r in resources]
    verdict = {'clean_title': 'synthetic no-match', 'found': False, 'content_type': 'movie',
               'reason': 'No matching work found'}
    info = {'method': 'synthetic fixture', 'data_sources_used': ['synthetic']}
    source = AsyncMock(side_effect=lambda *args, **kwargs: (copy.deepcopy(verdict), copy.deepcopy(info)))
    agent = UnifiedMetadataAgent()
    monkeypatch.setattr(agent, '_run_search_then_judge', source)
    async def run(index, force=False):
        async with factory() as db:
            resource = await db.get(FileResource, resource_ids[index])
            channel = await db.get(Channel, channel_id)
            result = await agent.process(resource, channel, db, force_refresh=force)
            await db.commit()
            return result
    assert (await run(0)).clean_title == 'synthetic no-match'
    assert source.await_count == 1
    assert (await run(1)).clean_title == 'synthetic no-match'
    assert source.await_count == 1, 'actual definitive cache must bypass source boundary'
    verdict['clean_title'] = 'synthetic refreshed'
    assert (await run(1, True)).clean_title == 'synthetic refreshed'
    assert source.await_count == 2
    verdict['reason'] = 'Agent error: Request timed out.'
    verdict['clean_title'] = 'synthetic transient'
    assert (await run(2, True)).clean_title == 'synthetic transient'
    assert source.await_count == 3
    async with factory() as db:
        rows = (await db.scalars(select(MetadataCache))).all()
        assert len(rows) == 1 and rows[0].title == title
        assert rows[0].metadata_json['clean_title'] == 'synthetic refreshed'
        channel = await db.get(Channel, channel_id)
        channel.metadata_source = 'tmdb'
        await db.commit()
    verdict['reason'] = 'No matching work found'
    verdict['clean_title'] = 'synthetic other source'
    react = AsyncMock(side_effect=lambda *args: (copy.deepcopy(verdict), copy.deepcopy(info)))
    async def fallback(result, search_info, *args, **kwargs):
        return result, search_info
    monkeypatch.setattr(agent, '_run_react', react)
    monkeypatch.setattr(agent, '_maybe_web_fallback', fallback)
    assert (await run(3)).clean_title == 'synthetic other source'
    assert react.await_count == 1
    async with factory() as db:
        rows = (await db.scalars(select(MetadataCache))).all()
        assert {r.source for r in rows} == {'metadata_agent:wikipedia', 'metadata_agent:tmdb'}
        assert all(r.title == title for r in rows)
        for resource_id in resource_ids:
            resource = await db.get(FileResource, resource_id)
            assert resource.last_metadata_attempt_at is not None
            assert resource.title_raw == title


async def test_postgres(dedup_postgres, monkeypatch):
    await check(dedup_postgres, monkeypatch)


async def test_turso(dedup_turso, monkeypatch):
    await check(dedup_turso, monkeypatch)
