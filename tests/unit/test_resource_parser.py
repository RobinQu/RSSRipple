"""Unit tests for the dynamic resource parser (app.services.resource_parser).

Tests parse_entry() with various field_mapping formats, regex patterns
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
        },
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
                "file_size": {"source": "enclosures[0].length", "transform": "int"}
            }
        }
        result = parse_entry(SAMPLE_ENTRY, mapping)
        assert result["file_size"] == 1234567
        assert isinstance(result["file_size"], int)

    def test_float_transform(self):
        entry = {"rating": "9.5"}
        mapping = {
            "field_mappings": {
                "rating": {"source": "rating", "transform": "float"}
            }
        }
        result = parse_entry(entry, mapping)
        assert result["rating"] == 9.5
        assert isinstance(result["rating"], float)

    def test_lowercase_transform(self):
        entry = {"format": "MKV"}
        mapping = {
            "field_mappings": {
                "container": {"source": "format", "transform": "lowercase"}
            }
        }
        result = parse_entry(entry, mapping)
        assert result["container"] == "mkv"

    def test_uppercase_transform(self):
        entry = {"format": "mkv"}
        mapping = {
            "field_mappings": {
                "container": {"source": "format", "transform": "uppercase"}
            }
        }
        result = parse_entry(entry, mapping)
        assert result["container"] == "MKV"

    def test_iso_datetime_transform(self):
        mapping = {
            "field_mappings": {
                "published_at": {"source": "published", "transform": "iso_datetime"}
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
        },
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
            "nonexistent_field": {"source": "does_not_exist"}
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
            "season": {"source": "title", "regex": "Season\\s+(\\d+)", "group": 1}
        }
    }
    result = parse_entry(SAMPLE_ENTRY, mapping)
    assert result["season"] is None


# =============================================================================
# detect_batch — multi-episode (合集) heuristic
# =============================================================================


import pytest
from app.services.resource_parser import detect_batch


@pytest.mark.parametrize(
    "title,expected",
    [
        # SxxEyy~zz
        (
            "魔法帽的工作室「とんがり帽子のアトリエ」Witch Hat Atelier S01E01~13 1080p 多国字幕",
            (True, 1, 13),
        ),
        # [01-12 合集]
        (
            "[LoliHouse] 异世界悠闲农家 2 / Isekai Nonbiri Nouka 2 [01-12 合集][WebRip 1080p HEVC-10bit AAC][简繁内封字幕][Fin]",
            (True, 1, 12),
        ),
        # [01-16 合集]
        (
            "[LoliHouse] 欢迎来到实力至上主义的教室 第四季 / Youkoso Jitsuryoku Shijou Shugi no Kyoushitsu e S4 [01-16 合集][WebRip 1080p HEVC-10bit AAC][简繁内封字幕][Fin]",
            (True, 1, 16),
        ),
        # SxxEyy-zz (with dash)
        ("Some Show S02E01-24 1080p BluRay", (True, 1, 24)),
        # Batch keyword only
        ("[SubGroup] Show S02 Season Pack 1080p", (True, None, None)),
        ("Anime Title 全集 1080p", (True, None, None)),
        # 第01-第12话
        ("番剧 第01-第12话 1080p 全", (True, 1, 12)),
        # Not a batch — single episode
        ("[LoliHouse] Show S04 - 05 [WebRip 1080p]", (False, None, None)),
        # Not a batch — random text
        ("random_bytes_xyz123 1080p", (False, None, None)),
        # Empty
        ("", (False, None, None)),
        (None, (False, None, None)),
    ],
)
def test_detect_batch(title, expected):
    assert detect_batch(title) == expected


def test_detect_batch_ignores_resolution_pairs():
    """1920x1080 must not be mistaken for a batch range."""
    result = detect_batch("[Group] Show - 05 (1920x1080 HEVC AAC)")
    assert result == (False, None, None)


# =============================================================================
# detect_subtitle_langs — BCP-47 tag mapping
# =============================================================================


from app.services.resource_parser import detect_subtitle_langs


@pytest.mark.parametrize(
    "title,expected",
    [
        ("[LoliHouse] Show - 05 [简体]", ["zh-CN"]),
        ("[LoliHouse] Show - 05 [CHS]", ["zh-CN"]),
        ("[LoliHouse] Show - 05 [繁体]", ["zh-TW"]),
        ("[LoliHouse] Show - 05 [CHT]", ["zh-TW"]),
        ("[LoliHouse] Show - 05 [简繁内封字幕]", ["zh-CN", "zh-TW"]),
        ("[LoliHouse] Show - 05 [简繁日内封字幕]", ["zh-CN", "zh-TW", "ja"]),
        ("[Skymoon-Raws] Show - 05 [CHT][1080p]", ["zh-TW"]),
        ("[Group] Movie 2024 [CHS][CHT][ENG]", ["zh-CN", "zh-TW", "en"]),
        # Multi-language sentinel — only "multi", never combined with specifics.
        ("Witch Hat Atelier S01E01~13 1080p 多国字幕", ["multi"]),
        ("[Group] Show 1080p Multi-Sub", ["multi"]),
        # No subtitle marker at all → empty list.
        ("Some Show S02E05 1080p", []),
        # Empty / None
        ("", []),
        (None, []),
    ],
)
def test_detect_subtitle_langs(title, expected):
    assert detect_subtitle_langs(title) == expected


def test_detect_subtitle_langs_dedupes_repeated_markers():
    # A pathological title that spells CHS multiple times should still get one
    # zh-CN back.
    assert detect_subtitle_langs("[CHS][简体][GB]") == ["zh-CN"]


# =============================================================================
# detect_absolute_episode — NN(MM) double-labeled episode parsing (P2)
# =============================================================================

from app.services.resource_parser import detect_absolute_episode


@pytest.mark.parametrize(
    "title,expected",
    [
        # Canonical fansub form — S4 Ep 13, absolute 85 across all seasons.
        ("[豌豆字幕组&LoliHouse] 关于我转生变成史莱姆这档事 第四季 - 13(85) [WebRip 1080p]", (13, 85)),
        # Same form, mainland-style bracket
        ("[Group] Show S04 - 13 (85) [1080p]", (13, 85)),
        # Small gap between numbers → NOT the absolute-episode pattern; likely
        # a runtime or part indicator ("13(15)"). We stay conservative.
        ("[Group] Show - 13(15) [1080p]", (None, None)),
        # Absolute smaller than per-season → not the pattern.
        ("[Group] Show - 85(13) [1080p]", (None, None)),
        # Missing parens
        ("[Group] Show - 13 85 [1080p]", (None, None)),
        # No episode marker
        ("Random title 1080p", (None, None)),
        # Empty / None
        ("", (None, None)),
        (None, (None, None)),
    ],
)
def test_detect_absolute_episode(title, expected):
    assert detect_absolute_episode(title) == expected
