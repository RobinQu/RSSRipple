"""In-process integration tests for the pure metadata leaf modules.

These modules are the deterministic core of the metadata pipeline (Wikipedia
episode parsing, episode reconciliation, anime signals, URL/parser/text
normalization, and the Filter DSL engine). They are exercised here in-process
under the integration coverage harness (``.coverage.test-runner``) so the
combined single-node suite measures them — mirroring how
``tests/integration/organize`` covers the organize subsystem.
"""

from __future__ import annotations

import pathlib
from datetime import date
from types import SimpleNamespace

import pytest

from app.services.anime_signals import (
    apply_is_anime,
    bangumi_verdict,
    is_anime_from_tmdb,
    is_anime_identity,
)
from app.services.filter_engine import (
    evaluate_field_condition,
    evaluate_filter_config,
    get_field_value,
    loaded_relation,
    merge_filters,
    validate_filter_config,
)
from app.services.genre_registry import genre_zh, normalize_genres
from app.services.metadata_dedup import (
    _cluster_by_shared_title,
    _merge_aliases,
    _title_keys,
)
from app.services.metadata_episode_reconcile import (
    apply_episode_reconcile,
    locate_absolute_episode,
    reconcile_episode,
    resolve_missing_season,
    seasons_map_from_list,
    verified_season_count,
)
from app.services.metadata_failure import _classify_failure, _record_metadata_attempt
from app.services.metadata_resource_meta import ResourceMetadata
from app.services.metadata_wiki_classify import (
    _classify_wikipedia_page,
    _infer_content_type_from_categories,
    _validate_matched_entity_kind,
)
from app.services.metadata_wiki_query import (
    _candidate_queries,
    _clean_query,
    _work_name_prefix,
)
from app.services.resource_parser import (
    detect_absolute_episode,
    detect_batch,
    detect_subtitle_langs,
    extract_compilation_work_title,
    extract_episode_fallback,
    extract_title_year,
    normalize_parsed_fields,
    parse_entry,
    strip_season_from_title,
)
from app.services.text_normalizer import (
    normalize_title,
    normalize_title_denoised,
    partial_similarity_score,
    similarity_score,
)
from app.services.url_tools import keep_k_per_hostname, normalize_url
from app.services.wikidata_collection import (
    _entity_titles,
    _normalize_title,
    _parse_wikipedia_url,
    entity_label_matches,
    entity_labels,
    extract_p179_qids,
)
from app.services.wikipedia_episode_parser import (
    _kanji_to_int,
    _parse_air_date,
    _parse_episode_number,
    clean_text,
    parse_episode_list,
    parse_seasons_from_infobox,
)

FIXTURES = pathlib.Path(__file__).parent.parent / "unit" / "fixtures" / "wikipedia"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# wikipedia_episode_parser
# ---------------------------------------------------------------------------


