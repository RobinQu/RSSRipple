"""Known-order HTTP pagination over captured candidate identities and synthetic history."""
import hashlib
import json
import math
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.models  # noqa: F401
from app.api.v1.decisions import _choice_decision_filter, router
from app.database import Base, apply_db_pragmas, get_db, normalize_database_url
from app.models.agent import Agent
from app.models.channel import Channel
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.services.decision_store import choice_identity

CORPUS = Path(__file__).parents[2] / 'fixtures/prod_works_v1.json'
CORPUS_SHA = 'd11651d2162ced23e8d919af0bff2d9f316e203234cc854909ba5f444a35ec32'
MOVIE_ID = '064abd83-8804-4591-8a08-6e116009fa7a'


async def seed_history(engine, count, padding):
    raw = CORPUS.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == CORPUS_SHA
    tables = json.loads(raw)['tables']
    captured = [row for row in tables['file_resources'] if row.get('movie_id') == MOVIE_ID][:2]
    assert len(captured) == 2
    movie = next(row for row in tables['movies'] if row['id'] == MOVIE_ID)
    channel, downloader, other_agent, agent = [str(uuid.uuid4()) for _ in range(4)]
    now = datetime(2026, 9, 4, 12)
    key, scope = choice_identity('movie', MOVIE_ID, None, None)
    decisions = [dict(
        id=str(uuid.uuid4()), agent_id=other_agent if i < 16 else agent,
        movie_id=MOVIE_ID, decision_key=key, decision_scope=scope,
        # Historical repeated decisions are legal; pending uniqueness remains intact.
        status='decided', candidates=[row['id'] for row in captured],
        reason=f'Synthetic historical choice {i}: ' + 'x' * padding,
        created_at=now - timedelta(seconds=i),
    ) for i in range(count)]
    async with engine.begin() as connection:
        await connection.execute(insert(Channel), dict(
            id=channel, name='Synthetic pagination channel', type='rss_feed',
            url='https://example.invalid/feed', field_mapping={},
        ))
        await connection.execute(insert(DownloaderInstance), dict(
            id=downloader, name='Synthetic pagination downloader', type='mock',
            url='https://example.invalid/rpc', download_dir='/synthetic',
        ))
        await connection.execute(insert(Agent), [dict(
            id=identity, name='Synthetic history owner', channel_id=channel,
            downloader_id=downloader, scope_channel_wide=True,
        ) for identity in [other_agent, agent]])
        await connection.execute(insert(Movie), dict(
            id=MOVIE_ID, title_cn=movie.get('title_cn'), title_en=movie.get('title_en'),
            external_source=movie.get('external_source'), external_id=movie.get('external_id'),
        ))
        await connection.execute(insert(FileResource), [dict(
            id=row['id'], channel_id=channel, guid=row['guid'], title_raw=row['title_raw'],
            movie_id=MOVIE_ID, torrent_url=f'https://example.invalid/{row["id"]}.torrent',
        ) for row in captured])
        for offset in range(0, len(decisions), 256):
            await connection.execute(insert(PendingDecision), decisions[offset:offset + 256])
    return agent, decisions[16:], captured


@pytest.mark.parametrize('count,padding,indexed', [
    (64, 512, False), (8192, 0, False), (8192, 512, False), (8192, 512, True),
])
async def test_captured_candidate_history_pages_follow_known_order(
    tmp_path, count, padding, indexed, record_property,
):
    engine = create_async_engine(normalize_database_url(f'sqlite+aioturso:///{tmp_path / "history.db"}'))
    apply_db_pragmas(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
            await connection.execute(text('PRAGMA journal_mode=mvcc'))
        agent, expected, captured = await seed_history(engine, count, padding)
        async with engine.begin() as connection:
            if indexed:
                # Test-only control; never mutate Base.metadata or add a production index.
                await connection.execute(text(
                    'CREATE INDEX test_decision_order ON pending_decisions(agent_id,created_at)'
                ))
            await connection.execute(text('ANALYZE'))
        query = select(PendingDecision.__table__).where(
            PendingDecision.agent_id == agent, _choice_decision_filter(),
        ).order_by(PendingDecision.created_at.desc()).limit(20)
        sql = str(query.compile(dialect=engine.dialect, compile_kwargs={'literal_binds': True}))
        async with engine.connect() as connection:
            plan = [list(row) for row in await connection.execute(text('EXPLAIN QUERY PLAN ' + sql))]
            names = set(await connection.scalars(text(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='pending_decisions'"
            )))
        assert ('test_decision_order' in names) == indexed
        record_property('corpus_sha256', CORPUS_SHA)
        record_property('query_plan', json.dumps(plan))
        record_property('synthetic_history_rows', count)

        async def database():
            async with factory() as session:
                yield session

        app = FastAPI()
        app.include_router(router, prefix='/api/v1')
        app.dependency_overrides[get_db] = database
        last_page = math.ceil(len(expected) / 20)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://synthetic') as client:
            for page in sorted({1, 2, last_page, last_page + 1}):
                response = await client.get(f'/api/v1/agents/{agent}/decisions', params={'page': page, 'page_size': 20})
                assert response.status_code == 200, response.text
                payload = response.json()
                assert payload['meta']['total'] == len(expected)
                wanted = expected[(page - 1) * 20:page * 20]
                assert [row['id'] for row in payload['data']] == [row['id'] for row in wanted]
                for row in payload['data']:
                    assert row['candidates'] == [item['id'] for item in captured]
                    assert {item['id']: item['title_raw'] for item in row['candidate_resources']} == {
                        item['id']: item['title_raw'] for item in captured
                    }
    finally:
        await engine.dispose()
