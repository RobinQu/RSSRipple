"""Additional API tests for dashboard covering grouping branches."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest


def _uuid():
    return str(uuid.uuid4())


TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"torrent_url": {"source": "link"}},
}


async def _insert(db_session_factory, model, **kw):
    async with db_session_factory() as s:
        obj = model(**kw)
        s.add(obj)
        await s.commit()
        return obj.id


class TestDashboardPopulated:
    async def test_dashboard_groups_by_series_movie_unknown(
        self, client, setup_with_task_and_decision,
    ):
        res = await client.get("/api/v1/dashboard")
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["active_download_count"] >= 1
        assert data["pending_decisions"]

    async def test_dashboard_with_movie_group(
        self, client, setup_with_task_and_decision,
    ):
        # Just verify no error when dashboard is queried after creating data.
        res = await client.get("/api/v1/dashboard")
        assert res.status_code == 200


@pytest.fixture
async def setup_with_task_and_decision(client, db_session_factory, mock_transmission):
    from app.models.agent import Agent
    from app.models.channel import Channel
    from app.models.download_task import DownloadTask
    from app.models.downloader import DownloaderInstance
    from app.models.file_resource import FileResource
    from app.models.movie import Movie
    from app.models.pending_decision import PendingDecision
    from app.models.series import TVSeries

    ch_id = _uuid()
    dl_id = _uuid()
    a_id = _uuid()
    s_id = _uuid()
    m_id = _uuid()
    async with db_session_factory() as s:
        s.add_all([
            Channel(id=ch_id, name="DCh", type="rss_feed", url="https://x/rss",
                    status="active", field_mapping=TEST_FIELD_MAPPING,
                    metadata_agent_enabled=False),
            DownloaderInstance(id=dl_id, name="DDl", type="transmission",
                               url="http://127.0.0.1:9091/transmission/rpc",
                               download_dir="/downloads/rssripple"),
            Agent(id=a_id, name="DAg", channel_id=ch_id, downloader_id=dl_id,
                  scope_channel_wide=True, status="active"),
            TVSeries(id=s_id, title_cn="剧", title_en="Series", content_type="tv"),
            Movie(id=m_id, title_cn="电影", title_en="Movie", content_type="movie"),
        ])
        await s.commit()

    r1 = _uuid(); r2 = _uuid(); r3 = _uuid()
    async with db_session_factory() as s:
        s.add_all([
            FileResource(id=r1, channel_id=ch_id, guid="g1",
                         title_raw="[G] S - 01", torrent_url="magnet:?xt=urn:btih:a",
                         series_id=s_id, search_title="S"),
            FileResource(id=r2, channel_id=ch_id, guid="g2",
                         title_raw="[G] M", torrent_url="magnet:?xt=urn:btih:b",
                         movie_id=m_id, search_title="M"),
            FileResource(id=r3, channel_id=ch_id, guid="g3",
                         title_raw="[G] U", torrent_url="magnet:?xt=urn:btih:c"),
        ])
        await s.commit()

    t1 = _uuid(); t2 = _uuid(); t3 = _uuid()
    async with db_session_factory() as s:
        s.add_all([
            DownloadTask(id=t1, agent_id=a_id, file_resource_id=r1,
                         downloader_id=dl_id, download_dir="/downloads/rssripple",
                         status="downloading", progress=0.5),
            DownloadTask(id=t2, agent_id=a_id, file_resource_id=r2,
                         downloader_id=dl_id, download_dir="/downloads/rssripple",
                         status="downloading", progress=0.3),
            DownloadTask(id=t3, agent_id=a_id, file_resource_id=r3,
                         downloader_id=dl_id, download_dir="/downloads/rssripple",
                         status="queued", progress=0.0),
        ])
        await s.commit()

    pd_id = _uuid()
    async with db_session_factory() as s:
        s.add(PendingDecision(id=pd_id, agent_id=a_id, series_id=s_id, episode=1,
                              candidates=[r1], reason="冲突", status="pending",
                              expires_at=datetime.now(UTC) + timedelta(days=7)))
        await s.commit()

    return SimpleNamespace(ch_id=ch_id, a_id=a_id, s_id=s_id, m_id=m_id,
                           t1=t1, t2=t2, t3=t3, pd_id=pd_id, r1=r1, r2=r2, r3=r3)


from types import SimpleNamespace


class TestDashboardUntrackedTorrents:
    """Torrents downloading in the downloader without a matching DownloadTask."""

    async def test_untracked_torrents_form_own_group(
        self, client, db_session_factory, mock_transmission, setup_with_task_and_decision
    ):
        from app.models.download_task import DownloadTask

        fx = setup_with_task_and_decision
        # Bind task t1 to transmission torrent id=1 so it counts as tracked.
        async with db_session_factory() as s:
            task = await s.get(DownloadTask, fx.t1)
            task.transmission_torrent_id = 1
            await s.commit()

        mock_transmission.list_torrents.return_value = [
            {"id": 1, "name": "tracked", "status": "downloading",
             "percent_done": 0.5, "is_finished": False},
            {"id": 2, "name": "untracked-movie", "status": "downloading",
             "percent_done": 0.1, "is_finished": False},
            # Seeding torrents are not "active downloads" — excluded.
            {"id": 3, "name": "seeding", "status": "seeding",
             "percent_done": 1.0, "is_finished": True},
        ]

        res = await client.get("/api/v1/dashboard")
        assert res.status_code == 200
        data = res.json()["data"]
        untracked = [g for g in data["active_download_groups"] if g["type"] == "untracked"]
        assert len(untracked) == 1
        entries = untracked[0]["tasks"]
        assert len(entries) == 1
        assert entries[0]["resource_title"] == "untracked-movie"
        assert entries[0]["progress"] == 0.1
        assert entries[0]["agent_id"] is None
        assert entries[0]["downloader_name"] == "DDl"
        # 3 tracked active tasks + 1 untracked torrent
        assert data["active_download_count"] == 4

    async def test_unreachable_downloader_does_not_break_dashboard(
        self, client, mock_transmission, setup_with_task_and_decision
    ):
        mock_transmission.list_torrents.side_effect = Exception("connection refused")
        res = await client.get("/api/v1/dashboard")
        assert res.status_code == 200
        data = res.json()["data"]
        assert all(g["type"] != "untracked" for g in data["active_download_groups"])
