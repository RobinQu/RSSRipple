"""Real HTTP/SQL time-window selection; snapshot construction is a test boundary."""
import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import insert, select

from app.api.v1.notifications import router
from app.database import get_db
from app.models.agent import Agent
from app.models.agent_webhook import AgentWebhook
from app.models.channel import Channel
from app.models.download_notification import DownloadNotification
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.webhook_delivery import WebhookDelivery
from tests.integration.time_contract.test_storage import channel_values

WINDOWS = [
    ('2026-11-01T01:30:00-04:00', [1, 2, 3]),
    ('2026-11-01T01:30:00-05:00', [2, 3]),
    ('2026-11-01T14:30:00+08:00', [2, 3]),
    ('2026-11-01T06:30:00Z', [2, 3]),
    ('2026-11-01T06:30:00', [2, 3]),
]


async def check_window(pair, monkeypatch, route, since, expected):
    engine, factory = pair
    raw = (Path(__file__).parents[2] / 'fixtures/prod_works_v1.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == 'd11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32'
    recorded = json.loads(raw)['tables']['file_resources'][0]
    channel = channel_values()
    downloader, agent, resource, webhook = [str(uuid.uuid4()) for _ in range(4)]
    tasks, notifications, deliveries = [[str(uuid.uuid4()) for _ in range(4)] for _ in range(3)]
    times = ['05:29:59', '05:30:00', '06:30:00', '06:30:01']
    async with engine.begin() as conn:
        await conn.execute(insert(Channel).values(**channel))
        await conn.execute(insert(DownloaderInstance).values(
            id=downloader, name='synthetic window', type='mock', url='mock://local',
            download_dir='/synthetic/no-media',
        ))
        await conn.execute(insert(Agent).values(
            id=agent, name='synthetic window', channel_id=channel['id'], downloader_id=downloader,
        ))
        await conn.execute(insert(FileResource).values(
            id=resource, channel_id=channel['id'], guid=recorded['guid'],
            title_raw=recorded['title_raw'], torrent_url=recorded['torrent_url'],
        ))
        await conn.execute(insert(AgentWebhook).values(
            id=webhook, agent_id=agent, url='http://synthetic.invalid/unused', mock=True,
        ))
        for index, clock in enumerate(times):
            stamp = datetime.fromisoformat('2026-11-01T' + clock)
            await conn.execute(insert(DownloadTask).values(
                id=tasks[index], agent_id=agent, file_resource_id=resource, downloader_id=downloader,
                download_dir='/synthetic/no-media', status='completed', completed_at=stamp,
            ))
            await conn.execute(insert(DownloadNotification).values(
                id=notifications[index], agent_id=agent, download_task_id=tasks[index],
                payload={'recorded_title': recorded['title_raw'], 'original': index}, created_at=stamp,
            ))
            await conn.execute(insert(WebhookDelivery).values(
                id=deliveries[index], notification_id=notifications[index], webhook_id=webhook,
                status='failed', attempt_count=5, attempt_token='original', error_message='synthetic',
            ))
    rebuilt = []

    async def snapshot(db, task, notification_id):
        rebuilt.append(task.id)
        return {'recorded_title': recorded['title_raw'], 'rebuilt_task_id': task.id}, True

    monkeypatch.setattr('app.services.notify_service._build_snapshot', snapshot)
    app = FastAPI()
    app.include_router(router, prefix='/api/v1')

    async def session():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = session
    path = '/api/v1/notifications/retry' if route == 'retry' else f'/api/v1/agents/{agent}/notifications/regenerate'
    body = {'since': since, **({'mode': 'failed', 'agent_id': agent} if route == 'retry' else {})}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://synthetic',
    ) as client:
        response = await client.post(path, json=body)
    assert response.status_code == 200, response.text
    assert response.json()['data'] == (
        {'reset': len(expected)} if route == 'retry' else {'created': 0, 'regenerated': len(expected)}
    )
    if route == 'regenerate':
        assert rebuilt == [tasks[index] for index in expected]
    async with engine.connect() as conn:
        rows = (await conn.execute(select(WebhookDelivery.__table__))).mappings().all()
        rows = {row['id']: row for row in rows}
        for index, delivery in enumerate(deliveries):
            row = rows[delivery]
            assert row['status'] == ('pending' if index in expected else 'failed')
            assert row['attempt_count'] == (0 if index in expected else 5)
            if index not in expected:
                assert row['attempt_token'] == 'original'
                assert row['error_message'] == 'synthetic'


@pytest.mark.parametrize('route', ['retry', 'regenerate'])
@pytest.mark.parametrize('since,expected', WINDOWS)
async def test_postgres_http_window(dedup_postgres, monkeypatch, route, since, expected):
    await check_window(dedup_postgres, monkeypatch, route, since, expected)


@pytest.mark.parametrize('route', ['retry', 'regenerate'])
@pytest.mark.parametrize('since,expected', WINDOWS)
async def test_turso_http_window(dedup_turso, monkeypatch, route, since, expected):
    await check_window(dedup_turso, monkeypatch, route, since, expected)
