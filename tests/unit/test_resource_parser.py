"""Unit tests for the dynamic resource parser (app.services.resource_parser).

Tests parse_entry() with various field_mapping formats, regex patterns,
transforms, nested source paths, and edge cases.
"""

import pytest

from app.services.resource_parser import parse_entry


SAMPLE_ENTRY = {
    "title": "[LoliHouse] Spy x Family - 12 [WebRip 1080p HEVC-10bit AAC][CHT].mkv",
    "description": "Some description",
    "enclosures": [{"url": "https://example.com/test.torrent", "length": "1234567"}],
    "link": "https://example.com/detail/123",
    "published": "2026-06-21T10:00:00",
}

SAMPLE_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {
        "title_cn": {"source": "title", "regex": "\\]\\s*(.+?)\\s*-", "group": 1},
        "subtitle_group": {"source": "title", "regex": "^\\[([^\\]]+)\\]", "group": 1},
        "episode": {"source": "title", "regex": "-\\s*(\\d+)\\s*\\[", "group": 1, "transform": "int"},
        "resolution": {"source": "title", "regex": "\\b(1080p|720p)\\b", "group": 1, "transform": "lowercase"},
        "torrent_url": {"source": "enclosures[0].url"},
        "file_size": {"source": "enclosures[0].length", "transform": "int"},
    },
}


# =============================================================================
# 1. test_parse_entry_no_mapping
# =============================================================================
def test_parse_entry_no_mapping():
    result = parse_entry(SAMPLE_ENTRY, None)
    assert result == {}


# =============================================================================
# 2. test_parse_entry_empty_mapping
# =============================================================================
def test_parse_entry_empty_mapping():
    result = parse_entry(SAMPLE_ENTRY, {})
    assert result == {}


# =============================================================================
# 3. test_parse_entry_new_format
# =============================================================================
def test_parse_entry_new_format():
    result = parse_entry(SAMPLE_ENTRY, SAMPLE_MAPPING)

    assert result["title_cn"] == "Spy x Family"
    assert result["subtitle_group"] == "LoliHouse"
    assert result["episode"] == 12
    assert result["resolution"] == "1080p"
    assert result["torrent_url"] == "https://example.com/test.torrent"
    assert result["file_size"] == 1234567


# =============================================================================
# 4. test_parse_entry_old_format_backward_compat
# =============================================================================
def test_parse_entry_old_format_backward_compat():
    """Flat dict (no list_locator/field_mappings wrapper) should still work."""
    old_mapping = {
        "subtitle_group": {"source": "title", "regex": "^\\[([^\\]]+)\\]", "group": 1},
        "torrent_url": {"source": "enclosures[0].url"},
    }

    result = parse_entry(SAMPLE_ENTRY, old_mapping)

    assert result["subtitle_group"] == "LoliHouse"
    assert result["torrent_url"] == "https://example.com/test.torrent"


# =============================================================================
# 5. test_parse_entry_with_regex
# =============================================================================
def test_parse_entry_with_regex():
    mapping = {
        "field_mappings": {
            "subtitle_group": {"source": "title", "regex": "^\\[([^\\]]+)\\]", "group": 1},
            "title_cn": {"source": "title", "regex": "\\]\\s*(.+?)\\s*-", "group": 1},
        }
    }

    result = parse_entry(SAMPLE_ENTRY, mapping)

    assert result["subtitle_group"] == "LoliHouse"
    assert result["title_cn"] == "Spy x Family"


# =============================================================================
# 6. test_parse_entry_with_transforms
# =============================================================================
class TestTransforms:
    def test_int_transform(self):
        mapping = {
            "field_mappings": {
                "file_size": {"source": "enclosures[0].length", "transform": "int"},
            }
        }
        result = parse_entry(SAMPLE_ENTRY, mapping)
        assert result["file_size"] == 1234567
        assert isinstance(result["file_size"], int)

    def test_float_transform(self):
        entry = {"rating": "9.5"}
        mapping = {
            "field_mappings": {
                "rating": {"source": "rating", "transform": "float"},
            }
        }
        result = parse_entry(entry, mapping)
        assert result["rating"] == 9.5
        assert isinstance(result["rating"], float)

    def test_lowercase_transform(self):
        entry = {"format": "MKV"}
        mapping = {
            "field_mappings": {
                "container": {"source": "format", "transform": "lowercase"},
            }
        }
        result = parse_entry(entry, mapping)
        assert result["container"] == "mkv"

    def test_uppercase_transform(self):
        entry = {"format": "mkv"}
        mapping = {
            "field_mappings": {
                "container": {"source": "format", "transform": "uppercase"},
            }
        }
        result = parse_entry(entry, mapping)
        assert result["container"] == "MKV"

    def test_iso_datetime_transform(self):
        mapping = {
            "field_mappings": {
                "published_at": {"source": "published", "transform": "iso_datetime"},
            }
        }
        result = parse_entry(SAMPLE_ENTRY, mapping)
        assert result["published_at"].year == 2026
        assert result["published_at"].month == 6
        assert result["published_at"].day == 21


# =============================================================================
# 7. test_parse_entry_nested_source
# =============================================================================
def test_parse_entry_nested_source():
    mapping = {
        "field_mappings": {
            "torrent_url": {"source": "enclosures[0].url"},
            "detail": {"source": "link"},
        }
    }
    result = parse_entry(SAMPLE_ENTRY, mapping)
    assert result["torrent_url"] == "https://example.com/test.torrent"
    assert result["detail"] == "https://example.com/detail/123"


# =============================================================================
# 8. test_parse_entry_missing_source
# =============================================================================
def test_parse_entry_missing_source():
    mapping = {
        "field_mappings": {
            "nonexistent_field": {"source": "does_not_exist"},
        }
    }
    result = parse_entry(SAMPLE_ENTRY, mapping)
    assert result["nonexistent_field"] is None


# =============================================================================
# 9. test_parse_entry_regex_no_match
# =============================================================================
def test_parse_entry_regex_no_match():
    mapping = {
        "field_mappings": {
            "season": {"source": "title", "regex": "Season\\s+(\\d+)", "group": 1},
        }
    }
    result = parse_entry(SAMPLE_ENTRY, mapping)
    assert result["season"] is None
