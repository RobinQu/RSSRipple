"""The already-correct Agent window input must preserve offsets and modes."""
import uuid
from datetime import datetime

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import insert

from app.api.v1.agents import router
from app.database import get_db
from app.models.agent import Agent
from app.models.channel import Channel
from app.models.downloader import DownloaderInstance
from app.services import task_queue
from tests.integration.time_contract.test_storage import channel_values

CASES = [
    ('2026-01-01T00:15:00+08:00', '2025-12-31T16:15:00'),
    ('2026-11-01T01:30:00-04:00', '2026-11-01T05:30:00'),
    ('2026-11-01T01:30:00-05:00', '2026-11-01T06:30:00'),
    ('2026-11-01T06:30:00Z', '2026-11-01T06:30:00'),
    ('2026-11-01T06:30:00', '2026-11-01T06:30:00'),
    (None, None),
    ('omitted', 'omitted'),
    ('2027-01-01T00:00:00Z', 'rejected'),
]


async def check_agent_window(pair, monkeypatch, incoming, expected):
    engine, factory = pair
    channel = channel_values()
    downloader, agent = str(uuid.uuid4()), str(uuid.uuid4())
    async with engine.begin() as conn:
        await conn.execute(insert(Channel).values(**channel))
        await conn.execute(insert(DownloaderInstance).values(
            id=downloader, name='synthetic UTC', type='mock', url='mock://local', download_dir='/synthetic',
        ))
        await conn.execute(insert(Agent).values(
            id=agent, name='synthetic UTC', channel_id=channel['id'], downloader_id=downloader,
        ))
    monkeypatch.setattr('app.utils.time.utcnow', lambda: datetime(2026, 12, 1))
    queue = task_queue.MemoryQueue()
    monkeypatch.setattr(task_queue, 'task_queue', queue)
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')

    async def session():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = session
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://synthetic') as client:
            args = {} if incoming == 'omitted' else {'json': {'scan_since': incoming}}
            response = await client.post(f'/api/v1/agents/{agent}/run', **args)
        if expected == 'rejected':
            assert response.status_code == 422
            assert await queue.list_jobs() == []
            return
        assert response.status_code == 200, response.text
        assert response.json()['data']['queued_at'].endswith('Z')
        # No worker executes the job: inspect the actual queue admission payload.
        job = queue._jobs_by_key[f'agent:{agent}']
        wanted = {'agent_id': agent}
        if expected != 'omitted':
            wanted['scan_since'] = expected
        assert job.payload == wanted
    finally:
        await queue.stop()


@pytest.mark.parametrize('incoming,expected', CASES)
async def test_postgres_agent_window(dedup_postgres, monkeypatch, incoming, expected):
    await check_agent_window(dedup_postgres, monkeypatch, incoming, expected)


@pytest.mark.parametrize('incoming,expected', CASES)
async def test_turso_agent_window(dedup_turso, monkeypatch, incoming, expected):
    await check_agent_window(dedup_turso, monkeypatch, incoming, expected)
