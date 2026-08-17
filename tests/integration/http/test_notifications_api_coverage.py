"""Notification API secondary-path integration coverage (primary app).

The scheduler-driven pipeline test (test_notifications.py) runs on app-llm
and exercises the happy-path create/list/detail/retry/regenerate. This file
covers the remaining ``app/api/v1/notifications.py`` surfaces that need no
completed task — the 404 helpers, webhook CRUD lifecycle, list status
filters + validation, bulk retry, and regenerate-with-no-work — against the
primary app.

Requirements: Docker test environment (app + test-server).
"""

from __future__ import annotations

import uuid

import pytest

from tests.integration.http._http import (
    DEFAULT_FIELD_MAPPING,
    TEST_SERVER,
    _api,
)

_CH = "Notifications Coverage Channel"


def _ensure_channel() -> str:
    r = _api("/api/v1/channels", params={"page_size": 100})
    for c in r.json().get("data", []):
        if c.get("name") == _CH:
            return c["id"]
    r = _api(
        "/api/v1/channels",
        method="post",
        json={
            "name": _CH,
            "url": f"{TEST_SERVER}/rss/mikanani-1",
            "field_mapping": DEFAULT_FIELD_MAPPING,
            "fetch_interval": 3600,
            "metadata_agent_enabled": False,
        },
    )
    assert r.status_code == 201, f"create channel failed: {r.text}"
    return r.json()["data"]["id"]


def _ensure_mock_downloader() -> str:
    r = _api("/api/v1/downloaders", params={"page_size": 100})
    for d in r.json().get("data", []):
        if d.get("type") == "mock":
            return d["id"]
    r = _api(
        "/api/v1/downloaders",
        method="post",
        json={"name": "Notify API Coverage Downloader", "type": "mock"},
    )
    assert r.status_code == 201, f"create downloader failed: {r.text}"
    return r.json()["data"]["id"]


@pytest.fixture(scope="class")
def _agent():
    ch_id = _ensure_channel()
    dl_id = _ensure_mock_downloader()
    r = _api(
        "/api/v1/agents",
        method="post",
        json={
            "name": f"Notify API Coverage Agent {uuid.uuid4().hex[:6]}",
            "channel_id": ch_id,
            "downloader_id": dl_id,
            "scope_channel_wide": True,
        },
    )
    assert r.status_code == 201, f"create agent failed: {r.text}"
    return r.json()["data"]["id"]


class TestNotification404s:
    def test_list_notifications_unknown_agent(self):
        r = _api("/api/v1/agents/no-such-agent/notifications")
        assert r.status_code == 404

    def test_notification_detail_404(self):
        r = _api("/api/v1/notifications/no-such-notification")
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "NOT_FOUND"

    def test_retry_notification_404(self):
        r = _api(
            "/api/v1/notifications/no-such-notification/retry",
            method="post",
            json={"mode": "all"},
        )
        assert r.status_code == 404

    def test_webhook_404_paths(self, _agent):
        r = _api(f"/api/v1/agents/{_agent}/webhooks")
        assert r.status_code == 200
        r = _api(
            f"/api/v1/agents/{_agent}/webhooks/no-such",
            method="put",
            json={"enabled": True},
        )
        assert r.status_code == 404
        r = _api(
            f"/api/v1/agents/{_agent}/webhooks/no-such",
            method="delete",
        )
        assert r.status_code == 404
        r = _api("/api/v1/agents/no-such-agent/webhooks", method="post",
                 json={"url": "http://x/hook"})
        assert r.status_code == 404


class TestNotificationList:
    def test_list_status_filters_and_validation(self, _agent):
        for status in ("pending", "done", "failed"):
            r = _api(f"/api/v1/agents/{_agent}/notifications", params={"status": status})
            assert r.status_code == 200, f"status={status}: {r.text}"
        r = _api(f"/api/v1/agents/{_agent}/notifications", params={"status": "bogus"})
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_regenerate_no_completed_tasks(self, _agent):
        r = _api(
            f"/api/v1/agents/{_agent}/notifications/regenerate",
            method="post",
            json={"since": None},
        )
        assert r.status_code == 200
        assert r.json()["data"] == {"created": 0, "regenerated": 0}

    def test_bulk_retry_no_rows(self, _agent):
        r = _api(
            "/api/v1/notifications/retry",
            method="post",
            json={"mode": "failed"},
        )
        assert r.status_code == 200
        assert r.json()["data"]["reset"] == 0


class TestWebhookLifecycle:
    def test_create_update_disable_list_delete(self, _agent):
        r = _api(
            f"/api/v1/agents/{_agent}/webhooks",
            method="post",
            json={"url": "http://consumer.invalid/hook", "mock": True},
        )
        assert r.status_code == 201, f"create webhook failed: {r.text}"
        wh = r.json()["data"]
        assert wh["mock"] is True and wh["enabled"] is True

        # Update: url / mock / enabled.
        r = _api(
            f"/api/v1/agents/{_agent}/webhooks/{wh['id']}",
            method="put",
            json={"enabled": False, "mock": False},
        )
        assert r.status_code == 200, f"update webhook failed: {r.text}"
        updated = r.json()["data"]
        assert updated["enabled"] is False and updated["mock"] is False

        # Update partial (url only).
        r = _api(
            f"/api/v1/agents/{_agent}/webhooks/{wh['id']}",
            method="put",
            json={"url": "http://consumer2.invalid/hook"},
        )
        assert r.status_code == 200
        assert r.json()["data"]["url"] == "http://consumer2.invalid/hook"
        assert r.json()["data"]["enabled"] is False

        # List reflects the changes; delete removes it.
        r = _api(f"/api/v1/agents/{_agent}/webhooks")
        assert any(w["id"] == wh["id"] and not w["enabled"] for w in r.json()["data"])
        r = _api(
            f"/api/v1/agents/{_agent}/webhooks/{wh['id']}",
            method="delete",
        )
        assert r.status_code == 200
        assert r.json()["data"]["deleted"] is True
        r = _api(f"/api/v1/agents/{_agent}/webhooks")
        assert all(w["id"] != wh["id"] for w in r.json()["data"])
