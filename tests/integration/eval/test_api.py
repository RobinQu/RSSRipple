"""API tests for the RSSRipple Metadata Eval Tool.

Tests all CRUD endpoints for datasets, title loading, metadata search,
and dataset delete endpoint + metadata source tracking.
"""

from __future__ import annotations

import os

import httpx
import pytest

EVAL_URL = os.environ.get("EVAL_URL", "http://localhost:8090")


@pytest.fixture
async def client():
    """Async HTTP client bound to the running eval server."""
    async with httpx.AsyncClient(base_url=EVAL_URL, timeout=30.0) as c:
        yield c


# ══════════════════════════════════════════════════════════════════════════
# Health / Index
# ══════════════════════════════════════════════════════════════════════════


class TestIndex:
    async def test_index_returns_html(self, client):
        r = await client.get("/")
        assert r.status_code == 200
        assert "<html" in r.text.lower()

    async def test_api_not_found(self, client):
        r = await client.get("/api/nonexistent")
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════════
# Dataset CRUD
# ══════════════════════════════════════════════════════════════════════════


class TestDatasetCreate:
    TEST_DS = "pytest-eval-v1"

    async def test_create_dataset(self, client):
        payload = {
            "name": self.TEST_DS,
            "data_source_type": "tmdb",
            "entries": [
                {
                    "id": "test-id-001",
                    "raw_title": "[KissSub] One Piece 1120 [1080p][CHS]",
                    "source_feed": "kisssub",
                    "resource_metadata": {
                        "clean_title": "One Piece",
                        "content_type": "tv",
                        "episode": 1120,
                        "confidence": 0.95,
                    },
                    "review_status": "accepted",
                    "notes": None,
                },
                {
                    "id": "test-id-002",
                    "raw_title": "[Mikan] Demon Slayer S05E01 [2160p]",
                    "source_feed": "mikanani",
                    "resource_metadata": {
                        "clean_title": "Demon Slayer",
                        "content_type": "tv",
                        "episode": 1,
                        "season": 5,
                        "confidence": 0.88,
                    },
                    "review_status": "pending",
                    "notes": None,
                },
            ],
        }
        r = await client.post("/api/datasets", json=payload)
        assert r.status_code == 200
        data = r.json()
        assert data["saved"] is True
        assert data["total_entries"] == 2
        assert data["name"] == self.TEST_DS
        assert data["data_source_type"] == "tmdb"

    async def test_create_duplicate_overwrites(self, client):
        """Creating a dataset with same name overwrites old entries."""
        payload = {
            "name": self.TEST_DS,
            "entries": [
                {
                    "id": "test-id-003",
                    "raw_title": "[EZT] Latest Movie [1080p]",
                    "source_feed": "eztv",
                    "resource_metadata": {"clean_title": "Latest Movie", "content_type": "movie", "confidence": 0.7},
                    "review_status": "accepted",
                    "notes": None,
                },
            ],
        }
        r = await client.post("/api/datasets", json=payload)
        assert r.status_code == 200
        data = r.json()
        assert data["saved"] is True
        assert data["total_entries"] == 1  # overwrote the 2 entries

    async def test_create_empty_name_rejected(self, client):
        r = await client.post("/api/datasets", json={"name": "  ", "entries": []})
        assert r.status_code == 422


class TestDatasetList:
    async def test_list_datasets(self, client):
        r = await client.get("/api/datasets")
        assert r.status_code == 200
        data = r.json()
        assert "datasets" in data
        names = [d["name"] for d in data["datasets"]]
        assert "pytest-eval-v1" in names

    async def test_list_returns_metadata(self, client):
        r = await client.get("/api/datasets")
        data = r.json()
        ds = next(d for d in data["datasets"] if d["name"] == "pytest-eval-v1")
        assert "total_entries" in ds
        assert "data_source_type" in ds
        assert ds["total_entries"] == 1  # from overwrite test


class TestDatasetGet:
    async def test_get_existing_dataset(self, client):
        r = await client.get("/api/datasets/pytest-eval-v1")
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "pytest-eval-v1"
        assert data["data_source_type"] in ("combined", "tmdb", "exa")
        assert len(data["entries"]) == 1
        assert data["entries"][0]["raw_title"] == "[EZT] Latest Movie [1080p]"
        assert data["entries"][0]["review_status"] == "accepted"

    async def test_get_nonexistent_dataset(self, client):
        r = await client.get("/api/datasets/nonexistent-ds-xyz")
        assert r.status_code == 404


class TestDatasetDelete:
    async def test_delete_existing_dataset(self, client):
        r = await client.delete("/api/datasets/pytest-eval-v1")
        assert r.status_code == 200
        data = r.json()
        assert data["deleted"] is True
        assert data["name"] == "pytest-eval-v1"

    async def test_delete_idempotent(self, client):
        """Deleting a non-existent dataset should still succeed."""
        r = await client.delete("/api/datasets/pytest-eval-v1")
        assert r.status_code == 200
        data = r.json()
        assert data["deleted"] is True
        # db_entries_removed may be 0 since it was already deleted

    async def test_deleted_dataset_not_listed(self, client):
        """After deletion, dataset should not appear in list."""
        r = await client.get("/api/datasets")
        data = r.json()
        names = [d["name"] for d in data["datasets"]]
        assert "pytest-eval-v1" not in names


