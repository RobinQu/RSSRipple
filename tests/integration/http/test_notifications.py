"""Download notification integration tests.

Runs against the app-llm instance (the only service in docker-compose.test.yml
with SCHEDULER_ENABLED=true) because the notification pipeline is driven by
the per-minute scheduler tick: completed task → stop torrent + create
notification → fan out per-webhook deliveries → deliver to the Agent's
registered webhooks.

Covers:
  - mock-webhook registration (collection endpoint)
  - automatic notification creation for a completed mock download
  - fan-out + delivery to a mock webhook (delivery done without HTTP)
  - retry endpoint (mode=all re-pends a done delivery; the next tick
    re-delivers it)
  - backfill idempotency (all tasks already have notifications → created=0)

Skips automatically when RSSRIPPLE_LLM_URL is not set (e.g. distributed
stacks without the app-llm service).
"""

from __future__ import annotations

import os
import time

import httpx
import pytest

from tests.integration.http._http import (
    API_HEADERS,
    MIKANANI_1_URL,
    RICH_FIELD_MAPPING,
    associate_metadata_request,
)

LLM_APP = os.environ.get("RSSRIPPLE_LLM_URL", "")
TIMEOUT = 60.0

pytestmark = pytest.mark.skipif(
    not LLM_APP, reason="RSSRIPPLE_LLM_URL not set (app-llm stack required)"
)


