"""API tests for TVSeries CRUD endpoints."""

from __future__ import annotations

import uuid


def _uuid():
    return str(uuid.uuid4())


class TestSeriesCRUD:
    async def test_create_series(self, client):
        res = await client.post("/api/v1/series", json={
            "title_cn": "剧", "title_en": "Show",
        })
        assert res.status_code == 201
        assert res.json()["data"]["title_cn"] == "剧"

    async def test_list_series(self, client, sample_series):
        res = await client.get("/api/v1/series")
        assert res.status_code == 200
        assert res.json()["meta"]["total"] >= 1

    async def test_get_series(self, client):
        create = await client.post("/api/v1/series", json={"title_en": "S"})
        sid = create.json()["data"]["id"]
        res = await client.get(f"/api/v1/series/{sid}")
        assert res.status_code == 200

    async def test_update_series(self, client):
        create = await client.post("/api/v1/series", json={"title_en": "S"})
        sid = create.json()["data"]["id"]
        res = await client.put(f"/api/v1/series/{sid}", json={"title_cn": "剧更新"})
        assert res.status_code == 200
        assert res.json()["data"]["title_cn"] == "剧更新"

    async def test_update_marks_manually_edited_fields(self, client):
        """PUT records edited fields in manually_edited_fields for auto-scan protection."""
        create = await client.post("/api/v1/series", json={"title_en": "S"})
        sid = create.json()["data"]["id"]
        res = await client.put(
            f"/api/v1/series/{sid}",
            json={"title_cn": "剧更新", "is_anime": True},
        )
        assert res.status_code == 200
        data = res.json()["data"]
        assert set(data["manually_edited_fields"]) == {"title_cn", "is_anime"}

    async def test_update_content_type_to_movie_clears_linked_ambiguous(
        self, client, db_session_factory, sample_channel,
    ):
        """Reclassifying a work away from tv (series → movie) clears the stale
        episode_confidence='ambiguous' flag on its linked resources — the
        episode/season question no longer applies."""
        from app.models.file_resource import FileResource

        create = await client.post("/api/v1/series", json={"title_en": "S"})
        sid = create.json()["data"]["id"]
        async with db_session_factory() as s:
            r = FileResource(
                id=_uuid(), channel_id=sample_channel.id, guid=_uuid(),
                title_raw="[G] S - 01", torrent_url="magnet:?xt=urn:btih:ct",
                series_id=sid, episode_confidence="ambiguous",
            )
            s.add(r)
            await s.commit()
            rid = r.id

        res = await client.put(f"/api/v1/series/{sid}", json={"content_type": "movie"})
        assert res.status_code == 200

        async with db_session_factory() as s:
            from sqlalchemy import select
            row = (await s.execute(
                select(FileResource).where(FileResource.id == rid)
            )).scalar_one()
            assert row.episode_confidence is None

    async def test_get_404(self, client):
        res = await client.get("/api/v1/series/nope")
        assert res.status_code == 404

    async def test_update_404(self, client):
        res = await client.put("/api/v1/series/nope", json={"title_cn": "x"})
        assert res.status_code == 404

    async def test_delete_nullifies_fks(self, client):
        # Create a series and a resource pointing to it.
        s = await client.post("/api/v1/series", json={"title_en": "Series"})
        sid = s.json()["data"]["id"]
        res = await client.delete(f"/api/v1/series/{sid}")
        assert res.status_code == 200
        res2 = await client.get(f"/api/v1/series/{sid}")
        assert res2.status_code == 404

    async def test_list_search(self, client, sample_series):
        """GET /api/v1/series?search=test filters results by title."""
        # Create a second series with a distinct title that won't match "test"
        await client.post("/api/v1/series", json={
            "title_cn": "无关剧集", "title_en": "Unrelated Show",
        })
        # Search for "test" — should match sample_series (title_en="Test Series")
        res = await client.get("/api/v1/series", params={"search": "test"})
        assert res.status_code == 200
        body = res.json()
        assert body["meta"]["total"] >= 1
        titles = [item["title_en"] for item in body["data"]]
        assert "Test Series" in titles
        assert "Unrelated Show" not in titles

        # Search for Chinese title
        res2 = await client.get("/api/v1/series", params={"search": "测试"})
        assert res2.status_code == 200
        body2 = res2.json()
        assert body2["meta"]["total"] >= 1
        cn_titles = [item["title_cn"] for item in body2["data"]]
        assert "测试剧集" in cn_titles

        # Search for something that matches nothing
        res3 = await client.get("/api/v1/series", params={"search": "zzzznonexistent"})
        assert res3.status_code == 200
        assert res3.json()["meta"]["total"] == 0

    async def test_get_series_detail(self, client, db_session, sample_series, sample_channel, sample_downloader):
        """GET /api/v1/series/{id} returns episodes, resources, task_count, agent_work_count."""
        from app.models.agent import Agent
        from app.models.agent_work import AgentWork
        from app.models.download_task import DownloadTask
        from app.models.episode import Episode
        from app.models.file_resource import FileResource

        sid = sample_series.id

        # Create episodes
        ep1 = Episode(id=_uuid(), series_id=sid, season=1, episode=1, title="Pilot")
        ep2 = Episode(id=_uuid(), series_id=sid, season=1, episode=2, title="Second")
        db_session.add_all([ep1, ep2])

        # Create a file resource linked to this series
        fr = FileResource(
            id=_uuid(),
            channel_id=sample_channel.id,
            guid="test-guid-series-001",
            title_raw="[SubGroup] Test Series - 01 [1080p]",
            title_cn="测试剧集",
            title_en="Test Series",
            search_title="Test Series",
            episode=1,
            season=1,
            resolution="1080p",
            torrent_url="magnet:?xt=urn:btih:abc123",
            series_id=sid,
        )
        db_session.add(fr)
        await db_session.flush()

        # Create an agent (needed for download task and agent work)
        agent = Agent(
            id=_uuid(),
            name="Test Agent",
            channel_id=sample_channel.id,
            downloader_id=sample_downloader.id,
            status="active",
            scope_channel_wide=False,
            conflict_resolution="ask",
        )
        db_session.add(agent)
        await db_session.flush()

        # Create an agent work referencing this series
        aw = AgentWork(
            id=_uuid(),
            agent_id=agent.id,
            content_type="tv",
            series_id=sid,
            enable_episode_dedup=True,
        )
        db_session.add(aw)

        # Create a download task for the resource
        task = DownloadTask(
            id=_uuid(),
            agent_id=agent.id,
            file_resource_id=fr.id,
            downloader_id=sample_downloader.id,
            download_dir="/downloads/test",
            status="downloading",
            progress=0.5,
        )
        db_session.add(task)
        await db_session.flush()
        await db_session.commit()

        # Now fetch the detail endpoint
        res = await client.get(f"/api/v1/series/{sid}")
        assert res.status_code == 200
        data = res.json()["data"]

        # Verify episodes
        assert "episodes" in data
        assert len(data["episodes"]) == 2
        ep_numbers = {e["episode"] for e in data["episodes"]}
        assert ep_numbers == {1, 2}

        # Verify resources
        assert "resources" in data
        assert len(data["resources"]) >= 1
        assert data["resources"][0]["series_id"] == sid

        # Verify resource_count
        assert "resource_count" in data
        assert data["resource_count"] >= 1

        # Verify task_count
        assert "task_count" in data
        assert data["task_count"] == 1

        # Verify agent_work_count
        assert "agent_work_count" in data
        assert data["agent_work_count"] == 1

    async def test_get_series_source_link_fields(self, client, db_session, sample_series):
        """GET /api/v1/series/{id} exposes canonical_name/wikipedia_url at top level."""
        sample_series.canonical_name = "Test Series"
        sample_series.wikipedia_url = "https://en.wikipedia.org/wiki/Test_Series"
        await db_session.commit()

        res = await client.get(f"/api/v1/series/{sample_series.id}")
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["canonical_name"] == "Test Series"
        assert data["wikipedia_url"] == "https://en.wikipedia.org/wiki/Test_Series"

    async def test_get_series_source_links(self, client, db_session, sample_series):
        """GET /api/v1/series/{id} computes source_links via the site registry."""
        sample_series.external_id = "TMDB:82684; IMDb:tt0944947"
        sample_series.external_source = "llm_search"
        await db_session.commit()

        res = await client.get(f"/api/v1/series/{sample_series.id}")
        assert res.status_code == 200
        links = res.json()["data"]["source_links"]
        assert [(link["source"], link["url"]) for link in links] == [
            ("tmdb", "https://www.themoviedb.org/tv/82684"),
            ("imdb", "https://www.imdb.com/title/tt0944947/"),
        ]

    async def test_get_series_source_links_include_identity_bag(
        self, client, db_session, sample_series
    ):
        """P3: source_links aggregates every id in the work's identity bag,
        deduped against the primary external_id."""
        from app.services.external_ids import add_external_id

        sample_series.external_id = "wikipedia:7727654"
        sample_series.external_source = "wikipedia"
        await db_session.commit()
        await add_external_id(db_session, "series", sample_series.id,
                              "wikipedia", "wikipedia:7727654")  # primary, deduped
        await add_external_id(db_session, "series", sample_series.id,
                              "wikipedia", "wikipedia:70545449")  # langlink page
        await add_external_id(db_session, "series", sample_series.id,
                              "tmdb", "tmdb:82684")
        await db_session.commit()

        res = await client.get(f"/api/v1/series/{sample_series.id}")
        assert res.status_code == 200
        links = res.json()["data"]["source_links"]
        assert {(link["source"], link["url"]) for link in links} == {
            ("wikipedia", "https://en.wikipedia.org/?curid=7727654"),
            ("wikipedia", "https://en.wikipedia.org/?curid=70545449"),
            ("tmdb", "https://www.themoviedb.org/tv/82684"),
        }


