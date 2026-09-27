"""Collection membership is inventory, not evidence of a resource's season."""
import pytest
from sqlalchemy import select

from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.models.work_external_id import WorkExternalId
from app.services.metadata_service import create_or_update_series_from_external


@pytest.mark.parametrize('route', ['title', 'collection_bag'])
@pytest.mark.parametrize('members,hint,count,expected', [
    ([0], None, None, None), ([1], None, None, None), ([3], None, None, None),
    ([1], None, 3, None), ([], None, None, None), ([1, 3], None, None, None),
    ([3], 3, None, 3), ([1], None, 1, 1),
])
async def test_collection_size_does_not_supply_season(db_session, route, members, hint, count, expected):
    collection = WorkCollection(title_cn='Synthetic evidence family')
    db_session.add(collection)
    await db_session.flush()
    cid = collection.id
    db_session.add_all([TVSeries(title_cn=collection.title_cn, collection_id=cid,
                                season_number=n, is_anime=False) for n in members])
    entity = {'title_cn': collection.title_cn, 'content_type': 'tv', 'external_source': 'tmdb', 'is_anime': False}
    if route == 'collection_bag':
        entity.update(title_cn='Synthetic source title', external_id='tmdb:90006001')
        db_session.add(WorkExternalId(work_type='collection', work_id=cid,
                                     source='tmdb', external_id=entity['external_id']))
    if count is not None:
        entity['number_of_seasons'] = count
    await db_session.commit()
    selected = await create_or_update_series_from_external(db_session, entity, season_hint=hint)
    assert (selected.season_number if selected else None) == expected
    await db_session.commit()
    from app.database import async_session_factory
    async with async_session_factory() as observer:
        seasons = list((await observer.scalars(select(TVSeries.season_number).where(
            TVSeries.collection_id == cid))).all())
        assert sorted(seasons) == sorted(members)
        if expected is None and route == 'collection_bag':
            identities = list((await observer.scalars(select(WorkExternalId).where(
                WorkExternalId.source == 'tmdb', WorkExternalId.external_id == entity['external_id']))).all())
            assert [(row.work_type, row.work_id) for row in identities] == [('collection', cid)]


@pytest.mark.parametrize('with_sibling', [False, True])
async def test_explicit_manual_target_supplies_its_season(db_session, with_sibling):
    collection = WorkCollection(title_cn='Synthetic manually selected family')
    db_session.add(collection)
    await db_session.flush()
    target = TVSeries(title_cn=collection.title_cn, collection_id=collection.id,
                      season_number=3, is_anime=False)
    db_session.add(target)
    if with_sibling:
        db_session.add(TVSeries(title_cn=collection.title_cn, collection_id=collection.id,
                               season_number=1, is_anime=False))
    await db_session.commit()
    selected = await create_or_update_series_from_external(db_session, {
        'title_cn': collection.title_cn, 'content_type': 'tv', 'external_source': 'tmdb',
        'is_anime': False, 'description': 'Same family, explicit manual season identity',
    }, expected_series_id=target.id)
    assert selected.id == target.id and selected.season_number == 3
    await db_session.commit()
    await db_session.refresh(target)
    assert target.description == 'Same family, explicit manual season identity'


