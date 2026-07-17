"""Unit tests for source isolation in UnifiedMetadataAgent."""

from app.services.metadata_agent import (
    SUPPORTED_METADATA_SOURCES,
    ResourceMetadata,
    UnifiedMetadataAgent,
    _seasons_map_from,
    normalize_metadata_source_type,
    reconcile_episode,
)


def _tool_names(agent: UnifiedMetadataAgent, source: str) -> set[str]:
    return {tool.name for tool in agent._tools_for_source(source)}


def test_metadata_source_normalization_maps_legacy_combined_to_exa():
    assert normalize_metadata_source_type(None) == "exa"
    assert normalize_metadata_source_type("combined") == "exa"
    assert normalize_metadata_source_type("TMDB") == "tmdb"
    assert normalize_metadata_source_type("wikipedia") == "wikipedia"
    assert normalize_metadata_source_type("unknown") == "exa"


def test_tools_are_restricted_to_selected_source():
    agent = UnifiedMetadataAgent()

    assert _tool_names(agent, "tmdb") == {
        "search_tmdb",
        "get_tmdb_details",
        "finalize",
    }
    assert _tool_names(agent, "exa") == {
        "search_exa_agent",
        "finalize",
    }
    assert _tool_names(agent, "wikipedia") == {
        "search_wikipedia",
        "get_wikipedia_page",
        "finalize",
    }


def test_jina_source_is_supported_and_normalized():
    assert "jina" in SUPPORTED_METADATA_SOURCES
    assert normalize_metadata_source_type("jina") == "jina"
    assert normalize_metadata_source_type("JINA") == "jina"


def test_tools_are_restricted_to_jina_source():
    agent = UnifiedMetadataAgent()
    assert _tool_names(agent, "jina") == {
        "search_jina",
        "read_jina_url",
        "finalize",
    }


def test_resource_metadata_parses_batch_fields():
    meta = ResourceMetadata.from_dict({
        "clean_title": "Witch Hat Atelier",
        "content_type": "tv",
        "found": True,
        "inferred_season": 1,
        "is_batch": True,
        "inferred_episode_start": 1,
        "inferred_episode_end": 13,
    })
    assert meta.is_batch is True
    assert meta.episode_start == 1
    assert meta.episode_end == 13


def test_resource_metadata_defaults_are_non_batch():
    meta = ResourceMetadata.from_dict({
        "clean_title": "Show",
        "content_type": "tv",
        "found": True,
        "inferred_episode": 5,
    })
    assert meta.is_batch is False
    assert meta.episode_start is None
    assert meta.episode_end is None


def test_resource_metadata_parses_subtitle_langs():
    meta = ResourceMetadata.from_dict({
        "clean_title": "Show",
        "content_type": "tv",
        "found": True,
        "subtitle_langs": ["zh-CN", "zh-TW"],
    })
    assert meta.subtitle_langs == ["zh-CN", "zh-TW"]


def test_resource_metadata_subtitle_langs_absent_is_none():
    """None means 'LLM had nothing to say' — pre-parser output should stand."""
    meta = ResourceMetadata.from_dict({
        "clean_title": "Show",
        "content_type": "tv",
        "found": True,
    })
    assert meta.subtitle_langs is None


# ---------------------------------------------------------------------------
# Episode reconciliation (P2)
# ---------------------------------------------------------------------------


def test_seasons_map_extracts_valid_entries_only():
    entity = {
        "seasons": [
            {"season_number": 1, "episode_count": 24},
            {"season_number": 2, "episode_count": 24},
            {"season_number": 0, "episode_count": 3},   # specials → skip
            {"season_number": 3},                          # no count → skip
            {"episode_count": 12},                          # no season → skip
            "not a dict",
        ],
    }
    assert _seasons_map_from(entity) == {1: 24, 2: 24}


def test_seasons_map_from_none_returns_empty():
    assert _seasons_map_from(None) == {}
    assert _seasons_map_from({}) == {}
    assert _seasons_map_from({"seasons": None}) == {}


class TestReconcileEpisode:
    def test_raw_within_bounds_stays_raw(self):
        # S1E5 with a 12-episode season — keep the raw number.
        r = reconcile_episode(raw_episode=5, raw_season=1, seasons_map={1: 12})
        assert r == (5, None, "raw")

    def test_raw_within_tolerance_stays_raw(self):
        # TMDB reports 12 episodes but show already aired 13; +2 tolerance
        # keeps it a "raw" call rather than triggering absolute conversion.
        r = reconcile_episode(raw_episode=13, raw_season=1, seasons_map={1: 12})
        assert r == (13, None, "raw")

    def test_absolute_converts_to_per_season(self):
        # Slime S4: absolute 84 → per-season 13 given prev seasons total 71.
        # seasons_map for S1..S4 with cumulative-71 before S4.
        seasons = {1: 24, 2: 24, 3: 23, 4: 13}
        r = reconcile_episode(raw_episode=84, raw_season=4, seasons_map=seasons)
        assert r == (13, 84, "reconciled")

    def test_absolute_last_episode(self):
        # Slime S4 finale — absolute 85, per-season 12 (if season size = 13,
        # 85-71 = 14 which is 1 over → clamps to 12 via tolerance envelope).
        # We accept anything in [1, season_count + tolerance].
        seasons = {1: 24, 2: 24, 3: 23, 4: 13}
        result = reconcile_episode(raw_episode=85, raw_season=4, seasons_map=seasons)
        assert result is not None
        ep, abs_ep, conf = result
        assert conf == "reconciled"
        assert abs_ep == 85
        # Within the season's episode range
        assert 1 <= ep <= 13

    def test_no_seasons_map_returns_none(self):
        assert reconcile_episode(raw_episode=84, raw_season=4, seasons_map={}) is None

    def test_unknown_season_returns_none(self):
        # We don't have data for season 5; can't reason about it.
        assert reconcile_episode(
            raw_episode=99, raw_season=5, seasons_map={1: 24, 2: 24}
        ) is None

    def test_conversion_out_of_range_is_ambiguous(self):
        # raw 200 with seasons_map summing to ~85 → converted candidate
        # 200-71=129 which is way outside season 4's 13 episodes. Flag.
        seasons = {1: 24, 2: 24, 3: 23, 4: 13}
        r = reconcile_episode(raw_episode=200, raw_season=4, seasons_map=seasons)
        assert r == (200, None, "ambiguous")

    def test_season_1_no_prev_total_but_raw_too_big(self):
        # If raw > season_count and it's season 1, nothing to subtract →
        # ambiguous rather than a wild conversion.
        r = reconcile_episode(raw_episode=99, raw_season=1, seasons_map={1: 12})
        assert r == (99, None, "ambiguous")