class TestWikipediaEpisodeParser:
    def test_zh_100gf_infobox_and_episode_list(self):
        wt = _load("zh_100gf.wikitext")
        assert parse_seasons_from_infobox(wt) == [
            {"season_number": 1, "episode_count": 12},
            {"season_number": 2, "episode_count": 12},
        ]
        data = parse_episode_list(wt)
        assert data is not None
        assert data["seasons"][2] == {"season_number": 3, "episode_count": 6}
        assert len(data["episodes"]) == 30
        ep1 = data["episodes"][0]
        assert ep1["title"] == "超超超超超喜歡你的 2個女朋友（還有98人）"
        assert ep1["air_date"] == "2023-10-08"

    def test_ja_mushoku_real_sample(self):
        wt = _load("ja_mushoku.wikitext")
        assert parse_seasons_from_infobox(wt) == [
            {"season_number": 1, "episode_count": 23},
            {"season_number": 2, "episode_count": 25},
        ]
        data = parse_episode_list(wt)
        assert data is not None
        s2 = [e for e in data["episodes"] if e["season"] == 2]
        assert s2[0]["episode"] == 0
        assert s2[0]["title"] == "守護術師フィッツ"

    def test_ja_gits_no_episode_section(self):
        wt = _load("ja_gits.wikitext")
        assert parse_episode_list(wt) is None
        assert parse_seasons_from_infobox(wt) == [
            {"season_number": 1, "episode_count": 26},
        ]

    def test_bookworm_multi_infobox_decoy(self):
        wt = _load("zh_bookworm.wikitext")
        assert parse_seasons_from_infobox(wt) == [
            {"season_number": 1, "episode_count": 14},
            {"season_number": 2, "episode_count": 12},
            {"season_number": 3, "episode_count": 10},
        ]

    def test_slime_no_tv_block(self):
        assert parse_seasons_from_infobox(_load("zh_slime.wikitext")) is None

    def test_infobox_variants(self):
        assert parse_seasons_from_infobox(
            "{{Infobox animanga/TVAnime\n| 话数 = {{ubl|第１季:共１２话|第２季：全13話}}\n}}"
        ) == [
            {"season_number": 1, "episode_count": 12},
            {"season_number": 2, "episode_count": 13},
        ]
        assert parse_seasons_from_infobox(
            "{{Infobox animanga/TVAnime\n| 話数 = 全12話\n}}"
        ) == [{"season_number": 1, "episode_count": 12}]
        # Novel/Manga decoys never leak into TV seasons.
        assert parse_seasons_from_infobox(
            "{{Infobox animanga/Novel\n| 話數 = 全677話（web）\n}}"
            "{{Infobox animanga/Manga\n| 話數 = 全51話\n}}"
        ) is None
        # Two plain TVAnime blocks are separate works — refuse to guess.
        assert parse_seasons_from_infobox(
            "{{Infobox animanga/TVAnime\n| 話數 = 全24話\n}}"
            "{{Infobox animanga/TVAnime\n| 集數 = 全24話預定\n}}"
        ) is None

    def test_episode_list_variants(self):
        wt = """=== 各話列表 ===
{{劇集列表/base
| Number = 第1話
| Title = 甲
| Aux5 = '''2024年'''<br />1月1日
}}
{{劇集列表/base
| Number = 2
| Title = 乙
| Aux5 = 1月8日
}}
"""
        data = parse_episode_list(wt)
        assert data["seasons"] == [{"season_number": 1, "episode_count": 2}]
        assert data["episodes"][1]["air_date"] == "2024-01-08"
        assert parse_episode_list("== 概要 ==\n本文") is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("第1話", 1), ("第13話", 13), ("3", 3), ("第１２話", 12),
            ("第一話", 1), ("第二十三話", 23), ("第零話", 0),
            ("番外編", None), ("最終回", None),
        ],
    )
    def test_parse_episode_number(self, raw, expected):
        assert _parse_episode_number(raw) == expected

    @pytest.mark.parametrize(
        ("s", "expected"),
        [("一", 1), ("十", 10), ("十一", 11), ("二十", 20), ("二十三", 23), ("零", 0), ("百", 100)],
    )
    def test_kanji_to_int(self, s, expected):
        assert _kanji_to_int(s) == expected

    def test_air_date_and_clean_text(self):
        d, year = _parse_air_date("'''2023年'''<br />10月8日", None)
        assert (d, year) == ("2023-10-08", 2023)
        d, year = _parse_air_date("10月15日", year)
        assert (d, year) == ("2023-10-15", 2023)
        assert _parse_air_date("10月15日", None) == (None, None)
        assert clean_text("{{ubl|甲|乙}}") == "甲 乙"
        assert clean_text("[[a|b]]と[[c]]") == "bとc"
        assert clean_text("") is None


# ---------------------------------------------------------------------------
# metadata_episode_reconcile
# ---------------------------------------------------------------------------


