"""P2 integration tests: wikipedia content attach (judge), episode_list
cache round-trip, seasons override + Episode upsert (metadata_service)."""

from __future__ import annotations

import pathlib
import uuid
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import app.services.metadata_wiki_judge as judge
from app.models.episode import Episode
from app.models.series import TVSeries
from app.services import metadata_service as ms
from app.services.metadata_resource_meta import ResourceMetadata

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "wikipedia"
ZH_WT = (FIXTURES / "zh_100gf.wikitext").read_text(encoding="utf-8")
JA_WT = (FIXTURES / "ja_mushoku.wikitext").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# _attach_wikipedia_content (judge-side merge)
# ---------------------------------------------------------------------------


async def test_attach_merges_seasons_and_episode_list(monkeypatch):
    monkeypatch.setattr(
        judge, "fetch_wikipedia_wikitext", AsyncMock(return_value=ZH_WT)
    )
    me = {"external_id": "wikipedia:1"}
    await judge._attach_wikipedia_content(me, {"title": "超超超超超喜歡你的100個女朋友", "lang": "zh"})
    # Episode list knows about season 3 (infobox lags) -> 3 seasons win.
    assert me["number_of_seasons"] == 3
    assert me["number_of_episodes"] == 30
    assert [s["season_number"] for s in me["seasons"]] == [1, 2, 3]
    assert len(me["episode_list"]) == 30
    ep1 = me["episode_list"][0]
    assert ep1["season"] == 1 and ep1["episode"] == 1
    assert ep1["air_date"] == "2023-10-08"


async def test_attach_prefers_infobox_when_episode_list_has_fewer_seasons(monkeypatch):
    # ja mushoku: infobox has 2 seasons, episode list 3 (incl. ongoing S3).
    monkeypatch.setattr(
        judge, "fetch_wikipedia_wikitext", AsyncMock(return_value=JA_WT)
    )
    me = {}
    await judge._attach_wikipedia_content(me, {"title": "無職転生", "lang": "ja"})
    assert me["number_of_seasons"] == 3
    s1 = next(s for s in me["seasons"] if s["season_number"] == 1)
    assert s1["episode_count"] == 22  # episode-list count (list had more seasons)


async def test_attach_langlink_retry_on_parse_failure(monkeypatch):
    calls = []

    async def fake_fetch(title, lang):
        calls.append((title, lang))
        return None if lang == "zh" else JA_WT

    monkeypatch.setattr(judge, "fetch_wikipedia_wikitext", fake_fetch)
    me = {}
    page = {"title": "无职转生", "lang": "zh", "langlinks": {"ja": "無職転生"}}
    await judge._attach_wikipedia_content(me, page)
    assert calls == [("无职转生", "zh"), ("無職転生", "ja")]
    assert me["number_of_seasons"] == 3
    assert len(me["episode_list"]) == 53


async def test_attach_total_failure_leaves_entity_untouched(monkeypatch):
    monkeypatch.setattr(
        judge, "fetch_wikipedia_wikitext", AsyncMock(return_value="== 概要 ==\n本文のみ")
    )
    me = {"external_id": "wikipedia:9"}
    await judge._attach_wikipedia_content(me, {"title": "X", "lang": "zh"})
    assert me == {"external_id": "wikipedia:9"}


async def test_attach_overlays_infobox_air_dates(monkeypatch):
    """Infobox broadcast dates (播放開始/播放結束) overlay onto the seasons
    entries as air_date/end_date and seed the entity start_date."""
    wt = ("{{Infobox animanga/TVAnime\n"
          "| 集數 = 第一季：全14話 <br />第二季：全12話\n"
          "| 播放開始 = 第一季：2019年10月2日－12月25日<br />第二季：2020年4月5日－6月21日\n"
          "}}")
    monkeypatch.setattr(
        judge, "fetch_wikipedia_wikitext", AsyncMock(return_value=wt)
    )
    me = {}
    await judge._attach_wikipedia_content(me, {"title": "小書痴的下剋上", "lang": "zh"})
    assert me["seasons"] == [
        {"season_number": 1, "episode_count": 14, "air_date": "2019-10-02", "end_date": "2019-12-25"},
        {"season_number": 2, "episode_count": 12, "air_date": "2020-04-05", "end_date": "2020-06-21"},
    ]
    assert me["start_date"] == "2019-10-02"


# ---------------------------------------------------------------------------
# ResourceMetadata cache round-trip keeps episode_list
# ---------------------------------------------------------------------------


def test_resource_metadata_roundtrip_keeps_episode_list():
    episode_list = [{"season": 1, "episode": 1, "title": "甲", "subtitle": None, "air_date": "2024-01-01"}]
    finalize = {
        "found": True,
        "clean_title": "作品",
        "content_type": "tv",
        "matched_entity": {
            "external_id": "wikipedia:1",
            "seasons": [{"season_number": 1, "episode_count": 1}],
            "episode_list": episode_list,
        },
    }
    meta = ResourceMetadata.from_dict(finalize)
    assert meta.matched_entity["episode_list"] == episode_list
    # Simulate the MetadataCache JSON round-trip (metadata_repository._set_cache
    # stores matched_entity wholesale, _get_cache rebuilds via from_dict).
    import json

    cached = json.loads(json.dumps({"matched_entity": meta.matched_entity, "clean_title": "作品"}))
    meta2 = ResourceMetadata.from_dict(cached)
    assert meta2.matched_entity["episode_list"] == episode_list


# ---------------------------------------------------------------------------
# fetch_wikipedia_wikitext
# ---------------------------------------------------------------------------


async def test_fetch_wikitext_empty_input():
    from app.services.metadata_wikipedia_client import fetch_wikipedia_wikitext

    assert await fetch_wikipedia_wikitext("") is None


