"""Additional API coverage tests part 2: downloaders, tasks, series, movies."""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest


def _uuid():
    return str(uuid.uuid4())


TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"torrent_url": {"source": "link"}},
}


@pytest.fixture
async def env(client):
    with patch(
        "app.api.v1.channels.validate_rss_url",
        AsyncMock(return_value=(True, "ok", 5, 5)),
    ):
        ch = await client.post("/api/v1/channels", json={
            "name": "C2", "type": "rss_feed", "url": "https://x/rss",
            "fetch_interval": 1800, "field_mapping": TEST_FIELD_MAPPING,
            "metadata_source": "none",
        })
    dl = await client.post("/api/v1/downloaders", json={
        "name": "DL2", "type": "transmission",
        "url": "http://127.0.0.1:9091/transmission/rpc",
        "download_dir": "/downloads/rssripple",
    })
    a = await client.post("/api/v1/agents", json={
        "name": "A2", "channel_id": ch.json()["data"]["id"],
        "downloader_id": dl.json()["data"]["id"], "scope_channel_wide": True,
    })
    return SimpleNamespace(
        ch_id=ch.json()["data"]["id"],
        dl_id=dl.json()["data"]["id"],
        aid=a.json()["data"]["id"],
    )


async def _make_resource(db_session_factory, ch_id, **kw):
    from app.models.file_resource import FileResource
    rid = _uuid()
    defaults = {
        "id": rid, "channel_id": ch_id, "guid": rid + "-g",
        "title_raw": kw.pop("title_raw", "[G] R - 01"),
        "search_title": "R",
        "torrent_url": f"magnet:?xt=urn:btih:{rid}",
        "parsed_at": datetime.now(UTC),
    }
    defaults.update(kw)
    async with db_session_factory() as s:
        s.add(FileResource(**defaults))
        await s.commit()
    return rid


async def _make_task(db_session_factory, aid, rid, dl_id, **kw):
    from app.models.download_task import DownloadTask
    tid = _uuid()
    async with db_session_factory() as s:
        t = DownloadTask(
            id=tid, agent_id=aid, file_resource_id=rid, downloader_id=dl_id,
            transmission_torrent_id=kw.pop("transmission_torrent_id", 42),
            download_dir=kw.pop("download_dir", "/downloads/rssripple"),
            status=kw.pop("status", "downloading"),
            progress=0.3, download_speed=0, upload_speed=0,
            retry_count=0, max_retries=3, **kw,
        )
        s.add(t)
        await s.commit()
    return tid


# ---------------------------------------------------------------------------
# Downloaders
# ---------------------------------------------------------------------------


class TestDownloadersMore:
    async def test_create_downloader_with_password(self, client):
        r = await client.post("/api/v1/downloaders", json={
            "name": "DLp", "type": "transmission",
            "url": "http://127.0.0.1:9091/transmission/rpc",
            "password": "secret",
            "download_dir": "/downloads/rssripple",
        })
        assert r.status_code == 201

    async def test_update_downloader_404(self, client):
        r = await client.put("/api/v1/downloaders/nope", json={"name": "X"})
        assert r.status_code == 404

    async def test_delete_downloader_404(self, client):
        r = await client.delete("/api/v1/downloaders/nope")
        assert r.status_code == 404

    async def test_list_downloader_tasks_empty(self, client, sample_downloader):
        r = await client.get(f"/api/v1/downloaders/{sample_downloader.id}/tasks")
        assert r.status_code == 200

    async def test_list_torrents_success(self, client, sample_downloader, mock_transmission):
        mock_transmission.list_torrents.return_value = [
            {"id": 1, "name": "x", "percentDone": 0.5}
        ]
        r = await client.get(f"/api/v1/downloaders/{sample_downloader.id}/torrents")
        assert r.status_code == 200
        assert len(r.json()["data"]) == 1


# ---------------------------------------------------------------------------
# Series
# ---------------------------------------------------------------------------


