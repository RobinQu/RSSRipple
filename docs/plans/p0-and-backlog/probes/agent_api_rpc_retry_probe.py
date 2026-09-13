"""Reviewed torrent, isolated Transmission, real RPC followed by SQL failure."""
import asyncio
import json
import os
import subprocess
import tempfile
import uuid
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

PROBE_ROOT = Path(tempfile.mkdtemp(prefix='rssripple-agent-rpc-'))
os.environ['DATABASE_URL'] = f'sqlite+aioturso:///{PROBE_ROOT / "probe.db"}'
os.environ['POSTER_CACHE_DIR'] = str(PROBE_ROOT / 'posters')

import httpx  # noqa: E402
from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

import app.database as database  # noqa: E402
import app.models  # noqa: E402,F401
from app.clients.transmission import TransmissionWrapper  # noqa: E402
from app.job_handlers import _handle_run_agent  # noqa: E402
from app.models.agent import Agent  # noqa: E402
from app.models.channel import Channel  # noqa: E402
from app.models.download_task import DownloadTask  # noqa: E402
from app.models.downloader import DownloaderInstance  # noqa: E402
from app.models.file_resource import FileResource  # noqa: E402
from app.models.series import TVSeries  # noqa: E402
from app.models.work_collection import WorkCollection  # noqa: E402
from app.services.agent_service import dispatch_download  # noqa: E402
from app.utils.time import utcnow  # noqa: E402
from tests.api.conftest import _build_test_app  # noqa: E402
from tests.metadata_corpus.dataset import ROOT, asset, digest, load_corpus  # noqa: E402
from tests.unit.test_agent_service import TEST_FIELD_MAPPING  # noqa: E402


def uid():
    return str(uuid.uuid4())