def _api(path: str, method: str = "get", **kw) -> httpx.Response:
    last_exc = None
    for attempt in range(3):
        try:
            c = httpx.Client(timeout=TIMEOUT, headers=API_HEADERS)
            fn = getattr(c, method.lower())
            return fn(f"{LLM_APP}{path}", **kw)
        except (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            last_exc = e
            time.sleep(1 * (attempt + 1))
    raise last_exc


def _poll(predicate, timeout: int = 240, interval: float = 5.0, desc: str = ""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise TimeoutError(f"condition not met within {timeout}s: {desc}")


def test_notification_pipeline_end_to_end():
    # ── 1. mock downloader + channel + fetch + agent dispatch ────────────
    r = _api("/api/v1/downloaders", params={"page_size": 100})
    dl_id = next(
        (d["id"] for d in r.json().get("data", []) if d.get("type") == "mock"),
        None,
    )
    if dl_id is None:
        r = _api(
            "/api/v1/downloaders",
            method="post",
            json={"name": "Notify Mock Downloader", "type": "mock"},
        )
        assert r.status_code == 201, r.text
        dl_id = r.json()["data"]["id"]

    r = _api(
        "/api/v1/channels",
        method="post",
        json={
            "name": "Notify Test Channel",
            "url": MIKANANI_1_URL,
            "field_mapping": RICH_FIELD_MAPPING,
            # app-llm runs the scheduler: a due-soon interval would trigger an
            # automatic fetch racing the manual one and hold fetch-status at
            # "running" past the poll timeout.
            "fetch_interval": 3600,
            # No metadata agent: the test links the resource manually below;
            # leaving it on would send every resource to the (real) metadata
            # source during fetch and can stall the fetch past the poll timeout.
            "metadata_agent_enabled": False,
        },
    )
    assert r.status_code == 201, r.text
    channel_id = r.json()["data"]["id"]
    # Channel creation auto-enqueues an initial fetch, and the scheduler's
    # interval job fires a second one right after — on this 87-entry feed
    # fetch-status stays non-terminal for many minutes. Resources are
    # committed progressively, so wait for the first ones instead.
    resources = _poll(
        lambda: (
            _api(
                f"/api/v1/channels/{channel_id}/resources", params={"page_size": 100}
            ).json().get("data")
            or None
        ),
        timeout=360,
        interval=3.0,
        desc="initial fetch produced resources",
    )
    assert resources, "channel fetch produced no resources"

    # Link the resource to a genre-carrying series (legacy alias "Anime"
    # normalizes to "Animation") so the notification snapshot can be
    # asserted to carry the canonical work.genre. The title is deliberately
    # unique — reusing 葬送的芙莉莲 would duplicate the _llm_env series and
    # trip the same-title collision guard for auto-link in other tests.
    r = associate_metadata_request(
        f"/api/v1/resources/{resources[0]['id']}/metadata/link",
        api=_api,
        method="put",
        json={
            "selected_result": {
                "content_type": "tv",
                "title_cn": "通知专用测试剧集",
                "title_en": "Notify Genre Test Series",
                "external_id": "notify-genre-series",
                "external_source": "tmdb",
                "genre": ["Anime", "Fantasy"],
                # Single-season evidence: without it the linked resource lands
                # season=None + episode_confidence=ambiguous (season-verify
                # rule) and dispatch skips it → no task → no notification.
                "number_of_seasons": 1,
            }
        },
    )
    assert r.status_code == 200, r.text
    notify_series_id = r.json()["data"]["series_id"]
    assert notify_series_id

    r = _api(
        "/api/v1/agents",
        method="post",
        json={
            "name": "Notify Test Agent",
            "channel_id": channel_id,
            "downloader_id": dl_id,
            "scope_channel_wide": True,
            "dispatch_resource_ids": [resources[0]["id"]],
        },
    )
    assert r.status_code == 201, r.text
    agent_id = r.json()["data"]["id"]

    # ── 2. register a mock webhook (url is inert for mock webhooks) ──────
    r = _api(
        f"/api/v1/agents/{agent_id}/webhooks",
        method="post",
        json={"url": "http://mock.invalid/hook", "mock": True},
    )
    assert r.status_code == 201, r.text
    webhook = r.json()["data"]
    assert webhook["mock"] is True and webhook["enabled"] is True

    r = _api(f"/api/v1/agents/{agent_id}/webhooks")
    assert any(w["id"] == webhook["id"] for w in r.json()["data"])

    # ── 3. scheduler ticks: download completes → notification created ────
    # mock torrents finish in 1-10s; sync (1min) marks completed, then the
    # notify tick (1min) creates the notification, fans out and delivers.
    def _delivered_notification():
        r = _api(f"/api/v1/agents/{agent_id}/notifications")
        items = r.json().get("data", [])
        delivered = [
            n for n in items if n.get("delivery_summary", {}).get("done")
        ]
        return delivered[0] if delivered else None

    notification = _poll(
        _delivered_notification,
        timeout=300,
        desc="notification created and delivered (mock webhook)",
    )
    assert notification["status"] == "done"  # all deliveries done

    # Detail carries the frozen payload snapshot and the delivery rows.
    r = _api(f"/api/v1/notifications/{notification['id']}")
    detail = r.json()["data"]
    assert detail["payload"]["task"]["download_task_id"]
    work = detail["payload"]["work"]
    assert work["type"] == "series"
    # genre snapshot: normalized to the closed TMDB set at build time
    assert work["genre"] == ["Animation", "Fantasy"], f"work: {work}"
    deliveries = detail["deliveries"]
    assert len(deliveries) == 1
    assert deliveries[0]["status"] == "done"
    assert deliveries[0]["delivered_at"]
    assert deliveries[0]["webhook_id"] == webhook["id"]

    # ── 4. retry: mode=all re-pends the done delivery ────────────────────
    r = _api(
        f"/api/v1/notifications/{notification['id']}/retry",
        method="post",
        json={"mode": "all"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["reset"] == 1

    r = _api(f"/api/v1/notifications/{notification['id']}")
    assert r.json()["data"]["status"] == "pending"

    # The next tick re-delivers (mock) → done again.
    def _redelivered():
        r = _api(f"/api/v1/notifications/{notification['id']}")
        d = r.json()["data"]
        return d if d["status"] == "done" else None

    _poll(_redelivered, timeout=180, desc="re-delivery after retry")

    # ── 5. regenerate: no missing notifications, existing ones get rebuilt ──
    r = _api(
        f"/api/v1/agents/{agent_id}/notifications/regenerate",
        method="post",
        json={"since": None},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["created"] == 0

    # ── cleanup ──────────────────────────────────────────────────────────
    _api(f"/api/v1/agents/{agent_id}", method="delete")
    _api(f"/api/v1/channels/{channel_id}", method="delete")
    _api(f"/api/v1/series/{notify_series_id}", method="delete")
