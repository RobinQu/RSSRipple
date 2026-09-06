"""Branch coverage for app.services.metadata_bangumi.

All Bangumi HTTP (search/details/episodes) is mocked at the module boundary
(``mb.search_subjects`` / ``mb.get_subject`` / ``mb.get_subject_episodes`` /
``mb.bangumi_configured``); the LLM judge runs against a fake model object.
Covers the deterministic auto-link (incl. the season-aware variant and the
year guard), episode-list mapping edges, matched-entity degradation, the
judge call, and every early-exit of ``run_bangumi_search_then_judge``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import app.services.metadata_bangumi as mb


def _subject(sid: int, name: str = "ぼっち・ざ・ろっく!", name_cn: str = "孤独摇滚！",
             date: str = "2022-10-08", platform: str = "TV", **extra) -> dict:
    return {
        "id": sid, "name": name, "name_cn": name_cn, "date": date,
        "platform": platform, "summary": "summary " * 60,
        "tags": [{"name": "校园"}, {"name": "音乐"}, {}],
        "images": {"large": "https://img.example/l.jpg", "common": "https://img.example/c.jpg"},
        "rating": {"score": 8.4},
        "eps": 12,
        **extra,
    }


def _episodes() -> list[dict]:
    return [
        {"sort": 2, "name": "ep2", "name_cn": "第二集"},
        {"sort": 1, "name": "ep1", "name_cn": ""},
        {"sort": None, "name": "no-sort"},
        {"sort": 1.5, "name": "in-between special"},
        {"sort": 0, "name": "zero"},
        {"sort": 2, "name": "dup"},
    ]


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(mb, "bangumi_configured", lambda: True)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestPureHelpers:
    def test_bangumi_queries_dedupes_case_insensitively(self):
        queries = mb._bangumi_queries("TestShow / testshow", None)
        assert [q.lower() for q in queries] == sorted({q.lower() for q in queries})
        assert all(isinstance(q, str) and q for q in queries)

    def test_subject_names_drops_empty(self):
        names = mb._subject_names({"name": "abc", "name_cn": None})
        assert names == {"abc"}

    def test_subject_season_single_marker(self):
        assert mb._subject_season({"name": "战国妖狐 第二季", "name_cn": None}) == 2

    def test_subject_season_conflicting_markers_yield_none(self):
        subj = {"name": "Show Season 2", "name_cn": "Show 第三季"}
        assert mb._subject_season(subj) is None

    def test_subject_season_no_marker(self):
        assert mb._subject_season({"name": "孤独摇滚", "name_cn": None}) is None

    def test_episode_list_from_skips_non_main_story(self):
        eps = mb._episode_list_from(_episodes(), season=3)
        assert [e["episode"] for e in eps] == [1, 2]
        assert all(e["season"] == 3 for e in eps)
        # name_cn preferred, falls back to name
        assert eps[0]["title"] == "ep1"
        assert eps[1]["title"] == "第二集"

    def test_build_evidence_text_renders_candidates(self):
        text = mb._build_evidence_text([_subject(1), _subject(2)])
        assert "[1] id=1" in text and "[2] id=2" in text
        assert "tags=校园, 音乐" in text
        assert "孤独摇滚" in text

    def test_build_evidence_text_caps_at_max_candidates(self):
        text = mb._build_evidence_text([_subject(i) for i in range(20)])
        assert f"[{mb._MAX_CANDIDATES}]" in text
        assert f"[{mb._MAX_CANDIDATES + 1}]" not in text


class TestAutolinkSubject:
    def test_unique_normalized_match_autolinks(self):
        cand = _subject(1)
        picked = mb._autolink_subject([cand], ["孤独摇滚！"], 2022)
        assert picked is cand

    def test_two_matching_candidates_do_not_autolink(self):
        cands = [_subject(1), _subject(2)]
        assert mb._autolink_subject(cands, ["孤独摇滚！"], None) is None

    def test_year_guard_rejects_far_off_candidate(self):
        cand = _subject(1, date="2010-01-01")
        assert mb._autolink_subject([cand], ["孤独摇滚！"], 2024) is None

    def test_year_guard_rejects_missing_date(self):
        cand = _subject(1, date=None)
        assert mb._autolink_subject([cand], ["孤独摇滚！"], 2024) is None

    def test_no_title_year_disables_guard(self):
        cand = _subject(1, date=None)
        assert mb._autolink_subject([cand], ["孤独摇滚！"], None) is cand

    def test_season_marked_resource_never_autolinks_base_entry(self):
        base = _subject(1, name_cn="战国妖狐")
        picked = mb._autolink_subject([base], ["战国妖狐"], None, resource_season=2)
        assert picked is None

    def test_season_marked_resource_autolinks_same_season_entry(self):
        base = _subject(1, name_cn="战国妖狐")
        s2 = _subject(2, name_cn="战国妖狐 第二季")
        picked = mb._autolink_subject([base, s2], ["战国妖狐"], None, resource_season=2)
        assert picked is s2

    def test_season_marked_autolink_still_year_guarded(self):
        s2 = _subject(2, name_cn="战国妖狐 第二季", date="2000-01-01")
        assert mb._autolink_subject([s2], ["战国妖狐"], 2024, resource_season=2) is None


# ---------------------------------------------------------------------------
# _build_matched_entity
# ---------------------------------------------------------------------------


class TestBuildMatchedEntity:
    async def test_full_expansion_tv(self, monkeypatch):
        async def _detail(client, sid):
            return {"summary": "long summary", "date": "2022-10-08",
                    "rating": {"score": 8.6}}

        async def _eps(client, sid):
            return _episodes()

        monkeypatch.setattr(mb, "get_subject", _detail)
        monkeypatch.setattr(mb, "get_subject_episodes", _eps)
        entity = await mb._build_matched_entity(None, _subject(7), season=1)
        assert entity["external_id"] == "bangumi:7"
        assert entity["external_source"] == "bangumi"
        assert entity["is_anime"] is True
        assert entity["_content_type"] == "tv"
        assert entity["single_season_entry"] is True
        assert entity["description"] == "long summary"
        assert entity["rating"] == 8.6
        assert [e["episode"] for e in entity["episode_list"]] == [1, 2]
        assert entity["poster_url"] == "https://img.example/l.jpg"
        assert entity["genre"] == ["校园", "音乐"]

    async def test_movie_platform_has_no_single_season_marker(self, monkeypatch):
        async def _detail(client, sid):
            raise RuntimeError("detail down")

        async def _eps(client, sid):
            raise RuntimeError("episodes down")

        monkeypatch.setattr(mb, "get_subject", _detail)
        monkeypatch.setattr(mb, "get_subject_episodes", _eps)
        entity = await mb._build_matched_entity(
            None, _subject(9, platform="剧场版"), season=1
        )
        # Detail/episode failures degrade to the search hit's own fields.
        assert entity["_content_type"] == "movie"
        assert "single_season_entry" not in entity
        assert entity["episode_list"] is None
        assert entity["number_of_episodes"] == 12


# ---------------------------------------------------------------------------
# _judge
# ---------------------------------------------------------------------------


class _FakeModel:
    def __init__(self, content=None, raises: bool = False):
        self._content = content
        self._raises = raises
        self.messages = None

    async def ainvoke(self, messages):
        self.messages = messages
        if self._raises:
            raise RuntimeError("llm endpoint down")
        return SimpleNamespace(content=self._content)


class TestJudge:
    async def test_parses_finalize_json(self):
        payload = {"found": True, "matched_entity": {"external_id": "bangumi:3"}}
        model = _FakeModel(content=json.dumps(payload))
        out = await mb._judge(model, "raw title", [_subject(3)], None)
        assert out == payload
        # System prompt + user message with the candidate evidence.
        assert len(model.messages) == 2
        assert "id=3" in model.messages[1].content

    async def test_list_content_is_joined(self):
        payload = {"found": False}
        model = _FakeModel(content=[SimpleNamespace(text=json.dumps(payload))])
        out = await mb._judge(model, "raw", [_subject(1)], None)
        assert out == payload

    async def test_llm_failure_returns_none(self):
        assert await mb._judge(_FakeModel(raises=True), "raw", [_subject(1)], None) is None

    async def test_unparseable_returns_none(self):
        assert await mb._judge(_FakeModel(content="no json here"), "raw", [_subject(1)], None) is None

    async def test_resource_hints_included(self):
        model = _FakeModel(content='{"found": false}')
        resource = SimpleNamespace(title_cn="孤独摇滚", title_en="Bocchi", episode=1, season=1)
        await mb._judge(model, "raw", [_subject(1)], resource)
        assert "title_cn='孤独摇滚'" in model.messages[1].content


# ---------------------------------------------------------------------------
# run_bangumi_search_then_judge
# ---------------------------------------------------------------------------


def _patch_search(monkeypatch, per_query, *, detail=None, episodes=None):
    async def _search(client, q, limit=5, anime_only=False):
        res = per_query(q)
        if isinstance(res, Exception):
            raise res
        return res

    async def _detail(client, sid):
        return detail if detail is not None else {}

    async def _eps(client, sid):
        return episodes if episodes is not None else []

    monkeypatch.setattr(mb, "search_subjects", _search)
    monkeypatch.setattr(mb, "get_subject", _detail)
    monkeypatch.setattr(mb, "get_subject_episodes", _eps)


class TestRunSearchThenJudge:
    async def test_not_configured_misses(self, monkeypatch):
        monkeypatch.setattr(mb, "bangumi_configured", lambda: False)
        finalize, info = await mb.run_bangumi_search_then_judge(None, "孤独摇滚！")
        assert finalize["found"] is False
        assert "not configured" in finalize["reason"]
        assert info["error"] == "Bangumi: api key not configured"

    async def test_no_usable_query_misses(self, configured):
        finalize, info = await mb.run_bangumi_search_then_judge(None, "   ")
        assert finalize["found"] is False
        assert finalize["reason"] == "no usable query"

    async def test_search_errors_surface_in_search_info(self, configured, monkeypatch):
        _patch_search(monkeypatch, lambda q: TimeoutError("bangumi timeout"))
        finalize, info = await mb.run_bangumi_search_then_judge(None, "孤独摇滚！")
        assert finalize["found"] is False
        assert finalize["reason"] == "no credible match on Bangumi"
        assert "TimeoutError" in info["source_errors"]["bangumi"]
        assert info["error"].startswith("Bangumi: TimeoutError")

    async def test_autolink_happy_path(self, configured, monkeypatch):
        _patch_search(
            monkeypatch,
            lambda q: [_subject(7)],
            detail={"summary": "detail summary"},
            episodes=_episodes(),
        )
        finalize, info = await mb.run_bangumi_search_then_judge(None, "孤独摇滚！")
        assert finalize["found"] is True
        assert finalize["content_type"] == "tv"
        assert finalize["confidence"] == 0.9
        assert "auto-linked" in finalize["reason"]
        assert finalize["matched_entity"]["external_id"] == "bangumi:7"
        assert finalize["matched_entity"]["single_season_entry"] is True
        assert info["method"] == "bangumi_search_then_autolink"

    async def test_autolink_dedups_candidates_across_queries(self, configured, monkeypatch):
        # Both query variants return the same subject id — merged once.
        _patch_search(monkeypatch, lambda q: [_subject(7), _subject(7)])
        finalize, _ = await mb.run_bangumi_search_then_judge(None, "孤独摇滚！")
        assert finalize["found"] is True
        assert finalize["confidence"] == 0.9

    async def test_season_aware_autolink_via_run(self, configured, monkeypatch):
        base = _subject(1, name="戦国妖狐", name_cn="战国妖狐", date="2024-01-01")
        s2 = _subject(2, name="戦国妖狐 第2期", name_cn="战国妖狐 第二季", date="2024-07-01")
        _patch_search(monkeypatch, lambda q: [base, s2])
        resource = SimpleNamespace(
            season=2, title_year=2024, search_title="战国妖狐 第二季",
            title_cn=None, title_en=None, episode=1,
        )
        finalize, info = await mb.run_bangumi_search_then_judge(
            None, "战国妖狐 第二季", resource
        )
        assert finalize["found"] is True
        assert finalize["matched_entity"]["external_id"] == "bangumi:2"
        assert info["method"] == "bangumi_search_then_autolink"

    async def test_judge_pick_expands_subject(self, configured, monkeypatch):
        # Two identical-name candidates defeat auto-link; the judge picks id 2.
        _patch_search(monkeypatch, lambda q: [_subject(1), _subject(2)])
        model = _FakeModel(content=json.dumps({
            "found": True,
            "clean_title": "孤独摇滚",
            "matched_entity": {"external_id": "bangumi:2"},
            "inferred_episode": 5,
        }))
        finalize, info = await mb.run_bangumi_search_then_judge(model, "孤独摇滚！")
        assert finalize["found"] is True
        assert finalize["confidence"] == 0.8
        assert finalize["reason"] == "bangumi judge pick"
        assert finalize["clean_title"] == "孤独摇滚"
        assert finalize["inferred_episode"] == 5
        assert finalize["matched_entity"]["external_id"] == "bangumi:2"
        assert info["method"] == "bangumi_search_then_judge"

    async def test_judge_not_found(self, configured, monkeypatch):
        _patch_search(monkeypatch, lambda q: [_subject(1), _subject(2)])
        model = _FakeModel(content=json.dumps({"found": False, "reason": "no match"}))
        finalize, info = await mb.run_bangumi_search_then_judge(model, "孤独摇滚！")
        assert finalize["found"] is False
        assert finalize["clean_title"] == "孤独摇滚！"  # defaults filled
        assert finalize["content_type"] == "tv"
        assert info["method"] == "bangumi_search_then_judge"

    async def test_judge_unknown_subject_misses(self, configured, monkeypatch):
        _patch_search(monkeypatch, lambda q: [_subject(1), _subject(2)])
        model = _FakeModel(content=json.dumps({
            "found": True, "matched_entity": {"external_id": "bangumi:999"},
        }))
        finalize, _ = await mb.run_bangumi_search_then_judge(model, "孤独摇滚！")
        assert finalize["found"] is False
        assert finalize["reason"] == "bangumi judge picked an unknown subject"

    async def test_judge_unparseable_misses(self, configured, monkeypatch):
        _patch_search(monkeypatch, lambda q: [_subject(1), _subject(2)])
        model = _FakeModel(content="garbage")
        finalize, info = await mb.run_bangumi_search_then_judge(model, "孤独摇滚！")
        assert finalize["found"] is False
        assert finalize["reason"] == "bangumi judge returned unparseable JSON"
        assert info["error"] == "Bangumi: judge call failed"

    async def test_movie_subject_run(self, configured, monkeypatch):
        movie = _subject(5, name="ぼっち・ざ・ろっく! Re:", name_cn="孤独摇滚！",
                         platform="剧场版")
        _patch_search(monkeypatch, lambda q: [movie])
        finalize, _ = await mb.run_bangumi_search_then_judge(None, "孤独摇滚！")
        assert finalize["found"] is True
        assert finalize["content_type"] == "movie"
        assert "single_season_entry" not in finalize["matched_entity"]
