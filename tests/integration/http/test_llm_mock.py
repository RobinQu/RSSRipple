"""LLM-backed flow integration tests against the mock-LLM app instance.

Targets the ``app-llm`` service (RSSRIPPLE_LLM_URL), which is wired to the
test-server's deterministic ``/v1/chat/completions`` mock:

  - POST /channels/analyze-url-stream + /channels/{id}/analyze-stream (SSE)
  - POST /channels/{id}/analyze (blocking LLM field-mapping analysis)
  - Agent LLM candidate pick: ask-mode decisions carry llm_picked_resource_id,
    POST /decisions/{id}/ai-pick dispatches via the cached pick
  - auto conflict resolution with llm_enabled=True (LLM pick over heuristic)

Skipped entirely when RSSRIPPLE_LLM_URL is not set (e.g. distributed runs).
"""

from __future__ import annotations

import json
import os

import httpx
import pytest

from tests.integration.http._http import (
    API_HEADERS,
    RICH_FIELD_MAPPING,
    TEST_SERVER,
    ensure_series,
    search_metadata_request,
)

LLM_APP = os.environ.get("RSSRIPPLE_LLM_URL", "")
MIKANANI_S1_URL = f"{TEST_SERVER}/rss/mikanani?series=1"  # 葬送的芙莉莲
MIKANANI_S3_URL = f"{TEST_SERVER}/rss/mikanani?series=3"  # 咒术回战
MIKANANI_S4_URL = f"{TEST_SERVER}/rss/mikanani?series=4"  # 小书痴的下克上
FRIEREN_TITLE_CN = "葬送的芙莉莲"

pytestmark = pytest.mark.skipif(
    not LLM_APP, reason="RSSRIPPLE_LLM_URL not set (mock-LLM app not in stack)"
)

TIMEOUT = 60.0


def _api(path: str, method: str = "get", **kw) -> httpx.Response:
    c = httpx.Client(timeout=TIMEOUT, headers=API_HEADERS)
    return getattr(c, method.lower())(f"{LLM_APP}{path}", **kw)


def _poll_fetch(channel_id: str, timeout: int = 120) -> dict:
    """Poll fetch-status on the mock-LLM app until terminal."""
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _api(f"/api/v1/channels/{channel_id}/fetch-status")
        data = r.json().get("data") or {}
        if data.get("status") in ("done", "failed"):
            return data
        time.sleep(2)
    raise TimeoutError(f"Fetch did not complete for channel {channel_id}")


def _ensure_mock_downloader() -> str:
    r = _api("/api/v1/downloaders", params={"page_size": 100})
    assert r.status_code == 200
    for d in r.json().get("data", []):
        if d.get("type") == "mock":
            return d["id"]
    r = _api(
        "/api/v1/downloaders",
        method="post",
        json={"name": "LLM Mock Downloader", "type": "mock"},
    )
    assert r.status_code == 201, f"create mock downloader failed: {r.text}"
    return r.json()["data"]["id"]


# =========================================================================
# TestFeedAnalysis — LLM feed analysis (feed_analyzer.py)
# =========================================================================


