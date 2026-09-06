"""HTTP API coverage supplement #2b — app-llm instance.

Runs against the app-llm service (the only docker-compose.test.yml service
with SCHEDULER_ENABLED=true), because OrganizePlans are only created by the
minutely notify tick: completed mock download → notification → organize
planning. Covers:

  - app/api/v1/organize.py — plan list/detail filters, classify, execute
    (202/409), execute-batch, cancel (with task cleanup), audit filter,
    rule-update field branches, rules preview (notification/resource
    payloads, draft rules, 404/422 edges), library update validation and
    DELETE_BLOCKED with an active plan.
  - app/api/v1/dashboard.py — pending-plan paging + plan todo ignore.
  - app/api/v1/resources.py — task-outcome annotation for completed tasks.
  - app/api/v1/agents.py — latest-completed work annotation after a real
    completed download.
  - app/api/v1/queue.py — scheduler snapshot with the scheduler enabled.

Skips automatically when RSSRIPPLE_LLM_URL is not set.
"""

from __future__ import annotations

import os
import time
import uuid

import httpx
import pytest

from tests.integration.http._http import (
    API_HEADERS,
    RICH_FIELD_MAPPING,
    TEST_SERVER,
)

LLM_APP = os.environ.get("RSSRIPPLE_LLM_URL", "")
TIMEOUT = 60.0

pytestmark = pytest.mark.skipif(
    not LLM_APP, reason="RSSRIPPLE_LLM_URL not set (app-llm stack required)"
)

MIKANANI_S2_URL = f"{TEST_SERVER}/rss/mikanani?series=2"  # 药屋少女的呢喃
APOTHECARY_TITLE_CN = "药屋少女的呢喃"
PLEX_URL = f"{TEST_SERVER}/plex"


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


