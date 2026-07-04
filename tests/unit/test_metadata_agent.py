"""Unit tests for source isolation in UnifiedMetadataAgent."""

from app.services.metadata_agent import (
    ResourceMetadata,
    UnifiedMetadataAgent,
    normalize_metadata_source_type,
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