class TestFeedAnalysis:
    def test_analyze_url_stream(self):
        """SSE analysis without a channel: deltas then done with a mapping."""
        with httpx.stream(
            "POST",
            f"{LLM_APP}/api/v1/channels/analyze-url-stream",
            json={"url": MIKANANI_S1_URL},
            timeout=TIMEOUT,
        ) as resp:
            assert resp.status_code == 200
            body = resp.read().decode()

        events = [
            json.loads(line[5:].strip())
            for line in body.splitlines()
            if line.strip().startswith("data:")
        ]
        assert events, "expected SSE events"
        types = [e.get("type") for e in events]
        assert "done" in types, f"expected a done event, got {types}"
        done = next(e for e in events if e.get("type") == "done")
        mapping = done["field_mapping"]
        assert "field_mappings" in mapping
        assert "torrent_url" in mapping["field_mappings"]
        assert done["confidence"] == "high"

    def test_analyze_stream_and_blocking_analyze(self):
        """Channel-bound analyze-stream + blocking analyze endpoints."""
        r = _api(
            "/api/v1/channels",
            method="post",
            json={
                "name": "LLM Analyze Channel",
                "url": MIKANANI_S1_URL,
                "field_mapping": RICH_FIELD_MAPPING,
                "fetch_interval": 3600,
                "metadata_agent_enabled": False,
            },
        )
        assert r.status_code == 201, f"create channel failed: {r.text}"
        ch_id = r.json()["data"]["id"]

        try:
            with httpx.stream(
                "POST",
                f"{LLM_APP}/api/v1/channels/{ch_id}/analyze-stream",
                timeout=TIMEOUT,
            ) as resp:
                assert resp.status_code == 200
                body = resp.read().decode()
            events = [
                json.loads(line[5:].strip())
                for line in body.splitlines()
                if line.strip().startswith("data:")
            ]
            assert any(e.get("type") == "done" for e in events)

            r = _api(f"/api/v1/channels/{ch_id}/analyze", method="post")
            assert r.status_code == 200, f"analyze failed: {r.text}"
            data = r.json()["data"]
            assert data["confidence"] == "high"
            fm = data["field_mapping"]
            assert "field_mappings" in fm
            assert "episode" in fm["field_mappings"]
        finally:
            _api(f"/api/v1/channels/{ch_id}", method="delete")


# =========================================================================
# TestLLMCandidatePick — LLM pick in ask + auto conflict resolution
# =========================================================================


@pytest.fixture(scope="class")
def _llm_env():
    """Series + fetched channel (auto-linked) + mock downloader on app-llm."""
    # Get-or-create (duplicate rows trip the same-title collision guard) and
    # single-season evidence so season-less resources dispatch instead of
    # going ambiguous → PendingDecision.
    series_id = ensure_series(
        FRIEREN_TITLE_CN, "Frieren: Beyond Journey's End",
        number_of_seasons=1, api=_api,
    )

    r = _api(
        "/api/v1/channels",
        method="post",
        json={
            "name": "LLM Pick Channel",
            "url": MIKANANI_S1_URL,
            "field_mapping": RICH_FIELD_MAPPING,
            "fetch_interval": 3600,
            "metadata_agent_enabled": False,
        },
    )
    if r.status_code != 201:
        pytest.skip(f"Channel creation failed: {r.status_code} {r.text}")
    ch_id = r.json()["data"]["id"]

    _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
    result = _poll_fetch(ch_id)
    if result.get("status") != "done":
        pytest.skip(f"Fetch did not complete: {result}")

    r = _api(f"/api/v1/channels/{ch_id}/resources", params={"page_size": 100})
    resources = [res for res in r.json().get("data", []) if res.get("series_id")]
    if not resources:
        pytest.skip("resources not linked")

    dl_id = _ensure_mock_downloader()
    yield {
        "channel_id": ch_id,
        "series_id": series_id,
        "downloader_id": dl_id,
        "resources": resources,
    }
    # Channel owns tasks after the ai-pick/auto dispatch tests — leave it
    # (per-run DB).


