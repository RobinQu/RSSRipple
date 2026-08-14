"""Bangumi metadata source integration test (mock API).

Runs against the mock-LLM app instance (``RSSRIPPLE_LLM_URL``), whose
``BANGUMI_API_BASE`` points at the test-server's mock Bangumi API. Covers the
bangumi channel-source search → deterministic auto-link → details/episodes
expansion path (``app/services/metadata_bangumi.py`` +
``app/services/bangumi_client.py``) without a real token or network.

Skipped automatically when RSSRIPPLE_LLM_URL is not set.
"""

from __future__ import annotations

import os

import httpx
import pytest

from tests.integration.http._http import API_HEADERS

LLM_APP = os.environ.get("RSSRIPPLE_LLM_URL", "")

pytestmark = pytest.mark.skipif(
    not LLM_APP, reason="RSSRIPPLE_LLM_URL not set (mock-LLM app not in stack)"
)

TITLE_EN = "Bangumi Mock Anime"
TIMEOUT = 120.0


def _llm_api(path: str, method: str = "get", **kw) -> httpx.Response:
    c = httpx.Client(timeout=TIMEOUT, headers=API_HEADERS)
    return getattr(c, method.lower())(f"{LLM_APP}{path}", **kw)


def _ensure_series() -> str:
    r = _llm_api("/api/v1/series", params={"page_size": 100, "title": TITLE_EN})
    if r.status_code == 200:
        for s in r.json().get("data", []):
            if s.get("title_en") == TITLE_EN:
                return s["id"]
    r = _llm_api(
        "/api/v1/series",
        method="post",
        json={"title_cn": "BANGUMI 模拟动画", "title_en": TITLE_EN},
    )
    assert r.status_code == 201, f"series create failed: {r.status_code} {r.text}"
    return r.json()["data"]["id"]


def test_bangumi_source_autolink():
    """refresh-metadata (source=bangumi) auto-links the mock subject and fills
    the work's empty fields from the expanded subject + episode list."""
    sid = _ensure_series()
    try:
        r = _llm_api(
            "/api/v1/works/refresh-metadata",
            method="post",
            json={"id": sid, "content_type": "tv", "source": "bangumi"},
        )
        assert r.status_code == 200, f"refresh failed: {r.status_code} {r.text}"
        data = r.json()["data"]
        assert data["found"] is True
        assert data["source"] == "bangumi"
        assert data["candidate"]["external_id"] == "bangumi:99999"
        assert data["candidate"]["external_source"] == "bangumi"

        # The work is now linked to the bangumi identity with the summary/rating
        # filled from the mock subject.
        r = _llm_api(f"/api/v1/series/{sid}")
        assert r.status_code == 200
        series = r.json()["data"]
        assert series["external_source"] == "bangumi"
        assert series["description"]
        assert series["rating"] == 9.1
    finally:
        try:
            _llm_api(f"/api/v1/series/{sid}", method="delete")
        except Exception:
            pass