# ---------------------------------------------------------------------------
# Jina source — _extract_search_info wiring
# ---------------------------------------------------------------------------


def _ai_message(tool_name: str, args: dict):
    from langchain_core.messages import AIMessage

    return AIMessage(
        content="",
        tool_calls=[{"name": tool_name, "args": args, "id": tool_name}],
    )


def _tool_message(name: str, content: dict, call_id: str):
    import json as _json

    from langchain_core.messages import ToolMessage

    return ToolMessage(content=_json.dumps(content), name=name, tool_call_id=call_id)


def test_extract_search_info_picks_up_jina_tool_calls():
    messages = [
        _ai_message("search_jina", {"query": "Breaking Bad"}),
        _tool_message(
            "search_jina",
            {"success": True, "data": [{"title": "Breaking Bad", "url": "https://en.wikipedia.org/wiki/Breaking_Bad"}]},
            "search_jina",
        ),
        _ai_message("read_jina_url", {"url": "https://www.imdb.com/title/tt0903747/"}),
        _tool_message(
            "read_jina_url",
            {"success": True, "data": {"title": "Breaking Bad", "url": "https://www.imdb.com/title/tt0903747/", "content": "..."}},
            "read_jina_url",
        ),
    ]
    info = UnifiedMetadataAgent._extract_search_info(messages)
    assert "jina" in info["data_sources_used"]
    assert info["method"] == "jina"
    assert info["source_errors"] == {}


def test_extract_search_info_tracks_jina_errors():
    messages = [
        _ai_message("search_jina", {"query": "Unknown Show"}),
        _tool_message(
            "search_jina",
            {"success": False, "data": [], "error": "JINA_API_KEY not configured"},
            "search_jina",
        ),
    ]
    info = UnifiedMetadataAgent._extract_search_info(messages)
    assert "jina" in info["data_sources_used"]
    assert info["source_errors"]["jina"] == "JINA_API_KEY not configured"
    assert info["error"] and "Jina" in info["error"]


def test_extract_search_info_tracks_jina_empty_results():
    messages = [
        _ai_message("search_jina", {"query": "No Match"}),
        _tool_message("search_jina", {"success": True, "data": []}, "search_jina"),
    ]
    info = UnifiedMetadataAgent._extract_search_info(messages)
    assert info["source_errors"]["jina"] == "no results"
    assert info["error"] and "Jina" in info["error"]


# ---------------------------------------------------------------------------
# Failure classification + retry-state recording + cache/force_refresh behavior
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock, MagicMock  # noqa: E402

from app.services.metadata_agent import (  # noqa: E402
    _classify_failure,
    _record_metadata_attempt,
)


def _meta(found=True, reason=None, search_error=None, matched_entity=None):
    return ResourceMetadata(
        clean_title="X", content_type="tv", found=found,
        reason=reason, search_error=search_error, matched_entity=matched_entity,
    )


def test_classify_failure_success_returns_none():
    # A real success has a matched_entity to link.
    assert _classify_failure(_meta(found=True, matched_entity={"external_id": "x"})) is None


def test_classify_failure_found_true_no_entity_is_transient():
    """found=True with no matched_entity is an LLM finalization gap, not a
    success - it must retry (transient) instead of being cached as a fake
    'match' that leaves the resource permanently unparsed."""
    assert _classify_failure(_meta(found=True)) == "transient"
    assert _classify_failure(_meta(found=True, matched_entity={})) == "transient"


def test_classify_failure_agent_error_recursion_is_transient():
    """Agent-level exceptions wrapped by _run_react as 'Agent error: ...'
    (recursion limit, LLM 4xx/5xx) are infra failures, not a definitive
    no-match - they must retry, never be cached as permanent not_found."""
    for reason in (
        "Agent error: Recursion limit of 25 reached without hitting a stop condition.",
        "Agent error: Error code: 400 - {'error': {'message': 'Error from provider'}}",
        "Agent error: https://en.wikipedia.org/w/api.php",
        "Agent error: Error code: 500 - Internal Server Error",
    ):
        assert _classify_failure(_meta(found=False, reason=reason)) == "transient", reason


def test_classify_failure_transient_markers():
    for reason in (
        "Agent error: Request timed out.",
        "Agent error: Connection error.",
        "Agent did not call finalize",
        "Agent error: Error code: 403 - AccountOverdueError",
    ):
        assert _classify_failure(_meta(found=False, reason=reason)) == "transient", reason
    # search_error is also consulted
    assert _classify_failure(_meta(found=False, search_error="Request timed out.")) == "transient"