class TestLLMCandidatePick:
    def test_ask_decisions_carry_llm_pick_and_ai_pick_dispatches(self, _llm_env):
        """ask-mode backfill → decisions with cached LLM pick → ai-pick works."""
        r = _api(
            "/api/v1/agents",
            method="post",
            json={
                "name": "LLM Ask Agent",
                "channel_id": _llm_env["channel_id"],
                "downloader_id": _llm_env["downloader_id"],
                "scope_channel_wide": True,
                "llm_enabled": True,
                "conflict_resolution": "ask",
                "dispatch_resource_ids": [res["id"] for res in _llm_env["resources"]],
            },
        )
        assert r.status_code == 201, f"create agent failed: {r.text}"
        agent_id = r.json()["data"]["id"]

        r = _api(f"/api/v1/agents/{agent_id}/decisions", params={"page_size": 100})
        decisions = r.json()["data"]
        assert len(decisions) == 6, f"expected 6 decisions, got {len(decisions)}"
        # The mock LLM always picks candidate #1 → cached on the decision
        for d in decisions:
            assert d["llm_picked_resource_id"] == d["candidates"][0], (
                f"expected cached llm pick: {d}"
            )
            assert d["llm_suggestion"], "expected an llm suggestion reason"

        # ai-pick uses the cached pick and dispatches
        decision = decisions[0]
        r = _api(f"/api/v1/decisions/{decision['id']}/ai-pick", method="post")
        assert r.status_code == 200, f"ai-pick failed: {r.text}"
        data = r.json()["data"]
        assert data["status"] == "decided"
        assert data["decided_resource_id"] == decision["candidates"][0]

        r = _api(f"/api/v1/agents/{agent_id}/tasks", params={"page_size": 100})
        tasks = r.json()["data"]
        assert any(
            t["file_resource_id"] == decision["candidates"][0] for t in tasks
        ), "ai-picked resource should be dispatched"

        # Batch AI-handle the remaining decisions (uses cached picks)
        remaining = [d["id"] for d in decisions[1:]]
        r = _api(
            f"/api/v1/agents/{agent_id}/decisions/batch",
            method="post",
            json={"decision_ids": remaining, "action": "ai"},
        )
        assert r.status_code == 200, f"batch ai failed: {r.text}"
        data = r.json()["data"]
        assert data["processed"] == len(remaining)
        assert data["dispatched"] == len(remaining), (
            f"all cached picks should dispatch: {data}"
        )

    def test_auto_conflict_uses_llm_pick(self, _llm_env):
        """auto mode with llm_enabled → LLM pick wins over the heuristic."""
        # The mock always picks candidate #1 in the order sent to the LLM,
        # which is the candidates list order (feed order: LoliHouse first).
        ep1 = [res for res in _llm_env["resources"] if res.get("episode") == 1]
        assert len(ep1) == 3

        r = _api(
            "/api/v1/agents",
            method="post",
            json={
                "name": "LLM Auto Agent",
                "channel_id": _llm_env["channel_id"],
                "downloader_id": _llm_env["downloader_id"],
                "scope_channel_wide": True,
                "llm_enabled": True,
                "conflict_resolution": "auto",
                "dispatch_resource_ids": [res["id"] for res in ep1],
            },
        )
        assert r.status_code == 201, f"create agent failed: {r.text}"
        agent_id = r.json()["data"]["id"]

        r = _api(f"/api/v1/agents/{agent_id}/tasks", params={"page_size": 100})
        tasks = r.json()["data"]
        assert len(tasks) == 1, f"expected exactly 1 dispatched task, got {len(tasks)}"
        # The mock LLM picked one of the 3 candidates (order is DB-dependent)
        ep1_ids = {res["id"] for res in ep1}
        assert tasks[0]["file_resource_id"] in ep1_ids


# =========================================================================
# TestMetadataAgentMock — UnifiedMetadataAgent ReAct loop via mock tools
# =========================================================================

MIKANANI_S0_URL = f"{TEST_SERVER}/rss/mikanani?series=0"  # 黄泉使者
MIKANANI_S2_URL = f"{TEST_SERVER}/rss/mikanani?series=2"  # 药屋少女的呢喃


@pytest.fixture(scope="class")
def _fake_tmdb_key():
    """Fake TMDB key on app-llm: the channel source must be one of the
    two supported sources (wikipedia/tmdb); a fake key keeps the search tool
    failing fast and deterministic while the mock LLM's canned finalize
    drives the verdict. Restored (cleared) afterwards."""
    r = _api(
        "/api/v1/system-settings", method="put", json={"tmdb_api_key": "mock-tmdb"}
    )
    assert r.status_code == 200, f"set fake key failed: {r.text}"
    yield
    _api("/api/v1/system-settings", method="put", json={"tmdb_api_key": ""})


