"""API tests for download notifications: queue listing (aggregated delivery
status), webhook collection CRUD, backfill, and the retry endpoints.

Delivery state lives on per-webhook WebhookDelivery rows; a notification's
displayed status is aggregated: no deliveries or any pending → "pending";
all done → "done"; otherwise → "failed".
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.models.agent import Agent
from app.models.agent_webhook import AgentWebhook
from app.models.download_notification import DownloadNotification
from app.models.download_task import DownloadTask
from app.models.file_resource import FileResource
from app.models.webhook_delivery import WebhookDelivery
from app.utils.time import utcnow

WEBHOOK_URL = "http://organizer:8910/webhook"


def _uuid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
async def seed(db_session, sample_channel, sample_downloader, sample_series):
    """Agent + one webhook + resource + completed task + one notification
    with a single pending delivery."""
    agent = Agent(
        id=_uuid(), name="Agent", channel_id=sample_channel.id,
        downloader_id=sample_downloader.id,
    )
    resource = FileResource(
        id=_uuid(), channel_id=sample_channel.id, guid="g1",
        title_raw="[G] Test Series - 05 [1080p]",
        torrent_url="magnet:?xt=urn:btih:abc",
        series_id=sample_series.id, season=1, episode=5, is_batch=False,
    )
    db_session.add_all([agent, resource])
    await db_session.flush()
    webhook = AgentWebhook(
        id=_uuid(), agent_id=agent.id, url=WEBHOOK_URL, mock=False, enabled=True,
    )
    db_session.add(webhook)
    task = DownloadTask(
        id=_uuid(), agent_id=agent.id, file_resource_id=resource.id,
        downloader_id=sample_downloader.id, download_dir="/downloads/x",
        transmission_torrent_id=7, status="completed", completed_at=utcnow(),
    )
    db_session.add(task)
    await db_session.flush()
    notification = DownloadNotification(
        id=_uuid(), agent_id=agent.id, download_task_id=task.id,
        payload={"notification_id": "n", "task": {"download_task_id": task.id}},
    )
    db_session.add(notification)
    await db_session.flush()
    delivery = WebhookDelivery(
        id=_uuid(), notification_id=notification.id, webhook_id=webhook.id,
        status="pending", next_attempt_at=utcnow(),
    )
    db_session.add(delivery)
    await db_session.commit()
    return {
        "agent": agent, "task": task, "notification": notification,
        "webhook": webhook, "delivery": delivery,
    }


# ---------------------------------------------------------------------------
# Listing & detail
# ---------------------------------------------------------------------------


async def test_list_notifications_aggregated_pending(client, seed):
    resp = await client.get(f"/api/v1/agents/{seed['agent'].id}/notifications")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["meta"]["total"] == 1
    item = body["data"][0]
    assert item["status"] == "pending"
    assert item["delivery_summary"] == {
        "total": 1, "done": 0, "failed": 0, "pending": 1,
    }
    assert "payload" not in item  # list items stay light


async def test_list_notifications_no_deliveries_is_pending(client, seed, db_session):
    seed["delivery"].status = "done"
    await db_session.delete(seed["delivery"])
    await db_session.commit()
    resp = await client.get(f"/api/v1/agents/{seed['agent'].id}/notifications")
    item = resp.json()["data"][0]
    assert item["status"] == "pending"
    assert item["delivery_summary"]["total"] == 0


async def test_list_notifications_status_filter(client, seed, db_session):
    aid = seed["agent"].id
    # Pending delivery → not "done".
    resp = await client.get(f"/api/v1/agents/{aid}/notifications?status=done")
    assert resp.json()["meta"]["total"] == 0
    resp = await client.get(f"/api/v1/agents/{aid}/notifications?status=pending")
    assert resp.json()["meta"]["total"] == 1

    # All deliveries done → aggregated "done".
    seed["delivery"].status = "done"
    seed["delivery"].delivered_at = utcnow()
    await db_session.commit()
    resp = await client.get(f"/api/v1/agents/{aid}/notifications?status=done")
    assert resp.json()["meta"]["total"] == 1
    assert resp.json()["data"][0]["status"] == "done"
    resp = await client.get(f"/api/v1/agents/{aid}/notifications?status=pending")
    assert resp.json()["meta"]["total"] == 0

    # Failed, none pending → aggregated "failed".
    seed["delivery"].status = "failed"
    seed["delivery"].error_message = "boom"
    await db_session.commit()
    resp = await client.get(f"/api/v1/agents/{aid}/notifications?status=failed")
    assert resp.json()["meta"]["total"] == 1
    assert resp.json()["data"][0]["status"] == "failed"
    resp = await client.get(f"/api/v1/agents/{aid}/notifications?status=done")
    assert resp.json()["meta"]["total"] == 0


async def test_list_notifications_rejects_unknown_status(client, seed):
    resp = await client.get(
        f"/api/v1/agents/{seed['agent'].id}/notifications?status=bogus"
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_list_notifications_agent_404(client):
    resp = await client.get(f"/api/v1/agents/{_uuid()}/notifications")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


async def test_get_notification_detail_with_deliveries(client, seed):
    resp = await client.get(f"/api/v1/notifications/{seed['notification'].id}")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["payload"]["notification_id"] == "n"
    assert data["status"] == "pending"
    assert len(data["deliveries"]) == 1
    d = data["deliveries"][0]
    assert d["webhook_id"] == seed["webhook"].id
    assert d["webhook_url"] == WEBHOOK_URL
    assert d["status"] == "pending"
    assert d["attempt_count"] == 0


async def test_get_notification_404(client):
    resp = await client.get(f"/api/v1/notifications/{_uuid()}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Webhook collection CRUD
# ---------------------------------------------------------------------------


async def test_webhook_crud_roundtrip(client, seed):
    aid = seed["agent"].id

    resp = await client.get(f"/api/v1/agents/{aid}/webhooks")
    assert resp.status_code == 200
    rows = resp.json()["data"]
    assert len(rows) == 1
    assert rows[0]["url"] == WEBHOOK_URL
    assert rows[0]["mock"] is False and rows[0]["enabled"] is True

    # Create: 201 with the created object.
    resp = await client.post(
        f"/api/v1/agents/{aid}/webhooks",
        json={"url": "http://second:9000/hook", "mock": True},
    )
    assert resp.status_code == 201
    created = resp.json()["data"]
    assert created["url"] == "http://second:9000/hook"
    assert created["mock"] is True and created["enabled"] is True
    assert created["id"] and created["created_at"]

    resp = await client.get(f"/api/v1/agents/{aid}/webhooks")
    assert len(resp.json()["data"]) == 2

    # Update: partial fields.
    resp = await client.put(
        f"/api/v1/agents/{aid}/webhooks/{created['id']}",
        json={"enabled": False, "url": "http://second:9000/v2"},
    )
    assert resp.status_code == 200
    updated = resp.json()["data"]
    assert updated["enabled"] is False
    assert updated["url"] == "http://second:9000/v2"
    assert updated["mock"] is True  # untouched

    # Delete.
    resp = await client.delete(f"/api/v1/agents/{aid}/webhooks/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"deleted": True}
    resp = await client.get(f"/api/v1/agents/{aid}/webhooks")
    assert len(resp.json()["data"]) == 1


async def test_webhook_create_rejects_bad_url(client, seed):
    resp = await client.post(
        f"/api/v1/agents/{seed['agent'].id}/webhooks",
        json={"url": "ftp://x/hook"},
    )
    assert resp.status_code == 422


async def test_webhook_create_mock_without_url(client, seed):
    """Mock webhooks don't need an http(s) URL; empty url gets a placeholder."""
    resp = await client.post(
        f"/api/v1/agents/{seed['agent'].id}/webhooks",
        json={"url": "", "mock": True},
    )
    assert resp.status_code == 201
    data = resp.json()["data"]
    assert data["mock"] is True
    assert data["url"] == "mock://local"


