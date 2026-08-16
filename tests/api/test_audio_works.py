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
