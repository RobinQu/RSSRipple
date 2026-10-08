"""Queue snapshots: actual providers with synthetic in-memory Redis history."""
from datetime import datetime
from types import SimpleNamespace

import fakeredis.aioredis
import httpx
import pytest
from fastapi import FastAPI

from app.api.v1 import queue as queue_api
from app.services import task_queue


@pytest.mark.parametrize('backend', ['memory', 'redis'])
async def test_queue_http_timestamps_and_opaque_result(monkeypatch, backend):
    stamp = datetime(2026, 11, 1, 6, 30)
    raw_text = '2026-11-01T01:30:00-05:00'
    if backend == 'memory':
        queue = task_queue.MemoryQueue()
        monkeypatch.setattr(task_queue, 'utcnow', lambda: stamp)
        await queue.enqueue('synthetic', 'utc', {})
        await queue.update_progress('utc', {'text': raw_text})
    else:
        redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
        queue = task_queue.RedisQueue(redis_client=redis)
        # Preserve older naive history; only the public snapshot is normalized.
        await redis.hset('rssripple:job:utc', mapping={
            'job_id': 'synthetic', 'job_type': 'synthetic', 'key': 'utc', 'status': 'done',
            'queued_at': '2026-11-01T06:30:00',
            'started_at': '2026-11-01T01:30:00-05:00',
            'finished_at': '2026-11-01T14:30:02+08:00',
            'result': '{"text":"2026-11-01T01:30:00-05:00"}',
        })
    monkeypatch.setattr(task_queue, 'task_queue', queue)
    app = FastAPI()
    app.include_router(queue_api.router, prefix='/api/v1')
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://synthetic') as client:
            response = await client.get('/api/v1/queue/jobs')
            overview = await client.get('/api/v1/queue/overview')
        assert response.status_code == overview.status_code == 200
        job = response.json()['data'][0]
        assert job['queued_at'] == '2026-11-01T06:30:00Z'
        assert job['result'] == {'text': raw_text}
        status = await queue.status('utc')
        assert status['queued_at'] == job['queued_at']
        if backend == 'memory':
            assert job['started_at'] is job['finished_at'] is None
        else:
            assert job['started_at'] == '2026-11-01T06:30:00Z'
            assert job['finished_at'] == '2026-11-01T06:30:02Z'
            assert overview.json()['data']['by_type'][0]['avg_duration_seconds'] == 2
            assert await redis.hget('rssripple:job:utc', 'started_at') == raw_text
    finally:
        await queue.stop()


async def test_scheduler_http_converts_offset_to_utc(monkeypatch):
    job = SimpleNamespace(id='synthetic', trigger='synthetic',
                          next_run_time=datetime.fromisoformat('2026-11-01T01:30:00-05:00'))
    monkeypatch.setattr(queue_api, 'get_scheduler', lambda: SimpleNamespace(get_jobs=lambda: [job]))
    app = FastAPI()
    app.include_router(queue_api.router, prefix='/api/v1')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://synthetic') as client:
        response = await client.get('/api/v1/queue/scheduler')
    assert response.json()['data']['jobs'][0]['next_run_time'] == '2026-11-01T06:30:00Z'
