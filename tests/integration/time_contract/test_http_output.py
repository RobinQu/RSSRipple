"""Actual response routes with recorded text and explicit synthetic timestamps."""
import hashlib
import json
import uuid
from datetime import date, datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import insert

from app.api.v1 import api_keys, channels, dashboard, resources, works
from app.database import get_db
from app.models.agent import Agent
from app.models.api_key import ApiKey
from app.models.channel import Channel
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.schemas.common import success_response
from tests.integration.time_contract.test_storage import channel_values

STAMP = datetime(2026, 11, 1, 6, 30, 0, 123456)
ISO = '2026-11-01T06:30:00.123456Z'
ROUTES = ['channel', 'resource', 'grouped', 'metadata', 'works', 'api_keys', 'resource_files', 'dashboard', 'resource_sse']


async def check_output(pair, monkeypatch, route):
    engine, factory = pair
    raw = (Path(__file__).parents[2] / 'fixtures/prod_works_v1.json').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == 'd11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32'
    corpus = json.loads(raw)['tables']
    recorded = corpus['file_resources'][0]
    channel = {**channel_values(), 'created_at': STAMP, 'updated_at': STAMP, 'last_fetched_at': STAMP}
    resource, movie, key, downloader, agent, alternate = [str(uuid.uuid4()) for _ in range(6)]
    async with engine.begin() as conn:
        await conn.execute(insert(Channel).values(**channel))
        await conn.execute(insert(Movie).values(
            id=movie, title_cn=corpus['movies'][0]['title_cn'], content_type='movie',
            release_date=date(2026, 1, 1), created_at=STAMP, updated_at=STAMP,
        ))
        await conn.execute(insert(FileResource).values(
            id=resource, channel_id=channel['id'], guid=recorded['guid'], movie_id=movie,
            title_raw=recorded['title_raw'],
            torrent_url=('magnet:?xt=urn:btih:' + 'ab'*20 if route == 'resource_files' else recorded['torrent_url']),
            magnet_resolve_updated_at=STAMP,
            created_at=STAMP, updated_at=STAMP, published_at=STAMP, metadata_matched_at=STAMP,
        ))
        await conn.execute(insert(DownloaderInstance).values(
            id=downloader, name='synthetic UTC', type='mock', url='mock://local', download_dir='/synthetic',
        ))
        await conn.execute(insert(Agent).values(
            id=agent, name='synthetic UTC', channel_id=channel['id'], downloader_id=downloader,
        ))
        other = corpus['file_resources'][1]
        await conn.execute(insert(FileResource).values(
            id=alternate, channel_id=channel['id'], guid=other['guid'], movie_id=movie,
            title_raw=other['title_raw'], torrent_url=other['torrent_url'],
            created_at=STAMP, updated_at=STAMP, published_at=STAMP,
        ))
        await conn.execute(insert(PendingDecision).values(
            id=str(uuid.uuid4()), agent_id=agent, movie_id=movie,
            decision_key='movie:' + movie, candidates=[resource, alternate], reason='synthetic time boundary',
            created_at=STAMP, updated_at=STAMP,
        ))
        await conn.execute(insert(ApiKey).values(
            id=key, name='synthetic UTC key', prefix='rr_test', key_hash='0'*64, created_at=STAMP,
        ))

    async def no_external_metadata(*args, **kwargs):
        pass

    monkeypatch.setattr(resources, 'fetch_and_link_metadata', no_external_metadata)

    async def no_external_files(*args, **kwargs):
        return [], 'none'

    monkeypatch.setattr(resources, '_resolve_resource_files', no_external_files)
    if route == 'resource_sse':
        from types import SimpleNamespace
        from unittest.mock import AsyncMock

        from app.services import task_queue

        result = {'at': STAMP, 'date': date(2026, 1, 1), 'raw_text': '2026-11-01T01:30:00-05:00'}
        state = AsyncMock(side_effect=[{'status': 'running'}, {'status': 'done', 'result': result}])
        monkeypatch.setattr(task_queue, 'task_queue', SimpleNamespace(status=state))
    app = FastAPI()
    for module in [channels, resources, works, api_keys, dashboard]:
        app.include_router(module.router, prefix='/api/v1')

    async def session():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_db] = session
    paths = {
        'channel': f'/channels/{channel["id"]}', 'resource': f'/resources/{resource}',
        'grouped': f'/channels/{channel["id"]}/resources?grouped=true',
        'metadata': f'/resources/{resource}/metadata', 'works': '/works', 'api_keys': '/api-keys',
        'resource_files': f'/resources/{resource}/files', 'dashboard': '/dashboard/overview',
        'resource_sse': f'/resources/{resource}/analyze-batch-stream',
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://synthetic') as client:
        response = await client.request('POST' if route == 'resource_sse' else 'GET', '/api/v1' + paths[route])
    assert response.status_code == 200, response.text
    if route == 'resource_sse':
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
        assert events[-1] == {'type': 'result', 'at': ISO, 'date': '2026-01-01',
                              'raw_text': '2026-11-01T01:30:00-05:00'}
        return
    data = response.json()['data']
    if route == 'resource_files':
        assert data['magnet_resolve']['updated_at'] == ISO
    elif route == 'dashboard':
        decision = data['pending_decisions'][0]
        assert decision['created_at'] == ISO
        assert {item['id'] for item in decision['candidate_resources']} == {resource, alternate}
        assert all(item['published_at'] == ISO for item in decision['candidate_resources'])
    elif route == 'channel':
        assert [data[k] for k in ['created_at', 'updated_at', 'last_fetched_at']] == [ISO]*3
    elif route == 'resource':
        assert [data[k] for k in ['created_at', 'updated_at', 'published_at', 'metadata_matched_at']] == [ISO]*4
        assert data['title_raw'] == recorded['title_raw']
        assert data['parsed_at'] is None
    elif route == 'grouped':
        group = data['groups'][0]
        assert group['last_update'] == ISO
        assert group['resources'][0]['published_at'] == ISO
    elif route == 'metadata':
        assert data['metadata_matched_at'] == ISO
        assert data['linked']['entity']['created_at'] == ISO
        assert data['linked']['entity']['release_date'] == '2026-01-01'
    elif route == 'works':
        assert data[0]['created_at'] == ISO
        assert data[0]['release_date'] == '2026-01-01'
    else:
        assert data[0]['created_at'] == ISO
        assert 'key_hash' not in data[0] and 'key' not in data[0]


@pytest.mark.parametrize('route', ROUTES)
async def test_postgres_http_output(dedup_postgres, monkeypatch, route):
    await check_output(dedup_postgres, monkeypatch, route)


@pytest.mark.parametrize('route', ROUTES)
async def test_turso_http_output(dedup_turso, monkeypatch, route):
    await check_output(dedup_turso, monkeypatch, route)


class NestedTimes(BaseModel):
    timestamp: datetime
    release_date: date
    raw_text: str
    secret: SecretStr
    excluded: str = Field(exclude=True)


async def test_nested_response_preserves_date_text_and_redaction():
    app = FastAPI()

    @app.get('/probe')
    async def probe():
        return success_response({'nested': [NestedTimes(
            timestamp=datetime.fromisoformat('2026-11-01T01:30:00.123456-05:00'),
            release_date=date(2026, 1, 1), raw_text='2026-11-01T01:30:00-05:00',
            secret='synthetic secret', excluded='must not appear',
        )], 'none': None, 'naive': STAMP})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://synthetic') as client:
        response = await client.get('/probe')
    data = response.json()['data']
    assert data == {'nested': [{
        'timestamp': ISO, 'release_date': '2026-01-01', 'raw_text': '2026-11-01T01:30:00-05:00',
        'secret': '**********',
    }], 'none': None, 'naive': ISO}


async def test_channel_sse_normalizes_actual_datetime_only(monkeypatch):
    async def entries(*args, **kwargs):
        return [{'title': 'synthetic SSE boundary'}]

    async def events(*args, **kwargs):
        yield {'type': 'result', 'at': STAMP, 'date': date(2026, 1, 1),
               'raw_text': '2026-11-01T01:30:00-05:00'}

    monkeypatch.setattr(channels, 'get_raw_entries', entries)
    monkeypatch.setattr(channels, 'analyze_feed_stream', events)
    app = FastAPI()
    app.include_router(channels.router, prefix='/api/v1')
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://synthetic') as client:
        response = await client.post('/api/v1/channels/analyze-url-stream', json={'url': 'http://synthetic.invalid/rss'})
    assert response.status_code == 200
    parsed = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
    assert parsed[-1] == {'type': 'result', 'at': ISO, 'date': '2026-01-01',
                          'raw_text': '2026-11-01T01:30:00-05:00'}