async def test_fetch_wikitext_url_and_params(monkeypatch):
    import httpx

    captured = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"parse": {"wikitext": "WT"}}

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    from app.services.metadata_wikipedia_client import fetch_wikipedia_wikitext

    out = await fetch_wikipedia_wikitext(
        "https://zh.wikipedia.org/wiki/%E9%BB%83%E6%B3%89%E4%BD%BF%E8%80%85", "zh"
    )
    assert out == "WT"
    assert captured["url"] == "https://zh.wikipedia.org/w/api.php"
    assert captured["params"]["action"] == "parse"
    assert captured["params"]["prop"] == "wikitext"
    assert captured["params"]["page"] == "黃泉使者"


# ---------------------------------------------------------------------------
# metadata_service: seasons override + Episode upsert
# ---------------------------------------------------------------------------


def _wiki_entity(**over):
    base = {
        "external_id": "wikipedia:123",
        "external_source": "wikipedia",
        "title_cn": "測試作品",
        "content_type": "tv",
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _no_poster_download(monkeypatch):
    monkeypatch.setattr(
        ms, "download_and_cache_poster", AsyncMock(return_value=None)
    )


async def _episode_rows(db, series_id):
    res = await db.execute(select(Episode).where(Episode.series_id == series_id))
    return res.scalars().all()


async def test_series_upsert_populates_and_updates_episodes(db_session):
    ep_list = [
        {"season": 1, "episode": 1, "title": "甲", "subtitle": "A", "air_date": "2024-01-01"},
        {"season": 1, "episode": 2, "title": "乙", "subtitle": "B", "air_date": "2024-01-08"},
    ]
    s = await ms.create_or_update_series_from_external(
        db_session, _wiki_entity(
            seasons=[{"season_number": 1, "episode_count": 2}],
            number_of_seasons=1,
            episode_list=ep_list,
        )
    )
    rows = await _episode_rows(db_session, s.id)
    assert len(rows) == 2
    assert rows[0].title == "甲"
    assert str(rows[0].air_date) == "2024-01-01"
    # Per-season model: the inert-orphan columns are never written.
    assert s.number_of_seasons is None
    assert s.seasons is None
    assert s.season_number == 1

    # Next refresh: the same work converges via the collection bag; season-1
    # episode rows update in place (no duplicates), and the season-2 entries
    # of the series-level entity do NOT land on the season-1 work.
    ep_list_v2 = [
        {"season": 1, "episode": 1, "title": "甲改", "subtitle": "A", "air_date": "2024-01-02"},
        {"season": 1, "episode": 2, "title": "乙", "subtitle": "B", "air_date": "2024-01-08"},
        {"season": 2, "episode": 1, "title": "丙", "subtitle": "C", "air_date": "2025-01-05"},
    ]
    s2 = await ms.create_or_update_series_from_external(
        db_session, _wiki_entity(
            seasons=[
                {"season_number": 1, "episode_count": 2},
                {"season_number": 2, "episode_count": 1},
            ],
            number_of_seasons=2,
            episode_list=ep_list_v2,
        )
    )
    assert s2.id == s.id
    assert s2.number_of_seasons is None
    assert s2.seasons is None
    rows = await _episode_rows(db_session, s.id)
    assert len(rows) == 2
    by_key = {(r.season, r.episode): r for r in rows}
    assert by_key[(1, 1)].title == "甲改"
    assert str(by_key[(1, 1)].air_date) == "2024-01-02"


async def test_upsert_episodes_idempotent(db_session):
    s = await ms.create_or_update_series_from_external(db_session, _wiki_entity())
    ep_list = [{"season": 1, "episode": 1, "title": "甲", "air_date": "2024-01-01"}]
    n1 = await ms.upsert_episodes(db_session, s, ep_list)
    n2 = await ms.upsert_episodes(db_session, s, ep_list)
    assert (n1, n2) == (1, 1)
    assert len(await _episode_rows(db_session, s.id)) == 1


async def test_upsert_episodes_skips_incomplete_entries(db_session):
    s = await ms.create_or_update_series_from_external(db_session, _wiki_entity())
    n = await ms.upsert_episodes(db_session, s, [
        {"season": None, "episode": 1},
        {"season": 1, "episode": None},
        {},
    ])
    assert n == 0
    assert await _episode_rows(db_session, s.id) == []


# ---------------------------------------------------------------------------
# Anti-regression guard (wikipedia seasons override) — RETIRED (P3): the
# ``seasons``/``number_of_seasons`` columns are inert orphans in the
# per-season work model, so ``seasons_overwrite_allowed`` and its override
# semantics no longer exist. The script-level guard lives inlined in
# ``scripts/wikipedia_seasons_eval.py`` and is covered below.
# ---------------------------------------------------------------------------


async def test_apply_report_guard_skip(db_session):
    """Script-level: a guard-flagged report writes nothing."""
    from scripts.wikipedia_seasons_eval import apply_report

    # A legacy unsplit row (the script's only remaining audience).
    s = TVSeries(
        id=str(uuid.uuid4()), title_cn="測試作品", content_type="tv",
        external_id="wikipedia:123", external_source="wikipedia",
        seasons=[{"season_number": n, "episode_count": 12} for n in (1, 2, 3, 4)],
        number_of_seasons=4,
    )
    db_session.add(s)
    await db_session.flush()
    rep = {
        "ok": True,
        "guard_skip": True,
        "seasons": [{"season_number": 1, "episode_count": 51}],
        "episodes": [{"season": 1, "episode": 1, "title": "甲", "air_date": None}],
    }
    n = await apply_report(db_session, s.id, rep)
    assert n == 0
    assert s.number_of_seasons == 4
    assert await _episode_rows(db_session, s.id) == []