@pytest.mark.parametrize('entry', ['repository', 'agent', 'cache', 'pipeline'])
@pytest.mark.parametrize('existing_season', [None, 3])
@pytest.mark.parametrize('hint', [None, 3])
async def test_recorded_title_preserves_unknown_season(db_session, sample_channel, monkeypatch,
                                                      entry, existing_season, hint):
    import gzip
    import json
    import uuid
    from pathlib import Path
    from unittest.mock import AsyncMock

    from app.models.file_resource import FileResource
    from app.services.metadata_agent import UnifiedMetadataAgent
    from app.services.metadata_repository import _apply_to_resource
    from app.services.metadata_resource_meta import ResourceMetadata

    corpus = Path(__file__).resolve().parents[1] / 'fixtures/metadata_corpus_v1/candidates.json.gz'
    with gzip.open(corpus, 'rt') as stream:
        case = next(c for c in json.load(stream)['cases'] if c['id'] == '011c6d44-68cf-43a8-bad3-f0398ce20a95')
    sample_channel.metadata_source = 'tmdb'
    sample_channel.metadata_agent_enabled = True
    collection = WorkCollection(title_cn='攻壳机动队')
    db_session.add(collection)
    await db_session.flush()
    cid = collection.id
    if existing_season is not None:
        db_session.add(TVSeries(title_cn=collection.title_cn, season_number=existing_season,
                                collection_id=cid, is_anime=True))
    db_session.add(WorkExternalId(work_type='collection', work_id=cid,
                                 source='tmdb', external_id='tmdb:90006484'))
    resource = FileResource(channel_id=sample_channel.id, guid=str(uuid.uuid4()),
                            title_raw=case['input']['title_raw'], torrent_url='', season=hint, episode=6)
    db_session.add(resource)
    if entry == 'pipeline':
        from app.services.resource_publication import publish_resource
        await db_session.flush()
        await publish_resource(db_session, resource.id, kind='created')
    await db_session.commit()
    rid = resource.id
    entity = {'title_cn': collection.title_cn, 'content_type': 'tv', 'external_source': 'tmdb',
              'external_id': 'tmdb:90006484', 'is_anime': True}
    payload = {'found': True, 'content_type': 'tv', 'clean_title': collection.title_cn, 'matched_entity': entity}
    if entry == 'repository':
        await _apply_to_resource(ResourceMetadata.from_dict(payload), resource, sample_channel, db_session)
    else:
        agent = UnifiedMetadataAgent()
        agent._ensure_genre = AsyncMock()
        agent._run_react = AsyncMock(return_value=(payload, {
            'method': 'synthetic', 'data_sources_used': [], 'source_errors': {}, 'error': None,
        }))
        monkeypatch.setattr('app.services.metadata_agent._attach_tmdb_episode_list', AsyncMock())
        if entry == 'cache':
            await agent._set_cache(resource.title_raw, 'tmdb', ResourceMetadata.from_dict(payload), db_session)
            await db_session.commit()
        if entry == 'pipeline':
            import asyncio

            from app.services.fetch_service import _process_resource_metadata_once
            monkeypatch.setattr('app.services.metadata_agent.get_agent', lambda: agent)
            monkeypatch.setattr('app.services.torrent_inspect.ensure_torrent_cached', AsyncMock())
            monkeypatch.setattr('app.services.torrent_inspect.maybe_inspect_torrent', AsyncMock())
            await _process_resource_metadata_once(rid, sample_channel.id, asyncio.Semaphore(1), force_refresh=True)
        else:
            await agent.process(resource, sample_channel, db_session, force_refresh=entry != 'cache')
        if entry == 'cache':
            agent._run_react.assert_not_awaited()
        else:
            agent._run_react.assert_awaited_once()
    await db_session.commit()
    from app.database import async_session_factory
    async with async_session_factory() as observer:
        saved = await observer.get(FileResource, rid)
        if entry == 'pipeline':
            from app.models.resource_publication import ResourcePublication
            published = (await observer.scalars(select(ResourcePublication).where(
                ResourcePublication.resource_id == rid, ResourcePublication.kind == 'metadata'))).all()
            assert len(published) == 1
        works = (await observer.scalars(select(TVSeries).where(TVSeries.collection_id == cid))).all()
        if hint is None:
            assert saved.series_id is None and saved.movie_id is None
            assert saved.collection_id == cid and saved.season is None
            assert saved.episode_confidence == 'ambiguous'
            assert len(works) == int(existing_season is not None)
        else:
            assert saved.series_id == works[0].id and saved.season == 3
            assert len(works) == 1 and works[0].season_number == 3
        identities = (await observer.scalars(select(WorkExternalId).where(
            WorkExternalId.source == 'tmdb', WorkExternalId.external_id == 'tmdb:90006484'))).all()
        assert [(r.work_type, r.work_id) for r in identities] == [('collection', cid)]


@pytest.mark.parametrize('existing_work', [False, True])
async def test_series_level_unknown_season_cannot_use_outer_defaults(db_session, existing_work):
    """Missing season evidence cannot be replaced by fresh/one-title defaults."""
    title = 'Synthetic undecided series-level candidate'
    if existing_work:
        collection = WorkCollection(title_cn='Synthetic differently named collection')
        db_session.add(collection)
        await db_session.flush()
        db_session.add(TVSeries(title_cn=title, collection_id=collection.id,
                                season_number=3, is_anime=False))
    await db_session.commit()
    selected = await create_or_update_series_from_external(db_session, {
        'title_cn': title, 'content_type': 'tv', 'external_source': 'tmdb',
        'external_id': 'tmdb:90006485', 'is_anime': False,
    })
    await db_session.commit()
    assert selected is None, 'Series-level identity supplies no season number'
    from app.database import async_session_factory
    async with async_session_factory() as observer:
        works = (await observer.scalars(select(TVSeries))).all()
        assert len(works) == int(existing_work)
        identities = (await observer.scalars(select(WorkExternalId).where(
            WorkExternalId.source == 'tmdb', WorkExternalId.external_id == 'tmdb:90006485'))).all()
        assert len(identities) == 1 and identities[0].work_type == 'collection'


@pytest.mark.parametrize('existing_work', [False, True])
async def test_season_identity_does_not_supply_season_number(db_session, existing_work):
    """A synthetic Bangumi subject identifies a season, not its series ordinal."""
    title = 'Synthetic ordinal-free season subject'
    if existing_work:
        collection = WorkCollection(title_cn='Synthetic differently named collection')
        db_session.add(collection)
        await db_session.flush()
        db_session.add(TVSeries(title_cn=title, collection_id=collection.id,
                                season_number=3, is_anime=False))
    await db_session.commit()
    selected = await create_or_update_series_from_external(db_session, {
        'title_cn': title, 'content_type': 'tv', 'external_source': 'bangumi',
        'external_id': 'bangumi:90006487', 'is_anime': False,
    })
    await db_session.commit()
    assert selected is None, 'Season-granularity identity supplies no ordinal'
    from app.database import async_session_factory
    async with async_session_factory() as observer:
        works = (await observer.scalars(select(TVSeries))).all()
        assert len(works) == int(existing_work)
