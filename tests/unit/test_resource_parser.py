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
        # --- real-world titles that used to be missed ---
        # Bare range after an S01 season marker, no keyword
        (
            "[7³ACG] 齐木楠雄的灾难/The Disastrous Life of Saiki K S01 | 01-24 [简繁字幕] BDrip 1080p x265 OPUS 2.0",
            (True, 1, 24),
        ),
        # Same, with a "+SPx11" extras suffix on the range
        (
            "[7³ACG] 葬送的芙莉莲/Sousou no Frieren S01 | 01-28+SPx11 [简繁字幕] BDrip 1080p AV1 OPUS 2.0",
            (True, 1, 28),
        ),
        # Range at the tail of a bracket that also holds title text
        (
            "[LinRip][青春猪头少年不会梦到圣诞服女郎 01-13][Rascal Does Not Dream of Santa Claus][BDRemux 1080p AVC FLAC]",
            (True, 1, 13),
        ),
        (
            "[LinRip][超超超超超喜欢你的100个女朋友 第二季 13-24][The 100 Girlfriends Who Really Love You][BDRip 1080p]",
            (True, 13, 24),
        ),
        # "TV fin" keyword — batch without explicit boundaries
        (
            "[AYN爱·怨念·字幕组][UFO机器人古连泰沙][UFOロボ グレンダイザー][TV fin][1975][BD1080P][简中内封]",
            (True, None, None),
        ),
        # Full-width tilde ～ (U+FF5E) and wave dash 〜 (U+301C) connectors
        ("[Group] Show S01E01～13 1080p", (True, 1, 13)),
        ("【某作品 01〜12】【BDRip 1080p】", (True, 1, 12)),
        # --- false-positive guards ---
        # Single episode with "S01 - 05" numbering (a dash, not a range)
        ("[Group] Show S01 - 05 [1080p]", (False, None, None)),
        ("[Group] Show S01E05 [1080p]", (False, None, None)),
        # Year pair: bracket-range shape, rejected by the sanity cap (end > 999)
        ("[Group] Show [2020-2021] [1080p]", (False, None, None)),
        # Resolution / codec tokens are not ranges
        ("[Group] Show [1080p-Hi10P] - 05", (False, None, None)),
        ("[Group] Show - 05 [WebRip 1080p HEVC-10bit AAC]", (False, None, None)),
        # Bare "Fin" on a single final episode is NOT a batch keyword
        ("[Group] Show - 13 [1080p][Fin]", (False, None, None)),
    ],
)
def test_detect_batch(title, expected):
    assert detect_batch(title) == expected


# =============================================================================
# extract_compilation_work_title - [整理搬运] archive torrents
# =============================================================================


from app.services.resource_parser import extract_compilation_work_title  # noqa: E402


@pytest.mark.parametrize(
    "title,expected",
    [
        # Primary work name before ／ (full-width slash) and ：description
        (
            "[整理搬运] 猫眼三姐妹／猫之眼 (キャッツ・アイ) (Kyattsu Ai／Cat's Eye)：TV动画 (1983年版、1984年版)+剧场版+漫画+CD",
            "猫眼三姐妹",
        ),
        # Work name before a half-width parenthetical alt title
        (
            "[整理搬运] 幸运星 (らき☆すた) (Lucky Star)：TV动画+OVA篇+漫画+音乐+其他",
            "幸运星",
        ),
        ("[整理搬运] 犬夜叉 (Inuyasha)：TV动画+剧场版+OVA篇", "犬夜叉"),
        # 【】tag form
        ("【整理搬运】新世纪福音战士 (EVANGELION)：TV动画+剧场版", "新世纪福音战士"),
        # Not a compilation -> None
        ("[LoliHouse] 无职转生 3期 / Mushoku Tensei S3 - 03 [1080p]", None),
        ("[G] Show - 01 [1080p]", None),
        ("", None),
        (None, None),
    ],
)
def test_extract_compilation_work_title(title, expected):
    assert extract_compilation_work_title(title) == expected