def test_classify_failure_http_source_errors_are_transient():
    """Billing/rate/auth/server errors from the external source are infra
    failures, not a definitive 'no match' - they must retry and must not be
    cached as permanent not_found."""
    for err in (
        "Jina: 402 Payment Required",
        "Client error '402 Payment Required' for url 'https://s.jina.ai/'",
        "Exa: 429 Too Many Requests",
        "Jina: 401 Unauthorized",
        "Exa: 502 Bad Gateway",
        "Jina: 503 Service Unavailable",
    ):
        assert _classify_failure(_meta(found=False, search_error=err)) == "transient", err
    # A real "no match" reason (no HTTP error in reason/search_error) stays not_found
    assert _classify_failure(
        _meta(found=False, reason="No matching work found in Jina search results")
    ) == "not_found"


def test_classify_failure_non_work_markers():
    for reason in (
        "The RSS entry is a music album release",
        "The RSS entry is an ASMR audio recording",
        "The provided RSS entry is for an opening theme song",
    ):
        assert _classify_failure(_meta(found=False, reason=reason)) == "non_work", reason


def test_classify_failure_not_found_default():
    assert _classify_failure(_meta(found=False, reason="No matching work found in Jina")) == "not_found"
    assert _classify_failure(_meta(found=False)) == "not_found"


def test_record_metadata_attempt_increments_and_stamps():
    res = SimpleNamespace(metadata_attempts=0, last_metadata_attempt_at=None, metadata_failure_type=None)
    _record_metadata_attempt(res, _meta(found=False, reason="Request timed out."))
    assert res.metadata_attempts == 1
    assert res.last_metadata_attempt_at is not None
    assert res.metadata_failure_type == "transient"
    # success clears the failure marker
    _record_metadata_attempt(res, _meta(found=True, matched_entity={"external_id": "x"}))
    assert res.metadata_attempts == 2
    assert res.metadata_failure_type is None


def _patched_agent():
    """A UnifiedMetadataAgent with all I/O methods stubbed, so process() can
    be exercised in isolation against the cache/force_refresh logic."""
    agent = UnifiedMetadataAgent()
    agent._get_cache = AsyncMock()
    agent._set_cache = AsyncMock()
    agent._apply_to_resource = AsyncMock()
    agent._run_react = AsyncMock()
    agent._find_known_work = AsyncMock(return_value=None)  # S1 short-circuit off in cache/agent tests
    agent._build_production_message = MagicMock(return_value="msg")
    return agent


def _ns_resource():
    return SimpleNamespace(
        title_raw="[G] Show - 01", series_id=None, movie_id=None,
        metadata_attempts=0, last_metadata_attempt_at=None, metadata_failure_type=None,
    )


def _react_return(found, reason=None):
    finalize = {"clean_title": "Show", "content_type": "tv", "found": found, "reason": reason}
    info = {"method": None, "data_sources_used": [], "source_errors": {}, "error": None}
    return finalize, info


async def test_process_uses_definitive_cache_without_refresh():
    agent = _patched_agent()
    resource = _ns_resource()
    cached = _meta(found=False, reason="No matching work found in Jina")
    agent._get_cache.return_value = cached
    res = await agent.process(resource, SimpleNamespace(id="ch", metadata_source=None), MagicMock())
    agent._run_react.assert_not_called()          # no live run
    agent._apply_to_resource.assert_called_once()  # cached applied
    assert res is cached
    assert resource.metadata_attempts == 1         # attempt recorded from cache
    assert resource.metadata_failure_type == "not_found"


async def test_process_force_refresh_bypasses_definitive_cache():
    agent = _patched_agent()
    agent._get_cache.return_value = _meta(found=False, reason="No matching work found")
    agent._run_react.return_value = _react_return(found=True)
    res = await agent.process(
        _ns_resource(), SimpleNamespace(id="ch", metadata_source=None), MagicMock(),
        force_refresh=True,
    )
    agent._get_cache.assert_not_called()  # cache read skipped
    agent._run_react.assert_called_once()  # ran live
    assert res.found is True


async def test_process_ignores_transient_cache_and_reruns():
    """A legacy cached transient failure must not short-circuit a live run."""
    agent = _patched_agent()
    agent._get_cache.return_value = _meta(found=False, reason="Agent error: Request timed out.")
    agent._run_react.return_value = _react_return(found=True)
    res = await agent.process(
        _ns_resource(), SimpleNamespace(id="ch", metadata_source=None), MagicMock(),
    )
    agent._run_react.assert_called_once()  # cache ignored → live run
    assert res.found is True


async def test_process_does_not_cache_transient_failure():
    agent = _patched_agent()
    agent._get_cache.return_value = None
    agent._run_react.return_value = _react_return(found=False, reason="Agent error: Request timed out.")
    resource = _ns_resource()
    await agent.process(resource, SimpleNamespace(id="ch", metadata_source=None), MagicMock())
    agent._set_cache.assert_not_called()  # transient never cached
    assert resource.metadata_failure_type == "transient"


async def test_process_caches_definitive_not_found():
    agent = _patched_agent()
    agent._get_cache.return_value = None
    agent._run_react.return_value = _react_return(found=False, reason="No matching work found in Jina")
    await agent.process(_ns_resource(), SimpleNamespace(id="ch", metadata_source=None), MagicMock())
    agent._set_cache.assert_called_once()  # definitive outcome cached


