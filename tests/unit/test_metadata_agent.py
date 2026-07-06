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
