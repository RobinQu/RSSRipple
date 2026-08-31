"""Shared HTTP helpers for integration tests against the dockerized RSSRipple app.

Consolidates the ``_client`` / ``_api`` / ``_poll_fetch`` / ``_poll_run`` /
``_ensure_downloader`` helpers and ``DEFAULT_FIELD_MAPPING`` that were
previously copy-pasted across test_e2e_pipeline, test_agent_pipeline,
test_channel_real_feeds, test_metadata_pipeline, and friends.
"""

from __future__ import annotations

import os
import time

import httpx

# ── Environment ──────────────────────────────────────────────────────────

RSSRIPPLE = os.environ.get("RSSRIPPLE_URL", "http://app:9001")
TEST_SERVER = os.environ.get("TEST_SERVER_URL", "http://test-server:8080")
MIKANANI_EXT_URL = f"{TEST_SERVER}/rss/mikanani-ext"
MIKANANI_1_URL = f"{TEST_SERVER}/rss/mikanani-1"
TIMEOUT = 60.0

# API key for the app's auth gate (AUTH_ENABLED defaults to true). Must match
# the ``API_KEY`` env var set on the app services in docker-compose.test*.yml;
# override locally via INTEGRATION_API_KEY.
API_KEY = os.environ.get("INTEGRATION_API_KEY", "test-integration-key")
API_HEADERS = {"X-API-Key": API_KEY}


def _client() -> httpx.Client:
    """Fresh HTTP client against the RSSRipple app."""
    return httpx.Client(timeout=TIMEOUT, headers=API_HEADERS)


def _api(path: str, method: str = "get", **kw):
    """Convenience HTTP call against the RSSRipple app (with 3x retry)."""
    last_exc = None
    for attempt in range(3):
        try:
            c = _client()
            fn = getattr(c, method.lower())
            return fn(f"{RSSRIPPLE}{path}", **kw)
        except (httpx.ReadTimeout, httpx.ConnectError, httpx.RemoteProtocolError) as e:
            last_exc = e
            time.sleep(1 * (attempt + 1))
    raise last_exc


def _poll_fetch(channel_id: str, timeout: int = 120, accept_failed: bool = False) -> dict:
    """Block until the channel fetch job reaches a terminal state.

    Returns the inner ``data`` dict. By default only ``done`` is terminal
    (matches the channel ground-truth tests). Pass ``accept_failed=True`` to
    also treat ``failed`` as terminal - used by the e2e/agent pipeline tests
    that tolerate a failed fetch rather than waiting out the full timeout.
    """
    terminal = ("done", "failed") if accept_failed else ("done",)
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api(f"/api/v1/channels/{channel_id}/fetch-status")
        data = r.json().get("data") or {}
        if data.get("status") in terminal:
            return data
        time.sleep(2)
    raise TimeoutError(f"Fetch did not complete for channel {channel_id}")


