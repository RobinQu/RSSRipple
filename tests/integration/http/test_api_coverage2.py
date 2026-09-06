"""HTTP API coverage supplement #2 (primary app).

Targets the gaps in the HTTP layer modules reported by the integration
coverage run:

  - app/api/v1/resources.py — list filters/grouped mode, field-values
    autocomplete, PATCH /resources/{id} corrections, PATCH episode (incl.
    absolute-number season derivation), /files, /magnet-resolve,
    PUT associations, analyze-batch (+ SSE stream).
  - app/api/v1/queue.py — overview / jobs / scheduler snapshots.
  - app/api/v1/agents.py — validation 422s, run scan_since handling, run
    history annotations, works CRUD, rules-preview, test-filters.
  - app/api/v1/dashboard.py — split overview/downloads endpoints, todo
    ignore (decision/confirmation kinds), work_ref serialization shapes.

Self-contained: builds its own channels/series/agents and never assumes an
empty database.

Requirements: Docker test environment (app + test-server).
"""

from __future__ import annotations

import time
import uuid

import httpx
import pytest

from tests.integration.http._http import (
    API_HEADERS,
    DEFAULT_FIELD_MAPPING,
    RICH_FIELD_MAPPING,
    RSSRIPPLE,
    TEST_SERVER,
    _api,
    _poll_fetch,
)

MIKANANI_S3_URL = f"{TEST_SERVER}/rss/mikanani?series=3"  # 咒术回战 (linked channel)
MIKANANI_S2_URL = f"{TEST_SERVER}/rss/mikanani?series=2"  # 药屋少女的呢喃 (ask channel)
EZTV_S0_URL = f"{TEST_SERVER}/rss/eztv?show=0"  # The Last of Us (unmatched)
DMHY_S2_URL = f"{TEST_SERVER}/rss/dmhy?series=2"  # magnet links (unmatched)
BATCH_URL = f"{TEST_SERVER}/rss/mikanani-batch"  # 葬送的芙莉莲 batch + ep29
JUJUTSU_TITLE_CN = "咒术回战"
FRIEREN_TITLE_CN = "葬送的芙莉莲"

# dmhy-style feeds carry the magnet in the enclosure, not the link (a GUID
# page) — see test_coverage_supplement.py.
MAGNET_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {
        "title_raw": {"source": "title"},
        "torrent_url": {"source": "enclosures[0].url"},
    },
}


def _ensure_mock_downloader() -> str:
    r = _api("/api/v1/downloaders", params={"page_size": 100})
    assert r.status_code == 200
    for d in r.json().get("data", []):
        if d.get("type") == "mock":
            return d["id"]
    r = _api(
        "/api/v1/downloaders",
        method="post",
        json={"name": f"Cov2 Mock Downloader {uuid.uuid4().hex[:6]}", "type": "mock"},
    )
    assert r.status_code == 201, f"create mock downloader failed: {r.text}"
    dl_id = r.json()["data"]["id"]
    _TRACK["downloaders"].append(dl_id)
    return dl_id


def _create_channel(name: str, url: str, field_mapping: dict | None = None) -> str:
    r = _api(
        "/api/v1/channels",
        method="post",
        json={
            "name": name,
            "url": url,
            "field_mapping": field_mapping or RICH_FIELD_MAPPING,
            "fetch_interval": 3600,
            "metadata_agent_enabled": False,
        },
    )
    assert r.status_code == 201, f"create channel failed: {r.status_code} {r.text}"
    channel_id = r.json()["data"]["id"]
    _TRACK["channels"].append(channel_id)
    return channel_id


def _fetch(channel_id: str) -> None:
    _api(f"/api/v1/channels/{channel_id}/fetch", method="post")
    result = _poll_fetch(channel_id, accept_failed=True)
    assert result.get("status") == "done", f"fetch failed: {result}"


def _resources(channel_id: str, **params) -> list[dict]:
    r = _api(
        f"/api/v1/channels/{channel_id}/resources",
        params={"page_size": 100, **params},
    )
    assert r.status_code == 200, f"list resources failed: {r.text}"
    return r.json().get("data", [])


def _create_agent(channel_id: str, downloader_id: str, **overrides) -> str:
    payload = {
        "name": f"Cov2 Agent {uuid.uuid4().hex[:6]}",
        "channel_id": channel_id,
        "downloader_id": downloader_id,
        "scope_channel_wide": True,
        "llm_enabled": False,
    }
    payload.update(overrides)
    r = _api("/api/v1/agents", method="post", json=payload)
    assert r.status_code == 201, f"create agent failed: {r.status_code} {r.text}"
    agent_id = r.json()["data"]["id"]
    _TRACK["agents"].append(agent_id)
    return agent_id


# ---------------------------------------------------------------------------
# Cleanup registry — every entity this module creates on the primary app is
# tracked here and removed/restored in the env fixture teardown, so the shared
# test database is exactly as clean after this module as before it.
# ---------------------------------------------------------------------------

_TRACK: dict[str, list] = {
    "channels": [],
    "agents": [],
    "tasks": [],  # manual (agent-less) task ids
    "movies": [],
    "series_created": [],  # work ids this module POSTed
    "series_touched": [],  # (id, original fields) this module PUT over
    "downloaders": [],  # created only when no mock downloader existed
}


def _track_series(
    title_cn: str,
    title_en: str,
    *,
    number_of_seasons: int | None = 1,
    number_of_episodes: int | None = None,
) -> str:
    """ensure_series variant that registers the work for teardown: created
    works are deleted afterwards; pre-existing works get their original field
    values restored."""
    r = _api("/api/v1/series", params={"page_size": 100, "title": title_cn})
    if r.status_code == 200:
        for s in r.json().get("data", []):
            if s.get("title_cn") == title_cn:
                sid = s["id"]
                original = {
                    k: s.get(k)
                    for k in (
                        "number_of_seasons", "start_date", "is_anime",
                        "number_of_episodes",
                    )
                }
                updates = {}
                if number_of_seasons is not None and s.get("number_of_seasons") != number_of_seasons:
                    updates["number_of_seasons"] = number_of_seasons
                if not s.get("start_date"):
                    updates["start_date"] = "2023-01-01"
                if s.get("is_anime") is None:
                    updates["is_anime"] = True
                if number_of_episodes is not None and s.get("number_of_episodes") != number_of_episodes:
                    updates["number_of_episodes"] = number_of_episodes
                if updates:
                    r = _api(f"/api/v1/series/{sid}", method="put", json=updates)
                    assert r.status_code == 200, f"series update failed: {r.text}"
                _TRACK["series_touched"].append((sid, original))
                return sid
    payload: dict = {
        "title_cn": title_cn,
        "title_en": title_en,
        "start_date": "2023-01-01",
        "is_anime": True,
    }
    if number_of_seasons is not None:
        payload["number_of_seasons"] = number_of_seasons
    if number_of_episodes is not None:
        payload["number_of_episodes"] = number_of_episodes
    r = _api("/api/v1/series", method="post", json=payload)
    assert r.status_code == 201, f"Series creation failed: {r.status_code} {r.text}"
    sid = r.json()["data"]["id"]
    _TRACK["series_created"].append(sid)
    return sid