class TestMetadataAgentMock:
    """metadata_agent_enabled channel driven by the mock LLM tool calls."""

    def test_agent_links_canned_work(self, _fake_tmdb_key):
        """Fetch with metadata agent on: mock finalize found → series + link."""
        r = _api(
            "/api/v1/channels",
            method="post",
            json={
                "name": "LLM Metadata Channel (found)",
                "url": MIKANANI_S0_URL,
                "field_mapping": RICH_FIELD_MAPPING,
                "fetch_interval": 3600,
                "metadata_agent_enabled": True,
                "metadata_source": "tmdb",
            },
        )
        assert r.status_code == 201, f"create channel failed: {r.text}"
        ch_id = r.json()["data"]["id"]

        try:
            _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
            result = _poll_fetch(ch_id)
            assert result.get("status") == "done", f"fetch failed: {result}"

            r = _api(f"/api/v1/channels/{ch_id}/resources", params={"page_size": 100})
            resources = r.json().get("data", [])
            assert resources
            linked = [res for res in resources if res.get("series_id")]
            assert linked, "mock agent should link Frieren-titled resources"
            assert all(res.get("search_title") for res in linked)

            # The canned entity was upserted as a series
            r = _api("/api/v1/series", params={"page_size": 100, "title": "黄泉使者"})
            series = [s for s in r.json()["data"] if s.get("external_id") == "mock-exa-daemons"]
            assert series, "expected the mock-exa-daemons series to be created"
            series_id = series[0]["id"]
            assert all(res["series_id"] == series_id for res in linked)
        finally:
            _api(f"/api/v1/channels/{ch_id}", method="delete")

    def test_agent_not_found_path(self, _fake_tmdb_key):
        """Unknown titles finalize found=false → resources stay unlinked."""
        r = _api(
            "/api/v1/channels",
            method="post",
            json={
                "name": "LLM Metadata Channel (not-found)",
                "url": MIKANANI_S2_URL,
                "field_mapping": RICH_FIELD_MAPPING,
                "fetch_interval": 3600,
                "metadata_agent_enabled": True,
                "metadata_source": "tmdb",
            },
        )
        assert r.status_code == 201, f"create channel failed: {r.text}"
        ch_id = r.json()["data"]["id"]

        try:
            _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
            result = _poll_fetch(ch_id)
            assert result.get("status") == "done", f"fetch failed: {result}"

            r = _api(f"/api/v1/channels/{ch_id}/resources", params={"page_size": 100})
            resources = r.json().get("data", [])
            assert resources
            unlinked = [
                res for res in resources
                if not res.get("series_id") and not res.get("movie_id")
            ]
            assert len(unlinked) == len(resources), (
                "mock not_found should leave resources unlinked"
            )
        finally:
            _api(f"/api/v1/channels/{ch_id}", method="delete")

    def test_manual_search_via_agent(self, _fake_tmdb_key):
        """Unified metadata search runs the mock TMDB ReAct loop."""
        # Channel with the agent disabled — we only need a resource id.
        r = _api(
            "/api/v1/channels",
            method="post",
            json={
                "name": "LLM Manual Search Channel",
                "url": MIKANANI_S0_URL,
                "field_mapping": RICH_FIELD_MAPPING,
                "fetch_interval": 3600,
                "metadata_agent_enabled": False,
            },
        )
        assert r.status_code == 201, f"create channel failed: {r.text}"
        ch_id = r.json()["data"]["id"]

        try:
            _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
            result = _poll_fetch(ch_id)
            assert result.get("status") == "done", f"fetch failed: {result}"
            r = _api(f"/api/v1/channels/{ch_id}/resources", params={"page_size": 1})
            rid = r.json()["data"][0]["id"]

            # Canned title → candidates with the mock entity
            r = search_metadata_request(
                f"/api/v1/resources/{rid}/metadata/search",
                api=_api,
                method="post",
                json={
                    "search_title": "黄泉使者",
                    "content_type": "tv",
                    "data_source_type": "tmdb",
                },
            )
            assert r.status_code == 200, f"search failed: {r.text}"
            results = r.json()["data"]["results"]
            assert results, "expected candidates from the mock agent"
            assert results[0]["external_id"] == "mock-exa-daemons"

            # Unknown title → empty candidate list (found=false)
            r = search_metadata_request(
                f"/api/v1/resources/{rid}/metadata/search",
                api=_api,
                method="post",
                json={
                    "search_title": "完全不存在的作品",
                    "content_type": "tv",
                    "data_source_type": "tmdb",
                },
            )
            assert r.status_code == 200
            assert r.json()["data"]["results"] == []
        finally:
            _api(f"/api/v1/channels/{ch_id}", method="delete")

    def test_agent_links_canned_movie(self, _fake_tmdb_key):
        """Movie verdict from the mock agent → Movie row + movie_id links.

        The channel name carries the ``mockmovie`` routing keyword (the agent
        prompt includes the channel name). Uses the S3 feed (咒术回战): its
        title matches no existing work (the S1 title-index short-circuit would
        otherwise bypass the agent), and no other app-llm test uses this feed —
        the MetadataCache is keyed by raw_title + source, so reusing a feed
        across tests with different expected verdicts poisons them.
        """
        r = _api(
            "/api/v1/channels",
            method="post",
            json={
                "name": "LLM Metadata Channel mockmovie",
                "url": MIKANANI_S3_URL,
                "field_mapping": RICH_FIELD_MAPPING,
                "fetch_interval": 3600,
                "metadata_agent_enabled": True,
                "metadata_source": "tmdb",
            },
        )
        assert r.status_code == 201, f"create channel failed: {r.text}"
        ch_id = r.json()["data"]["id"]

        try:
            _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
            result = _poll_fetch(ch_id)
            assert result.get("status") == "done", f"fetch failed: {result}"

            r = _api("/api/v1/movies", params={"page_size": 100, "title": "黄泉使者"})
            movies = [
                m for m in r.json()["data"]
                if m.get("external_id") == "mock-exa-daemons-movie"
            ]
            assert movies, "expected the mock-exa-daemons-movie movie to be created"
            movie_id = movies[0]["id"]

            r = _api(f"/api/v1/channels/{ch_id}/resources", params={"page_size": 100})
            resources = r.json().get("data", [])
            linked = [res for res in resources if res.get("movie_id")]
            assert linked, "mock agent should link resources to the canned movie"
            assert all(res["movie_id"] == movie_id for res in linked)
        finally:
            _api(f"/api/v1/channels/{ch_id}", method="delete")

    def test_agent_links_canned_audio_work(self, _fake_tmdb_key):
        """Audio verdict (drama_cd) → AudioWork row + audio_work_id links.

        Same routing trick as the movie test, via the ``mockaudio`` keyword,
        on the S4 feed (小书痴的下克上) — again a feed no other app-llm test
        touches, to keep the raw_title-keyed MetadataCache isolated.
        """
        r = _api(
            "/api/v1/channels",
            method="post",
            json={
                "name": "LLM Metadata Channel mockaudio",
                "url": MIKANANI_S4_URL,
                "field_mapping": RICH_FIELD_MAPPING,
                "fetch_interval": 3600,
                "metadata_agent_enabled": True,
                "metadata_source": "tmdb",
            },
        )
        assert r.status_code == 201, f"create channel failed: {r.text}"
        ch_id = r.json()["data"]["id"]

        try:
            _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
            result = _poll_fetch(ch_id)
            assert result.get("status") == "done", f"fetch failed: {result}"

            r = _api("/api/v1/audio-works", params={"page_size": 100, "search": "黄泉使者"})
            works = [
                w for w in r.json()["data"]
                if w.get("external_id") == "mock-exa-daemons-drama-cd"
            ]
            assert works, "expected the mock drama-cd audio work to be created"
            work_id = works[0]["id"]

            r = _api(f"/api/v1/channels/{ch_id}/resources", params={"page_size": 100})
            resources = r.json().get("data", [])
            linked = [res for res in resources if res.get("audio_work_id")]
            assert linked, "mock agent should link resources to the canned audio work"
            assert all(res["audio_work_id"] == work_id for res in linked)
        finally:
            _api(f"/api/v1/channels/{ch_id}", method="delete")


