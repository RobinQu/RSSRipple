"""API tests for Movie CRUD endpoints."""

from __future__ import annotations

import uuid


def _uuid():
    return str(uuid.uuid4())


class TestMoviesCRUD:
    async def test_create_movie(self, client):
        res = await client.post("/api/v1/movies", json={
            "title_cn": "电影", "title_en": "Movie",
        })
        assert res.status_code == 201
        assert res.json()["data"]["title_en"] == "Movie"

    async def test_list_movies(self, client, sample_movie):
        res = await client.get("/api/v1/movies")
        assert res.status_code == 200
        assert res.json()["meta"]["total"] >= 1

    async def test_get_movie(self, client):
        create = await client.post("/api/v1/movies", json={"title_en": "M"})
        mid = create.json()["data"]["id"]
        res = await client.get(f"/api/v1/movies/{mid}")
        assert res.status_code == 200

    async def test_update_movie(self, client):
        create = await client.post("/api/v1/movies", json={"title_en": "M"})
        mid = create.json()["data"]["id"]
        res = await client.put(f"/api/v1/movies/{mid}", json={"title_cn": "电影更新"})
        assert res.status_code == 200
        assert res.json()["data"]["title_cn"] == "电影更新"

    async def test_get_404(self, client):
        res = await client.get("/api/v1/movies/nope")
        assert res.status_code == 404

    async def test_delete(self, client):
        create = await client.post("/api/v1/movies", json={"title_en": "M"})
        mid = create.json()["data"]["id"]
        res = await client.delete(f"/api/v1/movies/{mid}")
        assert res.status_code == 200
        res2 = await client.get(f"/api/v1/movies/{mid}")
        assert res2.status_code == 404

    async def test_list_search(self, client, sample_movie):
        """GET /api/v1/movies?search=test filters results by title."""
        # Create a second movie with a distinct title that won't match "test"
        await client.post("/api/v1/movies", json={
            "title_cn": "无关电影", "title_en": "Unrelated Film",
        })
        # Search for "test" — should match sample_movie (title_en="Test Movie")
        res = await client.get("/api/v1/movies", params={"search": "test"})
        assert res.status_code == 200
        body = res.json()
        assert body["meta"]["total"] >= 1
        titles = [item["title_en"] for item in body["data"]]
        assert "Test Movie" in titles
        assert "Unrelated Film" not in titles

        # Search for Chinese title
        res2 = await client.get("/api/v1/movies", params={"search": "测试"})
        assert res2.status_code == 200
        body2 = res2.json()
        assert body2["meta"]["total"] >= 1
        cn_titles = [item["title_cn"] for item in body2["data"]]
        assert "测试电影" in cn_titles

        # Search for something that matches nothing
        res3 = await client.get("/api/v1/movies", params={"search": "zzzznonexistent"})
        assert res3.status_code == 200
        assert res3.json()["meta"]["total"] == 0

    async def test_get_movie_detail(self, client, db_session, sample_movie, sample_channel, sample_downloader):
        """GET /api/v1/movies/{id} returns resources, resource_count, task_count."""
        from app.models.agent import Agent
        from app.models.download_task import DownloadTask
        from app.models.file_resource import FileResource

        mid = sample_movie.id

        # Create file resources linked to this movie
        fr1 = FileResource(
            id=_uuid(),
            channel_id=sample_channel.id,
            guid="test-guid-movie-001",
            title_raw="[SubGroup] Test Movie [1080p]",
            title_cn="测试电影",
            title_en="Test Movie",
            search_title="Test Movie",
            resolution="1080p",
            torrent_url="magnet:?xt=urn:btih:movie123",
            movie_id=mid,
        )
        fr2 = FileResource(
            id=_uuid(),
            channel_id=sample_channel.id,
            guid="test-guid-movie-002",
            title_raw="[SubGroup] Test Movie [2160p]",
            title_cn="测试电影",
            title_en="Test Movie",
            search_title="Test Movie",
            resolution="2160p",
            torrent_url="magnet:?xt=urn:btih:movie456",
            movie_id=mid,
        )
        db_session.add_all([fr1, fr2])
        await db_session.flush()

        # Create an agent (needed for download task)
        agent = Agent(
            id=_uuid(),
            name="Test Agent Movie",
            channel_id=sample_channel.id,
            downloader_id=sample_downloader.id,
            status="active",
            scope_channel_wide=False,
            conflict_resolution="ask",
        )
        db_session.add(agent)
        await db_session.flush()

        # Create a download task for one of the resources
        task = DownloadTask(
            id=_uuid(),
            agent_id=agent.id,
            file_resource_id=fr1.id,
            downloader_id=sample_downloader.id,
            download_dir="/downloads/test",
            status="completed",
            progress=1.0,
        )
        db_session.add(task)
        await db_session.flush()
        await db_session.commit()

        # Now fetch the detail endpoint
        res = await client.get(f"/api/v1/movies/{mid}")
        assert res.status_code == 200
        data = res.json()["data"]

        # Verify resources
        assert "resources" in data
        assert len(data["resources"]) == 2
        for r in data["resources"]:
            assert r["movie_id"] == mid

        # Verify resource_count
        assert "resource_count" in data
        assert data["resource_count"] == 2

        # Verify task_count
        assert "task_count" in data
        assert data["task_count"] == 1