class TestSeriesMore:
    async def test_delete_series_cascade(self, client, db_session_factory):
        from app.models.file_resource import FileResource
        from app.models.agent_work import AgentWork
        from app.models.pending_decision import PendingDecision
        from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
        from app.models.channel import Channel
        from app.models.agent import Agent
        from app.models.downloader import DownloaderInstance
        from sqlalchemy import select

        s = await client.post("/api/v1/series", json={"title_cn": "剧D", "title_en": "SD"})
        sid = s.json()["data"]["id"]
        ch_id = _uuid(); dl_id = _uuid(); a_id = _uuid(); w_id = _uuid()
        rid = _uuid(); pd_id = _uuid(); mp_id = _uuid()
        async with db_session_factory() as ss:
            ss.add_all([
                Channel(id=ch_id, name="c", type="rss_feed", url="u",
                        status="active", field_mapping=TEST_FIELD_MAPPING,
                        metadata_source="none",
                        title_extraction_method="none"),
                DownloaderInstance(id=dl_id, name="d", type="transmission", url="u", download_dir="/downloads/rssripple"),
                Agent(id=a_id, name="a", channel_id=ch_id, downloader_id=dl_id,
                      scope_channel_wide=True),
                AgentWork(id=w_id, agent_id=a_id, content_type="tv", series_id=sid),
                FileResource(id=rid, channel_id=ch_id, guid="g", title_raw="r",
                             torrent_url="m:", series_id=sid, search_title="r"),
                PendingDecision(id=pd_id, agent_id=a_id, status="pending",
                                candidates=[rid], series_id=sid, reason="x"),
                ChannelRawTitleMapping(id=mp_id, channel_id=ch_id, raw_title="r",
                                       series_id=sid),
            ])
            await ss.commit()
        r = await client.delete(f"/api/v1/series/{sid}")
        assert r.status_code == 200
        async with db_session_factory() as ss:
            fr = (await ss.execute(select(FileResource).where(FileResource.id == rid))).scalar_one()
            assert fr.series_id is None
            w = (await ss.execute(select(AgentWork).where(AgentWork.id == w_id))).scalar_one_or_none()
            assert w is None

    async def test_list_series_populated(self, client):
        await client.post("/api/v1/series", json={"title_en": "S1"})
        r = await client.get("/api/v1/series")
        assert r.status_code == 200
        assert r.json()["meta"]["total"] >= 1

    async def test_delete_series_404(self, client):
        r = await client.delete("/api/v1/series/nope")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Movies
# ---------------------------------------------------------------------------


class TestMoviesMore:
    async def test_delete_movie_cascade(self, client, db_session_factory):
        from app.models.file_resource import FileResource
        from app.models.agent_work import AgentWork
        from app.models.pending_decision import PendingDecision
        from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
        from app.models.channel import Channel
        from app.models.agent import Agent
        from app.models.downloader import DownloaderInstance
        from sqlalchemy import select

        m = await client.post("/api/v1/movies", json={"title_cn": "电影D", "title_en": "MD"})
        mid = m.json()["data"]["id"]
        ch_id = _uuid(); dl_id = _uuid(); a_id = _uuid(); w_id = _uuid()
        rid = _uuid(); pd_id = _uuid(); mp_id = _uuid()
        async with db_session_factory() as ss:
            ss.add_all([
                Channel(id=ch_id, name="c", type="rss_feed", url="u",
                        status="active", field_mapping=TEST_FIELD_MAPPING,
                        metadata_source="none",
                        title_extraction_method="none"),
                DownloaderInstance(id=dl_id, name="d", type="transmission", url="u", download_dir="/downloads/rssripple"),
                Agent(id=a_id, name="a", channel_id=ch_id, downloader_id=dl_id,
                      scope_channel_wide=True),
                AgentWork(id=w_id, agent_id=a_id, content_type="movie", movie_id=mid),
                FileResource(id=rid, channel_id=ch_id, guid="g", title_raw="r",
                             torrent_url="m:", movie_id=mid, search_title="r"),
                PendingDecision(id=pd_id, agent_id=a_id, status="pending",
                                candidates=[rid], movie_id=mid, reason="x"),
                ChannelRawTitleMapping(id=mp_id, channel_id=ch_id, raw_title="r",
                                       movie_id=mid),
            ])
            await ss.commit()
        r = await client.delete(f"/api/v1/movies/{mid}")
        assert r.status_code == 200
        async with db_session_factory() as ss:
            fr = (await ss.execute(select(FileResource).where(FileResource.id == rid))).scalar_one()
            assert fr.movie_id is None
            w = (await ss.execute(select(AgentWork).where(AgentWork.id == w_id))).scalar_one_or_none()
            assert w is None

    async def test_list_movies_populated(self, client):
        await client.post("/api/v1/movies", json={"title_en": "M1"})
        r = await client.get("/api/v1/movies")
        assert r.status_code == 200
        assert r.json()["meta"]["total"] >= 1

    async def test_delete_movie_404(self, client):
        r = await client.delete("/api/v1/movies/nope")
        assert r.status_code == 404

    async def test_update_movie_404(self, client):
        r = await client.put("/api/v1/movies/nope", json={"title_en": "X"})
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


