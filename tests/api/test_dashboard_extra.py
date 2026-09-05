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
    async def test_overview_does_not_contact_downloader(
        self, client, mock_transmission, setup_with_task_and_decision,
    ):
        res = await client.get("/api/v1/dashboard/overview")
        assert res.status_code == 200
        mock_transmission.list_torrents.assert_not_awaited()

        live = await client.get("/api/v1/dashboard/downloads")
        assert live.status_code == 200
        mock_transmission.list_torrents.assert_awaited_once()

    async def test_dashboard_groups_by_series_movie_unknown(
        self, client, setup_with_task_and_decision,
    ):
        res = await client.get("/api/v1/dashboard")
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["active_download_count"] >= 1
        assert data["pending_decisions"]
        assert data["pending_decisions_total"] >= len(data["pending_decisions"])
        assert data["pending_confirmations_total"] >= len(data["pending_confirmations"])
        assert data["pending_plans_total"] >= len(data["pending_plans"])

    async def test_dashboard_with_movie_group(
        self, client, setup_with_task_and_decision,
    ):
        # Just verify no error when dashboard is queried after creating data.
        res = await client.get("/api/v1/dashboard")
        assert res.status_code == 200

    async def test_pending_decision_carries_candidate_resources(
        self, client, setup_with_task_and_decision,
    ):
        """Dashboard pending decisions expose full candidate rows (raw title +
        episode_confidence) so the frontend can render an inline correction
        form instead of a separate fetch."""
        fx = setup_with_task_and_decision
        res = await client.get("/api/v1/dashboard")
        data = res.json()["data"]
        decisions = [d for d in data["pending_decisions"] if d["id"] == fx.pd_id]
        assert len(decisions) == 1
        d = decisions[0]
        assert d["candidates"] == [fx.r1, fx.r2]
        assert len(d["candidate_resources"]) == 2
        c = d["candidate_resources"][0]
        assert c["id"] == fx.r1
        assert c["title_raw"] == "[G] S - 01"
        assert "episode_confidence" in c

    async def test_pending_decisions_have_independent_pagination_and_true_total(
        self, client, db_session_factory, setup_with_task_and_decision,
    ):
        from app.models.pending_decision import PendingDecision

        fx = setup_with_task_and_decision
        now = datetime.now(UTC)
        decision_ids = [fx.pd_id]
        async with db_session_factory() as s:
            for episode in (2, 3):
                decision_id = _uuid()
                decision_ids.append(decision_id)
                s.add(PendingDecision(
                    id=decision_id,
                    agent_id=fx.a_id,
                    series_id=fx.s_id,
                    episode=episode,
                    candidates=[fx.r1, fx.r2],
                    reason=f"冲突 {episode}",
                    status="pending",
                    expires_at=now + timedelta(days=7),
                    created_at=now + timedelta(seconds=episode),
                ))
            # Legacy single-resource confirmations are not Agent decisions and
            # must not inflate the total or occupy a page slot.
            s.add(PendingDecision(
                id=_uuid(),
                agent_id=fx.a_id,
                series_id=fx.s_id,
                episode=4,
                candidates=[fx.r1],
                reason="旧版单资源修订",
                status="pending",
                expires_at=now + timedelta(days=7),
                created_at=now + timedelta(seconds=4),
            ))
            await s.commit()

        res = await client.get(
            "/api/v1/dashboard?decision_page=2&confirmation_page=1&plan_page=1&page_size=1"
        )
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["pending_decisions_total"] == 3
        assert len(data["pending_decisions"]) == 1
        assert data["pending_decisions"][0]["id"] in decision_ids

    async def test_dashboard_pending_plans(
        self, client, db_session_factory, sample_channel, sample_downloader,
        sample_series, mock_transmission,
    ):
        """Pending organize plans surface on the dashboard with ops preview."""
        from app.models.download_notification import DownloadNotification
        from app.models.download_task import DownloadTask
        from app.models.file_resource import FileResource
        from app.models.organize_plan import OrganizePlan
        from app.models.organize_plan_op import OrganizePlanOp

        async with db_session_factory() as s:
            resource = FileResource(
                id=_uuid(), channel_id=sample_channel.id, guid="g2",
                title_raw="[G] S - 02", torrent_url="magnet:?xt=urn:btih:def",
                series_id=sample_series.id,
            )
            s.add(resource)
            await s.flush()
            task = DownloadTask(
                id=_uuid(), file_resource_id=resource.id,
                downloader_id=sample_downloader.id, download_dir="/downloads/x",
                status="completed",
            )
            s.add(task)
            await s.flush()
            notification = DownloadNotification(
                id=_uuid(), download_task_id=task.id,
                payload={"notification_id": "n"},
            )
            s.add(notification)
            await s.flush()
            plan = OrganizePlan(
                id=_uuid(), notification_id=notification.id, status="pending",
                payload=notification.payload,
            )
            s.add(plan)
            await s.flush()
            s.add(OrganizePlanOp(
                id=_uuid(), plan_id=plan.id, seq=0, op_type="move",
                src="/downloads/x/a.mkv", dst="/media/a.mkv", size=100,
            ))
            await s.commit()
            plan_id = plan.id

        res = await client.get("/api/v1/dashboard?page_size=1")
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["pending_plans_total"] == 1
        plans = [p for p in data["pending_plans"] if p["id"] == plan_id]
        assert len(plans) == 1
        assert plans[0]["status"] == "pending"
        assert len(plans[0]["ops_preview"]) == 1
        assert plans[0]["ops_preview"][0]["src"] == "/downloads/x/a.mkv"

        page_two = await client.get("/api/v1/dashboard?plan_page=2&page_size=1")
        assert page_two.status_code == 200
        assert page_two.json()["data"]["pending_plans_total"] == 1
        assert page_two.json()["data"]["pending_plans"] == []

        ignored = await client.post(
            "/api/v1/dashboard/todos/ignore",
            json={"kind": "plan", "ids": [plan_id]},
        )
        assert ignored.status_code == 200
        assert ignored.json()["data"]["ignored"] == 1
        after = await client.get("/api/v1/dashboard")
        assert after.json()["data"]["pending_plans_total"] == 0


    async def test_dashboard_pending_confirmations(
        self, client, db_session_factory, sample_channel, sample_series,
    ):
        """Ambiguous-episode resources surface as actionable confirmations
        (channel + work title attached for inline correction)."""
        from app.models.file_resource import FileResource

        async with db_session_factory() as s:
            resource = FileResource(
                id=_uuid(), channel_id=sample_channel.id, guid="gc",
                title_raw="[G] Amb - 01", torrent_url="magnet:?xt=urn:btih:gh",
                series_id=sample_series.id, episode_confidence="ambiguous",
                episode=1,
            )
            s.add(resource)
            await s.commit()
            rid = resource.id

        res = await client.get("/api/v1/dashboard")
        assert res.status_code == 200
        data = res.json()["data"]
        confs = [c for c in data["pending_confirmations"] if c["resource"]["id"] == rid]
        assert len(confs) == 1
        c = confs[0]
        assert c["resource"]["episode_confidence"] == "ambiguous"
        assert "season_ambiguous" in c["kinds"]
        assert c["channel_name"] == sample_channel.name
        assert c["work_title"] == sample_series.title_cn

        ignored = await client.post(
            "/api/v1/dashboard/todos/ignore",
            json={"kind": "confirmation", "ids": [rid]},
        )
        assert ignored.status_code == 200
        assert ignored.json()["data"]["ignored"] == 1
        after = await client.get("/api/v1/dashboard")
        assert rid not in {
            item["resource"]["id"]
            for item in after.json()["data"]["pending_confirmations"]
        }

    async def test_dashboard_pending_confirmations_links_carried_multi_season(
        self, client, db_session_factory, sample_channel,
    ):
        """Links-carried multi-season packs clear the flat work FKs; the
        dashboard must eager-load the work-link table or every work-derived
        required field (content_type / is_anime / year) reads as missing and
        the pack is falsely flagged metadata_unlinked."""
        from datetime import date

        from app.models.channel import Channel
        from app.models.file_resource import FileResource
        from app.models.resource_work_link import ResourceWorkLink
        from app.models.series import TVSeries
        from app.services.required_fields import normalize_required_fields

        async with db_session_factory() as s:
            ch = await s.get(Channel, sample_channel.id)
            ch.required_metadata_fields = normalize_required_fields([])
            s1 = TVSeries(
                id=_uuid(), title_cn="怪盗Joker", season_number=1,
                content_type="tv", is_anime=True, start_date=date(2014, 10, 6),
            )
            s2 = TVSeries(
                id=_uuid(), title_cn="怪盗Joker", season_number=2,
                content_type="tv", is_anime=True, start_date=date(2015, 4, 6),
            )
            undated = TVSeries(
                id=_uuid(), title_cn="古见同学有交流障碍", season_number=1,
                content_type="tv", is_anime=True,
            )
            full = FileResource(
                id=_uuid(), channel_id=sample_channel.id, guid="lc-full",
                title_raw="[LinRip] 怪盗Joker S1-S2 [BDRemux 1080p]",
                torrent_url="magnet:?xt=urn:btih:lc1",
                search_title="Kaitou Joker",
                is_batch=True, batch_scope="multi_season", batch_seasons=[1, 2],
            )
            noyear = FileResource(
                id=_uuid(), channel_id=sample_channel.id, guid="lc-noyear",
                title_raw="[VCB] 古见同学有交流障碍 [S1 Fin]",
                torrent_url="magnet:?xt=urn:btih:lc2",
                search_title="Komi Can't Communicate",
                is_batch=True, batch_scope="multi_season", batch_seasons=[1],
            )
            s.add_all([s1, s2, undated, full, noyear])
            await s.flush()
            s.add_all([
                ResourceWorkLink(resource_id=full.id, series_id=s1.id),
                ResourceWorkLink(resource_id=full.id, series_id=s2.id),
                ResourceWorkLink(resource_id=noyear.id, series_id=undated.id),
            ])
            await s.commit()
            full_id, noyear_id = full.id, noyear.id

        res = await client.get("/api/v1/dashboard")
        assert res.status_code == 200
        confs = res.json()["data"]["pending_confirmations"]
        # Fully-resolvable pack needs no confirmation at all.
        assert full_id not in {c["resource"]["id"] for c in confs}
        # Only the genuinely unknown work year may be flagged — content_type
        # and is_anime resolve through the link table.
        entry = next(c for c in confs if c["resource"]["id"] == noyear_id)
        assert entry["kinds"] == ["required_fields_missing"]
        assert entry["missing_fields"] == ["year"]

    async def test_dashboard_can_batch_ignore_decisions(
        self, client, setup_with_task_and_decision, db_session_factory,
    ):
        from app.models.pending_decision import PendingDecision

        decision_id = setup_with_task_and_decision.pd_id
        ignored = await client.post(
            "/api/v1/dashboard/todos/ignore",
            json={"kind": "decision", "ids": [decision_id]},
        )
        assert ignored.status_code == 200
        assert ignored.json()["data"] == {
            "requested": 1, "ignored": 1, "unchanged": 0,
        }
        async with db_session_factory() as session:
            row = await session.get(PendingDecision, decision_id)
            assert row.status == "skipped"

    async def test_pending_confirmations_have_independent_pagination_and_true_total(
        self, client, db_session_factory, sample_channel,
    ):
        from app.models.file_resource import FileResource

        now = datetime.now(UTC)
        resource_ids = []
        async with db_session_factory() as s:
            for index in range(3):
                resource_id = _uuid()
                resource_ids.append(resource_id)
                s.add(FileResource(
                    id=resource_id,
                    channel_id=sample_channel.id,
                    guid=f"confirmation-page-{index}",
                    title_raw=f"Unlinked {index}",
                    torrent_url=f"magnet:?xt=urn:btih:confirmation{index}",
                    created_at=now + timedelta(seconds=index),
                ))
            await s.commit()

        res = await client.get(
            "/api/v1/dashboard?decision_page=1&confirmation_page=2&plan_page=1&page_size=2"
        )
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["pending_confirmations_total"] == 3
        assert len(data["pending_confirmations"]) == 1
        assert data["pending_confirmations"][0]["resource"]["id"] == resource_ids[0]

    async def test_dashboard_rejects_invalid_todo_pagination(self, client):
        assert (await client.get("/api/v1/dashboard?decision_page=0")).status_code == 422
        assert (await client.get("/api/v1/dashboard?page_size=101")).status_code == 422

    async def test_dashboard_stale_ambiguous_is_not_an_issue_for_batch_or_movie(
        self, client, db_session_factory, sample_channel, sample_series,
    ):
        """A stale ambiguous flag adds no season/episode issue to batches or
        movies; independent missing required fields may still surface them."""
        from app.models.file_resource import FileResource
        from app.models.movie import Movie

        async with db_session_factory() as s:
            m = Movie(id=_uuid(), title_en="M")
            s.add(m)
            batch = FileResource(
                id=_uuid(), channel_id=sample_channel.id, guid="gb",
                title_raw="[G] Batch", torrent_url="magnet:?xt=urn:btih:gb",
                series_id=sample_series.id, episode_confidence="ambiguous",
                is_batch=True, batch_scope="season", season=1,
            )
            movie_res = FileResource(
                id=_uuid(), channel_id=sample_channel.id, guid="gm",
                title_raw="[G] Movie", torrent_url="magnet:?xt=urn:btih:gm",
                movie_id=m.id, episode_confidence="ambiguous",
            )
            s.add_all([batch, movie_res])
            await s.commit()
            batch_id, movie_id = batch.id, movie_res.id

        res = await client.get("/api/v1/dashboard")
        assert res.status_code == 200
        confirmations = {
            c["resource"]["id"]: c
            for c in res.json()["data"]["pending_confirmations"]
        }
        for resource_id in (batch_id, movie_id):
            kinds = confirmations.get(resource_id, {}).get("kinds", [])
            assert "season_ambiguous" not in kinds
            assert "episode_ambiguous" not in kinds


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
                              candidates=[r1, r2], reason="冲突", status="pending",
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