def _resource(**overrides):
    base = dict(
        episode=13, season=4, is_batch=False,
        absolute_episode=None, episode_confidence=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestEpisodeReconcile:
    SEASONS = {1: 12, 2: 12, 3: 12, 4: 12}

    def test_per_season_kept(self):
        assert reconcile_episode(
            raw_episode=3, raw_season=1, seasons_map=self.SEASONS
        ) == (3, None, "raw")

    def test_absolute_converted(self):
        # S4 raw 39 = 12+12+12+3 (season 4 episode 3).
        assert reconcile_episode(
            raw_episode=39, raw_season=4, seasons_map=self.SEASONS
        ) == (3, 39, "reconciled")

    def test_ambiguous_overshoot(self):
        assert reconcile_episode(
            raw_episode=99, raw_season=4, seasons_map=self.SEASONS
        ) == (99, None, "ambiguous")

    def test_no_season_entry(self):
        assert reconcile_episode(
            raw_episode=3, raw_season=9, seasons_map=self.SEASONS
        ) is None

    def test_reconcile_season_one_overshoot_is_ambiguous(self):
        # Season 1 with raw > season_count and no prior seasons -> ambiguous.
        assert reconcile_episode(
            raw_episode=99, raw_season=1, seasons_map=self.SEASONS
        ) == (99, None, "ambiguous")

    def test_locate_absolute(self):
        assert locate_absolute_episode(30, self.SEASONS) == (3, 6)
        assert locate_absolute_episode(0, self.SEASONS) is None
        assert locate_absolute_episode(500, self.SEASONS) is None
        # Within the final-season tolerance headroom.
        assert locate_absolute_episode(49, self.SEASONS) == (4, 13)
        assert locate_absolute_episode(3, {}) is None

    def test_seasons_map_from_ignores_specials_and_junk(self):
        assert seasons_map_from_list([
            {"season_number": 0, "episode_count": 5},
            {"season_number": 1, "episode_count": 12},
            "junk",
            {"season_number": 2, "episode_count": 0},
            {"season_number": 3},
        ]) == {1: 12}

    def test_verified_season_count(self):
        assert verified_season_count({"number_of_seasons": 4}) == 4
        assert verified_season_count({"number_of_seasons": True}) is None  # bool is not int
        assert verified_season_count({"seasons": [
            {"season_number": 0, "episode_count": 3},
            {"season_number": 1, "episode_count": 12},
            {"season_number": 2, "episode_count": 12},
        ]}) == 2
        assert verified_season_count(None) is None
        assert verified_season_count({}) is None

    def test_apply_episode_reconcile(self):
        r = _resource(episode=39)
        assert apply_episode_reconcile(r, self.SEASONS) is True
        assert (r.episode, r.absolute_episode, r.episode_confidence) == (3, 39, "reconciled")

    def test_apply_skips_batch_and_manual(self):
        assert apply_episode_reconcile(_resource(is_batch=True), self.SEASONS) is False
        assert apply_episode_reconcile(
            _resource(episode_confidence="manual"), self.SEASONS
        ) is False

    def test_apply_derives_season_from_absolute(self):
        r = _resource(episode=None, season=None, absolute_episode=30)
        assert apply_episode_reconcile(r, self.SEASONS) is True
        assert (r.season, r.episode) == (3, 6)

    def test_apply_consistency_crosscheck_marks_ambiguous(self):
        # Title gave S4E3 but the absolute number arithmetically lands at S3E6 —
        # a conflicting double-label must flag the resource, never trust a side.
        r = _resource(episode=3, season=4, absolute_episode=30)
        assert apply_episode_reconcile(r, self.SEASONS) is True
        assert r.episode_confidence == "ambiguous"

    def test_apply_no_basis_marks_raw(self):
        r = _resource(episode=3, season=4)
        assert apply_episode_reconcile(r, {}) is False
        assert r.episode_confidence == "raw"

    def test_seasons_map_from_list(self):
        assert seasons_map_from_list([
            {"season_number": 1, "episode_count": 12},
            {"season_number": 0, "episode_count": 3},
        ]) == {1: 12}

    def test_resolve_missing_season(self):
        r = _resource(season=None, episode=None, absolute_episode=None)
        assert resolve_missing_season(r, {"number_of_seasons": 1}) == "season-defaulted"
        assert r.season == 1
        r2 = _resource(season=None)
        assert resolve_missing_season(r2, {"number_of_seasons": 3}) == "marked-ambiguous"
        assert r2.episode_confidence == "ambiguous"
        r3 = _resource(season=2)
        assert resolve_missing_season(r3, {"number_of_seasons": 1}) is None


# ---------------------------------------------------------------------------
# anime_signals
# ---------------------------------------------------------------------------


class TestAnimeSignals:
    def test_is_anime_identity(self):
        assert is_anime_identity("bangumi") is True
        assert is_anime_identity("mal") is True
        assert is_anime_identity("wikipedia", ["mal:123"]) is True
        assert is_anime_identity("tmdb") is False
        assert is_anime_identity(None) is False

    @pytest.mark.parametrize(
        ("genre_ids", "lang", "countries", "expected"),
        [
            ([16, 10765], "ja", ["JP"], True),
            ([16], "en", ["US"], None),
            ([18], "ja", ["JP"], False),
            ([], "ja", ["JP"], None),
        ],
    )
    def test_is_anime_from_tmdb(self, genre_ids, lang, countries, expected):
        assert is_anime_from_tmdb(genre_ids, lang, countries) is expected

    def test_apply_is_anime_sticky(self):
        w = SimpleNamespace(is_anime=False)
        apply_is_anime(w, {"external_source": "bangumi"})
        assert w.is_anime is True
        w2 = SimpleNamespace(is_anime=None)
        apply_is_anime(w2, {"is_anime": False})
        assert w2.is_anime is False

    def test_apply_is_anime_respects_manual_edit(self):
        w = SimpleNamespace(is_anime=False, manually_edited_fields=["is_anime"])
        apply_is_anime(w, {"external_source": "bangumi"})
        assert w.is_anime is False

    def test_bangumi_verdict(self):
        subjects = [
            {"id": 1, "name": "無職転生", "name_cn": "无职转生", "date": "2021-01-10", "type": 2},
            {"id": 2, "name": "无职转生 真人版", "name_cn": "", "date": "2024-01-01", "type": 6},
        ]
        verdict, subj = bangumi_verdict(["无职转生", "無職転生"], 2021, subjects)
        assert (verdict, subj["id"]) == (True, 1)


# ---------------------------------------------------------------------------
# metadata_wiki_classify
# ---------------------------------------------------------------------------


class TestWikiClassify:
    def test_infer_content_type(self):
        assert _infer_content_type_from_categories(["2021年日本電視動畫"]) == "tv"
        assert _infer_content_type_from_categories(["2022年日本動畫電影"]) == "movie"
        assert _infer_content_type_from_categories([]) == "tv"

    def test_classify_work(self):
        assert _classify_wikipedia_page(["2021年日本電視動畫"]) == "work"
        assert _classify_wikipedia_page(["香港電視台"]) == "non_work"
        assert _classify_wikipedia_page(["2021年日本電視動畫", "香港電視台"]) == "ambiguous"
        # Summary tiebreaker.
        assert _classify_wikipedia_page(None, "X is a television series produced by ...") == "work"
        assert _classify_wikipedia_page(None, "X is a television channel in Hong Kong") == "non_work"
        assert _classify_wikipedia_page(None, "Some unrelated text") == "ambiguous"

    def test_validate_matched_entity_kind(self):
        meta = ResourceMetadata(
            clean_title="X", found=True,
            matched_entity={
                "external_source": "wikipedia", "external_id": "wikipedia:1",
                "categories": ["香港電視台"],
            },
        )
        meta = _validate_matched_entity_kind(meta)
        assert meta.found is False
        assert meta.matched_entity is None

        # non-wikipedia source is untouched.
        meta2 = ResourceMetadata(
            clean_title="X", found=True,
            matched_entity={"external_source": "tmdb", "external_id": "tmdb:1"},
        )
        assert _validate_matched_entity_kind(meta2).found is True


# ---------------------------------------------------------------------------
# url_tools
# ---------------------------------------------------------------------------


class TestUrlTools:
    def test_normalize_url(self):
        assert normalize_url("http://Example.com/Path/?utm_source=x&a=1") == "https://example.com/Path?a=1"
        assert normalize_url("https://example.com/path/") == "https://example.com/path"
        assert normalize_url("not-a-url") is None
        assert normalize_url("") is None

    def test_keep_k_per_hostname(self):
        items = [
            {"url": "https://a.example/1"},
            {"url": "https://a.example/2"},
            {"url": "https://a.example/3"},
            {"url": "https://b.example/1"},
        ]
        assert [i["url"] for i in keep_k_per_hostname(items, 2)] == [
            "https://a.example/1", "https://a.example/2", "https://b.example/1",
        ]


# ---------------------------------------------------------------------------
# resource_parser
# ---------------------------------------------------------------------------


class TestResourceParser:
    def test_strip_season(self):
        assert strip_season_from_title("進擊的巨人 第三季") == "進擊的巨人"
        assert strip_season_from_title("Some Show Season 4") == "Some Show"
        assert strip_season_from_title("Spy x Family") == "Spy x Family"
        assert strip_season_from_title(None) is None

    def test_detect_batch(self):
        assert detect_batch("Anime Title 全集 1080p") == (True, None, None)
        assert detect_batch("Some Show S02E01-24 1080p BluRay") == (True, 1, 24)
        assert detect_batch("[LoliHouse] Show S04 - 05 [WebRip 1080p]") == (False, None, None)
        assert detect_batch("random_bytes_xyz123 1080p") == (False, None, None)
        assert detect_batch(None) == (False, None, None)

    def test_detect_absolute_episode(self):
        assert detect_absolute_episode(
            "[Group] Show S04 - 13 (85) [1080p]"
        ) == (13, 85)
        assert detect_absolute_episode("[Group] Show - 85(13) [1080p]") == (None, None)
        assert detect_absolute_episode(None) == (None, None)

    def test_detect_subtitle_langs(self):
        assert detect_subtitle_langs("[LoliHouse] Show - 05 [简繁日内封字幕]") == ["zh-CN", "zh-TW", "ja"]
        assert detect_subtitle_langs("Some Show S02E05 1080p") == []
        assert detect_subtitle_langs(None) == []

    def test_extract_episode_fallback(self):
        assert extract_episode_fallback(
            "[Nix-Raws] Mushoku Tensei S03E06 [CR WEB-DL 1080p]"
        ) == (6, 3)
        assert extract_episode_fallback("[Group] Some Work [01V2]") == (1, None)
        assert extract_episode_fallback("[Group] Some Work [1080p][2026]") == (None, None)

    def test_extract_title_year(self):
        assert extract_title_year("X 2026") == 2026
        assert extract_title_year("X [1080p]") is None

    def test_normalize_parsed_fields(self):
        out = normalize_parsed_fields(
            "[绿茶字幕组] 无职转生 第三季 / Mushoku Tensei S3 [03][WebRip][1080p]",
            {"episode": None, "season": None},
        )
        assert out["episode"] == 3
        assert out["season"] == 3

    def test_extract_compilation_work_title(self):
        assert extract_compilation_work_title(
            "[整理搬运] 猫眼三姐妹／猫之眼 (キャッツ・アイ)：TV动画+剧场版"
        ) == "猫眼三姐妹"
        assert extract_compilation_work_title(
            "[LoliHouse] 无职转生 3期 / Mushoku Tensei S3 - 03 [1080p]"
        ) is None
        assert extract_compilation_work_title(None) is None

    def test_parse_entry_field_mapping(self):
        entry = {
            "title": "[LoliHouse] Spy x Family - 12 [WebRip 1080p HEVC-10bit AAC][CHT].mkv",
            "enclosures": [{"url": "https://example.com/test.torrent", "length": "1234567"}],
            "link": "https://example.com/detail/123",
        }
        mapping = {
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
        out = parse_entry(entry, mapping)
        assert out["title_cn"] == "Spy x Family"
        assert out["subtitle_group"] == "LoliHouse"
        assert out["episode"] == 12
        assert out["resolution"] == "1080p"
        assert out["torrent_url"] == "https://example.com/test.torrent"
        assert out["file_size"] == 1234567
        assert parse_entry(entry, None) == {}


# ---------------------------------------------------------------------------
# metadata_wiki_query
# ---------------------------------------------------------------------------


class TestWikiQuery:
    def test_clean_query(self):
        assert _clean_query("无职转生 3期 - 03 [1080p]") == "无职转生"
        assert _clean_query("[SweetSub] Show S03 [CHS]") == "Show"
        assert _clean_query("") == ""

    def test_work_name_prefix(self):
        assert _work_name_prefix("Mushoku Tensei S3 - 03") == "Mushoku Tensei"
        assert _work_name_prefix("无职转生 3期") == "无职转生"

    def test_candidate_queries(self):
        qs = _candidate_queries("[LoliHouse] 无职转生 3期 - 03 [1080p]")
        assert qs, "expected candidate queries"
        assert all(isinstance(q, tuple) and len(q) == 2 for q in qs)
        # CJK fragments search zh (and ja when kana present).
        langs = {lang for _, lang in qs}
        assert "zh" in langs
        # A bare station token is dropped entirely.
        assert _candidate_queries("ViuTV") == []
        # resource pre-parser hints are preferred.
        r = SimpleNamespace(search_title="Show", title_cn=None, title_en=None)
        hints = _candidate_queries("noise", r)
        assert any(q.lower() == "show" for q, _ in hints)


# ---------------------------------------------------------------------------
# text_normalizer
# ---------------------------------------------------------------------------


class TestTextNormalizer:
    def test_normalize_title(self):
        assert normalize_title("  Foo  Bar ") == "foo bar"
        assert normalize_title(None) == ""
        assert normalize_title_denoised("[VCB-Studio] 测试剧集 [01][1080p]") == "vcb-studio 测试剧集 01 1080p"
        assert normalize_title_denoised(None) == ""

    def test_similarity_score(self):
        assert similarity_score("测试剧集", "測試劇集") == 100
        assert similarity_score(" completely different ", "xyz") < 30
        assert similarity_score("", "x") == 0
        assert partial_similarity_score("Attack on Titan", "Attack on Titan Season 4 Part 2") == 100
        assert partial_similarity_score("", "abc") == 0


# ---------------------------------------------------------------------------
# filter_engine
# ---------------------------------------------------------------------------


def _fres(**overrides):
    defaults = dict(
        subtitle_group="LoliHouse", resolution="1080p", source="WebRip",
        video_codec="HEVC", audio_codec="AAC", subtitle_type="CHS",
        container="MKV", title_cn="标题", title_en="Title",
        search_title="Title 标题", file_size=1_500_000_000, episode=3, season=1,
        is_batch=False, episode_start=None, episode_end=None,
        subtitle_langs=None, episode_confidence=None, absolute_episode=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


class TestFilterEngine:
    def test_validate(self):
        assert validate_filter_config(None) == []
        assert validate_filter_config({"combinator": "and", "conditions": [
            {"field": "resolution", "operator": "eq", "value": "1080p"},
        ]}) == []
        errs = validate_filter_config({"combinator": "and", "conditions": [
            {"field": "bogus", "operator": "eq", "value": "x"},
        ]})
        assert any("unknown field" in e for e in errs)

    def test_evaluate_string_ops(self):
        r = _fres()
        assert evaluate_field_condition(
            {"field": "resolution", "operator": "eq", "value": "1080p"}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "resolution", "operator": "ne", "value": "720p"}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "subtitle_group", "operator": "contains", "value": "Loli"}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "subtitle_group", "operator": "fuzzy", "value": "LoliHouse"}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "title_cn", "operator": "regex", "value": "^标"}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "resolution", "operator": "in", "value": ["720p", "1080p"]}, r
        ) is True

    def test_evaluate_number_ops(self):
        r = _fres()
        assert evaluate_field_condition(
            {"field": "episode", "operator": "gte", "value": 3}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "file_size", "operator": "lt", "value": 2_000_000_000}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "episode", "operator": "gt", "value": 2}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "episode", "operator": "eq", "value": 3}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "episode", "operator": "lte", "value": 5}, r
        ) is True

    def test_evaluate_bool_and_emptiness(self):
        r = _fres()
        assert evaluate_field_condition(
            {"field": "is_batch", "operator": "eq", "value": False}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "is_batch", "operator": "ne", "value": True}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "episode_start", "operator": "is_empty"}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "episode", "operator": "is_not_empty"}, r
        ) is True

    def test_evaluate_list_ops(self):
        r = _fres(subtitle_langs=["zh-CN", "zh-TW"])
        assert evaluate_field_condition(
            {"field": "subtitle_langs", "operator": "contains", "value": "zh-CN"}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "subtitle_langs", "operator": "contains", "value": "ZH-CN"}, r
        ) is True
        assert evaluate_field_condition(
            {"field": "subtitle_langs", "operator": "in", "value": ["ja", "en"]}, _fres(subtitle_langs=["zh-CN", "en"])
        ) is True
        assert evaluate_field_condition(
            {"field": "subtitle_langs", "operator": "contains", "value": "ja"}, r
        ) is False

    def test_get_field_value_work_fields(self):
        r = _fres()
        r.series = SimpleNamespace(
            rating=8.5, start_date=date(2020, 1, 1), genre=["Animation"],
            collection=SimpleNamespace(title_cn="合集名", title_en=None),
        )
        assert get_field_value(r, "episode") == 3
        assert get_field_value(r, "series.rating") == 8.5
        assert get_field_value(r, "series.year") == 2020
        assert get_field_value(r, "series.genre") == ["Animation"]
        assert get_field_value(r, "series.collection") == "合集名"
        assert loaded_relation(r, "series") is r.series
        assert loaded_relation(_fres(), "movie") is None

    def test_evaluate_filter_config_and_merge(self):
        cfg = {"combinator": "and", "conditions": [
            {"field": "resolution", "operator": "eq", "value": "1080p"},
            {"field": "episode", "operator": "eq", "value": 3},
        ]}
        assert evaluate_filter_config(cfg, _fres()) is True
        assert evaluate_filter_config(cfg, _fres(episode=4)) is False
        or_cfg = {"combinator": "or", "conditions": [
            {"field": "episode", "operator": "eq", "value": 99},
            {"field": "episode", "operator": "eq", "value": 3},
        ]}
        assert evaluate_filter_config(or_cfg, _fres()) is True
        merged = merge_filters(
            {"combinator": "and", "conditions": [
                {"field": "resolution", "operator": "eq", "value": "1080p"},
            ]},
            {"combinator": "and", "conditions": [
                {"field": "episode", "operator": "eq", "value": 3},
            ]},
        )
        assert merged["combinator"] == "and"
        assert len(merged["conditions"]) == 2
        assert merge_filters(None, None) is None
        assert merge_filters(None, {"combinator": "and", "conditions": []}) == {"combinator": "and", "conditions": []}


