"""API tests for download notifications: queue listing, webhook registration
(per-Agent callback token), backfill, and the start/ack/fail/retry state
machine."""

from __future__ import annotations

import uuid

import pytest

from app.models.agent import Agent
from app.models.download_notification import DownloadNotification
from app.models.download_task import DownloadTask
from app.models.file_resource import FileResource
from app.utils.time import utcnow

AGENT_TOKEN = "test-callback-token"
CALLBACK_HEADERS = {"Authorization": f"Bearer {AGENT_TOKEN}"}


def _uuid() -> str:
    return str(uuid.uuid4())


@pytest.fixture
async def seed(db_session, sample_channel, sample_downloader, sample_series):
    """Agent (with registered webhook token) + resource + completed task +
    one pending notification."""
    agent = Agent(
        id=_uuid(), name="Agent", channel_id=sample_channel.id,
        downloader_id=sample_downloader.id,
        notify_webhook_url="http://organizer:8910/webhook",
        notify_webhook_token=AGENT_TOKEN,
    )
    resource = FileResource(
        id=_uuid(), channel_id=sample_channel.id, guid="g1",
        title_raw="[G] Test Series - 05 [1080p]",
        torrent_url="magnet:?xt=urn:btih:abc",
        series_id=sample_series.id, season=1, episode=5, is_batch=False,
    )
    db_session.add_all([agent, resource])
    await db_session.flush()
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
        status="pending", next_attempt_at=utcnow(),
    )
    db_session.add(notification)
    await db_session.commit()
    return {"agent": agent, "task": task, "notification": notification}


# ---------------------------------------------------------------------------
# Listing & detail
# ---------------------------------------------------------------------------


async def test_list_notifications(client, seed):
    resp = await client.get(f"/api/v1/agents/{seed['agent'].id}/notifications")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["meta"]["total"] == 1
    item = body["data"][0]
    assert item["status"] == "pending"
    assert "payload" not in item  # list items stay light


async def test_list_notifications_status_filter(client, seed):
    resp = await client.get(
        f"/api/v1/agents/{seed['agent'].id}/notifications?status=done"
    )
    assert resp.json()["meta"]["total"] == 0


async def test_list_notifications_agent_404(client):
    resp = await client.get(f"/api/v1/agents/{_uuid()}/notifications")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


async def test_get_notification_detail(client, seed):
    resp = await client.get(f"/api/v1/notifications/{seed['notification'].id}")
    assert resp.status_code == 200
    assert resp.json()["data"]["payload"]["notification_id"] == "n"


async def test_get_notification_404(client):
    resp = await client.get(f"/api/v1/notifications/{_uuid()}")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Webhook registration & per-Agent callback token
# ---------------------------------------------------------------------------


async def test_webhook_register_issues_token(client, seed):
    aid = seed["agent"].id
    resp = await client.get(f"/api/v1/agents/{aid}/webhook")
    data = resp.json()["data"]
    assert data["registered"] is True
    assert data["token"] == AGENT_TOKEN

    # Re-register: a fresh token is issued every time.
    resp = await client.put(
        f"/api/v1/agents/{aid}/webhook",
        json={"url": "http://organizer:8910/webhook", "mock": False},
    )
    data = resp.json()["data"]
    assert data["registered"] is True
    assert data["token"] and data["token"] != AGENT_TOKEN

    # Switch to mock: url cleared, token still issued.
    resp = await client.put(f"/api/v1/agents/{aid}/webhook", json={"mock": True})
    data = resp.json()["data"]
    assert data["mock"] is True and data["url"] is None and data["token"]

    # Unregister clears everything.
    resp = await client.delete(f"/api/v1/agents/{aid}/webhook")
    data = resp.json()["data"]
    assert data == {"registered": False, "url": None, "mock": False, "token": None}


async def test_webhook_register_requires_url_unless_mock(client, seed):
    resp = await client.put(
        f"/api/v1/agents/{seed['agent'].id}/webhook", json={}
    )
    assert resp.status_code == 422


async def test_webhook_register_rejects_bad_scheme(client, seed):
    resp = await client.put(
        f"/api/v1/agents/{seed['agent'].id}/webhook",
        json={"url": "ftp://x/hook"},
    )
    assert resp.status_code == 422


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
# State machine: start / ack / fail / retry
# ---------------------------------------------------------------------------