def test_detect_batch_flags_compilation_tag():
    # [整理搬运] archive torrents are flagged as batches (no explicit range).
    assert detect_batch("[整理搬运] 猫眼三姐妹／猫之眼：TV动画+剧场版") == (True, None, None)
    assert detect_batch("【打包】某作品 全集") == (True, None, None)


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


# ---------------------------------------------------------------------------
# strip_season_from_title
# ---------------------------------------------------------------------------

from app.services.resource_parser import strip_season_from_title  # noqa: E402


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("关于我转生变成史莱姆这档事 第四季", "关于我转生变成史莱姆这档事"),
        ("欢迎来到实力至上主义的教室 第四季", "欢迎来到实力至上主义的教室"),
        ("碧蓝航线：微速前行！第二季", "碧蓝航线：微速前行！"),
        ("That Time I Got Reincarnated as a Slime Season 4", "That Time I Got Reincarnated as a Slime"),
        ("Wistoria: Wand and Sword Season 2", "Wistoria: Wand and Sword"),
        ("Skeleton Knight in Another World S2", "Skeleton Knight in Another World"),
        ("Some Show 4th Season", "Some Show"),
        # Non-season titles are untouched
        ("名侦探柯南", "名侦探柯南"),
        ("Spy x Family", "Spy x Family"),
        # Ambiguous bare trailing digits are NOT stripped (too risky)
        ("异世界悠闲农家2", "异世界悠闲农家2"),
        # None / empty pass through
        (None, None),
        ("", ""),
    ],
)
def test_strip_season_from_title(raw, expected):
    assert strip_season_from_title(raw) == expected


# ---------------------------------------------------------------------------
# normalize_parsed_fields - post-parse repair of bracket leaks & missed tech
# ---------------------------------------------------------------------------

from app.services.resource_parser import normalize_parsed_fields  # noqa: E402


def _norm(title_raw, parsed):
    """Run the normalizer and drop None values, mirroring fetch_service usage."""
    return {k: v for k, v in normalize_parsed_fields(title_raw, parsed).items() if v is not None}


def test_normalize_repairs_multi_bracket_title_leak():
    """Second bracket [ViuTV粵語] leaks into title_cn/title_en; normalizer
    re-derives them from the bracket-stripped core."""
    raw = ("[jibaketa合成&音頻壓制][ViuTV粵語]幪面超人 / 假面騎士ZEZTZ / 假面骑士ZZZ / "
           "Kamen Rider Zeztz - 42 [粵日雙語+內封繁體中文字幕] "
           "(WEB 1920x1080 AVC AACx2 SRT+PGS ViuTV CHT)")
    parsed = {
        "title_cn": "粵語]幪面超人 ", "title_en": "[ViuTV",
        "subtitle_group": "jibaketa合成&音頻壓制", "episode": 42, "season": 1,
        "video_codec": "AVC", "subtitle_type": "CHT",
    }
    out = _norm(raw, parsed)
    assert out["title_cn"] == "幪面超人"
    assert out["title_en"] == "Kamen Rider Zeztz"
    # search_title prefers the latin variant (best local-match signal)
    assert out["search_title"] == "Kamen Rider Zeztz"
    # tech fields filled from the parenthetical block
    assert out["resolution"] == "1080p"
    assert out["source"] == "WEB"
    assert out["audio_codec"] == "AAC"
    # already-present values preserved
    assert out["video_codec"] == "AVC"
    assert out["subtitle_type"] == "CHT"
    assert out["episode"] == 42