# ---------------------------------------------------------------------------
# S3: Wikipedia candidate queries (recall-oriented query generation)
# ---------------------------------------------------------------------------

from app.services.metadata_agent import (  # noqa: E402
    _candidate_queries,
    _clean_query,
    _detect_audio_work_type,
    _is_non_media,
    _work_name_prefix,
)


def test_clean_query_strips_season_episode_quality():
    assert _clean_query("无职转生 3期") == "无职转生"
    assert _clean_query("Mushoku Tensei S3 - 03") == "Mushoku Tensei"
    assert _clean_query("Show [01] [1080p HEVC]") == "Show"
    assert _clean_query("Movie 2024") == "Movie 2024"


def test_clean_query_strips_parens_colon_roman():
    # Parenthetical alt titles + colon description tail -> base work name.
    assert _clean_query("新世纪福音战士 (新世紀エヴァンゲリオン) (Neon Genesis Evangelion)：TV动画+剧场版") == "新世纪福音战士"
    # Colon arc/description tail.
    assert _clean_query("辉夜大小姐想让我告白：通往大人的阶梯") == "辉夜大小姐想让我告白"
    # Trailing roman-numeral season marker.
    assert _clean_query("无职转生Ⅲ") == "无职转生"


def test_is_non_media_detects_software():
    assert _is_non_media("BitComet Stable (build 2.21.6.23) 比特彗星全功能解锁豪华版") is True
    assert _is_non_media("[LoliHouse] 无职转生 3期 - 03 [1080p]") is False
    assert _is_non_media("") is False


def test_work_name_prefix_splits_at_first_season_marker():
    assert _work_name_prefix("无职转生 3期") == "无职转生"
    assert _work_name_prefix("Mushoku Tensei S3 - 03") == "Mushoku Tensei"
    assert _work_name_prefix("樱桃小丸子第二期 1538 详情") == "樱桃小丸子"
    # No marker -> no prefix variant.
    assert _work_name_prefix("黄泉使者") == ""


def test_detect_audio_work_type():
    assert _detect_audio_work_type("【ASMR】愛上火車 少女們的祕事簿♪[WAV/MP3]") == "asmr"
    assert _detect_audio_work_type("[JMAX] ウマ娘 - トレセンラーメン列伝 [FLAC 96kHz/24bit]") == "music"
    assert _detect_audio_work_type("TVアニメ「X」OPテーマ「Y」／Aimer [FLAC]") == "music"
    assert _detect_audio_work_type("某ドラマCD 第1巻 [FLAC]") == "drama_cd"
    # Normal anime episodes are NOT flagged.
    assert _detect_audio_work_type("[LoliHouse] 无职转生 3期 - 03 [WebRip 1080p HEVC AAC]") is None
    assert _detect_audio_work_type("") is None
    assert _detect_audio_work_type(None) is None


def test_candidate_queries_emits_season_stripped_variants():
    r = SimpleNamespace(title_cn=None, title_en=None, search_title=None)
    qs = _candidate_queries(
        "[LoliHouse] 无职转生 3期 / Mushoku Tensei S3 - 03 [WebRip 1080p]", r
    )
    assert ("无职转生", "zh") in qs
    assert ("Mushoku Tensei", "en") in qs


def test_candidate_queries_recovers_bracketed_work_name():
    # All-bracket title: dropping every bracket would leave nothing. The
    # work-name bracket content must be recovered as a query.
    r = SimpleNamespace(title_cn=None, title_en=None, search_title=None)
    qs = _candidate_queries(
        "[SweetSub][小書痴的下剋上 領主的養女][Honzuki no Gekokujou S04][13][WebRip][1080P]",
        r,
    )
    assert any("小書痴" in q for q, _ in qs)
    assert any("Honzuki no Gekokujou" in q for q, _ in qs)


def test_candidate_queries_splits_full_width_slash():
    r = SimpleNamespace(title_cn=None, title_en=None, search_title=None)
    qs = _candidate_queries("[整理搬运] 猫眼三姐妹／猫之眼：TV动画+剧场版", r)
    assert ("猫眼三姐妹", "zh") in qs


def test_candidate_queries_uses_search_title_hint():
    r = SimpleNamespace(title_cn=None, title_en=None, search_title="猫眼三姐妹")
    qs = _candidate_queries("[整理搬运] 猫眼三姐妹／猫之眼：TV动画+剧场版", r)
    assert ("猫眼三姐妹", "zh") in qs


def test_candidate_queries_adds_ja_for_kana_fragments():
    r = SimpleNamespace(title_cn=None, title_en=None, search_title=None)
    qs = _candidate_queries("魔法少女まどか☆マギカ / Madoka Magica - 01 [1080p]", r)
    # CJK+kana fragment should produce a ja query in addition to zh.
    langs = {lang for _, lang in qs}
    assert "ja" in langs


# ---------------------------------------------------------------------------
# S1: work-level short-circuit (no LLM when title matches a known work)
# ---------------------------------------------------------------------------


