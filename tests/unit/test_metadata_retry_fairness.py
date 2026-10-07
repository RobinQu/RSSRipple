"""A metadata resource retains its concurrency slot across transaction retries."""
import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.models.resource_publication import ResourcePublication
from app.services import fetch_service, resource_publication
from app.services.metadata_concurrency import MetadataConcurrency
from app.services.publication_migration import bootstrap_publications
from app.utils.time import utcnow
from tests.unit.test_dedup_caller_transactions import _external_boundaries


@pytest.mark.parametrize('separate_channel', [False, True])
@pytest.mark.parametrize('capacity', [1, 4])
@pytest.mark.parametrize('cancel_during_backoff', [False, True])
async def test_retry_finishes_before_waiting_resource_takes_its_slot(
    db_session, monkeypatch, cancel_during_backoff, capacity, separate_channel,
):
    _external_boundaries(monkeypatch)
    monkeypatch.setattr('app.services.task_queue.task_queue.enqueue', AsyncMock())
    channel = Channel(name='Synthetic retry ordering', url='https://example.invalid/feed',
                      field_mapping={}, metadata_agent_enabled=False)
    db_session.add(channel)
    await db_session.flush()
    other = Channel(name='Synthetic independent channel', url='https://example.invalid/other',
                    field_mapping={}, metadata_agent_enabled=False) if separate_channel else channel
    if separate_channel:
        db_session.add(other)
        await db_session.flush()
    corpus = (Path(__file__).parents[1] / 'fixtures/prod_works_v1.json').read_bytes()
    assert hashlib.sha256(corpus).hexdigest() == 'd11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32'
    recorded = json.loads(corpus)['tables']['file_resources'][:2]
    # Recorded resource identities/titles, synthetic channel, metadata result
    # and transaction conflict. No remote metadata or torrent request is made.
    resources = [FileResource(id=row['id'], channel_id=owner.id, guid=row['guid'],
                              title_raw=row['title_raw'],
                              torrent_url=f'https://example.invalid/{row["id"]}.torrent')
                 for row, owner in zip(recorded, [channel, other])]
    db_session.add_all(resources)
    await bootstrap_publications(db_session, writers_stopped=True)
    await db_session.commit()
    first_id, second_id = [r.id for r in resources]
    channel_id = channel.id
    starts = []
    transaction_starts = []
    original_once = fetch_service._process_resource_metadata_once

    async def observe_transaction(resource_id, *args, **kwargs):
        transaction_starts.append(resource_id)
        return await original_once(resource_id, *args, **kwargs)

    monkeypatch.setattr(fetch_service, '_process_resource_metadata_once', observe_transaction)
    entered_backoff = asyncio.Event()
    release_backoff = asyncio.Event()
    later_waiting = asyncio.Event()
    delay = 0.123456789
    real_sleep = asyncio.tasks.sleep

    class ObservedConcurrency(MetadataConcurrency):
        @asynccontextmanager
        async def resource(self, channel_id):
            if asyncio.current_task().get_name() == 'later-resource':
                later_waiting.set()
            async with super().resource(channel_id) as mark_retry:
                yield mark_retry

    async def controlled_sleep(seconds):
        if seconds == delay:
            entered_backoff.set()
            await release_backoff.wait()
        else:
            await real_sleep(seconds)

    async def discovery(db, resource, channel):
        starts.append(resource.id)
        resource.metadata_attempts += 1
        resource.metadata_failure_type = 'not_found'
        resource.last_metadata_attempt_at = utcnow()

    publish = resource_publication.publish_resource
    injected = False

    async def fail_after_publication(db, resource_id, **kwargs):
        nonlocal injected
        result = await publish(db, resource_id, **kwargs)
        if resource_id == first_id and not injected:
            injected = True
            raise OperationalError('synthetic publication conflict', {}, RuntimeError('database is locked'))
        return result

    monkeypatch.setattr(fetch_service, 'fetch_and_link_metadata', discovery)
    monkeypatch.setattr(resource_publication, 'publish_resource', fail_after_publication)
    monkeypatch.setattr('app.database._backoff_delay', lambda attempt: delay)
    monkeypatch.setattr(asyncio, 'sleep', controlled_sleep)
    semaphore = ObservedConcurrency(capacity)
    tasks = []
    try:
        tasks.append(asyncio.create_task(fetch_service._process_resource_metadata(
            first_id, channel_id, semaphore), name='retrying-resource'))
        async with asyncio.timeout(10):
            await entered_backoff.wait()
        tasks.append(asyncio.create_task(fetch_service._process_resource_metadata(
            second_id, other.id, semaphore), name='later-resource'))
        async with asyncio.timeout(10):
            await later_waiting.wait()
        if cancel_during_backoff:
            tasks[0].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[0]
        release_backoff.set()
        async with asyncio.timeout(15):
            await asyncio.gather(*tasks[1:] if cancel_during_backoff else tasks)
    finally:
        release_backoff.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    db_session.expire_all()
    rows = list(await db_session.scalars(select(FileResource)))
    assert {row.id: row.metadata_attempts for row in rows} == {
        first_id: 0 if cancel_during_backoff else 1, second_id: 1,
    }
    publications = list(await db_session.scalars(select(ResourcePublication)))
    expected = [(first_id, 'created'), (second_id, 'created'), (second_id, 'metadata')]
    if not cancel_during_backoff:
        expected.append((first_id, 'metadata'))
    assert sorted((row.resource_id, row.kind) for row in publications) == sorted(expected)
    expected_starts = [first_id, second_id] if cancel_during_backoff else [first_id, first_id, second_id]
    if separate_channel and capacity > 1 and not cancel_during_backoff:
        expected_starts = [first_id, second_id, first_id]
        assert sorted(starts) == sorted(expected_starts)
    else:
        assert starts == expected_starts
    assert transaction_starts == expected_starts
    assert not semaphore._slots.locked()
