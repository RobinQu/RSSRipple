"""Tests for the Bangumi metadata source + post-link is_anime classification.

Covers ``bangumi_client`` (HTTP shape, episode pagination),
``metadata_bangumi`` (auto-link / judge flow, episode_list mapping, the
never-set-seasons invariant), and the post-link classification in
``metadata_service`` (channel default flag → True; Bangumi layer-1 verify).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.models.series import TVSeries
from app.services import metadata_bangumi as mb
from app.services import metadata_service as ms
from app.services.bangumi_client import (
    SUBJECT_TYPE_ANIME,
    get_subject_episodes,
    search_subjects,
)


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# bangumi_client — request shape + episode pagination
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    """Records calls; serves queued JSON payloads for get/post."""

    def __init__(self, posts=None, gets=None):
        self._posts = list(posts or [])
        self._gets = list(gets or [])
        self.post_calls = []
        self.get_calls = []

    async def post(self, url, json=None, headers=None, **kw):
        self.post_calls.append((url, json, headers))
        return _FakeResponse(self._posts.pop(0))

    async def get(self, url, params=None, headers=None, **kw):
        self.get_calls.append((url, params, headers))
        return _FakeResponse(self._gets.pop(0))


async def test_search_subjects_anime_only_filter_and_auth(monkeypatch):
    from app.services import runtime_config as _rc
    monkeypatch.setitem(_rc._overrides, "bangumi_api_key", "tok123")
    client = _FakeClient(posts=[{"data": [{"id": 1}], "total": 1}])
    out = await search_subjects(client, "芙莉莲", limit=5, anime_only=True)
    url, body, headers = client.post_calls[0]
    assert url.endswith("/v0/search/subjects")
    assert body == {"keyword": "芙莉莲", "limit": 5, "filter": {"type": [SUBJECT_TYPE_ANIME]}}
    assert headers["Authorization"] == "Bearer tok123"
    assert headers["User-Agent"] == "robinqu/RSSRipple"
    assert out == [{"id": 1}]


async def test_get_subject_episodes_paginates():
    page1 = {"data": [{"id": i, "sort": i} for i in range(1, 101)], "total": 101}
    page2 = {"data": [{"id": 101, "sort": 101}], "total": 101}
    client = _FakeClient(gets=[page1, page2])
    eps = await get_subject_episodes(client, 400602)
    assert len(eps) == 101
    assert client.get_calls[0][1]["offset"] == 0
    assert client.get_calls[1][1]["offset"] == 100
    assert client.get_calls[0][1]["type"] == 0  # main story only


# ---------------------------------------------------------------------------
# metadata_bangumi — pure helpers
# ---------------------------------------------------------------------------


def test_episode_list_from_skips_fractional_and_dedups():
    episodes = [
        {"sort": 1, "name": "ep1", "name_cn": "第一集"},
        {"sort": 2.0, "name": "ep2"},
        {"sort": 10.5, "name": "special"},   # fractional special — skipped
        {"sort": 0, "name": "op"},           # non-positive — skipped
        {"sort": 2, "name": "dup"},          # duplicate — skipped
    ]
    out = mb._episode_list_from(episodes, season=3)
    assert out == [
        {"season": 3, "episode": 1, "title": "第一集"},
        {"season": 3, "episode": 2, "title": "ep2"},
    ]


def test_autolink_subject_requires_unique_match():
    cands = [
        {"id": 1, "name": "無職転生", "name_cn": "无职转生", "date": "2021-01-10"},
        {"id": 2, "name": "無職転生 II", "name_cn": "无职转生 II", "date": "2023-07-02"},
    ]
    assert mb._autolink_subject(cands, ["无职转生"], None)["id"] == 1
    # Both candidates matching the query set → ambiguous, no auto-link.
    assert mb._autolink_subject(cands, ["无职转生", "无职转生 II"], None) is None
    # Year guard rejects the only title-equal candidate.
    assert mb._autolink_subject(cands, ["无职转生"], 2024) is None


# ---------------------------------------------------------------------------
# metadata_bangumi — full flow (network + LLM mocked)
# ---------------------------------------------------------------------------


def _frieren_subject():
    return {
        "id": 400602, "name": "葬送のフリーレン", "name_cn": "葬送的芙莉莲",
        "type": 2, "date": "2023-09-29", "platform": "TV",
        "summary": "魔法使芙莉莲……", "eps": 28,
        "images": {"large": "https://lain.bgm.tv/l/400602.jpg"},
        "rating": {"score": 8.5},
        "tags": [{"name": "奇幻"}, {"name": "治愈"}],
    }


async def test_run_bangumi_autolink_builds_entity():
    subject = _frieren_subject()
    resource = SimpleNamespace(
        search_title="葬送的芙莉莲", title_cn=None, title_en=None,
        season=None, episode=5, title_year=2023,
    )
    with (
        patch.object(mb, "search_subjects", AsyncMock(return_value=[subject])),
        patch.object(mb, "get_subject", AsyncMock(return_value=subject)),
        patch.object(
            mb, "get_subject_episodes",
            AsyncMock(return_value=[{"sort": i, "name_cn": f"第{i}话"} for i in range(1, 29)]),
        ),
        patch.object(mb, "bangumi_configured", return_value=True),
    ):
        finalize, info = await mb.run_bangumi_search_then_judge(
            AsyncMock(), "[G] 葬送的芙莉莲 - 05 [1080p]", resource=resource
        )
    assert info["method"] == "bangumi_search_then_autolink"
    assert finalize["found"] is True
    assert finalize["content_type"] == "tv"
    me = finalize["matched_entity"]
    assert me["external_id"] == "bangumi:400602"
    assert me["external_source"] == "bangumi"
    assert me["is_anime"] is True
    assert me["title_cn"] == "葬送的芙莉莲"
    assert me["rating"] == 8.5
    assert me["number_of_episodes"] == 28
    assert len(me["episode_list"]) == 28
    assert me["episode_list"][0] == {"season": 1, "episode": 1, "title": "第1话"}
    # A Bangumi subject is ONE season — never claim work-level season counts.
    assert "seasons" not in me
    assert "number_of_seasons" not in me


async def test_run_bangumi_movie_platform_maps_to_movie():
    subject = {**_frieren_subject(), "id": 999, "platform": "剧场版"}
    with (
        patch.object(mb, "search_subjects", AsyncMock(return_value=[subject])),
        patch.object(mb, "get_subject", AsyncMock(return_value=subject)),
        patch.object(mb, "get_subject_episodes", AsyncMock(return_value=[])),
        patch.object(mb, "bangumi_configured", return_value=True),
    ):
        finalize, _ = await mb.run_bangumi_search_then_judge(
            AsyncMock(), "葬送的芙莉莲", resource=None
        )
    assert finalize["content_type"] == "movie"
    assert finalize["matched_entity"]["episode_list"] is None


async def test_run_bangumi_not_configured_is_transient():
    with patch.object(mb, "bangumi_configured", return_value=False):
        finalize, info = await mb.run_bangumi_search_then_judge(
            AsyncMock(), "whatever", resource=None
        )
    assert finalize["found"] is False
    assert "api key not configured" in info["error"]  # transient marker


async def test_run_bangumi_judge_path_picks_subject():
    # Two title-equal candidates (duplicate-title subjects happen) → no
    # deterministic auto-link, the LLM judge decides.
    s1 = _frieren_subject()
    s2 = {**_frieren_subject(), "id": 111}
    judge_json = (
        '{"found": true, "clean_title": "葬送的芙莉莲", "content_type": "tv",'
        ' "inferred_season": 2, "matched_entity": {"external_id": "bangumi:111",'
        ' "external_source": "bangumi"}, "reason": "S2"}'
    )
    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=SimpleNamespace(content=judge_json))
    resource = SimpleNamespace(
        search_title="葬送的芙莉莲", title_cn=None, title_en=None,
        season=2, episode=1, title_year=None,
    )
    with (
        patch.object(mb, "search_subjects", AsyncMock(return_value=[s1, s2])),
        patch.object(mb, "get_subject", AsyncMock(return_value=s2)),
        patch.object(
            mb, "get_subject_episodes", AsyncMock(return_value=[{"sort": 1, "name": "x"}])
        ),
        patch.object(mb, "bangumi_configured", return_value=True),
    ):
        finalize, info = await mb.run_bangumi_search_then_judge(
            model, "[G] 葬送的芙莉莲 S02 - 01", resource=resource
        )
    assert info["method"] == "bangumi_search_then_judge"
    me = finalize["matched_entity"]
    assert me["external_id"] == "bangumi:111"
    # Season tag comes from the resource's parsed season marker.
    assert me["episode_list"][0]["season"] == 2
    assert finalize["inferred_season"] == 2


# ---------------------------------------------------------------------------
# Post-link classification — channel default flag + Bangumi layer-1 verify
# ---------------------------------------------------------------------------


async def test_classify_post_link_channel_default_marks_anime(db_session):
    channel = SimpleNamespace(default_is_anime=True)
    series = TVSeries(id=_uuid(), title_cn="某剧", content_type="tv")
    db_session.add(series)
    await db_session.flush()
    resource = SimpleNamespace(series_id=series.id, movie_id=None)
    await ms.classify_is_anime_post_link(db_session, channel, resource)
    assert series.is_anime is True


async def test_classify_post_link_bangumi_verify_marks_anime(db_session):
    channel = SimpleNamespace(default_is_anime=False)
    series = TVSeries(
        id=_uuid(), title_cn="葬送的芙莉莲", original_title="葬送のフリーレン",
        content_type="tv", is_anime=None,
    )
    db_session.add(series)
    await db_session.flush()
    resource = SimpleNamespace(series_id=series.id, movie_id=None)
    with (
        patch.dict("app.services.runtime_config._overrides", {"bangumi_api_key": "tok"}),
        patch(
            "app.services.bangumi_client.search_subjects",
            AsyncMock(return_value=[_frieren_subject()]),
        ),
    ):
        await ms.classify_is_anime_post_link(db_session, channel, resource)
    assert series.is_anime is True


async def test_classify_post_link_bangumi_live_action_marks_false(db_session):
    channel = SimpleNamespace(default_is_anime=False)
    series = TVSeries(
        id=_uuid(), title_cn="理智与情感", content_type="tv", is_anime=None,
    )
    db_session.add(series)
    await db_session.flush()
    resource = SimpleNamespace(series_id=series.id, movie_id=None)
    hit = {"id": 493301, "name": "理智与情感", "name_cn": "理智与情感",
           "type": 6, "date": "1995-12-13"}
    with (
        patch.dict("app.services.runtime_config._overrides", {"bangumi_api_key": "tok"}),
        patch(
            "app.services.bangumi_client.search_subjects", AsyncMock(return_value=[hit])
        ),
    ):
        await ms.classify_is_anime_post_link(db_session, channel, resource)
    assert series.is_anime is False


async def test_classify_post_link_skips_determined_and_unconfigured(db_session):
    channel = SimpleNamespace(default_is_anime=False)
    series = TVSeries(
        id=_uuid(), title_cn="已定", content_type="tv", is_anime=False
    )
    db_session.add(series)
    await db_session.flush()
    resource = SimpleNamespace(series_id=series.id, movie_id=None)
    searcher = AsyncMock()
    with (
        patch.dict("app.services.runtime_config._overrides", {"bangumi_api_key": "tok"}),
        patch("app.services.bangumi_client.search_subjects", searcher),
    ):
        await ms.classify_is_anime_post_link(db_session, channel, resource)
    searcher.assert_not_called()  # already determined → no network call
    assert series.is_anime is False


# ---------------------------------------------------------------------------
# Source catalog
# ---------------------------------------------------------------------------


def test_bangumi_in_channel_source_catalog(monkeypatch):
    from app.services.metadata_sources import (
        SUPPORTED_CHANNEL_METADATA_SOURCES,
        get_metadata_source_catalog,
        normalize_channel_metadata_source,
    )
    assert "bangumi" in SUPPORTED_CHANNEL_METADATA_SOURCES
    assert normalize_channel_metadata_source("bangumi") == "bangumi"
    from app.services import runtime_config as _rc
    monkeypatch.setitem(_rc._overrides, "bangumi_api_key", "")
    cat = {s["value"]: s for s in get_metadata_source_catalog(channel_only=True)}
    assert cat["bangumi"]["available"] is False
    monkeypatch.setitem(_rc._overrides, "bangumi_api_key", "tok")
    cat = {s["value"]: s for s in get_metadata_source_catalog(channel_only=True)}
    assert cat["bangumi"]["available"] is True