class TestDatasetSourceTracking:
    """Verify datasets track source (db vs json)."""
    TEST_DS = "pytest-source-test"

    async def test_dataset_source_is_db(self, client):
        """When saved with save_to_db=True, source should be 'db'."""
        payload = {
            "name": self.TEST_DS,
            "save_to_db": True,
            "save_to_json": False,
            "entries": [
                {
                    "id": "src-001",
                    "raw_title": "Test Title",
                    "source_feed": "kisssub",
                    "resource_metadata": {"clean_title": "Test", "content_type": "tv"},
                    "review_status": "accepted",
                    "notes": None,
                },
            ],
        }
        r = await client.post("/api/datasets", json=payload)
        assert r.status_code == 200

        r = await client.get(f"/api/datasets/{self.TEST_DS}")
        assert r.status_code == 200
        data = r.json()
        assert data.get("source") == "db"

        # Cleanup
        await client.delete(f"/api/datasets/{self.TEST_DS}")


# ══════════════════════════════════════════════════════════════════════════
# Title Loading
# ══════════════════════════════════════════════════════════════════════════


class TestLoadTitles:
    async def test_load_all_feeds(self, client):
        r = await client.post("/api/load-titles")
        assert r.status_code == 200
        data = r.json()
        assert "titles" in data
        assert data["total"] > 0
        for t in data["titles"]:
            assert "id" in t
            assert "raw_title" in t
            assert "source_feed" in t

    async def test_load_specific_feed(self, client):
        r = await client.post("/api/load-titles?feeds=kisssub")
        assert r.status_code == 200
        data = r.json()
        for t in data["titles"]:
            assert t["source_feed"] == "kisssub"

    async def test_load_with_sample(self, client):
        r = await client.post("/api/load-titles?sample_size=5")
        assert r.status_code == 200
        data = r.json()
        assert len(data["titles"]) <= 5

    async def test_load_unknown_feed_rejected(self, client):
        r = await client.post("/api/load-titles?feeds=unknown_feed")
        assert r.status_code == 422

    async def test_dedup_by_title(self, client):
        """Loading same feed twice should deduplicate."""
        r1 = await client.post("/api/load-titles?feeds=kisssub")
        r2 = await client.post("/api/load-titles?feeds=kisssub")
        assert r1.json()["total"] == r2.json()["total"]

    async def test_titles_have_required_fields(self, client):
        r = await client.post("/api/load-titles?feeds=mikanani&sample_size=3")
        data = r.json()
        for t in data["titles"]:
            assert len(t["id"]) == 16  # sha256 hex prefix
            assert t["raw_title"] != ""
            assert t["source_feed"] in ("mikanani", "kisssub", "eztv", "dmhy")

    async def test_title_ids_are_stable(self, client):
        """Same feed + same title should produce same ID across loads."""
        r1 = await client.post("/api/load-titles?feeds=kisssub&sample_size=5")
        r2 = await client.post("/api/load-titles?feeds=kisssub&sample_size=5")
        ids1 = {t["id"] for t in r1.json()["titles"]}
        ids2 = {t["id"] for t in r2.json()["titles"]}
        assert ids1 == ids2, "Title IDs must be deterministic across loads"

    async def test_title_ids_are_feed_scoped(self, client):
        """Same title from different feeds should have different IDs."""
        r = await client.post("/api/load-titles")
        titles = r.json()["titles"]
        # All IDs should be unique
        ids = [t["id"] for t in titles]
        assert len(ids) == len(set(ids)), "Duplicate IDs found"


# ══════════════════════════════════════════════════════════════════════════
# Metadata Search (may need LLM key)
# ══════════════════════════════════════════════════════════════════════════


class TestMetadataSearch:
    async def test_search_invalid_content_type(self, client):
        r = await client.post(
            "/api/search-metadata",
            json={"search_title": "One Piece", "content_type": "invalid"},
        )
        assert r.status_code == 422

    async def test_search_empty_may_fail_or_noop(self, client):
        """Search may fail without LLM key, but should handle gracefully."""
        r = await client.post(
            "/api/search-metadata",
            json={"search_title": "One Piece", "content_type": "tv"},
        )
        # Without LLM key, may get 502 or 200 with empty results
        assert r.status_code in (200, 502, 500)


# ══════════════════════════════════════════════════════════════════════════
# Dataset JSON Export / Import
# ══════════════════════════════════════════════════════════════════════════