async def main():
    project = 'rssripple-v4-agent-20260913-u'
    [container] = json.loads(subprocess.check_output(['docker', 'inspect', project + '-transmission-1']))
    assert container['Config']['Labels']['com.docker.compose.project'] == project
    address = container['NetworkSettings']['Networks'][project + '_isolated']['IPAddress']
    wrapper = TransmissionWrapper(f'http://{address}:9091/transmission/rpc')
    assert await wrapper.list_torrents() == [], 'Requires a fresh disposable daemon'
    _, corpus, reviews = load_corpus(ROOT)
    case_id = 'f79ef2eb-02d5-42d3-80dc-70dd3c1d733b'
    case = next(row for row in corpus['cases'] if row['id'] == case_id)
    assert reviews[case_id]['status'] == 'confirmed'
    [assignment] = reviews[case_id]['expected']['assignments']
    assert assignment['season'] == 1 and assignment['episode_start'] == 10
    torrent = asset(ROOT, case['evidence']['torrent'])
    assert digest(torrent.read_bytes()) == torrent.stem
    engine = create_async_engine(database.normalize_database_url(os.environ['DATABASE_URL']))
    database.apply_db_pragmas(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    database.engine, database.async_session_factory = engine, factory
    accepted_ids = []
    try:
        async with engine.begin() as connection:
            await connection.execute(text("PRAGMA journal_mode='mvcc'"))
            await connection.run_sync(database.Base.metadata.create_all)
        async with factory() as db:
            channel = Channel(id=uid(), name='reviewed retry fixture', type='rss_feed',
                              url='https://example.invalid/feed', field_mapping=TEST_FIELD_MAPPING,
                              metadata_agent_enabled=False)
            downloader = DownloaderInstance(id=uid(), name='isolated Transmission', type='transmission',
                                            url=f'http://{address}:9091/transmission/rpc', download_dir='/downloads')
            collection = WorkCollection(id=uid(), title_cn='猫与龙')
            series = TVSeries(id=uid(), title_cn='猫与龙', content_type='tv', collection_id=collection.id,
                              season_number=1, start_date=date(2026, 1, 1), is_anime=True)
            db.add_all([channel, downloader, collection, series])
            await db.flush()
            watermark = utcnow() - timedelta(days=1)
            agent = Agent(id=uid(), name='RPC retry', channel_id=channel.id, downloader_id=downloader.id,
                          scope_channel_wide=True, conflict_resolution='ask', last_consumed_at=watermark)
            resource = FileResource(id=uid(), channel_id=channel.id, guid=uid(),
                                    title_raw=case['input']['title_raw'], search_title='猫与龙',
                                    series_id=series.id, season=1, episode=10, is_batch=False,
                                    torrent_url='https://example.invalid/captured.torrent', torrent_file=str(torrent))
            db.add_all([agent, resource])
            await db.commit()
            agent_id = agent.id
            resource_id = resource.id

        async def dispatch_then_fail(agent, resource, unit):
            task = await dispatch_download(agent, resource, unit)
            assert task.status == 'downloading', task.error_message
            accepted_ids.append(task.transmission_torrent_id)
            if len(accepted_ids) == 1:
                task.download_dir = None
                await unit.flush()  # Real NOT NULL failure after real RPC acceptance.
            return task

        with patch('app.services.agent_service.dispatch_download', dispatch_then_fail):
            await _handle_run_agent({'agent_id': agent_id})
            async with factory() as db:
                assert (await db.scalars(select(DownloadTask))).all() == []
                agent = await db.get(Agent, agent_id)
                assert agent.last_consumed_at == watermark and agent.last_run_status == 'failed'
            assert len(await wrapper.list_torrents()) == 1
            await _handle_run_agent({'agent_id': agent_id})
        assert len(accepted_ids) == 2 and accepted_ids[0] == accepted_ids[1]
        [live] = await wrapper.list_torrents()
        assert live['peers_connected'] == 0 and live['have_valid'] == 0
        async with factory() as db:
            [task] = (await db.scalars(select(DownloadTask))).all()
            assert task.transmission_torrent_id == live['id']
            agent = await db.get(Agent, agent_id)
            assert agent.last_run_status == 'success' and agent.last_consumed_at > watermark
        background_id = accepted_ids[0]
        api_results = []
        app = _build_test_app(factory)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            for existing in (False, True):
                accepted_ids.clear()
                label = "API edit" if existing else "API create"
                payload = {"name": label, "channel_id": channel.id, "downloader_id": downloader.id,
                           "scope_channel_wide": True}
                existing_id = None
                if existing:
                    response = await client.post("/api/v1/agents", json=payload)
                    assert response.status_code == 201, response.text
                    existing_id = response.json()["data"]["id"]
                payload.update(name=label + " saved", dispatch_resource_ids=[resource_id])

                async def save():
                    if existing:
                        return await client.put(f"/api/v1/agents/{existing_id}", json=payload)
                    return await client.post("/api/v1/agents", json=payload)

                with patch("app.services.agent_service.dispatch_download", dispatch_then_fail):
                    response = await save()
                    assert response.status_code == 500, response.text
                    assert response.json()["error"]["code"] == "INTERNAL_SERVER_ERROR"
                    async with factory() as observer:
                        assert await observer.scalar(select(Agent.id).where(Agent.name == payload["name"])) is None
                        if existing:
                            original = await observer.get(Agent, existing_id)
                            assert original.name == label and original.last_consumed_at is None
                            assert await observer.scalar(select(DownloadTask.id).where(
                                DownloadTask.agent_id == existing_id)) is None
                    response = await save()
                    assert response.status_code == (200 if existing else 201), response.text
                assert accepted_ids == [background_id, background_id]
                saved_id = response.json()["data"]["id"]
                async with factory() as observer:
                    [task] = (await observer.scalars(select(DownloadTask).where(
                        DownloadTask.agent_id == saved_id))).all()
                    assert task.transmission_torrent_id == background_id
                    assert (await observer.get(Agent, saved_id)).last_consumed_at is not None
                api_results.append({"mode": label, "first_save": 500, "retry_save": response.status_code,
                                    "rpc_acceptances": 2, "same_torrent_id": True, "persisted_tasks": 1})
        [live] = await wrapper.list_torrents()
        assert live["id"] == background_id and live["have_valid"] == 0 and live["peers_connected"] == 0
        print(json.dumps({"case_id": case_id, "torrent_sha256": torrent.stem,
                          "background_retry": "passed", "api_results": api_results,
                          "daemon_torrents": 1, "received_media_bytes": 0,
                          "failure": "real NOT NULL after real RPC", "database": str(PROBE_ROOT / "probe.db")}))
    finally:
        await engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