def test_normalize_repairs_ultraman_title_to_latin_name():
    """The user-reported case: work name should be 'Ultraman Teo', not ViuTV."""
    raw = ("[jibaketa合成&二次壓制][ViuTV粵語]超人 / 超人力霸王狄奧 / 提欧奥特曼 / "
           "Ultraman Teo - 02 [粵語+無對白字幕](WEB 1920x1080 x264 AAC YUE CHT)")
    parsed = {"title_cn": "粵語]超人 ", "title_en": "[ViuTV",
              "subtitle_group": "jibaketa合成&二次壓制", "episode": 2}
    out = _norm(raw, parsed)
    assert out["title_en"] == "Ultraman Teo"
    assert out["search_title"] == "Ultraman Teo"
    assert out["resolution"] == "1080p"
    assert out["source"] == "WEB"
    assert out["audio_codec"] == "AAC"
    assert out["video_codec"] == "x264"


def test_normalize_is_noop_for_clean_parse():
    """A cleanly-parsed single-bracket title is untouched; search_title stays
    None (extract_search_title handles it later)."""
    raw = "[ANi] Kamen Rider ZEZTZ /  假面騎士 ZEZTZ - 39 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]"
    parsed = {
        "title_cn": "假面騎士 ZEZTZ", "title_en": "Kamen Rider ZEZTZ /",
        "subtitle_group": "ANi", "episode": 39, "source": "WEB-DL",
        "video_codec": "AVC", "audio_codec": "AAC", "subtitle_type": "CHT", "container": "MP4",
    }
    out = _norm(raw, parsed)
    # No bracket leak -> title fields unchanged, no search_title injected
    assert out["title_cn"] == "假面騎士 ZEZTZ"
    assert out["title_en"] == "Kamen Rider ZEZTZ /"
    assert "search_title" not in out
    # Existing tech values are NOT overwritten
    assert out["source"] == "WEB-DL"
    assert out["audio_codec"] == "AAC"
    assert out["container"] == "MP4"


def test_normalize_fills_missing_tech_without_title_repair():
    """Tech fields are filled from title_raw even when title_cn/title_en are
    clean (only None fields are filled - never overwritten)."""
    raw = "[Group] Some Show - 05 (WEB 1920x1080 x265 FLAC2.0 MP4)"
    parsed = {"title_cn": "Some Show", "subtitle_group": "Group", "episode": 5}
    out = _norm(raw, parsed)
    assert out["resolution"] == "1080p"
    assert out["source"] == "WEB"
    assert out["video_codec"] == "x265"
    assert out["audio_codec"] == "FLAC"
    assert out["container"] == "MP4"
    # title untouched (no leak)
    assert out["title_cn"] == "Some Show"
    assert "search_title" not in out


def test_normalize_resolution_from_4k():
    raw = "[G] Show - 1 (3840x2160 AVC AAC MKV)"
    out = _norm(raw, {})
    assert out["resolution"] == "2160p"


def test_normalize_no_brackets_in_search_title_when_no_latin_segment():
    """If only CJK segments exist, search_title falls back to the CJK one."""
    raw = "[Group][粵語]幪面超人 - 01 (WEB 1080p AAC)"
    parsed = {"title_cn": "語]幪面超人", "title_en": "[Group"}
    out = _norm(raw, parsed)
    # title_en leaked -> repaired; no pure-latin segment -> search_title = CJK
    assert out["title_cn"] == "幪面超人"
    assert out["search_title"] == "幪面超人"


def test_normalize_empty_title_and_missing_fields():
    assert normalize_parsed_fields(None, {"title_cn": None}) == {"title_cn": None}
    assert normalize_parsed_fields("", {"episode": 1}) == {"episode": 1}