def _delete(path: str) -> None:
    r = _api(path, method="delete")
    assert r.status_code in (200, 404), f"DELETE {path} failed: {r.status_code} {r.text}"


def _cleanup() -> None:
    """Teardown: remove every tracked entity from the primary app.

    Order: tasks → agents → channels (cascade resources) → movies → series
    (created: delete + drop the orphaned shell collection; touched: restore
    original fields) → downloader (only if this module created one).
    """
    task_ids = list(_TRACK["tasks"])
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
    for movie_id in _TRACK["movies"]:
        _delete(f"/api/v1/movies/{movie_id}")

    for sid in _TRACK["series_created"]:
        collection_id = None
        r = _api(f"/api/v1/series/{sid}")
        if r.status_code == 200:
            coll = r.json()["data"].get("collection")
            collection_id = coll.get("id") if coll else None
        _delete(f"/api/v1/series/{sid}")
        if collection_id:
            # Drop the orphaned shell collection left by the deleted work
            # (only when no other work still sits in it).
            r = _api(f"/api/v1/collections/{collection_id}/works")
            if r.status_code == 200 and not r.json().get("data"):
                _delete(f"/api/v1/collections/{collection_id}")
    for sid, original in _TRACK["series_touched"]:
        if sid in _TRACK["series_created"]:
            continue  # already deleted
        r = _api(f"/api/v1/series/{sid}", method="put", json=original)
        assert r.status_code in (200, 404), f"series restore failed: {r.text}"

    for dl_id in _TRACK["downloaders"]:
        _delete(f"/api/v1/downloaders/{dl_id}")


@pytest.fixture(scope="module")
def env():
    """Channels/series/agents shared by every class in this module.

    Teardown removes every entity this module created (and restores any
    pre-existing work it mutated) so later suites see a clean database.
    """
    mock_dl = _ensure_mock_downloader()

    # Linked channel: 咒术回战 S1 (single-season evidence) with 23 episodes so
    # absolute-episode → (season, episode) derivation has data to work with.
    series_id = _track_series(
        JUJUTSU_TITLE_CN, "Jujutsu Kaisen", number_of_episodes=23
    )

    ch_linked = _create_channel(f"Cov2 Linked {uuid.uuid4().hex[:6]}", MIKANANI_S3_URL)
    _fetch(ch_linked)
    res_linked = _resources(ch_linked)
    assert res_linked, "no resources on linked channel"
    linked = [r for r in res_linked if r.get("series_id") == series_id]
    assert linked, "resources did not auto-link to the series"

    # Auto agent: dispatch two distinct episodes → persistent downloading tasks.
    by_episode: dict[int, dict] = {}
    for res in linked:
        by_episode.setdefault(res.get("episode"), res)
    ep_keys = sorted(k for k in by_episode if k is not None)
    assert len(ep_keys) >= 2, "not enough parsed episodes"
    auto_agent = _create_agent(
        ch_linked,
        mock_dl,
        conflict_resolution="auto",
        dispatch_resource_ids=[by_episode[ep_keys[0]]["id"], by_episode[ep_keys[1]]["id"]],
    )
    dispatched = [by_episode[ep_keys[0]], by_episode[ep_keys[1]]]

    # Ask channel: mikanani series=2 titles carry "The Apothecary Diaries",
    # which exact-matches the 藥師少女的獨語 work — guarantee its
    # single-season evidence so the linked resources land season=1 and the
    # ask-mode backfill produces per-episode conflict decisions.
    _track_series("藥師少女的獨語", "The Apothecary Diaries")
    ch_ask = _create_channel(f"Cov2 Ask {uuid.uuid4().hex[:6]}", MIKANANI_S2_URL)
    _fetch(ch_ask)
    res_ask = [r for r in _resources(ch_ask) if r.get("series_id")]
    assert res_ask, "ask channel resources did not auto-link"
    ask_agent = _create_agent(
        ch_ask,
        mock_dl,
        conflict_resolution="ask",
        dispatch_resource_ids=[r["id"] for r in res_ask],
    )
    r = _api(f"/api/v1/agents/{ask_agent}/decisions")
    assert r.status_code == 200
    decisions = r.json().get("data", [])
    assert decisions, "ask agent produced no pending decisions"

    # Unmatched channel (no local work for "The Last of Us" on the primary
    # app — nothing to exact/fuzzy link against).
    ch_unmatched = _create_channel(
        f"Cov2 Unmatched {uuid.uuid4().hex[:6]}", EZTV_S0_URL,
        field_mapping=DEFAULT_FIELD_MAPPING,
    )
    _fetch(ch_unmatched)
    res_unmatched = _resources(ch_unmatched)
    assert res_unmatched, "no resources on unmatched channel"
    assert any(r.get("series_id") is None for r in res_unmatched)

    # Magnet channel (dmhy-style feed, magnet: enclosures).
    ch_magnet = _create_channel(
        f"Cov2 Magnet {uuid.uuid4().hex[:6]}", DMHY_S2_URL,
        field_mapping=MAGNET_FIELD_MAPPING,
    )
    _fetch(ch_magnet)
    res_magnet = [r for r in _resources(ch_magnet) if (r.get("torrent_url") or "").startswith("magnet:")]
    assert res_magnet, "no magnet resources after dmhy fetch"

    # Batch channel (芙莉莲 S01 合集 + single ep) for files/analyze-batch.
    _track_series(FRIEREN_TITLE_CN, "Frieren: Beyond Journey's End")
    ch_batch = _create_channel(f"Cov2 Batch {uuid.uuid4().hex[:6]}", BATCH_URL)
    _fetch(ch_batch)
    res_batch = [r for r in _resources(ch_batch) if r.get("is_batch")]
    assert res_batch, "no batch resource after batch-feed fetch"

    # A movie work without release_date: once associated, the resource is
    # missing the mandatory `year` field → a series/movie-linked confirmation.
    r = _api(
        "/api/v1/movies",
        method="post",
        json={"title_cn": f"覆盖率电影 {uuid.uuid4().hex[:6]}", "is_anime": False},
    )
    assert r.status_code == 201, f"movie create failed: {r.text}"
    movie_id = r.json()["data"]["id"]
    _TRACK["movies"].append(movie_id)

    # Associate one unmatched resource to the movie (movie-linked
    # confirmation) and give it a manual download task (movie download group).
    movie_resource_id = res_unmatched[0]["id"]
    r = _api(
        f"/api/v1/resources/{movie_resource_id}/associations",
        method="put",
        json={
            "is_batch": False,
            "works": [{"work_type": "movie", "work_id": movie_id}],
        },
    )
    assert r.status_code == 200, f"associate movie failed: {r.text}"
    r = _api(
        "/api/v1/tasks",
        method="post",
        json={"resource_id": movie_resource_id, "downloader_id": mock_dl},
    )
    assert r.status_code == 201, f"movie task create failed: {r.text}"
    _TRACK["tasks"].append(r.json()["data"]["id"])

    # A manual task on an unmatched resource → dashboard "unknown" group.
    r = _api(
        "/api/v1/tasks",
        method="post",
        json={"resource_id": res_unmatched[1]["id"], "downloader_id": mock_dl},
    )
    assert r.status_code == 201, f"unmatched task create failed: {r.text}"
    _TRACK["tasks"].append(r.json()["data"]["id"])

    env_dict = {
        "mock_dl": mock_dl,
        "series_id": series_id,
        "movie_id": movie_id,
        "ch_linked": ch_linked,
        "ch_ask": ch_ask,
        "ch_unmatched": ch_unmatched,
        "ch_magnet": ch_magnet,
        "ch_batch": ch_batch,
        "res_linked": linked,
        "res_unmatched": res_unmatched,
        "res_magnet": res_magnet,
        "res_batch": res_batch,
        "auto_agent": auto_agent,
        "ask_agent": ask_agent,
        "decisions": decisions,
        "dispatched": dispatched,
        "movie_resource_id": movie_resource_id,
    }
    yield env_dict
    _cleanup()