async def test_process_short_circuits_known_series(db_session, sample_channel):
    """A resource whose pre-parsed title matches a known TVSeries links
    directly with no LLM call (S1)."""
    import uuid

    from app.models.file_resource import FileResource
    from app.models.series import TVSeries

    series = TVSeries(id=str(uuid.uuid4()), title_cn="黄泉使者", title_en="Yomi no Tsugai")
    db_session.add(series)
    resource = FileResource(
        id=str(uuid.uuid4()), channel_id=sample_channel.id, guid="g1",
        title_raw="[LoliHouse] 黄泉使者 / Yomi no Tsugai - 14 [1080p]",
        title_cn="黄泉使者", episode=14, season=1,
        torrent_url="magnet:?xt=urn:btih:test1",
    )
    db_session.add(resource)
    await db_session.commit()

    agent = UnifiedMetadataAgent()
    agent._run_react = AsyncMock()  # must NOT be called - short-circuit wins
    res = await agent.process(resource, sample_channel, db_session)

    agent._run_react.assert_not_called()
    assert res.found is True
    assert res.content_type == "tv"
    assert resource.series_id == series.id
    assert resource.movie_id is None
    assert resource.metadata_matched_at is not None
    assert resource.metadata_failure_type is None  # success


async def test_process_resolves_asmr_to_audio_work(db_session, sample_channel):
    """An ASMR title is detected and resolved to an AudioWork stub (no LLM,
    no TV/movie agent run)."""
    import uuid

    from app.models.audio_work import AudioWork
    from app.models.file_resource import FileResource

    sample_channel.metadata_source = "wikipedia"
    resource = FileResource(
        id=str(uuid.uuid4()), channel_id=sample_channel.id, guid="g_asmr",
        title_raw="【ASMR】愛上火車 少女們的祕事簿♪[WAV/MP3]",
        torrent_url="magnet:?xt=urn:btih:asmr1",
    )
    db_session.add(resource)
    await db_session.commit()

    agent = UnifiedMetadataAgent()
    # No Wikipedia page for this ASMR title -> stub path.
    agent._search_audio_wikipedia = AsyncMock(return_value=None)
    agent._run_react = AsyncMock()  # must NOT be called - audio path wins

    res = await agent.process(resource, sample_channel, db_session, force_refresh=True)

    agent._run_react.assert_not_called()
    assert res.found is True
    assert res.content_type == "asmr"
    assert resource.audio_work_id is not None
    assert resource.series_id is None
    assert resource.movie_id is None
    assert resource.metadata_failure_type is None  # success
    aw = await db_session.get(AudioWork, resource.audio_work_id)
    assert aw is not None
    assert aw.content_type == "asmr"


async def test_process_short_circuit_fires_on_force_refresh(db_session, sample_channel):
    """force_refresh bypasses the *cache* but the S1 title match is live, so a
    resource matching a known series still short-circuits (no LLM). This is
    what lets the backfill (which uses force_refresh) link resources that now
    match a known work without re-running the agent."""
    import uuid

    from app.models.file_resource import FileResource
    from app.models.series import TVSeries

    series = TVSeries(id=str(uuid.uuid4()), title_cn="黄泉使者")
    db_session.add(series)
    resource = FileResource(
        id=str(uuid.uuid4()), channel_id=sample_channel.id, guid="g1",
        title_raw="[LoliHouse] 黄泉使者 - 14 [1080p]", title_cn="黄泉使者",
        torrent_url="magnet:?xt=urn:btih:test2",
    )
    db_session.add(resource)
    await db_session.commit()

    agent = UnifiedMetadataAgent()
    agent._get_cache = AsyncMock(return_value=None)
    agent._run_react = AsyncMock()  # must NOT be called - S1 short-circuits
    await agent.process(resource, sample_channel, db_session, force_refresh=True)
    agent._run_react.assert_not_called()
    assert resource.series_id == series.id  # S1 linked it even under force_refresh


async def test_process_short_circuit_matches_season_suffixed_title(db_session, sample_channel):
    """S1 strips season from both sides: a resource titled "X 第四季" matches a
    series titled "X" (base). Catches the Slime-style mismatch."""
    import uuid

    from app.models.file_resource import FileResource
    from app.models.series import TVSeries

    series = TVSeries(id=str(uuid.uuid4()), title_cn="关于我转生变成史莱姆这档事")
    db_session.add(series)
    resource = FileResource(
        id=str(uuid.uuid4()), channel_id=sample_channel.id, guid="g1",
        title_raw="[G] 关于我转生变成史莱姆这档事 第四季 - 14 [1080p]",
        title_cn="关于我转生变成史莱姆这档事 第四季",
        torrent_url="magnet:?xt=urn:btih:test3",
    )
    db_session.add(resource)
    await db_session.commit()

    agent = UnifiedMetadataAgent()
    agent._run_react = AsyncMock()
    await agent.process(resource, sample_channel, db_session)
    agent._run_react.assert_not_called()
    assert resource.series_id == series.id


# ---------------------------------------------------------------------------
# S3: search-first + single-LLM-judge routing
# ---------------------------------------------------------------------------


async def test_process_routes_wikipedia_to_search_then_judge(db_session, sample_channel):
    """S3: a wikipedia-source resource uses _run_search_then_judge, not _run_react."""
    import uuid

    from app.models.file_resource import FileResource

    sample_channel.metadata_source = "wikipedia"
    resource = FileResource(
        id=str(uuid.uuid4()), channel_id=sample_channel.id, guid="g1",
        title_raw="[G] Some New Show - 01 [1080p]", title_cn="Some New Show",
        torrent_url="magnet:?xt=urn:btih:s3route",
    )
    db_session.add(resource)
    await db_session.commit()

    agent = UnifiedMetadataAgent()
    agent._get_cache = AsyncMock(return_value=None)
    agent._find_known_work = AsyncMock(return_value=None)  # no S1 short-circuit
    agent._run_search_then_judge = AsyncMock(return_value=(
        {"found": True, "clean_title": "Some New Show", "content_type": "tv"},
        {"method": "search_then_judge", "data_sources_used": ["wikipedia"],
         "source_errors": {}, "error": None},
    ))
    agent._apply_to_resource = AsyncMock()
    agent._set_cache = AsyncMock()
    agent._run_react = AsyncMock()  # must NOT be called for wikipedia

    await agent.process(resource, sample_channel, db_session)

    agent._run_search_then_judge.assert_called_once()
    agent._run_react.assert_not_called()


