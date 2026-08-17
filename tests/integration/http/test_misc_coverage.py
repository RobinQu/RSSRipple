"""Miscellaneous API coverage (primary app).

Targets several small API surfaces that the focused suites only touch
incidentally:

  - /auth/status, /auth/otp (bad-code 401) and /auth/logout
  - /volumes CRUD + duplicate-name 409 + /volumes/dirs error paths
  - /agents/{id}/decisions listing + decision action 404s
  - /dashboard
  - /series + /movies 404 / validation edges

Requirements: Docker test environment (app + test-server).
"""

from __future__ import annotations

import uuid

import pytest

from tests.integration.http._http import (
    TEST_SERVER,
    _api,
)

# =========================================================================
# Auth — session endpoints (no TOTP secret needed for these paths)
# =========================================================================


class TestAuth:
    def test_status_with_api_key(self):
        r = _api("/api/v1/auth/status")
        assert r.status_code == 200
        assert r.json()["data"]["authenticated"] is True

    def test_status_without_credentials(self):
        import httpx

        from tests.integration.http._http import RSSRIPPLE

        # Use a bare client — the conftest wraps module-level httpx.get and
        # injects X-API-Key into it.
        r = httpx.Client().get(f"{RSSRIPPLE}/api/v1/auth/status")
        assert r.status_code == 200
        assert r.json()["data"]["authenticated"] is False

    def test_otp_bad_code_401(self):
        r = _api("/api/v1/auth/otp", method="post", json={"code": "000000"})
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "UNAUTHORIZED"

    def test_logout(self):
        r = _api("/api/v1/auth/logout", method="post")
        assert r.status_code == 200
        assert r.json()["data"]["authenticated"] is False


# =========================================================================
# Volumes
# =========================================================================


class TestVolumes:
    def test_crud_and_duplicate_409(self):
        name = f"vol-{uuid.uuid4().hex[:8]}"
        r = _api(
            "/api/v1/volumes",
            method="post",
            json={"name": name, "mount_path": "/tmp"},
        )
        assert r.status_code == 201, f"create volume failed: {r.text}"
        vol = r.json()["data"]
        assert vol["mount_path"] == "/tmp"

        # Duplicate name → 409.
        r = _api(
            "/api/v1/volumes",
            method="post",
            json={"name": name, "mount_path": "/app"},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "DUPLICATE_SUBMISSION"

        # List + detail + update + check + delete.
        r = _api("/api/v1/volumes")
        assert r.status_code == 200
        assert any(v["id"] == vol["id"] for v in r.json()["data"])

        r = _api(f"/api/v1/volumes/{vol['id']}")
        assert r.status_code == 200
        assert r.json()["data"]["name"] == name

        r = _api(
            f"/api/v1/volumes/{vol['id']}",
            method="put",
            json={"name": f"{name}-renamed", "mount_path": "/app"},
        )
        assert r.status_code == 200, f"update volume failed: {r.text}"
        assert r.json()["data"]["mount_path"] == "/app"

        r = _api(f"/api/v1/volumes/{vol['id']}/check", method="post")
        assert r.status_code == 200

        r = _api(f"/api/v1/volumes/{vol['id']}", method="delete")
        assert r.status_code == 200
        assert r.json()["data"]["deleted"] is True
        assert _api(f"/api/v1/volumes/{vol['id']}").status_code == 404

    def test_404_and_validation_paths(self):
        assert _api("/api/v1/volumes/no-such").status_code == 404
        r = _api(
            "/api/v1/volumes/no-such",
            method="put",
            json={"name": "x"},
        )
        assert r.status_code == 404
        assert _api("/api/v1/volumes/no-such", method="delete").status_code == 404

    def test_list_directories(self):
        r = _api("/api/v1/volumes/dirs", params={"path": "/"})
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["exists"] is True
        assert isinstance(data["dirs"], list)

        r = _api("/api/v1/volumes/dirs", params={"path": "/definitely-not-a-dir"})
        assert r.status_code == 422


# =========================================================================
# Decisions — listing + action 404s
# =========================================================================


@pytest.fixture(scope="class")
def _agent():
    from tests.integration.http._http import DEFAULT_FIELD_MAPPING

    r = _api(
        "/api/v1/channels",
        method="post",
        json={
            "name": f"Misc Decisions Channel {uuid.uuid4().hex[:6]}",
            "url": f"{TEST_SERVER}/rss/mikanani-1",
            "field_mapping": DEFAULT_FIELD_MAPPING,
            "fetch_interval": 3600,
            "metadata_agent_enabled": False,
        },
    )
    assert r.status_code == 201, f"create channel failed: {r.text}"
    ch_id = r.json()["data"]["id"]

    r = _api("/api/v1/downloaders", params={"page_size": 100})
    dl_id = next((d["id"] for d in r.json().get("data", []) if d.get("type") == "mock"), None)
    if dl_id is None:
        r = _api(
            "/api/v1/downloaders",
            method="post",
            json={"name": "Misc Decisions Downloader", "type": "mock"},
        )
        dl_id = r.json()["data"]["id"]

    r = _api(
        "/api/v1/agents",
        method="post",
        json={
            "name": f"Misc Decisions Agent {uuid.uuid4().hex[:6]}",
            "channel_id": ch_id,
            "downloader_id": dl_id,
            "scope_channel_wide": True,
        },
    )
    assert r.status_code == 201, f"create agent failed: {r.text}"
    return r.json()["data"]["id"]


class TestDecisions:
    def test_list_decisions_empty_and_agent_404(self, _agent):
        r = _api(f"/api/v1/agents/{_agent}/decisions")
        assert r.status_code == 200
        assert r.json()["data"] == []

        # Unknown agent → empty list (no existence check on the router).
        r = _api("/api/v1/agents/no-such-agent/decisions")
        assert r.status_code == 200
        assert r.json()["data"] == []

    def test_decision_actions_404(self):
        # confirm requires a body (resource_id) to reach the 404 lookup.
        r = _api(
            "/api/v1/decisions/no-such/confirm",
            method="post",
            json={"resource_id": "x"},
        )
        assert r.status_code == 404
        for action in ("skip", "ai-pick"):
            r = _api(f"/api/v1/decisions/no-such/{action}", method="post")
            assert r.status_code == 404, f"{action} should 404"


# =========================================================================
# Dashboard + work 404s
# =========================================================================


class TestDashboard:
    def test_dashboard(self):
        r = _api("/api/v1/dashboard")
        assert r.status_code == 200
        data = r.json()["data"]
        assert "active_download_count" in data


class TestWork404s:
    def test_series_movie_404s_and_validation(self):
        assert _api("/api/v1/series/no-such").status_code == 404
        assert _api("/api/v1/movies/no-such").status_code == 404

        # A genre outside the closed TMDB set → 422 (Create).
        r = _api("/api/v1/series", method="post", json={"title_cn": "x", "genre": ["NotAGenre"]})
        assert r.status_code == 422, f"series genre should 422: {r.text}"
        r = _api("/api/v1/movies", method="post", json={"title_cn": "x", "genre": ["NotAGenre"]})
        assert r.status_code == 422, f"movie genre should 422: {r.text}"