# ---------------------------------------------------------------------------
# wikidata_collection (pure helpers)
# ---------------------------------------------------------------------------


class TestWikidataCollection:
    def test_normalize_title(self):
        assert _normalize_title(" 攻壳机动队 SAC ") == "攻壳机动队 sac"
        assert _normalize_title(" Foo  Bar ") == "foo bar"

    def test_parse_wikipedia_url(self):
        assert _parse_wikipedia_url("https://en.wikipedia.org/wiki/Ghost_in_the_Shell") == (
            "en.wikipedia.org", "Ghost in the Shell",
        )
        assert _parse_wikipedia_url("https://zh.wikipedia.org/wiki/攻壳机动队") == (
            "zh.wikipedia.org", "攻壳机动队",
        )
        assert _parse_wikipedia_url("https://example.com/x") is None

    def test_entity_titles_and_label_match(self):
        entity = {
            "labels": {
                "en": {"value": "Ghost in the Shell"},
                "zh": {"value": "攻壳机动队"},
            },
            "aliases": {"en": [{"value": "GitS"}], "zh": [{"value": "攻壳"}]},
        }
        assert _entity_titles(entity) == {"ghost in the shell", "攻壳机动队", "gits", "攻壳"}
        assert entity_label_matches(entity, ["攻壳机动队"]) is True
        assert entity_label_matches(entity, ["Something Else"]) is False

    def test_extract_p179_qids(self):
        entity = {
            "claims": {
                "P179": [
                    {"mainsnak": {"snaktype": "value", "datavalue": {"value": {"id": "Q123"}}}},
                    {"mainsnak": {"snaktype": "somevalue", "datavalue": {"value": {"id": "Q999"}}}},
                    {"mainsnak": {"snaktype": "value", "datavalue": {"value": {"id": "Q123"}}}},
                ],
            },
        }
        assert extract_p179_qids(entity) == ["Q123"]
        assert extract_p179_qids({}) == []

    def test_entity_labels(self):
        assert entity_labels({"labels": {"zh": {"value": "中文"}, "en": {"value": "English"}}}) == (
            "中文", "English",
        )
        assert entity_labels({}) == (None, None)