# ---------------------------------------------------------------------------
# Source-scoped cache key + upsert
# ---------------------------------------------------------------------------


def test_cache_source_key_namespaces_by_source():
    from app.services.metadata_agent import _cache_source_key
    assert _cache_source_key("jina") == "metadata_agent:jina"
    assert _cache_source_key("exa") == "metadata_agent:exa"
    assert _cache_source_key("local") == "metadata_agent:local"
    # Unset source resolves to the default (exa), still its own namespace.
    assert _cache_source_key(None) == "metadata_agent:exa"


async def test_get_cache_is_source_scoped(db_session):
    """A result cached under one source must not be returned for another."""
    agent = UnifiedMetadataAgent()
    meta = ResourceMetadata(
        clean_title="Show", content_type="tv", found=False,
        reason="No matching work found in Jina",
    )
    await agent._set_cache("[G] Show - 01", "jina", meta, db_session)
    await db_session.commit()

    hit = await agent._get_cache("[G] Show - 01", "jina", db_session)
    assert hit is not None and hit.found is False

    # Different source -> miss (no cross-source poisoning)
    miss = await agent._get_cache("[G] Show - 01", "exa", db_session)
    assert miss is None


async def test_set_cache_upsert_overwrites_same_source(db_session):
    """A force_refresh re-run writes the same (title, source) again - it must
    overwrite the stale row, not violate the unique constraint."""
    from sqlalchemy import func, select

    from app.models.metadata_cache import MetadataCache

    agent = UnifiedMetadataAgent()
    m1 = ResourceMetadata(clean_title="Show", found=True, content_type="tv")
    m2 = ResourceMetadata(clean_title="Show", found=False, content_type="tv", reason="No match")
    await agent._set_cache("[G] Show - 01", "jina", m1, db_session)
    await db_session.commit()
    await agent._set_cache("[G] Show - 01", "jina", m2, db_session)  # must not raise
    await db_session.commit()

    hit = await agent._get_cache("[G] Show - 01", "jina", db_session)
    assert hit is not None and hit.found is False  # m2 overwrote m1
    count = (await db_session.execute(
        select(func.count()).select_from(MetadataCache).where(
            MetadataCache.title == "[G] Show - 01",
            MetadataCache.source == "metadata_agent:jina",
        )
    )).scalar_one()
    assert count == 1  # no duplicate rows


# ---------------------------------------------------------------------------
# Wikipedia: transient infra failures must classify as transient (retry, never
# cached as a permanent not_found). The wikipediaapi library raises typed
# WikipediaException subclasses (WikiConnectionError, WikiHttpTimeoutError,
# WikiInvalidJsonError, WikiHttpError) which _wiki_call maps to a
# "Wikipedia request failed: ..." transient error.
# ---------------------------------------------------------------------------


def test_classify_failure_wikipedia_infra_error_is_transient():
    """A wikipediaapi infra failure (connection, timeout, invalid JSON, non-200)
    is mapped to 'Wikipedia request failed: ...' - an infra failure, not a 'no
    match' - so it retries and is not cached as permanent not_found."""
    for err in (
        "Wikipedia: Wikipedia request failed: WikiConnectionError (https://zh.wikipedia.org/w/api.php)",
        "Wikipedia: Wikipedia request failed: WikiHttpTimeoutError (https://zh.wikipedia.org/w/api.php)",
        "Wikipedia: Wikipedia request failed: WikiInvalidJsonError (https://zh.wikipedia.org/w/api.php)",
        "Wikipedia: Wikipedia request failed: WikiHttpError (https://zh.wikipedia.org/w/api.php)",
    ):
        assert _classify_failure(_meta(found=False, search_error=err)) == "transient", err


def test_classify_failure_wikipedia_page_not_found_is_not_found():
    """A genuine page-not-found (page.exists() is False) is a real no-match,
    not an infra failure - it should be cached as not_found, not retried
    forever."""
    assert _classify_failure(
        _meta(found=False, search_error="Wikipedia: Page not found: Some Title")
    ) == "not_found"


def test_classify_failure_wikipedia_legit_no_results_is_not_found():
    """A successful search that simply had no hits is a definitive not_found."""
    assert _classify_failure(
        _meta(found=False, reason="No matching work found in Wikipedia")
    ) == "not_found"


def test_extract_search_info_surfaces_wikipedia_infra_error():
    """A failed search_wikipedia must set search_error (not just source_errors)
    so _classify_failure can see the transient marker. Previously the wikipedia
    branch never set search_error, so transient failures were misclassified as
    not_found and cached permanently."""
    messages = [
        _ai_message("search_wikipedia", {"query": "复制品也要谈恋爱", "lang": "zh"}),
        _tool_message(
            "search_wikipedia",
            {"success": False, "data": [], "error": "Wikipedia request failed: WikiConnectionError (https://zh.wikipedia.org/w/api.php)"},
            "search_wikipedia",
        ),
    ]
    info = UnifiedMetadataAgent._extract_search_info(messages)
    assert "wikipedia" in info["data_sources_used"]
    assert "Wikipedia request failed" in info["source_errors"]["wikipedia"]
    assert info["error"] and "Wikipedia request failed" in info["error"]
    # The surfaced search_error must classify as transient end-to-end.
    assert _classify_failure(_meta(found=False, search_error=info["error"])) == "transient"