# =============================================================================
# Case-insensitive extraction + resolution canonicalization
# =============================================================================
class TestCaseInsensitiveExtraction:
    def test_user_regex_matches_regardless_of_case(self):
        # "1080P" in the title must match a lowercase "1080p" pattern, and
        # the value is canonicalized to "1080p" (no transform configured).
        entry = {"title": "[ANi] Yomi no Tsugai - 16 [1080P][Baha][WEB-DL][MP4]"}
        mapping = {
            "field_mappings": {
                "resolution": {"source": "title", "regex": "\\b(1080p|720p)\\b", "group": 1},
            }
        }
        result = parse_entry(entry, mapping)
        assert result["resolution"] == "1080p"

    def test_resolution_canonicalized_without_transform(self):
        entry = {"title": "Show - 01 [720P]"}
        mapping = {
            "field_mappings": {
                "resolution": {"source": "title", "regex": "\\b(\\d{3,4}[pP])\\b", "group": 1},
            }
        }
        result = parse_entry(entry, mapping)
        assert result["resolution"] == "720p"

    def test_wxh_resolution_passes_through(self):
        # Only the plain "<digits>p" shape is canonicalized.
        entry = {"title": "Show - 01 (1920x1080)"}
        mapping = {
            "field_mappings": {
                "resolution": {"source": "title", "regex": "\\b(1920x1080)\\b", "group": 1},
            }
        }
        result = parse_entry(entry, mapping)
        assert result["resolution"] == "1920x1080"


# ---------------------------------------------------------------------------
# extract_episode_fallback / normalize episode-season fallback
# ---------------------------------------------------------------------------

from app.services.resource_parser import extract_episode_fallback  # noqa: E402


class TestExtractEpisodeFallback:
    def test_bracket_episode(self):
        assert extract_episode_fallback(
            "[绿茶字幕组] 攻壳机动队 The Ghost in the Shell / Koukaku Kidoutai 2026 [03][WebRip][1080p][简日内嵌]"
        ) == (3, None)

    def test_bracket_episode_with_s_season(self):
        assert extract_episode_fallback(
            "[绿茶字幕组] 无职转生 第三季 / Mushoku Tensei S3 [03][WebRip][1080p]"
        ) == (3, 3)

    def test_season_suffix(self):
        assert extract_episode_fallback(
            "[北宇治字幕组] 无职转生Ⅲ / Mushoku Tensei - Season 3 [04][WebRip]"
        ) == (4, 3)

    def test_sxxexx(self):
        assert extract_episode_fallback(
            "[Nix-Raws] 无职转生Ⅲ / Mushoku Tensei Isekai Ittara Honki Dasu S03E06 [CR WEB-DL 1080p]"
        ) == (6, 3)

    def test_kanji_season(self):
        assert extract_episode_fallback(
            "[ANi]  关于我转生变成史莱姆这档事 第四季 - 89 [1080P][Baha]"
        ) == (None, 4)

    def test_ordinal_season(self):
        assert extract_episode_fallback(
            "[Group] Some Show 2nd Season [05][WebRip 1080p]"
        ) == (5, 2)

    def test_ordinal_season_without_episode(self):
        assert extract_episode_fallback("[Group] Some Show 4th Season") == (None, 4)

    def test_season_suffix_wins_over_ordinal(self):
        # "Season 3" is the canonical suffix form; the ordinal regex must not
        # mis-fire on titles that carry both shapes.
        assert extract_episode_fallback(
            "[Group] Some Show Season 3 [02]"
        ) == (2, 3)

    def test_tech_brackets_never_match(self):
        # [1080p] is 4 digits; [2026] is a year — neither is an episode.
        assert extract_episode_fallback(
            "[Group] Some Work [1080p][2026]"
        ) == (None, None)

    def test_dash_format_left_to_field_mapping(self):
        # The "- NN" form is covered by the per-channel mapping; the fallback
        # intentionally does not grab it (it runs only when mapping missed).
        assert extract_episode_fallback(
            "[LoliHouse] 黄泉使者 / Yomi no Tsugai - 14 [1080p]"
        ) == (None, None)

    def test_bracket_episode_with_version_tag(self):
        # "[02v2]" = episode 2, second revised release. The version tag is
        # dropped; it is not part of the episode number.
        assert extract_episode_fallback(
            "[绿茶字幕组] 无职转生 第三季 ～到了异世界就拿出真本事～ / Mushoku Tensei S3 "
            "[02v2][WebRip][1080p][简日内嵌]"
        ) == (2, 3)

    def test_sxxexx_with_version_tag(self):
        assert extract_episode_fallback(
            "[Nix-Raws] Mushoku Tensei S03E06v2 [CR WEB-DL 1080p]"
        ) == (6, 3)

    def test_version_tag_uppercase_and_multi_digit(self):
        assert extract_episode_fallback("[Group] Some Work [01V2]") == (1, None)
        assert extract_episode_fallback("[Group] Some Work [12v10]") == (12, None)

    def test_version_tag_does_not_unlock_tech_brackets(self):
        # [1080p] / [2026] still never match; a bare [v2] has no episode number.
        assert extract_episode_fallback(
            "[Group] Some Work [1080p][2026][v2]"
        ) == (None, None)