class TestSeriesIdentityManualEdit:
    """PUT /series/{id} with content_type / external_id / external_source."""

    async def test_update_new_fields_marked_manually_edited(self, client):
        create = await client.post("/api/v1/series", json={"title_en": "S"})
        sid = create.json()["data"]["id"]
        res = await client.put(
            f"/api/v1/series/{sid}",
            json={
                "content_type": "movie",
                "external_id": "tmdb:123",
                "external_source": "tmdb",
            },
        )
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["content_type"] == "movie"
        assert data["external_id"] == "tmdb:123"
        assert data["external_source"] == "tmdb"
        assert {"content_type", "external_id", "external_source"} <= set(
            data["manually_edited_fields"]
        )

    async def test_invalid_content_type_422(self, client):
        create = await client.post("/api/v1/series", json={"title_en": "S"})
        sid = create.json()["data"]["id"]
        res = await client.put(
            f"/api/v1/series/{sid}", json={"content_type": "podcast"},
        )
        assert res.status_code == 422

    async def test_identity_change_bags_old_external_id(
        self, client, db_session_factory,
    ):
        """Overwriting the primary external id keeps the old one in the
        identity bag so later upserts still converge on this work."""
        from sqlalchemy import select

        from app.models.series import TVSeries
        from app.models.work_external_id import WorkExternalId

        sid = _uuid()
        async with db_session_factory() as s:
            s.add(TVSeries(
                id=sid, title_en="Bagged", content_type="tv",
                external_id="tmdb:111", external_source="tmdb",
            ))
            await s.commit()

        res = await client.put(
            f"/api/v1/series/{sid}", json={"external_id": "tmdb:222"},
        )
        assert res.status_code == 200
        assert res.json()["data"]["external_id"] == "tmdb:222"

        async with db_session_factory() as s:
            rows = (await s.execute(
                select(WorkExternalId).where(
                    WorkExternalId.work_type == "series",
                    WorkExternalId.work_id == sid,
                )
            )).scalars().all()
        assert {(r.source, r.external_id) for r in rows} == {("tmdb", "tmdb:111")}

    async def test_identity_unchanged_does_not_duplicate_bag(
        self, client, db_session_factory,
    ):
        from sqlalchemy import select

        from app.models.series import TVSeries
        from app.models.work_external_id import WorkExternalId

        sid = _uuid()
        async with db_session_factory() as s:
            s.add(TVSeries(
                id=sid, title_en="Same", content_type="tv",
                external_id="tmdb:111", external_source="tmdb",
            ))
            await s.commit()

        # Re-send the same identity plus an unrelated field — nothing to bag.
        res = await client.put(
            f"/api/v1/series/{sid}",
            json={"external_id": "tmdb:111", "title_cn": "同"},
        )
        assert res.status_code == 200
        async with db_session_factory() as s:
            rows = (await s.execute(
                select(WorkExternalId).where(
                    WorkExternalId.work_type == "series",
                    WorkExternalId.work_id == sid,
                )
            )).scalars().all()
        assert rows == []