def _poll(predicate, timeout: int = 300, interval: float = 5.0, desc: str = ""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise TimeoutError(f"condition not met within {timeout}s: {desc}")


# ---------------------------------------------------------------------------
# Cleanup registry — every entity this module creates on the app-llm instance
# is tracked here and removed/restored in the org_env teardown, so the shared
# LLM test database is exactly as clean after this module as before it.
# ---------------------------------------------------------------------------

_TRACK: dict[str, list] = {
    "rules": [],
    "libraries": [],
    "servers": [],
    "volumes": [],
    "channels": [],
    "agents": [],
    "downloaders": [],  # created only when no mock downloader existed
    "series_created": [],  # work ids this module POSTed
    "series_touched": [],  # (id, original fields) this module PUT over
    "collections": [],  # collection ids this module created
    "attachments": [],  # (collection_id, work_id) pairs this module attached
    "notification_ids": [],
}


def _delete(path: str, **kw) -> None:
    r = _api(path, method="delete", **kw)
    assert r.status_code in (200, 404), f"DELETE {path} failed: {r.status_code} {r.text}"


def _ensure_series() -> str:
    sid = None
    r = _api("/api/v1/series", params={"page_size": 100, "title": APOTHECARY_TITLE_CN})
    if r.status_code == 200:
        for s in r.json().get("data", []):
            if s.get("title_cn") == APOTHECARY_TITLE_CN:
                original = {
                    k: s.get(k)
                    for k in ("start_date", "is_anime", "number_of_seasons")
                }
                updates = {}
                if not s.get("start_date"):
                    updates["start_date"] = "2023-01-01"
                if s.get("is_anime") is None:
                    updates["is_anime"] = True
                if not s.get("number_of_seasons"):
                    updates["number_of_seasons"] = 1
                if updates:
                    r2 = _api(f"/api/v1/series/{s['id']}", method="put", json=updates)
                    assert r2.status_code == 200, r2.text
                _TRACK["series_touched"].append((s["id"], original))
                sid = s["id"]
                break
    if sid is None:
        r = _api(
            "/api/v1/series",
            method="post",
            json={
                "title_cn": APOTHECARY_TITLE_CN,
                "title_en": "The Apothecary Diaries",
                "start_date": "2023-01-01",
                "is_anime": True,
                "number_of_seasons": 1,
            },
        )
        assert r.status_code == 201, f"series create failed: {r.text}"
        sid = r.json()["data"]["id"]
        _TRACK["series_created"].append(sid)

    # The latest-completed annotation aggregates across the work's collection;
    # legacy rows predate the shell-collection invariant and have none — attach.
    r = _api(f"/api/v1/series/{sid}")
    assert r.status_code == 200
    if not r.json()["data"].get("collection"):
        r = _api(
            "/api/v1/collections",
            method="post",
            json={"title_cn": f"{APOTHECARY_TITLE_CN} 合集"},
        )
        assert r.status_code == 201, f"collection create failed: {r.text}"
        coll_id = r.json()["data"]["id"]
        _TRACK["collections"].append(coll_id)
        r = _api(
            f"/api/v1/collections/{coll_id}/works",
            method="post",
            json={"work_type": "series", "work_id": sid},
        )
        assert r.status_code == 201, f"attach work failed: {r.text}"
        _TRACK["attachments"].append((coll_id, sid))
    return sid


def _cleanup() -> None:
    """Teardown: remove every tracked entity from the app-llm instance.

    Order: cancel open plans → rules → libraries → media server → tasks →
    agent → channel (cascades resources) → series/collection restore →
    volume → downloader (only if this module created one).
    """
    # Cancel any still-open plans produced for this module's notifications.
    notification_ids = set(_TRACK["notification_ids"])
    rule_ids = set(_TRACK["rules"])
    for status in ("pending", "failed"):
        r = _api("/api/v1/organize/plans", params={"status": status, "page_size": 100})
        if r.status_code != 200:
            continue
        for plan in r.json().get("data", []):
            if (
                plan.get("notification_id") in notification_ids
                or plan.get("rule_id") in rule_ids
            ):
                _api(f"/api/v1/organize/plans/{plan['id']}/cancel", method="post")

    for rule_id in _TRACK["rules"]:
        _delete(f"/api/v1/organize-rules/{rule_id}")
    for lib_id in _TRACK["libraries"]:
        _delete(f"/api/v1/libraries/{lib_id}")
    for server_id in _TRACK["servers"]:
        _delete(f"/api/v1/media-servers/{server_id}")

    task_ids: list[str] = []
    for agent_id in _TRACK["agents"]:
        r = _api(f"/api/v1/agents/{agent_id}/tasks", params={"page_size": 100})
        if r.status_code == 200:
            task_ids.extend(t["id"] for t in r.json().get("data", []))
    for task_id in dict.fromkeys(task_ids):
        _delete(f"/api/v1/tasks/{task_id}")
    for agent_id in _TRACK["agents"]:
        _delete(f"/api/v1/agents/{agent_id}")
    for channel_id in _TRACK["channels"]:
        _delete(f"/api/v1/channels/{channel_id}")

    # Detach works this module attached to its own collections, then drop the
    # collections; delete created works (and their orphaned shell collection);
    # restore pre-existing works this module mutated.
    for coll_id, work_id in _TRACK["attachments"]:
        if work_id in _TRACK["series_created"]:
            continue  # the work itself is deleted below
        _delete(
            f"/api/v1/collections/{coll_id}/works/{work_id}",
            params={"work_type": "series"},
        )
    for sid in _TRACK["series_created"]:
        collection_id = None
        r = _api(f"/api/v1/series/{sid}")
        if r.status_code == 200:
            coll = r.json()["data"].get("collection")
            collection_id = coll.get("id") if coll else None
        _delete(f"/api/v1/series/{sid}")
        if collection_id:
            r = _api(f"/api/v1/collections/{collection_id}/works")
            if r.status_code == 200 and not r.json().get("data"):
                _delete(f"/api/v1/collections/{collection_id}")
    for coll_id in _TRACK["collections"]:
        _delete(f"/api/v1/collections/{coll_id}")
    for sid, original in _TRACK["series_touched"]:
        if sid in _TRACK["series_created"]:
            continue
        r = _api(f"/api/v1/series/{sid}", method="put", json=original)
        assert r.status_code in (200, 404), f"series restore failed: {r.text}"

    for volume_id in _TRACK["volumes"]:
        _delete(f"/api/v1/volumes/{volume_id}")
    for dl_id in _TRACK["downloaders"]:
        _delete(f"/api/v1/downloaders/{dl_id}")


def _ensure_mock_downloader() -> str:
    r = _api("/api/v1/downloaders", params={"page_size": 100})
    assert r.status_code == 200
    for d in r.json().get("data", []):
        if d.get("type") == "mock":
            return d["id"]
    r = _api(
        "/api/v1/downloaders",
        method="post",
        json={"name": f"Cov2 LLM Mock {uuid.uuid4().hex[:6]}", "type": "mock"},
    )
    assert r.status_code == 201, f"create mock downloader failed: {r.text}"
    dl_id = r.json()["data"]["id"]
    _TRACK["downloaders"].append(dl_id)
    return dl_id


@pytest.fixture(scope="module")
def org_env():
    """Volume + Plex scan + enabled rule + dispatched agent; waits for the
    notify tick to produce notifications and organize plans."""
    suffix = uuid.uuid4().hex[:6]

    # ── Volume + media server + derived libraries ───────────────────────
    r = _api(
        "/api/v1/volumes",
        method="post",
        json={"name": f"cov2-vol-{suffix}", "mount_path": "/tmp"},
    )
    assert r.status_code == 201, f"create volume failed: {r.text}"
    volume_id = r.json()["data"]["id"]
    _TRACK["volumes"].append(volume_id)

    r = _api(
        "/api/v1/media-servers",
        method="post",
        json={
            "name": f"cov2-plex-{suffix}",
            "type": "plex",
            "url": PLEX_URL,
            "token": "mock-token",
            "bindings": [
                {"server_path_prefix": "/data/tv", "volume_id": volume_id, "subpath": "tv"},
                {"server_path_prefix": "/data/movies", "volume_id": volume_id, "subpath": "movies"},
            ],
        },
    )
    assert r.status_code == 201, f"create media server failed: {r.text}"
    server_id = r.json()["data"]["id"]
    _TRACK["servers"].append(server_id)

    r = _api(f"/api/v1/media-servers/{server_id}/scan", method="post")
    assert r.status_code == 200, f"scan failed: {r.text}"
    r = _api("/api/v1/libraries")
    libs = [lib for lib in r.json()["data"] if lib["media_server_id"] == server_id]
    by_name = {lib["name"]: lib for lib in libs}
    tv_lib = by_name["TV Shows"]
    unbound_lib = by_name["Unbound Shows"]
    _TRACK["libraries"].extend(lib["id"] for lib in libs)

    # ── Enabled organize rule → the notify tick plans every notification ─
    # Priority 10 so this rule wins first-match over any residual enabled
    # rules left by earlier runs (ordered by priority, then created_at).
    r = _api(
        "/api/v1/organize-rules",
        method="post",
        json={
            "name": f"cov2-rule-{suffix}",
            "priority": 10,
            "library_id": tv_lib["id"],
            "path_template": "{title}/Season {season:02d}/{title} - s{season:02d}e{episode:02d}{ext}",
            "file_op": "move",
            "auto_execute": False,
        },
    )
    assert r.status_code == 201, f"create rule failed: {r.text}"
    rule_id = r.json()["data"]["id"]
    _TRACK["rules"].append(rule_id)

    # ── Series + channel + work-scoped agent dispatch ────────────────────
    series_id = _ensure_series()
    mock_dl = _ensure_mock_downloader()

    r = _api(
        "/api/v1/channels",
        method="post",
        json={
            "name": f"Cov2 LLM Channel {suffix}",
            "url": MIKANANI_S2_URL,
            "field_mapping": RICH_FIELD_MAPPING,
            "fetch_interval": 3600,
            "metadata_agent_enabled": False,
        },
    )
    assert r.status_code == 201, f"create channel failed: {r.text}"
    ch_id = r.json()["data"]["id"]
    _TRACK["channels"].append(ch_id)
    _api(f"/api/v1/channels/{ch_id}/fetch", method="post")

    def _fetch_done():
        r = _api(f"/api/v1/channels/{ch_id}/fetch-status")
        data = r.json().get("data") or {}
        return data if data.get("status") == "done" else None

    _poll(_fetch_done, timeout=180, desc="channel fetch")
    r = _api(f"/api/v1/channels/{ch_id}/resources", params={"page_size": 100})
    resources = r.json().get("data", [])
    linked = [res for res in resources if res.get("series_id") == series_id]
    assert len(linked) >= 2, "resources did not auto-link"

    by_episode: dict[int, dict] = {}
    for res in linked:
        by_episode.setdefault(res.get("episode"), res)
    eps = sorted(k for k in by_episode if k is not None)
    assert len(eps) >= 2

    r = _api(
        "/api/v1/agents",
        method="post",
        json={
            "name": f"Cov2 LLM Agent {suffix}",
            "channel_id": ch_id,
            "downloader_id": mock_dl,
            "scope_channel_wide": False,
            "llm_enabled": False,
            "conflict_resolution": "auto",
            "works": [{"content_type": "tv", "series_id": series_id}],
            "dispatch_resource_ids": [by_episode[eps[0]]["id"], by_episode[eps[1]]["id"]],
        },
    )
    assert r.status_code == 201, f"create agent failed: {r.text}"
    agent_id = r.json()["data"]["id"]
    _TRACK["agents"].append(agent_id)

    # ── Wait for completion → notification → organize plan ───────────────
    # The mock downloader store is in-memory and restarts its torrent ids at
    # 1 on every container boot, while residual completed DownloadTask rows
    # keep their old ids — the notify tick's stop-seeding can pause the fresh
    # torrents under the colliding ids. Resume any paused task until the sync
    # marks every task completed.
    def _tasks_completed():
        r = _api(f"/api/v1/agents/{agent_id}/tasks", params={"page_size": 100})
        if r.status_code != 200:
            return None
        tasks = r.json().get("data", [])
        if not tasks:
            return None
        for t in tasks:
            if t["status"] == "paused":
                _api(f"/api/v1/tasks/{t['id']}/resume", method="post")
        if all(t["status"] == "completed" for t in tasks):
            return tasks
        return None

    _poll(_tasks_completed, timeout=420, interval=10, desc="task completion")

    def _notifications():
        r = _api(f"/api/v1/agents/{agent_id}/notifications", params={"page_size": 100})
        if r.status_code != 200:
            return None
        rows = r.json().get("data", [])
        return rows if len(rows) >= 2 else None

    notifications = _poll(
        _notifications, timeout=420, interval=10,
        desc="notifications for completed mock downloads",
    )

    notification_ids = {n["id"] for n in notifications}
    _TRACK["notification_ids"].extend(sorted(notification_ids))

    def _plans():
        r = _api("/api/v1/organize/plans", params={"page_size": 100})
        if r.status_code != 200:
            return None
        rows = r.json().get("data", [])
        mine = [p for p in rows if p.get("notification_id") in notification_ids]
        return mine if mine else None

    plans = _poll(_plans, timeout=240, interval=10, desc="organize plans")

    env_dict = {
        "suffix": suffix,
        "volume_id": volume_id,
        "server_id": server_id,
        "tv_lib": tv_lib,
        "unbound_lib": unbound_lib,
        "rule_id": rule_id,
        "series_id": series_id,
        "channel_id": ch_id,
        "agent_id": agent_id,
        "mock_dl": mock_dl,
        "resources": linked,
        "notifications": notifications,
        "plans": plans,
    }
    yield env_dict
    _cleanup()


# =========================================================================
# Plans list / detail / audit
# =========================================================================


class TestPlanListing:
    def test_list_filters(self, org_env):
        # Mock downloads write no files to disk, so planning deterministically
        # rejects them: every produced plan is `failed` with a clear reason.
        plan = org_env["plans"][0]
        assert plan["status"] == "failed"
        assert plan["error_message"], "failed plan carries no error message"

        r = _api("/api/v1/organize/plans", params={"status": "failed", "page_size": 100})
        assert r.status_code == 200
        assert any(p["id"] == plan["id"] for p in r.json()["data"])

        # library_id filter is accepted and narrows the listing.
        r = _api(
            "/api/v1/organize/plans",
            params={"library_id": org_env["tv_lib"]["id"], "page_size": 100},
        )
        assert r.status_code == 200
        assert all(
            p["library_id"] == org_env["tv_lib"]["id"] for p in r.json()["data"]
        )

        # List item shape: ops summary + pending_reason derivation.
        r = _api("/api/v1/organize/plans", params={"page_size": 100})
        item = next(p for p in r.json()["data"] if p["id"] == plan["id"])
        assert "total" in item["ops_summary"]
        assert "pending_reason" in item

    def test_plan_detail(self, org_env):
        plan = org_env["plans"][0]
        r = _api(f"/api/v1/organize/plans/{plan['id']}")
        assert r.status_code == 200, f"plan detail failed: {r.text}"
        detail = r.json()["data"]
        assert detail["payload"]["resource"]["id"], "payload resource id missing"
        assert detail["resource_id"], "derived resource_id missing"
        assert isinstance(detail["ops"], list)
        assert isinstance(detail["audit_entries"], list)

    def test_audit_plan_id_filter(self, org_env):
        r = _api(
            "/api/v1/organize/audit",
            params={"plan_id": org_env["plans"][0]["id"]},
        )
        assert r.status_code == 200
        for entry in r.json()["data"]:
            assert entry["plan_id"] == org_env["plans"][0]["id"]


# =========================================================================
# Library update validation + delete blocking
# =========================================================================


class TestLibraryGuards:
    def test_update_subpath_validation(self, org_env):
        lib = org_env["unbound_lib"]
        r = _api(
            f"/api/v1/libraries/{lib['id']}",
            method="put",
            json={"root_subpath": "../escape"},
        )
        assert r.status_code == 422
        r = _api(
            f"/api/v1/libraries/{lib['id']}",
            method="put",
            json={"recycle_subpath": "/absolute"},
        )
        assert r.status_code == 422
        # Valid recycle subpath is accepted.
        r = _api(
            f"/api/v1/libraries/{lib['id']}",
            method="put",
            json={"recycle_subpath": "recycle-bin"},
        )
        assert r.status_code == 200, f"recycle update failed: {r.text}"
        assert r.json()["data"]["recycle_subpath"] == "recycle-bin"

    def test_delete_blocked_by_rule(self, org_env):
        # The flow rule still points at the TV library → DELETE_BLOCKED.
        r = _api(f"/api/v1/libraries/{org_env['tv_lib']['id']}", method="delete")
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "DELETE_BLOCKED"


# =========================================================================
# Rule update branches
# =========================================================================


class TestRuleUpdate:
    def test_update_all_fields(self, org_env):
        # A scratch rule so we don't disturb the flow rule.
        r = _api(
            "/api/v1/organize-rules",
            method="post",
            json={
                "name": f"cov2-scratch-{org_env['suffix']}",
                "priority": 950,
                "library_id": org_env["tv_lib"]["id"],
                "path_template": "{title}{ext}",
                "enabled": False,
            },
        )
        assert r.status_code == 201, f"create scratch rule failed: {r.text}"
        rule_id = r.json()["data"]["id"]
        _TRACK["rules"].append(rule_id)
        try:
            r = _api(
                f"/api/v1/organize-rules/{rule_id}",
                method="put",
                json={
                    "name": f"cov2-scratch-renamed-{org_env['suffix']}",
                    "priority": 951,
                    "enabled": True,
                    "filter": {"field": "is_batch", "operator": "eq", "value": False},
                    "path_template": "{title}/{title}{ext}",
                    "file_op": "copy",
                    "auto_execute": True,
                },
            )
            assert r.status_code == 200, f"rule update failed: {r.text}"
            data = r.json()["data"]
            assert data["priority"] == 951
            assert data["file_op"] == "copy"
            assert data["auto_execute"] is True
            assert data["enabled"] is True

            # Invalid filter → 422; unknown library → 404.
            r = _api(
                f"/api/v1/organize-rules/{rule_id}",
                method="put",
                json={"filter": {"field": "resolution", "operator": "eq", "value": ""}},
            )
            assert r.status_code == 422
            r = _api(
                f"/api/v1/organize-rules/{rule_id}",
                method="put",
                json={"library_id": "no-such-lib"},
            )
            assert r.status_code == 404

            # Unknown library in the update's library switch.
            r = _api(
                f"/api/v1/organize-rules/{rule_id}",
                method="put",
                json={"library_id": org_env["unbound_lib"]["id"]},
            )
            assert r.status_code == 200
            assert r.json()["data"]["library_id"] == org_env["unbound_lib"]["id"]
        finally:
            _api(f"/api/v1/organize-rules/{rule_id}", method="delete")


# =========================================================================
# Preview (draft rules + notification/resource payloads)
# =========================================================================


class TestPreview:
    def test_notification_payload_with_draft_rule(self, org_env):
        notification_id = org_env["notifications"][0]["id"]
        r = _api(
            "/api/v1/organize-rules/preview",
            method="post",
            json={
                "notification_id": notification_id,
                "rule": {
                    "name": "preview-draft",
                    "priority": 1,
                    "library_id": org_env["tv_lib"]["id"],
                    "path_template": "{title}/Season {season:02d}/{title} - s{season:02d}e{episode:02d}{ext}",
                    "file_op": "move",
                },
            },
        )
        # Mock downloads leave nothing on disk, so planning rejects the
        # preview with a PlanError (422); a fully-seeded environment renders
        # ops instead. Either way the request was validated and planned.
        assert r.status_code in (200, 422), f"unexpected: {r.status_code} {r.text}"
        if r.status_code == 200:
            data = r.json()["data"]
            assert data["matched_rule"]["name"] == "preview-draft"
            assert data["ops"]
        else:
            assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_notification_payload_current_rules(self, org_env):
        notification_id = org_env["notifications"][0]["id"]
        r = _api(
            "/api/v1/organize-rules/preview",
            method="post",
            json={"notification_id": notification_id},
        )
        assert r.status_code in (200, 422), f"unexpected: {r.status_code} {r.text}"
        if r.status_code == 200:
            assert "ops" in r.json()["data"]
        else:
            assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_resource_payload(self, org_env):
        # Dispatched resource has a download task → payload is built live.
        res = org_env["resources"][0]
        r = _api(
            "/api/v1/organize-rules/preview",
            method="post",
            json={"resource_id": res["id"]},
        )
        assert r.status_code in (200, 422), f"unexpected: {r.status_code} {r.text}"
        if r.status_code == 200:
            assert "ops" in r.json()["data"]
        else:
            assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_resource_without_task_422(self, org_env):
        # A channel resource that was never dispatched has no download task.
        r = _api(
            f"/api/v1/channels/{org_env['channel_id']}/resources",
            params={"page_size": 100},
        )
        untasked = [
            res for res in r.json()["data"] if not res.get("has_download_task")
        ]
        assert untasked, "expected an undispatched resource"
        r = _api(
            "/api/v1/organize-rules/preview",
            method="post",
            json={"resource_id": untasked[0]["id"]},
        )
        assert r.status_code == 422

    def test_404s(self, org_env):
        r = _api(
            "/api/v1/organize-rules/preview",
            method="post",
            json={"notification_id": "no-such-notification"},
        )
        assert r.status_code == 404
        r = _api(
            "/api/v1/organize-rules/preview",
            method="post",
            json={"resource_id": "no-such-resource"},
        )
        assert r.status_code == 404
        # Draft rule referencing an unknown library → 404.
        r = _api(
            "/api/v1/organize-rules/preview",
            method="post",
            json={
                "notification_id": org_env["notifications"][0]["id"],
                "rule": {
                    "name": "draft",
                    "library_id": "no-such-lib",
                    "path_template": "{title}{ext}",
                },
            },
        )
        assert r.status_code == 404
        # Draft rule with an invalid template → 422.
        r = _api(
            "/api/v1/organize-rules/preview",
            method="post",
            json={
                "notification_id": org_env["notifications"][0]["id"],
                "rule": {
                    "name": "draft",
                    "library_id": org_env["tv_lib"]["id"],
                    "path_template": "/abs/path",
                },
            },
        )
        assert r.status_code == 422


# =========================================================================
# Classify / execute / cancel lifecycle
# =========================================================================


class TestPlanLifecycle:
    def test_classify_execute_cancel_flow(self, org_env):
        plans = list(org_env["plans"])
        plan = next(p for p in plans if p["status"] in ("pending", "failed"))
        plan_id = plan["id"]

        # Unknown library → 404.
        r = _api(
            f"/api/v1/organize/plans/{plan_id}/classify",
            method="post",
            json={"library_id": "no-such-lib"},
        )
        assert r.status_code == 404

        # Re-classify onto the Movies library: re-rendering the ops needs the
        # download's files on disk, which mock downloads never write → the
        # service rejects with OrganizeError (422).
        r = _api("/api/v1/libraries")
        movies_lib = next(
            lib for lib in r.json()["data"]
            if lib["media_server_id"] == org_env["server_id"] and lib["name"] == "Movies"
        )
        r = _api(
            f"/api/v1/organize/plans/{plan_id}/classify",
            method="post",
            json={"library_id": movies_lib["id"], "category": "anime"},
        )
        assert r.status_code in (200, 422), f"unexpected: {r.status_code} {r.text}"
        classified = r.status_code == 200
        if classified:
            data = r.json()["data"]
            assert data["library_id"] == movies_lib["id"]
            assert data["category"] == "anime"
        else:
            assert r.json()["error"]["code"] == "VALIDATION_ERROR"

        # Execute: without a library (mock downloads leave nothing on disk,
        # so classify could not re-render) the unclassified gate 409s; a
        # classified plan is scheduled in the background (202).
        r = _api(f"/api/v1/organize/plans/{plan_id}/execute", method="post")
        if classified:
            assert r.status_code == 202, f"execute failed: {r.text}"
            assert r.json()["data"]["id"] == plan_id

            def _terminal():
                r = _api(f"/api/v1/organize/plans/{plan_id}")
                if r.status_code != 200:
                    return None
                p = r.json()["data"]
                return p if p["status"] in ("done", "failed") else None

            final = _poll(_terminal, timeout=120, interval=3, desc="plan execution")
            assert final["status"] in ("done", "failed")
        else:
            assert r.status_code == 409
            assert r.json()["error"]["code"] == "INVALID_STATE"

        # Batch execution over a terminal plan reports per-plan results.
        r = _api(
            "/api/v1/organize/plans/execute-batch",
            method="post",
            json={"plan_ids": [plan_id]},
        )
        assert r.status_code == 200
        results = r.json()["data"]["results"]
        assert results and results[0]["plan_id"] == plan_id

        # Cancel with task cleanup: the linked download task is removed from
        # the mock downloader and marked cancelled.
        r = _api(
            f"/api/v1/organize/plans/{plan_id}/cancel",
            method="post",
            json={"delete_task": True},
        )
        if r.status_code == 200:
            assert r.json()["data"]["status"] == "cancelled"
            assert r.json()["data"]["task_cleaned"] is not None
        else:
            # A done plan can no longer be cancelled.
            assert r.status_code == 409
            assert r.json()["error"]["code"] == "INVALID_STATE"

    def test_execute_terminal_plan_409(self, org_env):
        cancelled = None
        r = _api("/api/v1/organize/plans", params={"status": "cancelled", "page_size": 5})
        rows = r.json().get("data", [])
        if rows:
            cancelled = rows[0]
        if cancelled is None:
            pytest.skip("no cancelled plan available")
        r = _api(f"/api/v1/organize/plans/{cancelled['id']}/execute", method="post")
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "INVALID_STATE"

    def test_classify_terminal_plan_409(self, org_env):
        r = _api("/api/v1/organize/plans", params={"status": "cancelled", "page_size": 5})
        rows = r.json().get("data", [])
        if not rows:
            pytest.skip("no cancelled plan available")
        r = _api(
            f"/api/v1/organize/plans/{rows[0]['id']}/classify",
            method="post",
            json={"library_id": org_env["tv_lib"]["id"]},
        )
        assert r.status_code == 409


# =========================================================================
# Dashboard plans + resources task outcomes + agent progress annotation
# =========================================================================


class TestDashboardPlans:
    def test_overview_lists_pending_plans(self, org_env):
        r = _api("/api/v1/dashboard/overview", params={"page_size": 50})
        assert r.status_code == 200
        data = r.json()["data"]
        assert "pending_plans" in data
        assert isinstance(data["pending_plans_total"], int)
        for plan in data["pending_plans"]:
            assert plan["status"] == "pending"

    def test_ignore_plan_todo(self, org_env):
        # Re-fetch: earlier lifecycle tests may have cancelled fixture plans.
        r = _api("/api/v1/organize/plans", params={"page_size": 100})
        notification_ids = {n["id"] for n in org_env["notifications"]}
        open_plans = [
            p for p in r.json().get("data", [])
            if p.get("notification_id") in notification_ids
            and p["status"] in ("pending", "failed")
        ]
        if not open_plans:
            pytest.skip("no open plan left to ignore")
        plan_id = open_plans[0]["id"]
        r = _api(
            "/api/v1/dashboard/todos/ignore",
            method="post",
            json={"kind": "plan", "ids": [plan_id]},
        )
        assert r.status_code == 200, f"ignore failed: {r.text}"
        assert r.json()["data"]["ignored"] == 1

        r = _api(f"/api/v1/organize/plans/{plan_id}")
        assert r.json()["data"]["status"] == "cancelled"

        # The ignore wrote an audit entry attributed to the dashboard.
        r = _api("/api/v1/organize/audit", params={"plan_id": plan_id})
        assert r.status_code == 200
        actions = [e["action"] for e in r.json()["data"]]
        assert "cancelled" in actions


class TestResourceTaskOutcome:
    def test_completed_task_outcome_on_listing(self, org_env):
        r = _api(
            f"/api/v1/channels/{org_env['channel_id']}/resources",
            params={"page_size": 100},
        )
        assert r.status_code == 200
        tasked = [res for res in r.json()["data"] if res["has_download_task"]]
        assert tasked, "no tasked resources"
        # Mock downloads completed minutes ago; organize cleanup may have
        # flipped some to cancelled — every tasked row must still surface an
        # outcome string.
        outcomes = {res["download_status"] for res in tasked}
        assert outcomes <= {"completed", "organized", "cancelled"}
        assert "completed" in outcomes or "organized" in outcomes

    def test_agent_work_latest_completed_annotation(self, org_env):
        r = _api(f"/api/v1/agents/{org_env['agent_id']}")
        assert r.status_code == 200
        works = r.json()["data"]["works"]
        assert works, "agent works missing"
        work = works[0]
        assert work["series_id"] == org_env["series_id"]
        assert work["latest_completed_episode"] is not None, (
            "latest completed episode not annotated after real downloads"
        )

        # Same annotation through the works collection endpoint.
        r = _api(f"/api/v1/agents/{org_env['agent_id']}/works")
        assert r.status_code == 200
        assert r.json()["data"][0]["latest_completed_episode"] is not None


class TestQueueSchedulerEnabled:
    def test_scheduler_snapshot_lists_jobs(self, org_env):
        r = _api("/api/v1/queue/scheduler")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["enabled"] is True
        assert data["jobs"], "expected APScheduler jobs on the app-llm instance"
        job = data["jobs"][0]
        assert job["id"] and job["trigger"]