# ---------------------------------------------------------------------------
# metadata_dedup (pure helpers)
# ---------------------------------------------------------------------------


def _entity(id_, cn=None, en=None, aliases=None):
    return SimpleNamespace(id=id_, title_cn=cn, title_en=en, original_title=None, aliases=aliases or [])


class TestMetadataDedup:
    def test_title_keys(self):
        assert _title_keys(_entity("1", cn="攻壳机动队", en="Ghost in the Shell")) == {
            "攻壳机动队", "ghost in the shell",
        }

    def test_cluster_by_shared_title(self):
        a = _entity("a", cn="进击的巨人")
        b = _entity("b", cn="進擊的巨人")
        c = _entity("c", cn="名侦探柯南")
        clusters = _cluster_by_shared_title([a, b, c])
        assert len(clusters) == 2
        by_id = {e.id: e for cl in clusters for e in cl}
        assert set(by_id) == {"a", "b", "c"}

    def test_merge_aliases(self):
        a = _entity("a", cn="甲", en="A", aliases=["乙"])
        b = _entity("b", cn="乙", en="B")
        assert _merge_aliases([a, b]) == ["甲", "A", "乙", "B"]


# ---------------------------------------------------------------------------
# genre_registry
# ---------------------------------------------------------------------------


class TestGenreRegistry:
    def test_normalize_genres(self):
        assert normalize_genres(["Animation", "Sci-Fi & Fantasy", "bogus"]) == [
            "Animation", "Sci-Fi & Fantasy",
        ]
        assert normalize_genres(None) == []

    def test_genre_zh(self):
        assert genre_zh("Animation") == "动画"
        assert genre_zh("Bogus") is None