async def test_webhook_create_agent_404(client):
    resp = await client.post(
        f"/api/v1/agents/{_uuid()}/webhooks", json={"url": WEBHOOK_URL}
    )
    assert resp.status_code == 404


async def test_webhook_update_and_delete_404(client, seed):
    aid = seed["agent"].id
    resp = await client.put(
        f"/api/v1/agents/{aid}/webhooks/{_uuid()}", json={"enabled": False}
    )
    assert resp.status_code == 404
    resp = await client.delete(f"/api/v1/agents/{aid}/webhooks/{_uuid()}")
    assert resp.status_code == 404
    # Webhook belonging to a different agent is not reachable here.
    other = Agent(
        id=_uuid(), name="Other",
        channel_id=seed["agent"].channel_id,
        downloader_id=seed["agent"].downloader_id,
    )
    resp = await client.put(
        f"/api/v1/agents/{other.id}/webhooks/{seed['webhook'].id}",
        json={"enabled": False},
    )
    assert resp.status_code == 404


async def test_webhook_create_fans_out_backlog(client, seed, db_session):
    """A newly registered webhook immediately receives deliveries for the
    existing notification backlog."""
    aid = seed["agent"].id
    resp = await client.post(
        f"/api/v1/agents/{aid}/webhooks", json={"url": "http://new:9000/hook"}
    )
    assert resp.status_code == 201
    new_id = resp.json()["data"]["id"]

    resp = await client.get(f"/api/v1/notifications/{seed['notification'].id}")
    deliveries = resp.json()["data"]["deliveries"]
    assert len(deliveries) == 2
    new_d = next(d for d in deliveries if d["webhook_id"] == new_id)
    assert new_d["status"] == "pending"


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------