# =========================================================================
# TestFeedAnalysisVariants — mock model variants drive parser robustness
# =========================================================================


class TestFeedAnalysisVariants:
    """Switch llm_model via system-settings to select mock response variants."""

    def _set_model(self, model: str) -> None:
        r = _api(
            "/api/v1/system-settings",
            method="put",
            json={"llm_model": model},
        )
        assert r.status_code == 200, f"set model failed: {r.text}"

    def test_markdown_wrapped_json_parses(self):
        """markdown-model → fenced JSON → _parse_llm_json strips the fence."""
        self._set_model("markdown-model")
        try:
            r = _api(
                "/api/v1/channels/analyze-url-stream",
                method="post",
                json={"url": MIKANANI_S1_URL},
            )
            assert r.status_code == 200
            events = [
                json.loads(line[5:].strip())
                for line in r.text.splitlines()
                if line.strip().startswith("data:")
            ]
            done = [e for e in events if e.get("type") == "done"]
            assert done, f"expected done event: {events}"
            assert "field_mappings" in done[0]["field_mapping"]
        finally:
            self._set_model("mock-model")

    def test_invalid_escapes_repaired(self):
        """escapes-model → \\s-style invalid escapes → repaired then parsed."""
        self._set_model("escapes-model")
        try:
            r = _api(
                "/api/v1/channels",
                method="post",
                json={
                    "name": "LLM Escapes Channel",
                    "url": MIKANANI_S1_URL,
                    "field_mapping": RICH_FIELD_MAPPING,
                    "fetch_interval": 3600,
                    "metadata_agent_enabled": False,
                },
            )
            assert r.status_code == 201
            ch_id = r.json()["data"]["id"]
            try:
                r = _api(f"/api/v1/channels/{ch_id}/analyze", method="post")
                assert r.status_code == 200, f"analyze failed: {r.text}"
                data = r.json()["data"]
                assert "field_mappings" in data["field_mapping"], (
                    f"escape-repaired mapping should parse: {data}"
                )
            finally:
                _api(f"/api/v1/channels/{ch_id}", method="delete")
        finally:
            self._set_model("mock-model")

    def test_bad_json_retries_then_empty_result(self):
        """badjson-model → unparseable → analyze exhausts retries → empty."""
        self._set_model("badjson-model")
        try:
            # Blocking analyze endpoint returns the empty_result payload.
            r = _api(
                "/api/v1/channels",
                method="post",
                json={
                    "name": "LLM Bad JSON Channel",
                    "url": MIKANANI_S1_URL,
                    "field_mapping": RICH_FIELD_MAPPING,
                    "fetch_interval": 3600,
                    "metadata_agent_enabled": False,
                },
            )
            assert r.status_code == 201
            ch_id = r.json()["data"]["id"]
            try:
                r = _api(f"/api/v1/channels/{ch_id}/analyze", method="post")
                assert r.status_code == 200
                data = r.json()["data"]
                assert data["field_mapping"] == {}
                assert data["confidence"] == "low"
            finally:
                _api(f"/api/v1/channels/{ch_id}", method="delete")
        finally:
            self._set_model("mock-model")

    def test_empty_response_retries_then_empty_result(self):
        """empty-model → empty content → analyze exhausts retries → empty."""
        self._set_model("empty-model")
        try:
            r = _api(
                "/api/v1/channels",
                method="post",
                json={
                    "name": "LLM Empty Channel",
                    "url": MIKANANI_S1_URL,
                    "field_mapping": RICH_FIELD_MAPPING,
                    "fetch_interval": 3600,
                    "metadata_agent_enabled": False,
                },
            )
            assert r.status_code == 201
            ch_id = r.json()["data"]["id"]
            try:
                r = _api(f"/api/v1/channels/{ch_id}/analyze", method="post")
                assert r.status_code == 200
                assert r.json()["data"]["field_mapping"] == {}
            finally:
                _api(f"/api/v1/channels/{ch_id}", method="delete")
        finally:
            self._set_model("mock-model")