# ---------------------------------------------------------------------------
# metadata_failure
# ---------------------------------------------------------------------------


class TestMetadataFailure:
    def test_classify_failure(self):
        ok = SimpleNamespace(found=True, matched_entity={"id": 1}, reason=None, search_error=None)
        assert _classify_failure(ok) is None
        # found=True but no entity -> transient.
        gap = SimpleNamespace(found=True, matched_entity=None, reason=None, search_error=None)
        assert _classify_failure(gap) == "transient"
        transient = SimpleNamespace(found=False, matched_entity=None, reason="Agent error: boom", search_error=None)
        assert _classify_failure(transient) == "transient"
        non_work = SimpleNamespace(found=False, matched_entity=None, reason="not a tv show", search_error=None)
        assert _classify_failure(non_work) == "non_work"
        nf = SimpleNamespace(found=False, matched_entity=None, reason="no match", search_error=None)
        assert _classify_failure(nf) == "not_found"

    def test_record_metadata_attempt(self):
        r = SimpleNamespace(metadata_attempts=0, last_metadata_attempt_at=None, metadata_failure_type=None)
        _record_metadata_attempt(r, SimpleNamespace(found=True, matched_entity={"id": 1}, reason=None, search_error=None))
        assert r.metadata_attempts == 1
        assert r.metadata_failure_type is None
