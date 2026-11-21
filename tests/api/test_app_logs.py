"""Application log list and retry API tests."""

import uuid

from app.models.app_log import AppLog
from app.models.file_resource import FileResource


def _uuid():
    return str(uuid.uuid4())


TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"torrent_url": {"source": "link"}},
}


async def test_list_logs_resolves_links_only_work_title(client, db_session_factory):
    from app.models.channel import Channel
    from app.models.resource_work_link import ResourceWorkLink
    from app.models.series import TVSeries

    async with db_session_factory() as db:
        channel = Channel(
            id=_uuid(), name="logs-links", url="https://example.com/rss2",
            field_mapping=TEST_FIELD_MAPPING,
        )
        series = TVSeries(id=_uuid(), title_cn="链接作品", season_number=1)
        resource = FileResource(
            id=_uuid(), channel_id=channel.id, guid=_uuid(),
            title_raw="[G] links", torrent_url="https://example.com/b.torrent",
            is_batch=True, batch_scope="multi_season",
        )
        db.add_all([channel, series, resource])
        await db.flush()
        db.add(ResourceWorkLink(resource_id=resource.id, series_id=series.id, source="manual"))
        db.add(AppLog(
            id=_uuid(), category="metadata", message="timeout",
            resource_id=resource.id, channel_id=channel.id, attempts=2,
        ))
        await db.commit()

    response = await client.get("/api/v1/app-logs?category=metadata")
    assert response.status_code == 200
    item = next(
        row for row in response.json()["data"]
        if row["resource_id"] == resource.id
    )
    assert item["resource"]["work_title"] == "链接作品"


async def test_list_logs_and_reject_duplicate_retry(client, db_session_factory):
    from app.models.channel import Channel

    async with db_session_factory() as db:
        channel = Channel(
            id=_uuid(), name="logs", url="https://example.com/rss",
            field_mapping=TEST_FIELD_MAPPING,
        )
        db.add(channel)
        await db.flush()
        resource = FileResource(
            id=_uuid(), channel_id=channel.id, guid=_uuid(),
            title_raw="43yrs ago - Example", search_title="Clean Example",
            torrent_url="https://example.com/a.torrent",
        )
        db.add(resource)
        await db.flush()
        log = AppLog(
            id=_uuid(), category="torrent", message="timeout",
            resource_id=resource.id, channel_id=channel.id, attempts=5,
        )
        db.add(log)
        await db.commit()

    response = await client.get("/api/v1/app-logs?status=failed")
    assert response.status_code == 200
    payload = response.json()
    assert payload["meta"]["total"] == 1
    item = payload["data"][0]
    assert item["resource_title"] == "Clean Example"
    assert item["channel_name"] == "logs"
    assert item["resource"]["title_raw"] == "43yrs ago - Example"
    assert item["resource"]["search_title"] == "Clean Example"
    assert item["resource"]["guid"] == resource.guid
    assert item["resource"]["torrent_fetch_status"] is None

    async with db_session_factory() as db:
        row = await db.get(AppLog, log.id)
        row.status = "retrying"
        await db.commit()
    duplicate = await client.post(f"/api/v1/app-logs/{log.id}/retry")
    assert duplicate.status_code == 409