# =========================================================================
# Queue (app/api/v1/queue.py)
# =========================================================================


class TestQueueApi:
    def test_overview(self, env):
        r = _api("/api/v1/queue/overview")
        assert r.status_code == 200
        data = r.json()["data"]
        # Single-node stack: memory/all/since_restart; distributed: redis/web/last_24h.
        if data["backend"] == "memory":
            assert data["app_role"] == "all"
            assert data["stats_scope"] == "since_restart"
        else:
            assert data["backend"] == "redis"
            assert data["app_role"] == "web"
            assert data["stats_scope"] == "last_24h"
        assert isinstance(data["counts"], dict)
        assert isinstance(data["by_type"], list)
        assert data["total"] == sum(data["counts"].values())

    def test_jobs_filters_and_pagination(self, env):
        r = _api("/api/v1/queue/jobs", params={"page_size": 5})
        assert r.status_code == 200
        assert "total" in r.json()["meta"]

        r = _api("/api/v1/queue/jobs", params={"status": "done", "page_size": 5})
        assert r.status_code == 200
        for job in r.json()["data"]:
            assert job["status"] == "done"

        r = _api(
            "/api/v1/queue/jobs",
            params={"job_type": "run_agent", "page_size": 5},
        )
        assert r.status_code == 200
        for job in r.json()["data"]:
            assert job["job_type"] == "run_agent"

    def test_scheduler_disabled_snapshot(self, env):
        # Primary app runs with SCHEDULER_ENABLED=false → enabled: False.
        r = _api("/api/v1/queue/scheduler")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["enabled"] is False
        assert data["jobs"] == []


# =========================================================================
# Resource listing / field values (app/api/v1/resources.py)
# =========================================================================


class TestResourceListing:
    def test_matched_filters(self, env):
        matched = _resources(env["ch_linked"], matched="true")
        assert matched and all(
            r["series_id"] or r["movie_id"] for r in matched
        )
        unmatched = _resources(env["ch_unmatched"], matched="false")
        assert unmatched and all(
            not (r["series_id"] or r["movie_id"]) for r in unmatched
        )

    def test_grouped_series_and_unknown_buckets(self, env):
        # Linked channel → a series work group carrying every resource.
        r = _api(
            f"/api/v1/channels/{env['ch_linked']}/resources",
            params={"grouped": "true", "page_size": 50},
        )
        assert r.status_code == 200
        groups = r.json()["data"]["groups"]
        series_groups = [g for g in groups if g["type"] == "series"]
        assert series_groups, "expected a series group"
        assert series_groups[0]["resources"], "series group has no resources"
        assert series_groups[0]["title"], "series group title missing"

        # Unmatched channel → the synthetic 未识别 bucket.
        r = _api(
            f"/api/v1/channels/{env['ch_unmatched']}/resources",
            params={"grouped": "true", "matched": "false", "page_size": 50},
        )
        assert r.status_code == 200
        groups = r.json()["data"]["groups"]
        unknown = [g for g in groups if g["type"] == "unknown"]
        assert unknown and unknown[0]["resources"], "unknown bucket missing"

        # grouped + matched=true drops the unknown bucket entirely.
        r = _api(
            f"/api/v1/channels/{env['ch_unmatched']}/resources",
            params={"grouped": "true", "matched": "true", "page_size": 50},
        )
        assert r.status_code == 200
        assert all(
            g["type"] != "unknown" for g in r.json()["data"]["groups"]
        )

    def test_grouped_download_status_annotation(self, env):
        # The auto agent's dispatched resources surface their task outcome.
        r = _api(
            f"/api/v1/channels/{env['ch_linked']}/resources",
            params={"grouped": "true", "page_size": 50},
        )
        assert r.status_code == 200
        resources = [
            res
            for g in r.json()["data"]["groups"]
            for res in g["resources"]
        ]
        tasked = [res for res in resources if res["has_download_task"]]
        assert tasked, "expected resources annotated with a download task"
        assert all(res["download_status"] for res in tasked)

    def test_channel_404(self):
        assert _api("/api/v1/channels/no-such/resources").status_code == 404

    def test_field_values(self, env):
        ch = env["ch_linked"]
        r = _api(f"/api/v1/channels/{ch}/field-values", params={"field": "resolution"})
        assert r.status_code == 200
        values = r.json()["data"]
        assert "1080p" in values

        # Prefix filter narrows the list.
        r = _api(
            f"/api/v1/channels/{ch}/field-values",
            params={"field": "resolution", "q": "1080"},
        )
        assert r.status_code == 200
        assert r.json()["data"] == ["1080p"]

        # Whitelist guard.
        r = _api(f"/api/v1/channels/{ch}/field-values", params={"field": "id"})
        assert r.status_code == 422

        assert _api("/api/v1/channels/no-such/field-values", params={"field": "resolution"}).status_code == 404

    def test_field_values_subtitle_langs_aggregation(self, env):
        ch = env["ch_unmatched"]
        res = env["res_unmatched"]
        # Seed edge values: a normal tag, an empty tag, and an empty list.
        r = _api(
            f"/api/v1/resources/{res[2]['id']}",
            method="patch",
            json={"subtitle_langs": ["zh-CN", ""]},
        )
        assert r.status_code == 200, f"patch subtitle_langs failed: {r.text}"
        r = _api(
            f"/api/v1/resources/{res[3]['id']}",
            method="patch",
            json={"subtitle_langs": []},
        )
        assert r.status_code == 200

        r = _api(f"/api/v1/channels/{ch}/field-values", params={"field": "subtitle_langs"})
        assert r.status_code == 200
        assert "zh-CN" in r.json()["data"]

        # Prefix that matches nothing → empty result (prefix guard branch).
        r = _api(
            f"/api/v1/channels/{ch}/field-values",
            params={"field": "subtitle_langs", "q": "ja"},
        )
        assert r.status_code == 200
        assert "zh-CN" not in r.json()["data"]