def test_extract_search_info_wikipedia_empty_results_no_search_error():
    """A successful search returning no hits records 'no results' but must NOT
    set search_error (it is a definitive not_found, not an infra failure)."""
    messages = [
        _ai_message("search_wikipedia", {"query": "No Match"}),
        _tool_message("search_wikipedia", {"success": True, "data": []}, "search_wikipedia"),
    ]
    info = UnifiedMetadataAgent._extract_search_info(messages)
    assert info["source_errors"]["wikipedia"] == "no results"
    assert info["error"] is None  # legit empty -> not an infra failure


# ---------------------------------------------------------------------------
# _wikipedia_client construction + _wiki_call error mapping (wikipediaapi mocked)
# ---------------------------------------------------------------------------


class _FakeWikiPage:
    """Stand-in for a ``wikipediaapi.WikipediaPage`` exposing the attributes
    touched by _execute_search_wikipedia / _execute_get_wikipedia_page."""

    def __init__(
        self,
        *,
        title="Some Page",
        pageid=42,
        fullurl="https://en.wikipedia.org/wiki/Some_Page",
        summary="A summary",
        categories=None,
        exists=True,
        summary_exc=None,
        categories_exc=None,
    ):
        self.title = title
        self.pageid = pageid
        self.fullurl = fullurl
        self._summary = summary
        self._categories = categories if categories is not None else {}
        self._exists = exists
        self._summary_exc = summary_exc
        self._categories_exc = categories_exc

    @property
    def summary(self):
        if self._summary_exc is not None:
            raise self._summary_exc
        return self._summary

    @property
    def categories(self):
        if self._categories_exc is not None:
            raise self._categories_exc
        return self._categories

    def exists(self):
        return self._exists


def _fake_search_results(pages):
    """Build a SearchResults-like object with a ``.pages`` dict keyed by title."""
    from types import SimpleNamespace

    return SimpleNamespace(pages={p.title: p for p in pages})


def test_wikipedia_client_sets_ua_and_language(monkeypatch):
    """_wikipedia_client builds a wikipediaapi.Wikipedia with a Wikimedia-
    compliant user_agent and the requested language."""
    import wikipediaapi

    from app.services import metadata_agent as ma
    from app.services.metadata_agent import _WIKIPEDIA_USER_AGENT

    ma._wikipedia_client.cache_clear()
    captured = {}

    class _FakeWikipedia:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(wikipediaapi, "Wikipedia", _FakeWikipedia)
    try:
        ma._wikipedia_client("zh")
    finally:
        ma._wikipedia_client.cache_clear()

    assert captured["user_agent"] == _WIKIPEDIA_USER_AGENT
    assert "rssripple" in captured["user_agent"].lower()
    assert captured["language"] == "zh"
    assert captured["extract_format"] == wikipediaapi.ExtractFormat.WIKI


def test_wikipedia_client_caches_per_language():
    """Repeated calls for the same language return the cached client."""
    from app.services import metadata_agent as ma

    ma._wikipedia_client.cache_clear()
    a = ma._wikipedia_client("en")
    b = ma._wikipedia_client("en")
    assert a is b
    ma._wikipedia_client.cache_clear()


async def test_execute_search_wikipedia_maps_infra_error_to_transient(monkeypatch):
    """A wikipediaapi WikipediaException is mapped to a transient 'Wikipedia
    request failed' error - never a definitive not_found - so the backfill
    retries and the cache is not poisoned. (Transient retrying itself is
    handled inside wikipediaapi.)"""
    import wikipediaapi

    from app.services import metadata_agent as ma

    wiki = MagicMock()
    wiki.search.side_effect = wikipediaapi.WikiConnectionError(
        "https://zh.wikipedia.org/w/api.php"
    )
    monkeypatch.setattr(ma, "_wikipedia_client", lambda lang: wiki)

    result = await ma._execute_search_wikipedia("复制品也要谈恋爱", lang="zh")

    assert result["success"] is False
    assert result["data"] == []
    assert "Wikipedia request failed" in result["error"]
    assert "WikiConnectionError" in result["error"]


async def test_execute_search_wikipedia_returns_pages_on_success(monkeypatch):
    """A successful search returns pages with the uniform
    {title, page_id, url, summary} shape the agent tool contract expects."""
    from app.services import metadata_agent as ma

    page = _FakeWikiPage(
        title="Breaking Bad",
        pageid=14426270,
        fullurl="https://en.wikipedia.org/wiki/Breaking_Bad",
        summary="Breaking Bad is an American neo-Western crime drama television series.",
    )
    wiki = MagicMock()
    wiki.search.return_value = _fake_search_results([page])
    monkeypatch.setattr(ma, "_wikipedia_client", lambda lang: wiki)

    result = await ma._execute_search_wikipedia("Breaking Bad", lang="en")

    assert result["success"] is True
    assert len(result["data"]) == 1
    p = result["data"][0]
    assert p["title"] == "Breaking Bad"
    assert p["page_id"] == 14426270
    assert p["url"] == "https://en.wikipedia.org/wiki/Breaking_Bad"
    assert p["summary"].startswith("Breaking Bad is an American")


