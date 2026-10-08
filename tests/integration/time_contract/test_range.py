"""Reject out-of-range UTC instants before queue or database side effects."""
import uuid
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy import insert

from app.api.v1 import agents, notifications
from app.clients.rss_parser import _extract_published_at
from app.database import get_db
from app.main import validation_exception_handler
from app.models.agent import Agent
from app.models.channel import Channel
from app.models.downloader import DownloaderInstance
from app.schemas.notification import RegenerateRequest, RetryRequest
from app.services import task_queue
from app.services.resource_parser import _apply_transform
from tests.integration.time_contract.test_storage import channel_values

OUTSIDE = ['0001-01-01T00:00:00+08:00', '9999-12-31T23:59:59-08:00']


async def check_rejection(pair, monkeypatch, route, stamp):
    engine, factory = pair
    channel = channel_values()
    downloader, agent = str(uuid.uuid4()), str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(insert(Channel).values(**channel))
        await conn.execute(insert(DownloaderInstance).values(
            id=downloader, name='synthetic range', type='mock',
            url='mock://local', download_dir='/synthetic',
        ))
        await conn.execute(insert(Agent).values(
            id=agent, name='synthetic range', channel_id=channel['id'], downloader_id=downloader,
        ))
    queue = task_queue.MemoryQueue()
    monkeypatch.setattr(task_queue, 'task_queue', queue)
    app = FastAPI(exception_handlers={RequestValidationError: validation_exception_handler})
    app.include_router(agents.router, prefix='/api/v1')
    app.include_router(notifications.router, prefix='/api/v1')

    async def session():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = session
    paths = {
        'run': f'/agents/{agent}/run',
        'retry': '/notifications/retry',
        'regenerate': f'/agents/{agent}/notifications/regenerate',
    }
    body = {'scan_since' if route == 'run' else 'since': stamp}
    if route == 'retry':
        body.update(mode='failed', agent_id=agent)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
            base_url='http://synthetic',
        ) as client:
            response = await client.post('/api/v1' + paths[route], json=body)
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
        assert await queue.list_jobs() == []
    finally:
        await queue.stop()


@pytest.mark.parametrize('stamp', OUTSIDE)
@pytest.mark.parametrize('route', ['run', 'retry', 'regenerate'])
async def test_postgres_range_rejection(dedup_postgres, monkeypatch, route, stamp):
    await check_rejection(dedup_postgres, monkeypatch, route, stamp)


@pytest.mark.parametrize('stamp', OUTSIDE)
@pytest.mark.parametrize('route', ['run', 'retry', 'regenerate'])
async def test_turso_range_rejection(dedup_turso, monkeypatch, route, stamp):
    await check_rejection(dedup_turso, monkeypatch, route, stamp)


@pytest.mark.parametrize('stamp', OUTSIDE)
def test_unrepresentable_mapped_feed_time_is_invalid(stamp):
    assert _apply_transform(stamp, 'iso_datetime') is None


@pytest.mark.parametrize('stamp,expected', [
    ('0001-01-01T08:00:00+08:00', datetime(1, 1, 1)),
    ('9999-12-31T15:59:59-08:00', datetime(9999, 12, 31, 23, 59, 59)),
])
def test_representable_boundary_instants_remain_valid(stamp, expected):
    assert RetryRequest(mode='failed', since=stamp).since == expected
    assert RegenerateRequest(since=stamp).since == expected
    assert _apply_transform(stamp, 'iso_datetime') == expected


@pytest.mark.parametrize('stamp', OUTSIDE)
@pytest.mark.parametrize('fallback', [False, True])
def test_unrepresentable_rss_time_uses_existing_fallback(stamp, fallback):
    entry = SimpleNamespace(torrent_pubdate=stamp)
    if fallback:
        entry.published_parsed = (2026, 1, 2, 3, 4, 5, 0, 0, 0)
    assert _extract_published_at(entry) == (datetime(2026, 1, 2, 3, 4, 5) if fallback else None)