async def test_start_transitions_pending_to_processing(client, seed):
    nid = seed["notification"].id
    resp = await client.post(
        f"/api/v1/notifications/{nid}/start", headers=CALLBACK_HEADERS
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["status"] == "processing"
    # Idempotent: a second start returns the current state.
    resp = await client.post(
        f"/api/v1/notifications/{nid}/start", headers=CALLBACK_HEADERS
    )
    assert resp.status_code == 200


async def test_start_rejects_terminal_states(client, seed):
    nid = seed["notification"].id
    await client.post(
        f"/api/v1/notifications/{nid}/fail",
        json={"error": "x"}, headers=CALLBACK_HEADERS,
    )
    resp = await client.post(
        f"/api/v1/notifications/{nid}/start", headers=CALLBACK_HEADERS
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "INVALID_STATE"


async def test_ack_marks_done_and_removes_torrent(
    client, seed, mock_transmission
):
    nid = seed["notification"].id
    resp = await client.post(
        f"/api/v1/notifications/{nid}/ack", headers=CALLBACK_HEADERS
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "done"
    assert data["processed_at"] is not None
    mock_transmission.remove_torrent.assert_awaited_once_with(7, delete_data=False)


async def test_ack_remove_failure_keeps_done_with_warning(
    client, seed, mock_transmission, monkeypatch
):
    from unittest.mock import AsyncMock

    mock_transmission.remove_torrent = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "app.clients.transmission.TransmissionWrapper.remove_torrent",
        mock_transmission.remove_torrent,
    )
    resp = await client.post(
        f"/api/v1/notifications/{seed['notification'].id}/ack",
        headers=CALLBACK_HEADERS,
    )
    data = resp.json()["data"]
    assert data["status"] == "done"
    assert data["error_message"].startswith("warning:")


async def test_ack_without_torrent_id_skips_remove(
    client, seed, mock_transmission, db_session
):
    # 任务没有 torrent id（从未提交成功）——ack 照常 done，不调 remove。
    seed["task"].transmission_torrent_id = None
    await db_session.commit()
    resp = await client.post(
        f"/api/v1/notifications/{seed['notification'].id}/ack",
        headers=CALLBACK_HEADERS,
    )
    assert resp.json()["data"]["status"] == "done"
    mock_transmission.remove_torrent.assert_not_awaited()


async def test_fail_records_error(client, seed):
    nid = seed["notification"].id
    resp = await client.post(
        f"/api/v1/notifications/{nid}/fail",
        json={"error": "集号无法映射"},
        headers=CALLBACK_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["status"] == "failed"
    assert data["error_message"] == "集号无法映射"


async def test_fail_on_done_rejected(client, seed):
    nid = seed["notification"].id
    await client.post(
        f"/api/v1/notifications/{nid}/ack", headers=CALLBACK_HEADERS
    )
    resp = await client.post(
        f"/api/v1/notifications/{nid}/fail",
        json={"error": "x"}, headers=CALLBACK_HEADERS,
    )
    assert resp.status_code == 409


async def test_retry_resets_to_pending(client, seed):
    nid = seed["notification"].id
    await client.post(
        f"/api/v1/notifications/{nid}/fail",
        json={"error": "boom"}, headers=CALLBACK_HEADERS,
    )
    resp = await client.post(f"/api/v1/notifications/{nid}/retry")
    data = resp.json()["data"]
    assert data["status"] == "pending"
    assert data["attempt_count"] == 0
    assert data["error_message"] is None


async def test_retry_done_rejected(client, seed):
    nid = seed["notification"].id
    await client.post(
        f"/api/v1/notifications/{nid}/ack", headers=CALLBACK_HEADERS
    )
    resp = await client.post(f"/api/v1/notifications/{nid}/retry")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "INVALID_STATE"


# ---------------------------------------------------------------------------
# Callback token guard (per-Agent token)
# ---------------------------------------------------------------------------


async def test_callback_requires_token(client, seed):
    nid = seed["notification"].id
    resp = await client.post(f"/api/v1/notifications/{nid}/start")
    assert resp.status_code == 401
    resp = await client.post(
        f"/api/v1/notifications/{nid}/start",
        headers={"Authorization": "Bearer wrong"},
    )
    assert resp.status_code == 401


async def test_callback_503_when_agent_has_no_token(client, seed, db_session):
    seed["agent"].notify_webhook_token = None
    await db_session.commit()
    resp = await client.post(
        f"/api/v1/notifications/{seed['notification'].id}/ack",
        headers=CALLBACK_HEADERS,
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "CALLBACK_TOKEN_NOT_CONFIGURED"