def test_normalize_fills_episode_and_season_from_brackets():
    from app.services.resource_parser import normalize_parsed_fields
    out = normalize_parsed_fields(
        "[绿茶字幕组] 无职转生 第三季 / Mushoku Tensei S3 [03][WebRip][1080p]",
        {"episode": None, "season": None},
    )
    assert out["episode"] == 3
    assert out["season"] == 3


def test_normalize_does_not_override_parsed_episode():
    from app.services.resource_parser import normalize_parsed_fields
    out = normalize_parsed_fields(
        "[绿茶字幕组] 无职转生 第三季 / Mushoku Tensei S3 [03][WebRip][1080p]",
        {"episode": 3, "season": 1},
    )
    assert out["episode"] == 3
    assert out["season"] == 1  # untouched even though the title says S3


# =============================================================================
# extract_title_year
# =============================================================================


class TestExtractTitleYear:
    def test_standalone_year_token(self):
        from app.services.resource_parser import extract_title_year
        assert extract_title_year(
            "[绿茶字幕组] 攻壳机动队 The Ghost in the Shell / Koukaku Kidoutai 2026 "
            "[03][WebRip][1080p][简繁日内封]"
        ) == 2026

    def test_no_year_returns_none(self):
        from app.services.resource_parser import extract_title_year
        assert extract_title_year(
            "【豌豆字幕组】[关于我转生变成史莱姆这档事 第四季 / Tensei Shitara Slime "
            "Datta Ken S4][16(88)][繁体][1080P][MP4]"
        ) is None

    def test_bracketed_year(self):
        from app.services.resource_parser import extract_title_year
        assert extract_title_year("[Group] Some Work [2026] [01]") == 2026

    def test_resolution_never_matches(self):
        from app.services.resource_parser import extract_title_year
        assert extract_title_year("[Group] Some Work - 01 1920x1080") is None
        assert extract_title_year("[Group] Some Work - 01 [1080p]") is None

    def test_codec_adjacent_rejected(self):
        from app.services.resource_parser import extract_title_year
        assert extract_title_year("[Group] Work x264 [01]") is None
        assert extract_title_year("[Group] Work 2026x264 [01]") is None

    def test_sanity_range(self):
        from app.services.resource_parser import extract_title_year
        assert extract_title_year("[Group] Work [1910]") is None
        assert extract_title_year("[Group] Work [2101]") is None
        assert extract_title_year("[Group] Work [1995]") == 1995

    def test_empty_title(self):
        from app.services.resource_parser import extract_title_year
        assert extract_title_year("") is None


def test_normalize_fills_title_year():
    from app.services.resource_parser import normalize_parsed_fields
    out = normalize_parsed_fields(
        "[绿茶字幕组] 攻壳机动队 / Koukaku Kidoutai 2026 [03][WebRip][1080p]",
        {},
    )
    assert out["title_year"] == 2026


def test_normalize_does_not_override_title_year():
    from app.services.resource_parser import normalize_parsed_fields
    out = normalize_parsed_fields(
        "[Group] Work 2026 [01]",
        {"title_year": 1995},
    )
    assert out["title_year"] == 1995