# =========================================================================
# PATCH /resources/{id} (parse-field correction)
# =========================================================================


class TestParseCorrection:
    def test_404(self):
        r = _api("/api/v1/resources/no-such", method="patch", json={"episode": 1})
        assert r.status_code == 404

    def test_episode_fields_mark_manual(self, env):
        res = env["res_linked"][2]
        r = _api(
            f"/api/v1/resources/{res['id']}",
            method="patch",
            json={"episode": 7, "season": 1, "absolute_episode": 7},
        )
        assert r.status_code == 200, f"patch failed: {r.text}"
        data = r.json()["data"]
        assert data["episode"] == 7
        assert data["season"] == 1
        assert data["absolute_episode"] == 7
        assert data["episode_confidence"] == "manual"

    def test_generic_media_fields(self, env):
        res = env["res_linked"][3]
        r = _api(
            f"/api/v1/resources/{res['id']}",
            method="patch",
            json={
                "resolution": "2160p",
                "source": "BDRip",
                "video_codec": "HEVC",
                "audio_codec": "FLAC",
                "subtitle_type": "简繁内封字幕",
                "container": "mkv",
            },
        )
        assert r.status_code == 200, f"patch failed: {r.text}"
        data = r.json()["data"]
        assert data["resolution"] == "2160p"
        assert data["source"] == "BDRip"
        # Media-descriptor corrections never touch episode_confidence.
        assert data["episode_confidence"] != "manual"

    def test_subtitle_groups_sync_and_conflict(self, env):
        res = env["res_linked"][4]
        # Plural-only update: legacy scalar is derived from the list.
        r = _api(
            f"/api/v1/resources/{res['id']}",
            method="patch",
            json={"subtitle_groups": ["GroupA", "GroupB"]},
        )
        assert r.status_code == 200, f"patch failed: {r.text}"
        data = r.json()["data"]
        assert data["subtitle_groups"] == ["GroupA", "GroupB"]
        assert data["subtitle_group"] == "GroupA&GroupB"

        # Agreeing pair is accepted.
        r = _api(
            f"/api/v1/resources/{res['id']}",
            method="patch",
            json={"subtitle_group": "GroupA", "subtitle_groups": ["GroupA"]},
        )
        assert r.status_code == 200, f"agreeing pair failed: {r.text}"
        assert r.json()["data"]["subtitle_group"] == "GroupA"

        # Conflicting pair → 422.
        r = _api(
            f"/api/v1/resources/{res['id']}",
            method="patch",
            json={"subtitle_group": "GroupA", "subtitle_groups": ["GroupC"]},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_subtitle_group_scalar_only(self, env):
        res = env["res_linked"][11]
        r = _api(
            f"/api/v1/resources/{res['id']}",
            method="patch",
            json={"subtitle_group": "Solo"},
        )
        assert r.status_code == 200, f"patch failed: {r.text}"
        data = r.json()["data"]
        assert data["subtitle_group"] == "Solo"
        assert data["subtitle_groups"] == ["Solo"]

    def test_batch_invariants(self, env):
        res = env["res_linked"][5]
        # Flip to batch: episode cleared, batch_scope defaults to season.
        r = _api(
            f"/api/v1/resources/{res['id']}",
            method="patch",
            json={"is_batch": True, "episode_start": 1, "episode_end": 12},
        )
        assert r.status_code == 200, f"patch failed: {r.text}"
        data = r.json()["data"]
        assert data["is_batch"] is True
        assert data["episode"] is None
        assert data["batch_scope"] == "season"
        assert data["episode_start"] == 1 and data["episode_end"] == 12

        # Flip back: batch fields cleared.
        r = _api(
            f"/api/v1/resources/{res['id']}",
            method="patch",
            json={"is_batch": False, "episode": 3},
        )
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["is_batch"] is False
        assert data["batch_scope"] is None
        assert data["episode_start"] is None and data["episode_end"] is None
        assert data["episode"] == 3
        assert data["episode_confidence"] == "manual"


# =========================================================================
# PATCH /resources/{id}/episode
# =========================================================================


class TestEpisodeCorrection:
    def test_404(self):
        r = _api(
            "/api/v1/resources/no-such/episode",
            method="patch",
            json={"episode": 1},
        )
        assert r.status_code == 404

    def test_explicit_season_and_absolute(self, env):
        res = env["res_linked"][6]
        r = _api(
            f"/api/v1/resources/{res['id']}/episode",
            method="patch",
            json={"episode": 4, "season": 1, "absolute_episode": 4},
        )
        assert r.status_code == 200, f"episode patch failed: {r.text}"
        data = r.json()["data"]
        assert data["episode"] == 4
        assert data["season"] == 1
        assert data["episode_confidence"] == "manual"

    def test_absolute_episode_derives_season_via_collection(self, env):
        """No explicit season: the absolute number is located along the
        work's collection members (single-season work, 23 episodes)."""
        res = env["res_linked"][7]
        r = _api(
            f"/api/v1/resources/{res['id']}/episode",
            method="patch",
            json={"episode": 5, "absolute_episode": 5},
        )
        assert r.status_code == 200, f"episode patch failed: {r.text}"
        data = r.json()["data"]
        assert data["season"] == 1  # derived, not sent
        assert data["episode"] == 5
        assert data["series_id"] == env["series_id"]
        assert data["episode_confidence"] == "manual"


# =========================================================================
# GET /resources/{id}/files + metadata
# =========================================================================


class TestResourceFiles:
    def test_404(self):
        assert _api("/api/v1/resources/no-such/files").status_code == 404

    def test_torrent_listing(self, env):
        # The dispatched resource has a mock-downloader task; the fake-hash
        # torrent URL 404s, so the listing falls through to the downloader RPC.
        res = env["dispatched"][0]
        r = _api(f"/api/v1/resources/{res['id']}/files")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["files"], "expected a non-empty torrent file listing"
        assert data["source"] == "downloader"
        assert data["magnet_resolve"] is None
        names = [f["name"] for f in data["files"]]
        assert any(name for name in names)

    def test_magnet_resource_reports_resolve_state(self, env):
        res = env["res_magnet"][0]
        r = _api(f"/api/v1/resources/{res['id']}/files")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["files"] == []
        assert data["source"] == "none"
        assert data["magnet_resolve"] is not None
        assert "status" in data["magnet_resolve"]

    def test_metadata_linked_movie(self, env):
        r = _api(f"/api/v1/resources/{env['movie_resource_id']}/metadata")
        assert r.status_code == 200, f"metadata failed: {r.text}"
        data = r.json()["data"]
        assert data["movie_id"] == env["movie_id"]
        assert data["linked"]["type"] == "movie"
        assert data["linked"]["entity"]["id"] == env["movie_id"]

    def test_metadata_404(self):
        assert _api("/api/v1/resources/no-such/metadata").status_code == 404


# =========================================================================
# POST /resources/{id}/magnet-resolve
# =========================================================================


class TestMagnetResolve:
    def test_404(self):
        r = _api("/api/v1/resources/no-such/magnet-resolve", method="post")
        assert r.status_code == 404

    def test_non_magnet_422(self, env):
        r = _api(
            f"/api/v1/resources/{env['res_linked'][0]['id']}/magnet-resolve",
            method="post",
        )
        assert r.status_code == 422
        assert "magnet" in r.json()["error"]["message"].lower()

    def test_conflict_while_in_progress_or_done(self, env):
        """Fetch auto-enqueues magnet resolution, so a manual retry on a
        freshly-fetched magnet resource either conflicts (409, resolution
        pending/running) or — once the local seeder answered — re-enqueues."""
        res = env["res_magnet"][0]
        r = _api(f"/api/v1/resources/{res['id']}/magnet-resolve", method="post")
        assert r.status_code in (200, 409), f"unexpected: {r.status_code} {r.text}"
        if r.status_code == 409:
            assert r.json()["error"]["code"] == "INVALID_STATE"
        else:
            assert r.json()["data"]["magnet_resolve"]["status"] == "pending"

    def test_terminal_retry_flow(self, env):
        """Once a resolution reaches a terminal state, a manual retry with an
        invalid tracker list is rejected (422); a clean retry resets state and
        re-enqueues (200 → pending)."""

        def _terminal_state(res_id: str):
            r = _api(f"/api/v1/resources/{res_id}/files")
            if r.status_code != 200:
                return None
            state = (r.json()["data"].get("magnet_resolve") or {})
            return state if state.get("status") in ("done", "failed") else None

        res_id = None
        deadline = time.time() + 300
        while time.time() < deadline and res_id is None:
            for res in env["res_magnet"]:
                state = _terminal_state(res["id"])
                if state is not None:
                    res_id = res["id"]
                    break
            if res_id is None:
                time.sleep(10)
        if res_id is None:
            pytest.skip("no magnet resolution reached a terminal state in time")

        # Invalid tracker URLs are validated before anything is enqueued.
        r = _api(
            f"/api/v1/resources/{res_id}/magnet-resolve",
            method="post",
            json={"trackers": ["not-a-tracker-url"]},
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"

        r = _api(f"/api/v1/resources/{res_id}/magnet-resolve", method="post")
        assert r.status_code == 200, f"magnet-resolve retry failed: {r.text}"
        state = r.json()["data"]["magnet_resolve"]
        assert state["status"] == "pending"
        assert state["attempts"] == 0

        # Immediately re-retrying conflicts while the new attempt is active
        # (or legally re-enqueues when the seeder answered instantly).
        r = _api(f"/api/v1/resources/{res_id}/magnet-resolve", method="post")
        assert r.status_code in (200, 409)
        if r.status_code == 409:
            assert r.json()["error"]["code"] == "INVALID_STATE"


# =========================================================================
# PUT /resources/{id}/associations
# =========================================================================


class TestAssociations:
    def test_404(self):
        r = _api(
            "/api/v1/resources/no-such/associations",
            method="put",
            json={"is_batch": False, "works": []},
        )
        assert r.status_code == 404

    def test_unknown_work_422(self, env):
        r = _api(
            f"/api/v1/resources/{env['res_unmatched'][4]['id']}/associations",
            method="put",
            json={
                "is_batch": False,
                "works": [{"work_type": "series", "work_id": "no-such-work"}],
            },
        )
        assert r.status_code == 422
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_multi_work_pack_requires_assignments_422(self, env):
        r = _api(
            f"/api/v1/resources/{env['res_unmatched'][5]['id']}/associations",
            method="put",
            json={
                "is_batch": True,
                "works": [
                    {"work_type": "series", "work_id": env["series_id"]},
                    {"work_type": "movie", "work_id": env["movie_id"]},
                ],
                "assignments": [],
            },
        )
        assert r.status_code == 422, f"expected 422: {r.text}"

    def test_single_work_bind_success(self, env):
        res = env["res_unmatched"][6]
        r = _api(
            f"/api/v1/resources/{res['id']}/associations",
            method="put",
            json={
                "is_batch": False,
                "works": [{"work_type": "series", "work_id": env["series_id"]}],
                "season": 1,
                "episode": 9,
            },
        )
        assert r.status_code == 200, f"associations failed: {r.text}"
        data = r.json()["data"]
        assert data["series_id"] == env["series_id"]
        assert data["episode"] == 9
        assert "warnings" in data

        # Saving the identical state again is idempotent.
        r = _api(
            f"/api/v1/resources/{res['id']}/associations",
            method="put",
            json={
                "is_batch": False,
                "works": [{"work_type": "series", "work_id": env["series_id"]}],
                "season": 1,
                "episode": 9,
            },
        )
        assert r.status_code == 200

    def test_empty_works_keeps_mount(self, env):
        res = env["res_unmatched"][7]
        r = _api(
            f"/api/v1/resources/{res['id']}/associations",
            method="put",
            json={"is_batch": False, "works": []},
        )
        assert r.status_code == 200, f"empty-works submit failed: {r.text}"

    def test_linked_channel_resource_retriggers_agents(self, env):
        # The channel has active agents: the association commit enqueues a
        # targeted rerun for each of them (and the organize refresh job).
        res = env["res_linked"][10]
        r = _api(
            f"/api/v1/resources/{res['id']}/associations",
            method="put",
            json={
                "is_batch": False,
                "works": [{"work_type": "series", "work_id": env["series_id"]}],
                "season": 1,
                "episode": res.get("episode") or 1,
            },
        )
        assert r.status_code == 200, f"associations failed: {r.text}"
        assert r.json()["data"]["series_id"] == env["series_id"]


# =========================================================================
# analyze-batch (+ SSE stream)
# =========================================================================


class TestAnalyzeBatch:
    def test_404(self):
        r = _api("/api/v1/resources/no-such/analyze-batch", method="post")
        assert r.status_code == 404

    def test_no_listing_returns_none_suggestion(self, env):
        res = env["res_magnet"][2]
        r = _api(f"/api/v1/resources/{res['id']}/analyze-batch", method="post")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["suggestion"] is None
        assert data["listing_source"] == "none"

    def test_batch_resource_analysis(self, env):
        # Dispatched resource → listing resolved via the mock downloader RPC.
        res = env["dispatched"][1]
        r = _api(f"/api/v1/resources/{res['id']}/analyze-batch", method="post")
        assert r.status_code == 200, f"analyze-batch failed: {r.text}"
        data = r.json()["data"]
        assert data["listing_source"] == "downloader"
        assert data["suggestion"] is not None
        assert "deterministic" in data["suggestion"]

    def test_stream_404(self):
        r = _api("/api/v1/resources/no-such/analyze-batch-stream", method="post")
        assert r.status_code == 404

    def test_stream_events(self, env):
        """Consume the SSE stream until the terminal result event.

        Uses a magnet resource: its listing is empty, so the background
        analysis job finishes immediately (no LLM call) and the result is
        cached — the follow-up call exercises the cache-reuse branch.
        """
        res = env["res_magnet"][3]
        events: list[str] = []
        deadline = time.time() + 90
        with httpx.Client(timeout=httpx.Timeout(100.0), headers=API_HEADERS) as c:
            with c.stream(
                "POST", f"{RSSRIPPLE}/api/v1/resources/{res['id']}/analyze-batch-stream"
            ) as resp:
                assert resp.status_code == 200
                for line in resp.iter_lines():
                    if line.startswith("data:"):
                        events.append(line)
                        if '"type": "result"' in line or '"type": "warning"' in line:
                            break
                        if '"type":"result"' in line or '"type":"warning"' in line:
                            break
                    if time.time() > deadline:
                        break
        assert events, "no SSE events received"
        terminal = [e for e in events if '"result"' in e or '"warning"' in e]
        assert terminal, "stream never reached a terminal event"

        # Second call serves the cached analysis immediately.
        events2: list[str] = []
        with httpx.Client(timeout=httpx.Timeout(30.0), headers=API_HEADERS) as c:
            with c.stream(
                "POST", f"{RSSRIPPLE}/api/v1/resources/{res['id']}/analyze-batch-stream"
            ) as resp:
                assert resp.status_code == 200
                for line in resp.iter_lines():
                    if line.startswith("data:"):
                        events2.append(line)
        assert any("已复用文件解析缓存" in e for e in events2), (
            f"expected the cache-reuse event, got: {events2}"
        )


# =========================================================================
# Agents (app/api/v1/agents.py)
# =========================================================================


class TestAgentValidation:
    def test_create_invalid_filter_422(self, env):
        r = _api(
            "/api/v1/agents",
            method="post",
            json={
                "name": "bad-filter",
                "channel_id": env["ch_linked"],
                "downloader_id": env["mock_dl"],
                "scope_channel_wide": True,
                "filter_config": {"field": "resolution", "operator": "eq", "value": ""},
            },
        )
        assert r.status_code == 422

    def test_create_invalid_pick_preferences_422(self, env):
        r = _api(
            "/api/v1/agents",
            method="post",
            json={
                "name": "bad-pick",
                "channel_id": env["ch_linked"],
                "downloader_id": env["mock_dl"],
                "scope_channel_wide": True,
                "pick_preferences": [
                    {"field": "not_a_field", "operator": "eq", "value": "x"}
                ],
            },
        )
        assert r.status_code == 422

    def test_create_invalid_work_overrides_422(self, env):
        # Syntactically invalid override → validate_filter_config error.
        r = _api(
            "/api/v1/agents",
            method="post",
            json={
                "name": "bad-work-filter",
                "channel_id": env["ch_linked"],
                "downloader_id": env["mock_dl"],
                "works": [{
                    "content_type": "tv",
                    "series_id": env["series_id"],
                    "filter_overrides": {"field": "resolution", "operator": "eq", "value": ""},
                }],
            },
        )
        assert r.status_code == 422

        # Work field not declared by the channel → channel gate error.
        r = _api(
            "/api/v1/agents",
            method="post",
            json={
                "name": "gated-work-filter",
                "channel_id": env["ch_linked"],
                "downloader_id": env["mock_dl"],
                "works": [{
                    "content_type": "tv",
                    "series_id": env["series_id"],
                    "filter_overrides": {"field": "series.rating", "operator": "gt", "value": 5},
                }],
            },
        )
        assert r.status_code == 422

    def test_create_too_many_works_422(self, env):
        r = _api(
            "/api/v1/agents",
            method="post",
            json={
                "name": "too-many-works",
                "channel_id": env["ch_linked"],
                "downloader_id": env["mock_dl"],
                "scope_channel_wide": False,
                "works": [
                    {"content_type": "tv", "series_id": env["series_id"]}
                    for _ in range(11)
                ],
            },
        )
        assert r.status_code == 422
        assert "10" in r.json()["error"]["message"]

    def test_create_skips_invalid_work_entries(self, env):
        r = _api(
            "/api/v1/agents",
            method="post",
            json={
                "name": f"skip-works {uuid.uuid4().hex[:6]}",
                "channel_id": env["ch_linked"],
                "downloader_id": env["mock_dl"],
                "works": [
                    {"content_type": "tv"},  # no series_id → skipped
                    {"content_type": "movie"},  # no movie_id → skipped
                    {  # both set → skipped
                        "content_type": "tv",
                        "series_id": env["series_id"],
                        "movie_id": env["movie_id"],
                    },
                    {"content_type": "tv", "series_id": env["series_id"]},
                ],
            },
        )
        assert r.status_code == 201, f"create failed: {r.text}"
        works = r.json()["data"]["works"]
        assert len(works) == 1
        assert works[0]["series_id"] == env["series_id"]

    def test_create_run_immediately(self, env):
        agent_id = _create_agent(
            env["ch_unmatched"], env["mock_dl"], run_immediately=True
        )
        # The background full-history run was enqueued at create time.
        deadline = time.time() + 60
        status = None
        while time.time() < deadline:
            r = _api(f"/api/v1/agents/{agent_id}/run-status")
            assert r.status_code == 200
            status = r.json()["data"].get("status")
            if status in ("done", "failed"):
                break
            time.sleep(2)
        assert status is not None, "run_immediately did not enqueue a run"


class TestAgentUpdate:
    def test_update_filter_error_422(self, env):
        r = _api(
            f"/api/v1/agents/{env['auto_agent']}",
            method="put",
            json={"filter_config": {"field": "resolution", "operator": "eq", "value": ""}},
        )
        assert r.status_code == 422

    def test_update_unknown_downloader_422(self, env):
        r = _api(
            f"/api/v1/agents/{env['auto_agent']}",
            method="put",
            json={"downloader_id": "no-such-downloader"},
        )
        assert r.status_code == 422

    def test_update_replaces_works(self, env):
        agent_id = _create_agent(env["ch_linked"], env["mock_dl"])
        r = _api(
            f"/api/v1/agents/{agent_id}",
            method="put",
            json={
                "works": [
                    {"content_type": "tv", "series_id": env["series_id"]},
                    {"content_type": "tv"},  # invalid → skipped
                ],
            },
        )
        assert r.status_code == 200, f"update failed: {r.text}"
        works = r.json()["data"]["works"]
        assert len(works) == 1
        assert works[0]["series_id"] == env["series_id"]

    def test_update_404(self):
        r = _api("/api/v1/agents/no-such", method="put", json={"name": "x"})
        assert r.status_code == 404


def _run_to_completion(agent_id: str, body: dict | None = None, timeout: int = 120) -> dict:
    """POST /agents/{id}/run and block until the job is done/failed."""
    r = _api(f"/api/v1/agents/{agent_id}/run", method="post", json=body)
    if r.status_code == 409:
        # A previous run is still listed; wait it out then retry once.
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = _api(f"/api/v1/agents/{agent_id}/run-status").json()["data"]
            if st.get("status") in ("done", "failed"):
                break
            time.sleep(2)
        r = _api(f"/api/v1/agents/{agent_id}/run", method="post", json=body)
    assert r.status_code == 200, f"run failed: {r.text}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = _api(f"/api/v1/agents/{agent_id}/run-status").json()["data"]
        if st.get("status") in ("done", "failed"):
            return st
        time.sleep(2)
    raise TimeoutError(f"agent run did not finish for {agent_id}")


class TestAgentRun:
    def test_scan_since_future_422(self, env):
        r = _api(
            f"/api/v1/agents/{env['auto_agent']}/run",
            method="post",
            json={"scan_since": "2999-01-01T00:00:00+00:00"},
        )
        assert r.status_code == 422
        assert "scan_since" in r.json()["error"]["message"]

    def test_scan_since_past_enqueues(self, env):
        agent_id = _create_agent(env["ch_unmatched"], env["mock_dl"])
        # Timezone-aware past datetime is normalised to naive UTC.
        r = _api(
            f"/api/v1/agents/{agent_id}/run",
            method="post",
            json={"scan_since": "2020-01-01T00:00:00+02:00"},
        )
        assert r.status_code == 200, f"run failed: {r.text}"
        assert r.json()["data"]["job_type"] == "run_agent"

    def test_run_404(self):
        r = _api("/api/v1/agents/no-such/run", method="post")
        assert r.status_code == 404

    def test_run_status_404(self):
        assert _api("/api/v1/agents/no-such/run-status").status_code == 404

    def test_runs_history_annotations(self, env):
        # A full-history windowed run matches every linked resource; the
        # already-dispatched ones are flagged dispatched in the run history.
        st = _run_to_completion(env["auto_agent"], {"scan_since": None})
        assert st["status"] == "done", f"run failed: {st.get('error')}"

        r = _api(
            f"/api/v1/agents/{env['auto_agent']}/runs",
            params={"page_size": 50},
        )
        assert r.status_code == 200
        rows = r.json()["data"]
        assert rows, "no run history after a completed run"
        latest = rows[0]
        assert latest["matched"] >= 1
        assert latest["matched_resources"], "matched resources not hydrated"
        assert any(m["dispatched"] for m in latest["matched_resources"])

        # non_empty filter keeps only productive runs.
        r = _api(
            f"/api/v1/agents/{env['auto_agent']}/runs",
            params={"non_empty": "true", "page_size": 50},
        )
        assert r.status_code == 200
        for row in r.json()["data"]:
            assert (
                row["dispatched"] > 0
                or row["pending_decisions"] > 0
                or row["status"] in ("running", "failed")
            )

    def test_runs_pending_decision_marks(self, env):
        # The ask agent's full-history run re-confirms the per-episode
        # conflicts → run lands on pending_decisions with marked resources.
        st = _run_to_completion(env["ask_agent"], {"scan_since": None})
        assert st["status"] == "done", f"run failed: {st.get('error')}"
        assert (st.get("result") or {}).get("pending_decisions", 0) >= 1

        r = _api(
            f"/api/v1/agents/{env['ask_agent']}/runs",
            params={"page_size": 50},
        )
        assert r.status_code == 200
        rows = r.json()["data"]
        assert rows, "ask agent has no run history"
        latest = rows[0]
        assert latest["status"] == "pending_decisions"
        marked = [
            m for m in latest.get("matched_resources", []) if m.get("pending_decision")
        ]
        assert marked, "no matched resource marked as pending decision"

        # The channel resource listing carries the same pending-decision flag.
        r = _api(
            f"/api/v1/channels/{env['ch_ask']}/resources",
            params={"page_size": 100},
        )
        assert r.status_code == 200
        assert any(res["pending_decision"] for res in r.json()["data"])

    def test_runs_404(self):
        assert _api("/api/v1/agents/no-such/runs").status_code == 404


class TestAgentMisc:
    def test_test_filters(self, env):
        res_ids = [r["id"] for r in env["res_linked"][:3]]
        r = _api(
            f"/api/v1/agents/{env['auto_agent']}/test-filters",
            method="post",
            json={"resource_ids": res_ids},
        )
        assert r.status_code == 200, f"test-filters failed: {r.text}"
        data = r.json()["data"]
        assert data["total"] == len(res_ids)
        assert {item["resource_id"] for item in data["resources"]} == set(res_ids)

    def test_test_filters_404(self):
        r = _api("/api/v1/agents/no-such/test-filters", method="post", json={})
        assert r.status_code == 404

    def test_rules_preview(self, env):
        # Create-mode preview with explicit works (tv + movie buckets).
        r = _api(
            "/api/v1/agents/rules-preview",
            method="post",
            json={
                "channel_id": env["ch_linked"],
                "scope_channel_wide": False,
                "works": [
                    {"content_type": "tv", "series_id": env["series_id"]},
                    {"content_type": "movie", "movie_id": env["movie_id"]},
                ],
            },
        )
        assert r.status_code == 200, f"rules-preview failed: {r.text}"
        data = r.json()["data"]
        assert data["newly_matching"], "expected newly-matching resources"

        # Existing-agent preview diffs against the stored rules.
        r = _api(
            "/api/v1/agents/rules-preview",
            method="post",
            json={
                "agent_id": env["auto_agent"],
                "scope_channel_wide": True,
                "filter_config": {
                    "field": "resolution", "operator": "eq", "value": "1080p"
                },
            },
        )
        assert r.status_code == 200

    def test_rules_preview_errors(self, env):
        r = _api(
            "/api/v1/agents/rules-preview",
            method="post",
            json={
                "channel_id": env["ch_linked"],
                "filter_config": {"field": "resolution", "operator": "eq", "value": ""},
            },
        )
        assert r.status_code == 422

        r = _api(
            "/api/v1/agents/rules-preview",
            method="post",
            json={"agent_id": "no-such-agent"},
        )
        assert r.status_code == 404

        r = _api("/api/v1/agents/rules-preview", method="post", json={})
        assert r.status_code == 422

    def test_works_crud(self, env):
        agent_id = _create_agent(
            env["ch_linked"], env["mock_dl"], scope_channel_wide=False
        )
        r = _api(f"/api/v1/agents/{agent_id}/works")
        assert r.status_code == 200
        assert r.json()["data"] == []

        r = _api(
            f"/api/v1/agents/{agent_id}/works",
            method="post",
            json={"content_type": "tv", "series_id": env["series_id"]},
        )
        assert r.status_code == 201, f"create work failed: {r.text}"
        work_id = r.json()["data"]["id"]

        r = _api(
            f"/api/v1/agents/{agent_id}/works/{work_id}",
            method="put",
            json={"display_name_override": "JJK"},
        )
        assert r.status_code == 200
        assert r.json()["data"]["display_name_override"] == "JJK"

        r = _api(f"/api/v1/agents/{agent_id}/works/{work_id}", method="delete")
        assert r.status_code == 200
        assert r.json()["data"]["deleted"] is True

    def test_works_validation(self, env):
        agent_id = _create_agent(
            env["ch_linked"], env["mock_dl"], scope_channel_wide=False
        )
        # tv work without series_id → 422.
        r = _api(
            f"/api/v1/agents/{agent_id}/works",
            method="post",
            json={"content_type": "tv"},
        )
        assert r.status_code == 422
        # movie work without movie_id → 422.
        r = _api(
            f"/api/v1/agents/{agent_id}/works",
            method="post",
            json={"content_type": "movie"},
        )
        assert r.status_code == 422
        # Both targets set → 422.
        r = _api(
            f"/api/v1/agents/{agent_id}/works",
            method="post",
            json={
                "content_type": "tv",
                "series_id": env["series_id"],
                "movie_id": env["movie_id"],
            },
        )
        assert r.status_code == 422

    def test_works_404s(self):
        assert _api("/api/v1/agents/no-such/works").status_code == 404
        r = _api(
            "/api/v1/agents/no-such/works",
            method="post",
            json={"content_type": "tv", "series_id": "x"},
        )
        assert r.status_code == 404
        r = _api(
            "/api/v1/agents/no-such/works/no-such",
            method="put",
            json={"display_name_override": "x"},
        )
        assert r.status_code == 404
        r = _api("/api/v1/agents/no-such/works/no-such", method="delete")
        assert r.status_code == 404

    def test_suggestions_404(self):
        assert _api("/api/v1/agents/no-such/suggestions").status_code == 404

    def test_suggestions_list(self, env):
        r = _api(f"/api/v1/agents/{env['auto_agent']}/suggestions")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["scope_channel_wide"] is True
        assert isinstance(data["suggestions"], list)

    def test_create_work_max_10(self, env):
        agent_id = _create_agent(
            env["ch_linked"], env["mock_dl"], scope_channel_wide=False
        )
        for i in range(10):
            r = _api(
                f"/api/v1/agents/{agent_id}/works",
                method="post",
                json={
                    "content_type": "tv",
                    "series_id": env["series_id"],
                    "display_name_override": f"w{i}",
                },
            )
            assert r.status_code == 201, f"work {i} failed: {r.text}"
        r = _api(
            f"/api/v1/agents/{agent_id}/works",
            method="post",
            json={"content_type": "tv", "series_id": env["series_id"]},
        )
        assert r.status_code == 400
        assert "10" in r.json()["error"]["message"]


# =========================================================================
# Dashboard (app/api/v1/dashboard.py)
# =========================================================================


class TestDashboard:
    def test_overview_split_endpoint(self, env):
        r = _api("/api/v1/dashboard/overview")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["pending_decisions_total"] >= 1
        my_ids = {dec["id"] for dec in env["decisions"]}
        decision = next(d for d in data["pending_decisions"] if d["id"] in my_ids)
        assert decision["candidate_resources"], "decision candidates not hydrated"
        assert decision["title"], "decision title missing"

    def test_downloads_split_endpoint(self, env):
        r = _api("/api/v1/dashboard/downloads")
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["active_download_count"] >= 1
        groups = {g["type"] for g in data["active_download_groups"]}
        # Auto-agent series tasks + manual movie task + manual unmatched task.
        assert "series" in groups
        assert "movie" in groups
        assert "unknown" in groups
        series_group = next(
            g for g in data["active_download_groups"] if g["type"] == "series"
        )
        assert series_group["tasks"][0]["agent_name"]

    def test_overview_confirmations(self, env):
        # The movie-associated resource misses the mandatory year field → a
        # movie-linked confirmation with a resolvable work_ref.
        r = _api(
            "/api/v1/dashboard/overview",
            params={"page_size": 100},
        )
        assert r.status_code == 200
        confirmations = r.json()["data"]["pending_confirmations"]
        mine = [
            c for c in confirmations
            if c["resource"]["id"] == env["movie_resource_id"]
        ]
        assert mine, "movie-linked confirmation not listed"
        assert mine[0]["work_ref"] == {"kind": "movie", "id": env["movie_id"]}
        assert mine[0]["work_title"]
        assert "required_fields_missing" in mine[0]["kinds"]

    def test_ignore_decision_todos(self, env):
        dec_id = env["decisions"][0]["id"]
        r = _api(
            "/api/v1/dashboard/todos/ignore",
            method="post",
            json={"kind": "decision", "ids": [dec_id]},
        )
        assert r.status_code == 200, f"ignore failed: {r.text}"
        data = r.json()["data"]
        assert data == {"requested": 1, "ignored": 1, "unchanged": 0}

        # Already-skipped decision is unchanged on a second ignore.
        r = _api(
            "/api/v1/dashboard/todos/ignore",
            method="post",
            json={"kind": "decision", "ids": [dec_id]},
        )
        assert r.json()["data"]["ignored"] == 0

    def test_ignore_confirmation_todos(self, env):
        res_id = env["res_unmatched"][8]["id"]
        r = _api(
            "/api/v1/dashboard/todos/ignore",
            method="post",
            json={"kind": "confirmation", "ids": [res_id]},
        )
        assert r.status_code == 200
        assert r.json()["data"]["ignored"] == 1

        # Ignored confirmations leave the pending list.
        r = _api("/api/v1/dashboard/overview", params={"page_size": 100})
        confirmations = r.json()["data"]["pending_confirmations"]
        assert all(c["resource"]["id"] != res_id for c in confirmations)

    def test_ignore_plan_todos_unknown_ids(self, env):
        r = _api(
            "/api/v1/dashboard/todos/ignore",
            method="post",
            json={"kind": "plan", "ids": ["no-such-plan"]},
        )
        assert r.status_code == 200
        assert r.json()["data"] == {"requested": 1, "ignored": 0, "unchanged": 1}

    def test_aggregate_endpoint(self, env):
        r = _api("/api/v1/dashboard")
        assert r.status_code == 200
        data = r.json()["data"]
        assert "active_download_count" in data
        assert "pending_decisions" in data

    def test_run_status_correction_after_all_decisions_resolved(self, env):
        """Once every pending decision is handled, historical
        ``pending_decisions`` runs are presented as ``success`` (read-time
        correction)."""
        r = _api(f"/api/v1/agents/{env['ask_agent']}/decisions")
        assert r.status_code == 200
        ids = [
            d["id"] for d in r.json()["data"]
            if d.get("status") in (None, "pending")
        ]
        assert ids, "no pending decisions left to ignore"
        r = _api(
            "/api/v1/dashboard/todos/ignore",
            method="post",
            json={"kind": "decision", "ids": ids},
        )
        assert r.status_code == 200
        assert r.json()["data"]["ignored"] >= 1

        r = _api(
            f"/api/v1/agents/{env['ask_agent']}/runs",
            params={"page_size": 50},
        )
        assert r.status_code == 200
        rows = r.json()["data"]
        assert rows
        # The run that produced the decisions now reads as success.
        assert all(row["status"] != "pending_decisions" for row in rows)
