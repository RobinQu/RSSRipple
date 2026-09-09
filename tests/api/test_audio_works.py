"""AudioWork API tests - detail endpoint serialization edge cases."""

import uuid

import pytest


@pytest.mark.asyncio
async def test_audio_work_detail_all_null_fields(client, db_session):
    """A shell AudioWork (all title fields NULL) must serialize fine."""
    from app.models.audio_work import AudioWork

    a = AudioWork(id=str(uuid.uuid4()), external_source="llm_search", content_type="other")
    db_session.add(a)
    await db_session.commit()

    r = await client.get(f"/api/v1/audio-works/{a.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["id"] == a.id
    assert body["data"]["title_cn"] is None
    assert body["data"]["resources"] == []
    assert body["data"]["resource_count"] == 0


@pytest.mark.asyncio
async def test_audio_work_detail_with_linked_resources(client, db_session, sample_channel, sample_series):
    """Regression: linked resources whose series/movie relations are not in the
    session used to 500 (MissingGreenlet on FileResourceResponse.series)."""
    from app.models.audio_work import AudioWork
    from app.models.file_resource import FileResource

    a = AudioWork(id=str(uuid.uuid4()), title_cn="测试音频", content_type="music")
    res = FileResource(
        id=str(uuid.uuid4()),
        channel_id=sample_channel.id,
        guid="g-1",
        title_raw="[G] Test - 01",
        torrent_url="https://example.com/t.torrent",
        audio_work_id=a.id,
        series_id=sample_series.id,
    )
    db_session.add_all([a, res])
    await db_session.commit()

    r = await client.get(f"/api/v1/audio-works/{a.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["resource_count"] == 1
    assert body["data"]["resources"][0]["id"] == res.id
    assert body["data"]["resources"][0]["series_id"] == sample_series.id


@pytest.mark.asyncio
async def test_audio_work_detail_not_found(client):
    r = await client.get(f"/api/v1/audio-works/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "NOT_FOUND"


class TestAudioWorkList:
    """GET /audio-works — search, content_type filter and pagination."""

    @pytest.mark.asyncio
    async def test_list_without_search(self, client, db_session):
        from app.models.audio_work import AudioWork

        for i in range(2):
            db_session.add(AudioWork(id=str(uuid.uuid4()), title_cn=f"音频{i}"))
        await db_session.commit()

        r = await client.get("/api/v1/audio-works")
        assert r.status_code == 200
        body = r.json()
        assert body["meta"]["total"] == 2
        assert len(body["data"]) == 2

    @pytest.mark.asyncio
    async def test_list_search_fts_hits(self, client, db_session, monkeypatch):
        from unittest.mock import AsyncMock

        from app.models.audio_work import AudioWork

        a = AudioWork(id=str(uuid.uuid4()), title_cn="测试音频", content_type="music")
        db_session.add(a)
        await db_session.commit()
        monkeypatch.setattr(
            "app.services.fts.search_audio_work_fts",
            AsyncMock(return_value=[a.id]),
        )
        r = await client.get("/api/v1/audio-works", params={"search": "测试"})
        assert r.status_code == 200
        body = r.json()
        assert body["meta"]["total"] == 1
        assert body["data"][0]["id"] == a.id

    @pytest.mark.asyncio
    async def test_list_search_like_fallback(self, client, db_session, monkeypatch):
        from unittest.mock import AsyncMock

        from app.models.audio_work import AudioWork

        a = AudioWork(id=str(uuid.uuid4()), title_cn="特殊名", content_type="music")
        db_session.add(a)
        await db_session.commit()
        monkeypatch.setattr(
            "app.services.fts.search_audio_work_fts", AsyncMock(return_value=[]),
        )
        r = await client.get("/api/v1/audio-works", params={"search": "特殊"})
        assert r.status_code == 200
        body = r.json()
        assert body["meta"]["total"] == 1
        assert body["data"][0]["id"] == a.id

    @pytest.mark.asyncio
    async def test_list_content_type_filter(self, client, db_session):
        from app.models.audio_work import AudioWork

        db_session.add_all([
            AudioWork(id=str(uuid.uuid4()), title_cn="音乐", content_type="music"),
            AudioWork(id=str(uuid.uuid4()), title_cn="助眠", content_type="asmr"),
        ])
        await db_session.commit()

        r = await client.get("/api/v1/audio-works", params={"content_type": "music"})
        assert r.status_code == 200
        body = r.json()
        # total is counted pre-filter in the router; the page itself is filtered.
        assert len(body["data"]) == 1
        assert body["data"][0]["title_cn"] == "音乐"

    @pytest.mark.asyncio
    async def test_list_pagination(self, client, db_session):
        from app.models.audio_work import AudioWork

        for i in range(3):
            db_session.add(AudioWork(id=str(uuid.uuid4()), title_cn=f"音频{i}"))
        await db_session.commit()

        r = await client.get("/api/v1/audio-works", params={"page": 2, "page_size": 2})
        assert r.status_code == 200
        body = r.json()
        assert body["meta"]["total"] == 3
        assert len(body["data"]) == 1


class TestAudioWorkUpdateDelete:
    """PUT/DELETE /audio-works/{id} — update + delete (unlink resources)."""

    @pytest.mark.asyncio
    async def test_update(self, client, db_session):
        from app.models.audio_work import AudioWork

        a = AudioWork(id=str(uuid.uuid4()), title_cn="旧名", content_type="other")
        db_session.add(a)
        await db_session.commit()

        r = await client.put(
            f"/api/v1/audio-works/{a.id}",
            json={"title_cn": "新名", "content_type": "music", "rating": 4.5},
        )
        assert r.status_code == 200
        body = r.json()["data"]
        assert body["title_cn"] == "新名"
        assert body["content_type"] == "music"
        assert body["rating"] == 4.5

    @pytest.mark.asyncio
    async def test_update_404(self, client):
        r = await client.put(
            f"/api/v1/audio-works/{uuid.uuid4()}", json={"title_cn": "x"},
        )
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "NOT_FOUND"

    @pytest.mark.asyncio
    async def test_delete_unlinks_resources(
        self, client, db_session, db_session_factory, sample_channel,
    ):
        from app.models.audio_work import AudioWork
        from app.models.file_resource import FileResource

        a = AudioWork(id=str(uuid.uuid4()), title_cn="待删", content_type="music")
        res = FileResource(
            id=str(uuid.uuid4()),
            channel_id=sample_channel.id,
            guid="g-del",
            title_raw="[G] audio",
            torrent_url="magnet:?xt=urn:btih:abc",
            audio_work_id=a.id,
        )
        db_session.add_all([a, res])
        await db_session.commit()

        r = await client.delete(f"/api/v1/audio-works/{a.id}")
        assert r.status_code == 200
        assert r.json()["data"] == {"deleted": True}

        async with db_session_factory() as s:
            row = await s.get(FileResource, res.id)
            assert row.audio_work_id is None

    @pytest.mark.asyncio
    async def test_delete_404(self, client):
        r = await client.delete(f"/api/v1/audio-works/{uuid.uuid4()}")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "NOT_FOUND"