def _poll_run(agent_id: str, timeout: int = 120) -> dict:
    """Block until the agent run job finishes (done/failed) or times out."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api(f"/api/v1/agents/{agent_id}/run-status")
        data = r.json().get("data") or {}
        if data.get("status") in ("done", "failed"):
            return data
        time.sleep(2)
    raise TimeoutError(f"Agent run did not complete for agent {agent_id}")


def search_metadata(
    query: str,
    content_type: str,
    *,
    mode: str = "local",
    source: str | None = None,
    api=_api,
):
    """Call the canonical metadata search endpoint used by the edit wizard."""
    payload = {"query": query, "content_type": content_type, "mode": mode}
    if mode == "online":
        payload["source"] = source or "wikipedia"
    return api("/api/v1/metadata/search", method="post", json=payload)


def search_metadata_request(path: str, *, json: dict, api=_api, **_ignored):
    """Request-shaped adapter returning the former flat result envelope.

    The assertions using this helper predate the edit wizard but still cover
    useful local-search behavior. The HTTP request itself goes through the
    canonical global endpoint.
    """
    source = str(json.get("data_source_type") or "local").lower()
    mode = "local" if source == "local" else "online"
    if source not in {"wikipedia", "tmdb", "bangumi"}:
        source = "wikipedia"
    response = search_metadata(
        json.get("search_title") or json.get("query") or "",
        json.get("content_type") or "tv",
        mode=mode,
        source=source,
        api=api,
    )
    if response.status_code != 200:
        return response
    candidates = response.json().get("data", {}).get("candidates", [])
    results = []
    for candidate in candidates:
        item = dict(candidate.get("metadata") or {})
        for key in (
            "content_type", "title_cn", "title_en", "original_title",
            "year", "poster_url", "external_id",
        ):
            if item.get(key) is None and candidate.get(key) is not None:
                item[key] = candidate[key]
        if candidate.get("identity_source"):
            item["external_source"] = candidate["identity_source"]
        results.append(item)
    return httpx.Response(
        200,
        json={"success": True, "data": {"results": results}, "error": None, "meta": {}},
        request=response.request,
    )


def associate_external_metadata(
    resource_id: str,
    selected: dict,
    *,
    api=_api,
):
    """Associate one non-batch resource through the canonical wizard API."""
    content_type = selected.get("content_type", "tv")
    work_type = "movie" if content_type == "movie" else "series"
    identity_source = str(selected.get("external_source") or "tmdb").lower()
    if identity_source not in {
        "wikipedia", "tmdb", "bangumi", "mal", "anilist", "imdb", "douban"
    }:
        identity_source = "tmdb"
    external_id = str(selected.get("external_id") or f"integration:{resource_id}")
    primary_source = identity_source if identity_source in {
        "wikipedia", "tmdb", "bangumi"
    } else "wikipedia"
    metadata = dict(selected)
    metadata["external_source"] = identity_source
    metadata["external_id"] = external_id
    metadata.setdefault("is_anime", work_type == "series")
    year = selected.get("year") or 2023
    if work_type == "series":
        metadata.setdefault("start_date", f"{year}-01-01")
    else:
        metadata.setdefault("release_date", f"{year}-01-01")
    candidate = {
        "origin": "external",
        "content_type": "movie" if work_type == "movie" else "tv",
        "title_cn": selected.get("title_cn"),
        "title_en": selected.get("title_en"),
        "original_title": selected.get("original_title"),
        "year": selected.get("year"),
        "poster_url": selected.get("poster_url"),
        "primary_source": primary_source,
        "identity_source": identity_source,
        "external_id": external_id,
        "match_path": "primary",
        "selectable": True,
        "metadata": metadata,
    }
    detail_response = api(f"/api/v1/resources/{resource_id}")
    detail = (
        detail_response.json().get("data", {})
        if detail_response.status_code == 200 else {}
    )
    association_payload = {
        "is_batch": False,
        "works": [{
            "work_type": work_type,
            "client_key": "selected-candidate",
            "candidate": candidate,
        }],
        "assignments": [],
    }
    if work_type == "series" and detail.get("episode") is not None:
        association_payload["season"] = detail.get("season") or 1
        association_payload["episode"] = detail["episode"]
    return api(
        f"/api/v1/resources/{resource_id}/associations",
        method="put",
        json=association_payload,
    )


def associate_metadata_request(path: str, *, json: dict, api=_api, **_ignored):
    """Small request-shaped adapter for migrated integration scenarios."""
    resource_id = path.split("/resources/", 1)[1].split("/", 1)[0]
    return associate_external_metadata(
        resource_id, json["selected_result"], api=api
    )


def refresh_work_metadata(work_id: str, content_type: str, source: str, *, api=_api):
    """Run the current search → apply workflow and return a compact result."""
    work_path = "movies" if content_type == "movie" else "series"
    work_response = api(f"/api/v1/{work_path}/{work_id}")
    if work_response.status_code != 200:
        return work_response
    work = work_response.json()["data"]
    query = work.get("title_cn") or work.get("title_en") or work.get("original_title")
    searched = search_metadata(
        query, content_type, mode="online", source=source, api=api
    )
    if searched.status_code != 200:
        return searched
    candidates = searched.json()["data"]["candidates"]
    candidate = next((item for item in candidates if item.get("selectable")), None)
    if candidate is None:
        return httpx.Response(
            200,
            json={"success": True, "data": {
                "found": False, "source": source, "filled": [], "candidate": None,
            }, "error": None, "meta": {}},
            request=searched.request,
        )
    applied = api(
        "/api/v1/works/metadata/apply",
        method="post",
        json={
            "id": work_id,
            "content_type": content_type,
            "candidate": candidate,
            "override_manual_edits": False,
        },
    )
    if applied.status_code != 200:
        return applied
    result = applied.json()["data"]
    flat_candidate = dict(candidate.get("metadata") or {})
    flat_candidate.setdefault("external_id", candidate.get("external_id"))
    flat_candidate.setdefault("external_source", candidate.get("identity_source"))
    return httpx.Response(
        200,
        json={"success": True, "data": {
            "found": True,
            "source": source,
            "filled": result.get("applied", []),
            "candidate": flat_candidate,
        }, "error": None, "meta": {}},
        request=applied.request,
    )


def _get_first_downloader_id() -> str | None:
    """Get the ID of the first downloader, or None if none exist."""
    r = _api("/api/v1/downloaders", params={"page_size": 100})
    if r.status_code != 200:
        return None
    data = r.json().get("data", [])
    if not data:
        return None
    return data[0]["id"]


def _ensure_downloader() -> str:
    """Get or create a Transmission downloader. Returns the downloader ID."""
    dl_id = _get_first_downloader_id()
    if dl_id:
        return dl_id
    r = _api(
        "/api/v1/downloaders",
        method="post",
        json={
            "name": "E2E Test Transmission",
            "type": "transmission",
            "url": "http://transmission:9091/transmission/rpc",
            "download_dir": "/downloads/e2e-test",
        },
    )
    assert r.status_code == 201, f"create downloader failed: {r.text}"
    return r.json()["data"]["id"]


# ── Series seeding ─────────────────────────────────────────────────────────


def ensure_series(
    title_cn: str,
    title_en: str,
    number_of_seasons: int | None = None,
    api=_api,
    *,
    start_date: str = "2023-01-01",
    is_anime: bool = True,
) -> str:
    """Get-or-create a series by exact title_cn; returns the series id.

    Since 81a9d06, Layer-3 auto-link abandons top-1 linking when >1 local
    works share a normalized title — so tests must never blindly POST a
    duplicate. When ``number_of_seasons`` is given, the row is also brought
    to that value (create or update): it supplies the single-season evidence
    ``resolve_missing_season`` needs to land ``season=1`` instead of marking
    season-less resources ``episode_confidence=ambiguous`` (which routes them
    to a PendingDecision instead of dispatch).
    ``api`` allows reusing the helper against the app-llm instance.
    """
    r = api("/api/v1/series", params={"page_size": 100, "title": title_cn})
    if r.status_code == 200:
        for s in r.json().get("data", []):
            if s.get("title_cn") == title_cn:
                sid = s["id"]
                break
        else:
            sid = None
        if sid is not None:
            updates = {}
            if number_of_seasons is not None and s.get("number_of_seasons") != number_of_seasons:
                updates["number_of_seasons"] = number_of_seasons
            if not s.get("start_date"):
                updates["start_date"] = start_date
            if s.get("is_anime") is None:
                updates["is_anime"] = is_anime
            if updates:
                r = api(
                    f"/api/v1/series/{sid}",
                    method="put",
                    json=updates,
                )
                assert r.status_code == 200, f"series update failed: {r.text}"
            return sid
    payload: dict = {
        "title_cn": title_cn,
        "title_en": title_en,
        "start_date": start_date,
        "is_anime": is_anime,
    }
    if number_of_seasons is not None:
        payload["number_of_seasons"] = number_of_seasons
    r = api("/api/v1/series", method="post", json=payload)
    assert r.status_code == 201, f"Series creation failed: {r.status_code} {r.text}"
    return r.json()["data"]["id"]


# ── Default field mapping ────────────────────────────────────────────────

DEFAULT_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {
        "title_raw": {"source": "title"},
        "torrent_url": {"source": "link"},
    },
}

# Richer mapping for the test-server's mikanani-style feeds:
#   "[Group] Title / Alt - 01 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
# Extracts the structured fields the agent pipeline needs (episode keys the
# dedup/conflict grouping; resolution drives score_and_pick).
RICH_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {
        "title_raw": {"source": "title"},
        "torrent_url": {"source": "link"},
        "subtitle_group": {"source": "title", "regex": r"^\[([^\]]+)\]", "group": 1},
        "episode": {
            "source": "title",
            "regex": r" - (\d{1,3}) \[",
            "group": 1,
            "transform": "int",
        },
        "resolution": {"source": "title", "regex": r"(\d{3,4}p)"},
    },
}