class TestDatasetJSON:
    TEST_DS = "pytest-json-test"

    async def test_save_with_json(self, client):
        payload = {
            "name": self.TEST_DS,
            "save_to_db": True,
            "save_to_json": True,
            "entries": [
                {
                    "id": "json-001",
                    "raw_title": "JSON Test Title",
                    "source_feed": "dmhy",
                    "resource_metadata": {"clean_title": "JSON Test", "content_type": "tv", "episode": 1},
                    "review_status": "accepted",
                    "notes": None,
                },
            ],
        }
        r = await client.post("/api/datasets", json=payload)
        assert r.status_code == 200
        data = r.json()
        assert "json_path" in data

        # Verify JSON file exists
        import json as _json
        from pathlib import Path

        path = Path(data["json_path"])
        assert path.exists()

        content = _json.loads(path.read_text(encoding="utf-8"))
        assert content["name"] == self.TEST_DS
        assert len(content["entries"]) == 1

        # Cleanup
        path.unlink(missing_ok=True)
        await client.delete(f"/api/datasets/{self.TEST_DS}")


# ══════════════════════════════════════════════════════════════════════════
# Page serving
# ══════════════════════════════════════════════════════════════════════════


class TestPageServing:
    async def test_index_contains_expected_elements(self, client):
        r = await client.get("/")
        html = r.text
        assert "Metadata Eval" in html
        assert "btnLoadTitles" in html
        assert "btnRunAgent" in html
        assert "Run All" in html  # Renamed from "Run Agent"
        assert "Run Selected" in html
        assert "datasetSection" in html
        assert "btnNewDataset" in html
        assert "btnDeleteDataset" in html
        assert "processingStatus" in html  # JS state variable
        assert "computeSearchStats" in html
        assert "btnSelectTmdbFailed" in html
        assert "btnSelectExaFailed" in html
        assert "btnSelectWikiFailed" in html
        assert "datasetSourceType" not in html
        assert "newDatasetSourceType" in html
        assert "Exo Agent Search" in html
        assert "data_sources_used" in html
        assert "reviewTimestamps" in html
        assert "agent_result" in html

    async def test_index_has_load_dataset_only_after_titles(self, client):
        """The dataset section should start hidden (display:none)."""
        r = await client.get("/")
        html = r.text
        # The datasetSection span should have display:none
        assert 'id="datasetSection" style="display:none"' in html or 'id="datasetSection" style="display:none;"' in html


# ══════════════════════════════════════════════════════════════════════════
# Async job tracking (survives page refresh)
# ══════════════════════════════════════════════════════════════════════════


class TestAsyncJobTracking:
    """Tests for the fire-and-forget /run-agent + polling flow."""

    async def test_run_agent_empty_returns_job_immediately(self, client):
        """POST /run-agent with no titles returns a completed job immediately."""
        r = await client.post("/api/run-agent?max_concurrency=3", json={"titles": []})
        assert r.status_code == 200
        data = r.json()
        assert "job_id" in data
        assert data["title_ids"] == []
        assert data["total"] == 0

        # Status should be completed
        r2 = await client.get(f"/api/run-agent/{data['job_id']}/status")
        assert r2.status_code == 200
        status = r2.json()
        assert status["status"] == "completed"
        assert status["total"] == 0
        assert status["results"] == {}

    async def test_status_404_for_unknown_job(self, client):
        """GET status for a non-existent job returns 404."""
        r = await client.get("/api/run-agent/nonexistent-job-id/status")
        assert r.status_code == 404

    async def test_run_agent_returns_job_id_and_title_ids(self, client):
        """POST /run-agent with titles returns job_id + title_ids immediately."""
        titles = [
            {"id": "abc123", "raw_title": "Test Title 1", "source_feed": "test"},
            {"id": "def456", "raw_title": "Test Title 2", "source_feed": "test"},
        ]
        r = await client.post("/api/run-agent?max_concurrency=3", json={"titles": titles})
        assert r.status_code == 200
        data = r.json()
        assert "job_id" in data
        assert data["title_ids"] == ["abc123", "def456"]
        assert data["total"] == 2

        # Status should show running (or completed if fast enough)
        r2 = await client.get(f"/api/run-agent/{data['job_id']}/status")
        assert r2.status_code == 200
        status = r2.json()
        assert status["status"] in ("running", "completed")
        assert status["total"] == 2
        assert set(status["title_ids"]) == {"abc123", "def456"}

    async def test_run_agent_title_ids_only_rejected(self, client):
        """POST /run-agent with only title_ids (no titles) is rejected."""
        r = await client.post(
            "/api/run-agent?max_concurrency=3",
            json={"title_ids": ["abc123"]},
        )
        assert r.status_code == 422

    async def test_index_contains_polling_infrastructure(self, client):
        """The frontend HTML should contain the polling + restore logic."""
        r = await client.get("/")
        html = r.text
        assert "pollJobStatus" in html
        assert "restoreActiveJob" in html
        assert "apiJobStatus" in html
        assert "saveJobId" in html
        assert "_storageKey('job')" in html