async def test_backfill_creates_only_missing(client, seed, db_session):
    # seed's task already has a notification → nothing to backfill.
    resp = await client.post(
        f"/api/v1/agents/{seed['agent'].id}/notifications/backfill",
        json={"since": None},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["created"] == 0

    # A second completed task without a notification gets one.
    task2 = DownloadTask(
        id=_uuid(), agent_id=seed["agent"].id,
        file_resource_id=seed["task"].file_resource_id,
        downloader_id=seed["task"].downloader_id,
        download_dir="/downloads/x", transmission_torrent_id=None,
        status="completed", completed_at=utcnow(),
    )
    db_session.add(task2)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/agents/{seed['agent'].id}/notifications/backfill",
        json={"since": None},
    )
    assert resp.json()["data"]["created"] == 1


# ---------------------------------------------------------------------------
# Retry endpoints
# ---------------------------------------------------------------------------


async def test_retry_single_failed_mode(client, seed, db_session):
    seed["delivery"].status = "failed"
    seed["delivery"].attempt_count = 5
    seed["delivery"].error_message = "boom"
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/notifications/{seed['notification'].id}/retry",
        json={"mode": "failed"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"] == {"reset": 1}

    await db_session.refresh(seed["delivery"])
    assert seed["delivery"].status == "pending"
    assert seed["delivery"].attempt_count == 0
    assert seed["delivery"].error_message is None
    assert seed["delivery"].next_attempt_at is not None


async def test_retry_single_all_mode_resets_done(client, seed, db_session):
    seed["delivery"].status = "done"
    seed["delivery"].delivered_at = utcnow()
    await db_session.commit()

    # mode=failed leaves done rows alone.
    resp = await client.post(
        f"/api/v1/notifications/{seed['notification'].id}/retry",
        json={"mode": "failed"},
    )
    assert resp.json()["data"] == {"reset": 0}

    resp = await client.post(
        f"/api/v1/notifications/{seed['notification'].id}/retry",
        json={"mode": "all"},
    )
    assert resp.json()["data"] == {"reset": 1}
    await db_session.refresh(seed["delivery"])
    assert seed["delivery"].status == "pending"


async def test_retry_single_404(client):
    resp = await client.post(
        f"/api/v1/notifications/{_uuid()}/retry", json={"mode": "failed"}
    )
    assert resp.status_code == 404


async def test_retry_bulk_scoping(client, seed, db_session, db_session_factory):
    # Second agent + notification + failed delivery.
    other = Agent(
        id=_uuid(), name="Other", channel_id=seed["agent"].channel_id,
        downloader_id=seed["agent"].downloader_id,
    )
    db_session.add(other)
    await db_session.flush()
    other_webhook = AgentWebhook(
        id=_uuid(), agent_id=other.id, url="http://other/hook",
    )
    other_task = DownloadTask(
        id=_uuid(), agent_id=other.id,
        file_resource_id=seed["task"].file_resource_id,
        downloader_id=seed["task"].downloader_id,
        download_dir="/downloads/x", transmission_torrent_id=None,
        status="completed", completed_at=utcnow(),
    )
    db_session.add_all([other_webhook, other_task])
    await db_session.flush()
    old_notification = DownloadNotification(
        id=_uuid(), agent_id=other.id, download_task_id=other_task.id,
        payload={"notification_id": "old"},
        created_at=utcnow() - timedelta(days=10),
    )
    db_session.add(old_notification)
    await db_session.flush()
    other_delivery = WebhookDelivery(
        id=_uuid(), notification_id=old_notification.id,
        webhook_id=other_webhook.id, status="failed", error_message="x",
    )
    seed["delivery"].status = "failed"
    db_session.add(other_delivery)
    await db_session.commit()

    # Unscoped: both failed deliveries reset.
    resp = await client.post("/api/v1/notifications/retry", json={"mode": "failed"})
    assert resp.status_code == 200
    assert resp.json()["data"] == {"reset": 2}

    # Re-fail both through a FRESH session — the long-lived fixture session
    # holds a pre-API-call MVCC snapshot, and writes to rows the API session
    # just touched would be silently discarded.
    from sqlalchemy import select

    async with db_session_factory() as s:
        for d in (await s.execute(select(WebhookDelivery))).scalars().all():
            d.status = "failed"
        await s.commit()

    # Scope by agent: only the seed agent's delivery resets.
    resp = await client.post(
        "/api/v1/notifications/retry",
        json={"mode": "failed", "agent_id": seed["agent"].id},
    )
    assert resp.json()["data"] == {"reset": 1}

    # Scope by since: only the 10-day-old notification's delivery is still
    # failed, and it is excluded.
    resp = await client.post(
        "/api/v1/notifications/retry",
        json={"mode": "failed", "since": (utcnow() - timedelta(days=1)).isoformat()},
    )
    assert resp.json()["data"] == {"reset": 0}