class TestTasksMore:
    async def test_task_pause_no_downloader(self, client, env, db_session_factory):
        rid = await _make_resource(db_session_factory, env.ch_id)
        tid = await _make_task(db_session_factory, env.aid, rid, None)
        r = await client.post(f"/api/v1/tasks/{tid}/pause")
        assert r.status_code == 200
        assert r.json()["data"]["message"] == "failed"

    async def test_task_resume_no_downloader(self, client, env, db_session_factory):
        rid = await _make_resource(db_session_factory, env.ch_id)
        tid = await _make_task(db_session_factory, env.aid, rid, None)
        r = await client.post(f"/api/v1/tasks/{tid}/resume")
        assert r.status_code == 200
        assert r.json()["data"]["message"] == "failed"

    async def test_task_retry_no_downloader(self, client, env, db_session_factory):
        rid = await _make_resource(db_session_factory, env.ch_id)
        tid = await _make_task(db_session_factory, env.aid, rid, None)
        r = await client.post(f"/api/v1/tasks/{tid}/retry")
        assert r.status_code == 200
        assert r.json()["data"]["message"] == "failed"

    async def test_task_retry_no_resource(self, client, env, db_session_factory, monkeypatch):
        """Retry when file_resource no longer exists returns 'failed'."""
        rid = await _make_resource(db_session_factory, env.ch_id)
        tid = await _make_task(db_session_factory, env.aid, rid, env.dl_id)
        # Patch _apply_torrent_action to return False (resource missing)
        from app.api.v1 import tasks as tmod
        async def fake_apply(db, task, action, **kw):
            # simulate failure when trying to look up resource
            return False
        monkeypatch.setattr(tmod, "_apply_torrent_action", fake_apply)
        r = await client.post(f"/api/v1/tasks/{tid}/retry")
        assert r.status_code == 200
        assert r.json()["data"]["message"] == "failed"

    async def test_task_pause_exception(self, client, env, db_session_factory, mock_transmission):
        mock_transmission.pause_torrent.side_effect = RuntimeError("x")
        rid = await _make_resource(db_session_factory, env.ch_id)
        tid = await _make_task(db_session_factory, env.aid, rid, env.dl_id)
        r = await client.post(f"/api/v1/tasks/{tid}/pause")
        assert r.status_code == 200
        assert r.json()["data"]["message"] == "failed"

    async def test_task_delete_no_torrent_id(self, client, env, db_session_factory):
        rid = await _make_resource(db_session_factory, env.ch_id)
        tid = await _make_task(db_session_factory, env.aid, rid, env.dl_id,
                               transmission_torrent_id=None)
        r = await client.delete(f"/api/v1/tasks/{tid}")
        assert r.status_code == 200
        assert r.json()["data"]["deleted"] is True

    async def test_task_delete_with_data_flag(self, client, env, db_session_factory, mock_transmission):
        rid = await _make_resource(db_session_factory, env.ch_id)
        tid = await _make_task(db_session_factory, env.aid, rid, env.dl_id)
        r = await client.delete(f"/api/v1/tasks/{tid}?delete_data=true")
        assert r.status_code == 200
        mock_transmission.remove_torrent.assert_awaited()

    async def test_list_agent_tasks_with_status_filter(self, client, env, db_session_factory):
        rid = await _make_resource(db_session_factory, env.ch_id)
        await _make_task(db_session_factory, env.aid, rid, env.dl_id, status="completed")
        await _make_task(db_session_factory, env.aid, rid, env.dl_id, status="downloading")
        r = await client.get(f"/api/v1/agents/{env.aid}/tasks?status=completed")
        assert r.status_code == 200
        assert r.json()["meta"]["total"] == 1

    async def test_resume_marks_queued(self, client, env, db_session_factory, mock_transmission):
        rid = await _make_resource(db_session_factory, env.ch_id)
        tid = await _make_task(db_session_factory, env.aid, rid, env.dl_id, status="paused")
        r = await client.post(f"/api/v1/tasks/{tid}/resume")
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "queued"

    async def test_pause_failed_response(self, client, env, db_session_factory, mock_transmission):
        mock_transmission.pause_torrent.return_value = False
        rid = await _make_resource(db_session_factory, env.ch_id)
        tid = await _make_task(db_session_factory, env.aid, rid, env.dl_id)
        r = await client.post(f"/api/v1/tasks/{tid}/pause")
        assert r.status_code == 200
        assert r.json()["data"]["message"] == "failed"

    async def test_list_agents_counts_tasks(self, client, env, db_session_factory):
        rid = await _make_resource(db_session_factory, env.ch_id)
        await _make_task(db_session_factory, env.aid, rid, env.dl_id)
        r = await client.get("/api/v1/agents")
        assert r.status_code == 200
        items = r.json()["data"]
        our = next(i for i in items if i["id"] == env.aid)
        assert our["active_task_count"] == 1
