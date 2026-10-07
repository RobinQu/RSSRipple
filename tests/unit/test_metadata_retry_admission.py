"""A bounded captured backlog must not starve an older transaction behind newcomers."""
import asyncio
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from feedparser import FeedParserDict
from sqlalchemy import select

from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.resource_publication import ResourcePublication
from app.services import fetch_service as fetch
from app.services.publication_migration import bootstrap_publications
from app.services.text_normalizer import normalize_title
from app.utils.time import utcnow
from tests.unit.test_dedup_caller_transactions import _external_boundaries


async def test_captured_backlog_commits_all_selected_resources_under_newcomer_contention(db_session, monkeypatch, record_property):
    from app.database import async_session_factory

    _external_boundaries(monkeypatch)
    monkeypatch.setattr('app.services.task_queue.task_queue.enqueue', AsyncMock())
    monkeypatch.setattr(fetch, '_parse_feed_sync', lambda _: FeedParserDict(entries=[], feed={}, bozo=False))
    monkeypatch.setattr('app.database._backoff_delay', lambda _: 0)
    channel = Channel(name='Synthetic competing backlog', url='https://example.invalid/rss',
                      type='rss_feed', metadata_agent_enabled=False,
                      field_mapping={'list_locator':{'source':'entries'}, 'field_mappings':{'title_cn':{'source':'title'}}})
    db_session.add(channel)
    await db_session.flush()
    raw = (Path(__file__).parents[1] / 'fixtures/prod_works_v1.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == 'd11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32'
    resources, keys = [], set()
    for row in json.loads(raw)['tables']['file_resources']:
        resource = FileResource(id=row['id'], channel_id=channel.id, guid=row['guid'],
                                title_raw=row['title_raw'], search_title=row.get('search_title'),
                                torrent_url=f'https://example.invalid/{row["id"]}.torrent')
        key = normalize_title(resource.search_title or fetch.extract_search_title(resource))
        if key in keys:
            continue
        keys.add(key)
        resource.created_at = utcnow() + timedelta(seconds=len(resources))
        resources.append(resource)
        if len(resources) == 40:
            break
    assert len(resources) == 40
    db_session.add_all(resources)
    await bootstrap_publications(db_session, writers_stopped=True)
    await db_session.commit()
    first_id, channel_id = resources[0].id, channel.id
    condition = asyncio.Condition()
    generation, first_done = 0, False
    targets, completed = {}, {}
    cohorts = []
    once, process = fetch._process_resource_metadata_once, fetch._process_resource_metadata

    async def observed_once(resource_id, *args, **kwargs):
        if resource_id != first_id and resource_id not in targets:
            # New peers join the next old-resource attempt. Already admitted
            # peers may retry freely so the old resource can wait for their
            # actual commits without manufacturing a circular dependency.
            targets[resource_id] = generation + 1
            completed[resource_id] = asyncio.Event()
        return await once(resource_id, *args, **kwargs)

    async def observed_process(resource_id, *args, **kwargs):
        nonlocal first_done
        try:
            return await process(resource_id, *args, **kwargs)
        finally:
            if resource_id == first_id:
                async with condition:
                    first_done = True
                    condition.notify_all()
            elif resource_id in completed:
                completed[resource_id].set()

    async def discovery(db, resource, channel):
        nonlocal generation
        if resource.id == first_id:
            async with condition:
                generation += 1
                peers = [rid for rid, target in targets.items() if target <= generation and not completed[rid].is_set()]
                cohorts.append(len(peers))
                condition.notify_all()
            # The old physical snapshot predates these peers' publication
            # commits; a nonempty cohort causes a real counter MVCC conflict.
            for resource_id in peers:
                await completed[resource_id].wait()
        else:
            async with condition:
                await condition.wait_for(lambda: first_done or generation >= targets[resource.id])
        resource.metadata_attempts += 1
        resource.metadata_failure_type = 'not_found'
        resource.last_metadata_attempt_at = utcnow()

    monkeypatch.setattr(fetch, '_process_resource_metadata_once', observed_once)
    monkeypatch.setattr(fetch, '_process_resource_metadata', observed_process)
    monkeypatch.setattr(fetch, 'fetch_and_link_metadata', discovery)
    async with asyncio.timeout(45):
        result = await fetch.fetch_channel_resources(channel, db_session)
    # Keep parent objects strongly referenced intentionally. An observer must
    # read committed database state, never this session's old identity map.
    async with async_session_factory() as observer:
        rows = list(await observer.scalars(select(FileResource).where(FileResource.channel_id == channel_id)))
        events = list(await observer.scalars(select(ResourcePublication).where(ResourcePublication.kind == 'metadata')))
    attempted = [row for row in rows if row.metadata_attempts == 1]
    untouched = [row for row in rows if row.metadata_attempts == 0]
    record_property('old_resource_attempts', generation)
    record_property('admitted_peer_cohorts', json.dumps(cohorts))
    record_property('committed_attempted', len(attempted))
    assert cohorts[0] > 0, 'The test must cause actual overlapping old snapshots'
    assert result['backfilled_count'] == 30
    assert len(attempted) == 30 and len(untouched) == 10
    assert all(row.metadata_failure_type == 'not_found' and row.last_metadata_attempt_at for row in attempted)
    assert {row.id for row in attempted} == {row.id for row in resources[:30]}
    assert len(events) == 30 and {event.resource_id for event in events} == {row.id for row in attempted}
