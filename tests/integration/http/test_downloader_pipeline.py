"""Downloader CRUD, connection test, and task listing integration tests.

Tests the downloader lifecycle:
  - Downloader CRUD (create/read/list/update/delete)
  - Connection test (Transmission RPC reachability)
  - Task listing via downloader-scoped endpoint

Requirements: Docker test environment with app + transmission services.

Usage:
    docker compose -f docker-compose.test.yml up --build
    uv run pytest tests/integration/test_downloader_pipeline.py -v --timeout=120
"""

from __future__ import annotations

import pytest

from tests.integration.http._http import _api

# =========================================================================
# TestDownloaderCRUD — lifecycle of a downloader instance
# =========================================================================


class TestDownloaderCRUD:
    """CRUD operations for downloader instances."""

    downloader_id: str = ""
    downloader_name: str = "Test Downloader"

    def test_create_downloader(self):
        """POST /downloaders — create a downloader with Transmission config."""
        r = _api(
            "/api/v1/downloaders",
            method="post",
            json={
                "name": self.downloader_name,
                "type": "transmission",
                "url": "http://transmission:9091/transmission/rpc",
                "download_dir": "/downloads/test",
            },
        )
        assert r.status_code == 201, f"create downloader failed: {r.status_code} {r.text}"
        data = r.json()["data"]
        assert data["name"] == self.downloader_name
        assert data["type"] == "transmission"
        assert data["url"] == "http://transmission:9091/transmission/rpc"
        assert data["download_dir"] == "/downloads/test"

        TestDownloaderCRUD.downloader_id = data["id"]

    def test_list_downloaders(self):
        """GET /downloaders — verify total >= 1 and our downloader appears."""
        r = _api("/api/v1/downloaders")
        assert r.status_code == 200, f"list downloaders failed: {r.text}"
        body = r.json()
        assert body["data"] is not None
        meta = body.get("meta", {})
        assert meta.get("total", 0) >= 1, "Expected at least one downloader"

        if TestDownloaderCRUD.downloader_id:
            ids = [d["id"] for d in body["data"]]
            assert TestDownloaderCRUD.downloader_id in ids, (
                f"Downloader {TestDownloaderCRUD.downloader_id} not found in list"
            )

    def test_get_downloader(self):
        """GET /downloaders/{id} — verify fields match creation payload."""
        if not TestDownloaderCRUD.downloader_id:
            pytest.skip("No downloader created — prerequisite test failed")

        r = _api(f"/api/v1/downloaders/{TestDownloaderCRUD.downloader_id}")
        assert r.status_code == 200, f"get downloader failed: {r.text}"
        data = r.json()["data"]
        assert data["id"] == TestDownloaderCRUD.downloader_id
        assert data["name"] == self.downloader_name
        assert data["type"] == "transmission"
        assert data["url"] == "http://transmission:9091/transmission/rpc"

    def test_update_downloader(self):
        """PUT /downloaders/{id} — update name, verify it persisted."""
        if not TestDownloaderCRUD.downloader_id:
            pytest.skip("No downloader created — prerequisite test failed")

        new_name = "Test Downloader (Updated)"
        r = _api(
            f"/api/v1/downloaders/{TestDownloaderCRUD.downloader_id}",
            method="put",
            json={"name": new_name},
        )
        assert r.status_code == 200, f"update downloader failed: {r.text}"
        assert r.json()["data"]["name"] == new_name

        # Verify persisted
        r2 = _api(f"/api/v1/downloaders/{TestDownloaderCRUD.downloader_id}")
        assert r2.json()["data"]["name"] == new_name

    def test_test_connection(self):
        """POST /downloaders/{id}/test — test Transmission RPC connectivity."""
        if not TestDownloaderCRUD.downloader_id:
            pytest.skip("No downloader created — prerequisite test failed")

        r = _api(
            f"/api/v1/downloaders/{TestDownloaderCRUD.downloader_id}/test",
            method="post",
        )
        # Connection test may succeed or fail depending on Transmission state;
        # we just verify the endpoint is reachable and returns a valid response.
        assert r.status_code in (200, 502), (
            f"test connection unexpected status: {r.status_code} {r.text}"
        )
        body = r.json()
        assert "success" in body, f"Response missing 'success': {body}"
        # Status should now reflect the connectivity check result
        r2 = _api(f"/api/v1/downloaders/{TestDownloaderCRUD.downloader_id}")
        status = r2.json()["data"].get("status")
        assert status in ("connected", "disconnected", "error"), (
            f"Unexpected downloader status after test: {status}"
        )


# =========================================================================
# TestDownloaderTasks — task listing and deletion
# =========================================================================


class TestDownloaderTasks:
    """Task listing and downloader deletion tests."""

    def test_list_tasks(self):
        """GET /downloaders/{id}/tasks — verify returns list (may be empty)."""
        if not TestDownloaderCRUD.downloader_id:
            pytest.skip("No downloader created — prerequisite test failed")

        r = _api(f"/api/v1/downloaders/{TestDownloaderCRUD.downloader_id}/tasks")
        assert r.status_code == 200, f"list tasks failed: {r.status_code} {r.text}"
        body = r.json()
        assert body["success"] is True
        assert isinstance(body.get("data"), list), (
            f"Expected list data, got: {type(body.get('data')).__name__}"
        )
        # Meta may or may not be present; if present, total should be >= 0
        meta = body.get("meta", {})
        if "total" in meta:
            assert meta["total"] >= 0

    def test_delete_downloader(self):
        """DELETE /downloaders/{id} — remove downloader; accept 200 or 409."""
        if not TestDownloaderCRUD.downloader_id:
            pytest.skip("No downloader created — prerequisite test failed")

        r = _api(
            f"/api/v1/downloaders/{TestDownloaderCRUD.downloader_id}",
            method="delete",
        )
        # 200 = deleted successfully; 409 = blocked (may have associated agents)
        assert r.status_code in (200, 409), (
            f"delete downloader unexpected status: {r.status_code} {r.text}"
        )

        if r.status_code == 200:
            # Verify gone
            r2 = _api(f"/api/v1/downloaders/{TestDownloaderCRUD.downloader_id}")
            assert r2.status_code == 404, (
                f"Downloader still exists after delete: {r2.status_code}"
            )