async def test_execute_search_wikipedia_empty_results_is_success_empty(monkeypatch):
    """A successful search with no hits is a definitive not_found
    (success=True, empty data), not an infra failure."""
    from app.services import metadata_agent as ma

    wiki = MagicMock()
    wiki.search.return_value = _fake_search_results([])
    monkeypatch.setattr(ma, "_wikipedia_client", lambda lang: wiki)

    result = await ma._execute_search_wikipedia("No Match", lang="en")

    assert result["success"] is True
    assert result["data"] == []


async def test_execute_search_wikipedia_skips_missing_and_summary_failure(monkeypatch):
    """A non-existent result stub is skipped, and a transient summary-extract
    failure on one page must not sink the whole result - the page is kept with
    an empty summary."""
    import wikipediaapi

    from app.services import metadata_agent as ma

    good = _FakeWikiPage(title="Good Page", pageid=1, summary="ok")
    missing = _FakeWikiPage(title="Missing Page", pageid=-1, exists=False)
    broken = _FakeWikiPage(
        title="Broken Summary",
        pageid=2,
        summary_exc=wikipediaapi.WikiInvalidJsonError("https://en.wikipedia.org/w/api.php"),
    )
    wiki = MagicMock()
    wiki.search.return_value = _fake_search_results([good, missing, broken])
    monkeypatch.setattr(ma, "_wikipedia_client", lambda lang: wiki)

    result = await ma._execute_search_wikipedia("query", lang="en")

    assert result["success"] is True
    titles = [p["title"] for p in result["data"]]
    assert "Good Page" in titles
    assert "Missing Page" not in titles  # exists() False -> skipped
    assert "Broken Summary" in titles    # kept despite summary failure
    broken_row = next(p for p in result["data"] if p["title"] == "Broken Summary")
    assert broken_row["summary"] == ""   # fell back to empty


async def test_execute_get_wikipedia_page_returns_full_page(monkeypatch):
    """A successful page fetch returns title, page_id, url, summary, and
    categories in the agent tool contract shape."""
    from app.services import metadata_agent as ma

    page = _FakeWikiPage(
        title="Breaking Bad",
        pageid=14426270,
        fullurl="https://en.wikipedia.org/wiki/Breaking_Bad",
        summary="Breaking Bad is an American neo-Western crime drama.",
        categories={
            "Category:2000s American crime drama television series": object(),
            "Category:Television series by Sony Pictures Television": object(),
        },
    )
    wiki = MagicMock()
    wiki.page.return_value = page
    monkeypatch.setattr(ma, "_wikipedia_client", lambda lang: wiki)

    result = await ma._execute_get_wikipedia_page("Breaking Bad", lang="en")

    assert result["success"] is True
    d = result["data"]
    assert d["title"] == "Breaking Bad"
    assert d["page_id"] == 14426270
    assert d["url"] == "https://en.wikipedia.org/wiki/Breaking_Bad"
    assert d["summary"].startswith("Breaking Bad is an American")
    assert "Category:2000s American crime drama television series" in d["categories"]


async def test_execute_get_wikipedia_page_not_found(monkeypatch):
    """A page that does not exist returns a non-transient 'Page not found'
    error (cached as not_found, not retried forever)."""
    from app.services import metadata_agent as ma

    page = _FakeWikiPage(title="Nope", pageid=-1, exists=False)
    wiki = MagicMock()
    wiki.page.return_value = page
    monkeypatch.setattr(ma, "_wikipedia_client", lambda lang: wiki)

    result = await ma._execute_get_wikipedia_page("Nope", lang="en")

    assert result["success"] is False
    assert "Page not found" in result["error"]


async def test_execute_get_wikipedia_page_maps_infra_error_to_transient(monkeypatch):
    """A wikipediaapi WikipediaException during page load is mapped to a
    transient 'Wikipedia request failed' error."""
    import wikipediaapi

    from app.services import metadata_agent as ma

    page = _FakeWikiPage()
    # exists() raises - simulates an info-fetch infra failure.
    page.exists = MagicMock(
        side_effect=wikipediaapi.WikiHttpTimeoutError("https://en.wikipedia.org/w/api.php")
    )
    wiki = MagicMock()
    wiki.page.return_value = page
    monkeypatch.setattr(ma, "_wikipedia_client", lambda lang: wiki)

    result = await ma._execute_get_wikipedia_page("Some Title", lang="en")

    assert result["success"] is False
    assert "Wikipedia request failed" in result["error"]
    assert "WikiHttpTimeoutError" in result["error"]


async def test_execute_get_wikipedia_page_detects_disambiguation(monkeypatch):
    """A disambiguation page (detected via category membership) returns the
    disambiguation payload so the agent can ask for a more specific title."""
    from app.services import metadata_agent as ma

    page = _FakeWikiPage(
        title="Mercury (disambiguation)",
        categories={"Category:Disambiguation pages": object()},
    )
    wiki = MagicMock()
    wiki.page.return_value = page
    monkeypatch.setattr(ma, "_wikipedia_client", lambda lang: wiki)

    result = await ma._execute_get_wikipedia_page("Mercury", lang="en")

    assert result["success"] is True
    assert result["data"]["disambiguation"] is True


def test_is_disambiguation_category_heuristic():
    from app.services.metadata_agent import _is_disambiguation_category as _isd

    assert _isd(["Category:Disambiguation pages"]) is True
    assert _isd(["Category:All article disambiguation pages"]) is True
    assert _isd(["Category:消歧义页"]) is True
    assert _isd(["Category:曖昧さ回避"]) is True
    assert _isd(["Category:2000s American crime drama television series"]) is False
    assert _isd([]) is False
