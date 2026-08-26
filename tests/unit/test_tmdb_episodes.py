"""P4 tests: TMDB web-fallback wiring, fetch_tmdb_episode_list, tmdb
episode_list -> Episode upsert, and backfill selection logic.

No network: httpx / web_fallback_judge / the LLM model are mocked.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

import app.services.metadata_agent as ma
from app.models.episode import Episode
from app.models.series import TVSeries
from app.services import metadata_service as ms
from app.services.metadata_source_io import (
    TMDB_EPISODE_FETCH_MAX_SEASONS,
    fetch_tmdb_episode_list,
)

# ---------------------------------------------------------------------------
# fetch_tmdb_episode_list (mocked httpx)
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, payload=None, status_exc=None):
        self._payload = payload
        self._status_exc = status_exc

    def raise_for_status(self):
        if self._status_exc:
            raise self._status_exc

    def json(self):
        return self._payload


class _FakeClient:
    """Routes URL -> _Resp; records requested URLs."""

    def __init__(self, routes):
        self._routes = routes
        self.requested: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None):
        self.requested.append(url)
        for suffix, resp in self._routes.items():
            if url.endswith(suffix):
                return resp
        return _Resp(status_exc=RuntimeError(f"no route for {url}"))


def _patch_tmdb(monkeypatch, routes):
    import httpx

    import app.services.runtime_config as rc

    client = _FakeClient(routes)
    # fetch_tmdb_episode_list imports httpx lazily inside the function, so
    # patch the attribute on the httpx module itself.
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    monkeypatch.setitem(rc._overrides, "tmdb_api_key", "test-key")
    return client


def _season_payload(*episodes):
    return {"episodes": [
        {"episode_number": n, "name": name, "air_date": date}
        for n, name, date in episodes
    ]}


async def test_fetch_tmdb_episode_list_multi_season(monkeypatch):
    client = _patch_tmdb(monkeypatch, {
        "/tv/85937/season/1": _Resp(_season_payload((1, "開始", "2023-04-09"), (2, "続き", "2023-04-16"))),
        "/tv/85937/season/2": _Resp(_season_payload((1, "再開", "2024-01-07"))),
    })
    out = await fetch_tmdb_episode_list("85937", [
        {"season_number": 0, "episode_count": 2},  # specials - skipped
        {"season_number": 1, "episode_count": 2},
        {"season_number": 2, "episode_count": 1},
    ])
    assert out == [
        {"season": 1, "episode": 1, "title": "開始", "air_date": "2023-04-09"},
        {"season": 1, "episode": 2, "title": "続き", "air_date": "2023-04-16"},
        {"season": 2, "episode": 1, "title": "再開", "air_date": "2024-01-07"},
    ]
    # Season 0 never requested; accepts a canonical "tmdb:"-less bare id.
    assert not any(u.endswith("/season/0") for u in client.requested)


async def test_fetch_tmdb_episode_list_accepts_canonical_id_and_tolerates_failure(monkeypatch):
    client = _patch_tmdb(monkeypatch, {
        "/tv/55/season/1": _Resp(_season_payload((1, "甲", None))),
        "/tv/55/season/2": _Resp(status_exc=RuntimeError("boom")),
    })
    out = await fetch_tmdb_episode_list("tmdb:55", [
        {"season_number": 1, "episode_count": 1},
        {"season_number": 2, "episode_count": 1},
    ])
    # Season 2 failed -> its episodes omitted, season 1 still returned.
    assert out == [{"season": 1, "episode": 1, "title": "甲", "air_date": None}]
    assert len(client.requested) == 2


async def test_fetch_tmdb_episode_list_all_failed_returns_none(monkeypatch):
    _patch_tmdb(monkeypatch, {
        "/tv/9/season/1": _Resp(status_exc=RuntimeError("down")),
    })
    assert await fetch_tmdb_episode_list("9", [{"season_number": 1, "episode_count": 3}]) is None


async def test_fetch_tmdb_episode_list_season_cap(monkeypatch):
    client = _patch_tmdb(monkeypatch, {})
    seasons = [{"season_number": n, "episode_count": 10} for n in range(1, TMDB_EPISODE_FETCH_MAX_SEASONS + 2)]
    assert await fetch_tmdb_episode_list("1", seasons) is None
    assert client.requested == []  # cap short-circuits before any HTTP


async def test_fetch_tmdb_episode_list_requires_api_key(monkeypatch):
    import app.services.runtime_config as rc

    monkeypatch.setitem(rc._overrides, "tmdb_api_key", "")
    assert await fetch_tmdb_episode_list("1", [{"season_number": 1, "episode_count": 1}]) is None


async def test_fetch_tmdb_episode_list_skips_bad_episode_entries(monkeypatch):
    _patch_tmdb(monkeypatch, {
        "/tv/7/season/1": _Resp({"episodes": [
            {"episode_number": 1, "name": "ok", "air_date": "2024-01-01"},
            {"episode_number": None, "name": "skip"},
            {"name": "skip too"},
        ]}),
    })
    out = await fetch_tmdb_episode_list("7", [{"season_number": 1, "episode_count": 3}])
    assert [e["episode"] for e in out] == [1]


# ---------------------------------------------------------------------------
# _attach_tmdb_episode_list (agent-side merge)
# ---------------------------------------------------------------------------


async def test_attach_tmdb_episode_list_merges(monkeypatch):
    fetch = AsyncMock(return_value=[{"season": 1, "episode": 1, "title": "甲", "air_date": None}])
    monkeypatch.setattr(ma, "fetch_tmdb_episode_list", fetch)
    finalize = {
        "found": True, "content_type": "tv",
        "matched_entity": {
            "external_id": "tmdb:85937", "external_source": "tmdb",
            "seasons": [{"season_number": 1, "episode_count": 12}],
        },
    }
    await ma._attach_tmdb_episode_list(finalize)
    fetch.assert_awaited_once_with("85937", finalize["matched_entity"]["seasons"])
    assert len(finalize["matched_entity"]["episode_list"]) == 1


@pytest.mark.parametrize("finalize", [
    # not found / movie -> never fires
    {"found": False, "content_type": "tv", "matched_entity": {"external_id": "tmdb:1", "seasons": [{}]}},
    {"found": True, "content_type": "movie", "matched_entity": {"external_id": "tmdb:1", "seasons": [{}]}},
    # non-tmdb identity (wikipedia) -> single-source rule
    {"found": True, "content_type": "tv", "matched_entity": {
        "external_id": "wikipedia:123", "external_source": "wikipedia",
        "seasons": [{"season_number": 1, "episode_count": 1}]}},
    # tmdb id but no seasons -> nothing to iterate
    {"found": True, "content_type": "tv", "matched_entity": {"external_id": "tmdb:1"}},
    # episode_list already present -> do not refetch
    {"found": True, "content_type": "tv", "matched_entity": {
        "external_id": "tmdb:1", "seasons": [{"season_number": 1, "episode_count": 1}],
        "episode_list": [{"season": 1, "episode": 1}]}},
])
async def test_attach_tmdb_episode_list_skips(monkeypatch, finalize):
    fetch = AsyncMock()
    monkeypatch.setattr(ma, "fetch_tmdb_episode_list", fetch)
    await ma._attach_tmdb_episode_list(dict(finalize))
    fetch.assert_not_awaited()


async def test_attach_tmdb_episode_list_empty_fetch_leaves_entity(monkeypatch):
    monkeypatch.setattr(ma, "fetch_tmdb_episode_list", AsyncMock(return_value=None))
    me = {"external_id": "tmdb:1", "seasons": [{"season_number": 1, "episode_count": 1}]}
    finalize = {"found": True, "content_type": "tv", "matched_entity": me}
    await ma._attach_tmdb_episode_list(finalize)
    assert "episode_list" not in finalize["matched_entity"]


# ---------------------------------------------------------------------------
# Agent wiring: TMDB ReAct found=False -> web fallback
# ---------------------------------------------------------------------------


def _patched_agent():
    agent = ma.UnifiedMetadataAgent()
    agent._get_cache = AsyncMock(return_value=None)
    agent._set_cache = AsyncMock()
    agent._apply_to_resource = AsyncMock()
    agent._run_react = AsyncMock()
    agent._find_known_work = AsyncMock(return_value=None)
    agent._build_production_message = MagicMock(return_value="msg")
    return agent


def _ns_resource():
    return SimpleNamespace(
        title_raw="[G] Show - 01", series_id=None, movie_id=None,
        metadata_attempts=0, last_metadata_attempt_at=None, metadata_failure_type=None,
    )


def _react_not_found():
    return (
        {"found": False, "clean_title": "Show", "content_type": "tv",
         "reason": "No matching work found in TMDB"},
        {"method": "tmdb", "data_sources_used": ["tmdb"],
         "source_errors": {"tmdb": "no results"}, "error": "TMDB: no results"},
    )


_FB_FOUND = (
    {"found": True, "content_type": "tv",
     "matched_entity": {"external_id": "bangumi:9", "external_source": "bangumi"}},
    {"method": "search_then_web_fallback", "data_sources_used": ["wigolo"],
     "source_errors": {}, "error": None},
)


async def test_tmdb_not_found_invokes_web_fallback_with_channel_whitelist(monkeypatch):
    monkeypatch.setattr(ma, "fetch_tmdb_episode_list", AsyncMock(return_value=None))
    fb = AsyncMock(return_value=_FB_FOUND)
    monkeypatch.setattr(ma, "web_fallback_judge", fb)
    agent = _patched_agent()
    agent._run_react.return_value = _react_not_found()
    channel = SimpleNamespace(
        id="ch", metadata_source="tmdb", name="c",
        metadata_fallback_sources=["bangumi", "tmdb"],
    )
    resource = _ns_resource()
    meta = await agent.process(resource, channel, MagicMock())
    fb.assert_awaited_once()
    assert fb.await_args.kwargs["fallback_sources"] == ["bangumi", "tmdb"]
    assert fb.await_args.kwargs["resource"] is resource
    assert meta.found is True
    assert meta.matched_entity["external_id"] == "bangumi:9"
    assert meta.search_method == "react_then_web_fallback"
    assert set(meta.data_sources_used) == {"tmdb", "wigolo"}


async def test_tmdb_found_skips_web_fallback(monkeypatch):
    fetch = AsyncMock(return_value=[{"season": 1, "episode": 1, "title": "甲", "air_date": None}])
    monkeypatch.setattr(ma, "fetch_tmdb_episode_list", fetch)
    fb = AsyncMock(return_value=_FB_FOUND)
    monkeypatch.setattr(ma, "web_fallback_judge", fb)
    agent = _patched_agent()
    agent._run_react.return_value = (
        {"found": True, "clean_title": "Show", "content_type": "tv",
         "matched_entity": {
             "external_id": "tmdb:55", "external_source": "tmdb",
             "seasons": [{"season_number": 1, "episode_count": 1}]}},
        {"method": "tmdb", "data_sources_used": ["tmdb"], "source_errors": {}, "error": None},
    )
    channel = SimpleNamespace(id="ch", metadata_source="tmdb", name="c", metadata_fallback_sources=None)
    meta = await agent.process(_ns_resource(), channel, MagicMock())
    fb.assert_not_awaited()
    assert meta.found is True
    # Symmetric episode fill fired on the direct tmdb hit.
    fetch.assert_awaited_once()
    assert meta.matched_entity["episode_list"][0]["title"] == "甲"


async def test_tmdb_transient_react_outcome_skips_fallback(monkeypatch):
    fb = AsyncMock(return_value=_FB_FOUND)
    monkeypatch.setattr(ma, "web_fallback_judge", fb)
    agent = _patched_agent()
    agent._run_react.return_value = (
        {"found": False, "clean_title": "", "content_type": "tv",
         "reason": "Agent error: Request timed out."},
        {"method": None, "data_sources_used": ["tmdb"], "source_errors": {},
         "error": "Request timed out."},
    )
    channel = SimpleNamespace(id="ch", metadata_source="tmdb", name="c", metadata_fallback_sources=None)
    resource = _ns_resource()
    meta = await agent.process(resource, channel, MagicMock())
    fb.assert_not_awaited()
    assert resource.metadata_failure_type == "transient"
    agent._set_cache.assert_not_called()
    assert meta.found is False


async def test_tmdb_fallback_disabled_keeps_react_not_found(monkeypatch):
    fb = AsyncMock(return_value=None)  # fallback disabled / empty whitelist
    monkeypatch.setattr(ma, "web_fallback_judge", fb)
    agent = _patched_agent()
    agent._run_react.return_value = _react_not_found()
    channel = SimpleNamespace(id="ch", metadata_source="tmdb", name="c", metadata_fallback_sources=[])
    meta = await agent.process(_ns_resource(), channel, MagicMock())
    fb.assert_awaited_once()
    assert fb.await_args.kwargs["fallback_sources"] == []
    assert meta.found is False
    assert meta.reason == "No matching work found in TMDB"
    assert meta.search_method == "tmdb"  # original search_info untouched


async def test_tmdb_fallback_failure_is_transient(monkeypatch):
    fb = AsyncMock(return_value=(
        {"found": False, "clean_title": "Show", "content_type": "tv",
         "reason": "web search failed: RuntimeError: 429"},
        {"method": "search_then_web_fallback", "data_sources_used": ["wigolo"],
         "source_errors": {"wigolo": "web search failed: RuntimeError: 429"},
         "error": "web search failed: RuntimeError: 429"},
    ))
    monkeypatch.setattr(ma, "web_fallback_judge", fb)
    agent = _patched_agent()
    agent._run_react.return_value = _react_not_found()
    channel = SimpleNamespace(id="ch", metadata_source="tmdb", name="c", metadata_fallback_sources=None)
    resource = _ns_resource()
    await agent.process(resource, channel, MagicMock())
    assert resource.metadata_failure_type == "transient"
    agent._set_cache.assert_not_called()  # transient never cached


async def test_tmdb_title_only_uses_default_fallback_order(monkeypatch):
    monkeypatch.setattr(ma, "fetch_tmdb_episode_list", AsyncMock(return_value=None))
    fb = AsyncMock(return_value=_FB_FOUND)
    monkeypatch.setattr(ma, "web_fallback_judge", fb)
    with patch.dict("app.services.runtime_config._overrides", {"llm_api_key": "k"}):
        agent = ma.UnifiedMetadataAgent()
        agent._run_react = AsyncMock(return_value=_react_not_found())
        meta = await agent.process_title_only("Show - 01", "tmdb")
    fb.assert_awaited_once()
    # No channel in title-only mode -> default order (parity with wikipedia).
    assert fb.await_args.kwargs["fallback_sources"] is None
    assert fb.await_args.kwargs["resource"] is None
    assert meta.found is True


async def test_non_tmdb_sources_do_not_use_fallback(monkeypatch):
    fb = AsyncMock(return_value=_FB_FOUND)
    monkeypatch.setattr(ma, "web_fallback_judge", fb)
    agent = _patched_agent()
    agent._run_react.return_value = _react_not_found()
    channel = SimpleNamespace(id="ch", metadata_source="jina", name="c", metadata_fallback_sources=None)
    await agent.process(_ns_resource(), channel, MagicMock())
    fb.assert_not_awaited()


# ---------------------------------------------------------------------------
# tmdb episode_list -> Episode rows (metadata_service integration)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_poster_download(monkeypatch):
    monkeypatch.setattr(ms, "download_and_cache_poster", AsyncMock(return_value=None))


async def _episode_rows(db, series_id):
    res = await db.execute(select(Episode).where(Episode.series_id == series_id))
    return res.scalars().all()


async def test_tmdb_series_upsert_populates_episodes(db_session):
    entity = {
        "external_id": "tmdb:85937",
        "external_source": "tmdb",
        "title_cn": "測試番組",
        "content_type": "tv",
        "seasons": [{"season_number": 1, "episode_count": 2}],
        "number_of_seasons": 1,
        "episode_list": [
            {"season": 1, "episode": 1, "title": "開始", "air_date": "2023-04-09"},
            {"season": 1, "episode": 2, "title": "続き", "air_date": None},
        ],
    }
    s = await ms.create_or_update_series_from_external(db_session, entity)
    rows = await _episode_rows(db_session, s.id)
    assert len(rows) == 2
    assert rows[0].title == "開始"
    assert str(rows[0].air_date) == "2023-04-09"
    assert rows[1].air_date is None


# ---------------------------------------------------------------------------
# Backfill selection logic
# ---------------------------------------------------------------------------


async def _mk_series(db, **kw) -> TVSeries:
    s = TVSeries(**kw)
    db.add(s)
    await db.flush()
    return s


async def test_select_tmdb_series(db_session):
    from scripts.tmdb_episodes_backfill import select_tmdb_series, tmdb_id_of

    a = await _mk_series(db_session, title_cn="A", external_id="tmdb:1", external_source="tmdb")
    await _mk_series(db_session, title_cn="B", external_id="wikipedia:2", external_source="wikipedia")
    await _mk_series(db_session, title_cn="C", external_id=None)
    rows = await select_tmdb_series(db_session)
    assert [r.id for r in rows] == [a.id]
    assert tmdb_id_of(a) == "1"
    assert tmdb_id_of(TVSeries(external_id="wikipedia:2")) is None
    assert tmdb_id_of(TVSeries(external_id=None)) is None


async def test_select_wikipedia_series(db_session):
    from scripts.wikipedia_seasons_eval import select_wikipedia_series

    a = await _mk_series(db_session, title_cn="A", external_id="wikipedia:1")
    b = await _mk_series(db_session, title_cn="B", wikipedia_url="https://zh.wikipedia.org/wiki/X")
    await _mk_series(db_session, title_cn="C", external_id="tmdb:3")
    rows = await select_wikipedia_series(db_session)
    assert {r.id for r in rows} == {a.id, b.id}


async def test_wikipedia_apply_report_writes_seasons_and_episodes(db_session):
    from scripts.wikipedia_seasons_eval import apply_report

    s = await _mk_series(db_session, title_cn="W", external_id="wikipedia:9")
    rep = {
        "ok": True,
        "seasons": [
            {"season_number": 1, "episode_count": 2},
            {"season_number": 2, "episode_count": 1},
        ],
        "episodes": [
            {"season": 1, "episode": 1, "title": "甲", "air_date": "2024-01-01"},
            {"season": 2, "episode": 1, "title": "乙", "air_date": None},
        ],
    }
    n = await apply_report(db_session, s.id, rep)
    assert n == 2
    # Override semantics: seasons fields replaced from the parse.
    assert s.number_of_seasons == 2
    assert s.number_of_episodes == 3
    assert [x["season_number"] for x in s.seasons] == [1, 2]
    rows = await _episode_rows(db_session, s.id)
    assert {(r.season, r.episode) for r in rows} == {(1, 1), (2, 1)}
