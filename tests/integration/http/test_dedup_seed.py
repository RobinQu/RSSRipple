"""Seed duplicate metadata rows for the post-suite dedup-script run.

The daily metadata-dedup job (app.services.metadata_dedup) has no HTTP
trigger; instead the coverage-report service runs
``python -m app.scripts.dedup_metadata`` under coverage against the test
database after the suite. For that run to exercise the real merge paths
(not just the no-op scans), this suite seeds:

  - two TVSeries rows sharing a normalized title (series-table cluster),
    one referenced by a FileResource (via manual link) and an AgentWork;
  - a Movie row sharing titles with that series (cross-type pair).

The merge itself runs after the suite — these tests only assert the seed.
"""

from __future__ import annotations

from uuid import uuid4

from tests.integration.http._http import (
    RICH_FIELD_MAPPING,
    TEST_SERVER,
    _api,
    _poll_fetch,
    associate_metadata_request,
)

# This test intentionally leaves seed rows for the post-suite dedup job.  Keep
# each run isolated so a rerun against a preserved integration database cannot
# resolve the metadata link through an earlier run's external identity.
_RUN_ID = uuid4().hex[:10]
DUP_TITLE_CN = f"去重重复剧-{_RUN_ID}"
DUP_EXTERNAL_ID = f"dedup-ext-{_RUN_ID}"
FEED_URL = f"{TEST_SERVER}/rss/mikanani?series=3"


class TestDedupSeed:
    def test_seed_duplicate_series_movie_and_refs(self):
        # Series A + duplicate series B (no uniqueness constraint by design)
        r = _api(
            "/api/v1/series",
            method="post",
            json={
                "title_cn": DUP_TITLE_CN,
                "title_en": "Dedup Dup Series",
                "external_id": DUP_EXTERNAL_ID,
                "external_source": "tmdb",
            },
        )
        assert r.status_code == 201, f"series A failed: {r.text}"
        series_a = r.json()["data"]["id"]

        r = _api(
            "/api/v1/series",
            method="post",
            json={"title_cn": DUP_TITLE_CN, "aliases": ["Dedup Alias"]},
        )
        assert r.status_code == 201, f"series B failed: {r.text}"
        series_b = r.json()["data"]["id"]
        assert series_a != series_b

        # Cross-type pair: a Movie with the same titles, plus a duplicate
        # Movie pair to exercise the movie-table merge as well.
        for _ in range(2):
            r = _api(
                "/api/v1/movies",
                method="post",
                json={"title_cn": DUP_TITLE_CN, "title_en": "Dedup Dup Series"},
            )
            assert r.status_code == 201, f"movie failed: {r.text}"

        # A fetched channel resource linked to series A (episode-bearing, so
        # the cross-type merge keeps the series side).
        r = _api(
            "/api/v1/channels",
            method="post",
            json={
                "name": "Dedup Seed Channel",
                "url": FEED_URL,
                "field_mapping": RICH_FIELD_MAPPING,
                "fetch_interval": 3600,
                "metadata_agent_enabled": False,
            },
        )
        assert r.status_code == 201, f"channel failed: {r.text}"
        ch_id = r.json()["data"]["id"]

        _api(f"/api/v1/channels/{ch_id}/fetch", method="post")
        result = _poll_fetch(ch_id, accept_failed=True)
        assert result.get("status") == "done", f"fetch failed: {result}"
        r = _api(f"/api/v1/channels/{ch_id}/resources", params={"page_size": 1})
        rid = r.json()["data"][0]["id"]

        r = associate_metadata_request(
            f"/api/v1/resources/{rid}/metadata/link",
            method="put",
            json={
                "selected_result": {
                    "content_type": "tv",
                    "title_cn": DUP_TITLE_CN,
                    "title_en": "Dedup Dup Series",
                    "external_id": DUP_EXTERNAL_ID,
                    "external_source": "tmdb",
                }
            },
        )
        assert r.status_code == 200, f"link failed: {r.text}"
        assert r.json()["data"]["series_id"] == series_a

        # AgentWork referencing the duplicate (loser) series B
        r = _api("/api/v1/downloaders", params={"page_size": 100})
        dl_id = next(
            (d["id"] for d in r.json()["data"] if d.get("type") == "mock"), None
        )
        if not dl_id:
            r = _api(
                "/api/v1/downloaders",
                method="post",
                json={"name": "Dedup Seed Mock", "type": "mock"},
            )
            assert r.status_code == 201
            dl_id = r.json()["data"]["id"]
        r = _api(
            "/api/v1/agents",
            method="post",
            json={
                "name": "Dedup Seed Agent",
                "channel_id": ch_id,
                "downloader_id": dl_id,
                "scope_channel_wide": False,
                "llm_enabled": False,
                "works": [{"content_type": "tv", "series_id": series_b}],
            },
        )
        assert r.status_code == 201, f"agent failed: {r.text}"

        # Sanity: both duplicates + the movies are listable pre-merge
        r = _api("/api/v1/series", params={"page_size": 100, "title": DUP_TITLE_CN})
        dup_series = [s for s in r.json()["data"] if s.get("title_cn") == DUP_TITLE_CN]
        assert len(dup_series) == 2
        r = _api("/api/v1/movies", params={"page_size": 100, "title": DUP_TITLE_CN})
        dup_movies = [m for m in r.json()["data"] if m.get("title_cn") == DUP_TITLE_CN]
        assert len(dup_movies) == 2
