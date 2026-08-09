"""Unit tests for the deterministic Wikipedia episode parser (P2).

Fixtures under ``tests/unit/fixtures/wikipedia/`` are trimmed REAL wikitext
samples (infobox 話數 fields + the full 各話列表/各話リスト section) fetched
from zh/ja Wikipedia on 2026-08.
"""

from __future__ import annotations

import pathlib

import pytest

from app.services.wikipedia_episode_parser import (
    _kanji_to_int,
    _parse_air_date,
    _parse_episode_number,
    clean_text,
    parse_episode_list,
    parse_seasons_from_infobox,
)

FIXTURES = pathlib.Path(__file__).parent / "fixtures" / "wikipedia"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Real-sample fixtures
# ---------------------------------------------------------------------------


class TestZh100gfRealSample:
    """超超超超超喜歡你的100個女朋友 (zh) - the design's reference page."""

    wt = _load("zh_100gf.wikitext")

    def test_infobox_seasons(self):
        # Infobox only listed seasons 1-2 at fetch time (season 3 ongoing).
        assert parse_seasons_from_infobox(self.wt) == [
            {"season_number": 1, "episode_count": 12},
            {"season_number": 2, "episode_count": 12},
        ]

    def test_episode_list_three_chapters(self):
        data = parse_episode_list(self.wt)
        assert data is not None
        assert data["seasons"] == [
            {"season_number": 1, "episode_count": 12},
            {"season_number": 2, "episode_count": 12},
            {"season_number": 3, "episode_count": 6},
        ]
        assert len(data["episodes"]) == 30

    def test_absolute_numbering_rebased_per_chapter(self):
        data = parse_episode_list(self.wt)
        # Season 2 rows are numbered 第13話..第24話 in wikitext (absolute) and
        # must be rebased to per-season episode numbers.
        s2 = [e for e in data["episodes"] if e["season"] == 2]
        assert [e["episode"] for e in s2] == list(range(1, 13))

    def test_row_content(self):
        data = parse_episode_list(self.wt)
        ep1 = data["episodes"][0]
        assert ep1 == {
            "season": 1,
            "episode": 1,
            "title": "超超超超超喜歡你的 2個女朋友（還有98人）",
            "subtitle": "君のことが大大大大大好きな 2人の彼女（あと98人）",
            "air_date": "2023-10-08",
        }
        # Continuing-year Aux5 form (no year marker) reuses the running year.
        ep2 = data["episodes"][1]
        assert ep2["air_date"] == "2023-10-15"
        # Season 2 first row carries its own year marker.
        s2_first = next(e for e in data["episodes"] if e["season"] == 2)
        assert s2_first["air_date"] == "2025-01-12"
        assert s2_first["title"] == "她的名字。"
        # {{ruby|彼|カノ}} keeps the base text.
        assert s2_first["subtitle"] == "彼の名は。"

    def test_footnote_wrapped_row(self):
        # One real row is wrapped as {{^|劇集列表/base | Number = 第30話 ...}}
        # and has an empty Title - it must still parse.
        data = parse_episode_list(self.wt)
        ep30 = [e for e in data["episodes"] if e["season"] == 3][-1]
        assert ep30["episode"] == 6
        assert ep30["title"] is None
        assert ep30["subtitle"] == "紅葉ちゃんのもみもみフェスティバル"
        assert ep30["air_date"] == "2026-08-09"


class TestJaMushokuRealSample:
    """無職転生 (ja) - エピソードリスト variant, kanji numerals, <br/> infobox."""

    wt = _load("ja_mushoku.wikitext")

    def test_infobox_br_separated(self):
        assert parse_seasons_from_infobox(self.wt) == [
            {"season_number": 1, "episode_count": 23},
            {"season_number": 2, "episode_count": 25},
        ]

    def test_kanji_numbers_and_chapters(self):
        data = parse_episode_list(self.wt)
        assert data is not None
        assert data["seasons"] == [
            {"season_number": 1, "episode_count": 22},
            {"season_number": 2, "episode_count": 25},
            {"season_number": 3, "episode_count": 6},
        ]
        ep1 = data["episodes"][0]
        assert ep1["season"] == 1 and ep1["episode"] == 1
        assert ep1["title"] == "無職転生"  # {{Sfnp|...}} citation stripped
        assert ep1["air_date"] == "2021-01-11"

    def test_episode_zero_kept(self):
        # Season 2 starts at 第零話; chapters starting at 0/1 keep printed
        # numbers (no rebasing).
        data = parse_episode_list(self.wt)
        s2 = [e for e in data["episodes"] if e["season"] == 2]
        assert s2[0]["episode"] == 0
        assert s2[0]["title"] == "守護術師フィッツ"
        assert s2[0]["air_date"] == "2023-07-03"
        assert s2[1]["episode"] == 1

    def test_bangaihen_row_skipped(self):
        # The 番外編 row has no parseable Number and is skipped, which is why
        # season 1 has 22 rows despite the infobox's 全23話+番外編1話.
        data = parse_episode_list(self.wt)
        s1 = [e for e in data["episodes"] if e["season"] == 1]
        assert all(e["title"] != "エリスのゴブリン討伐" for e in s1)
        assert len(s1) == 22


class TestJa100gfRealSample:
    """君のことが大大大大大好きな100人の彼女 (ja) - same work as zh fixture."""

    wt = _load("ja_100gf.wikitext")

    def test_infobox(self):
        assert parse_seasons_from_infobox(self.wt) == [
            {"season_number": 1, "episode_count": 12},
            {"season_number": 2, "episode_count": 12},
        ]

    def test_episode_list(self):
        data = parse_episode_list(self.wt)
        assert data is not None
        ep1 = data["episodes"][0]
        assert ep1["title"] == "君のことが大大大大大好きな 2人の彼女（あと98人）"
        assert ep1["air_date"] == "2023-10-08"
        # {{nobr|10月15日}}-wrapped continuing date.
        assert data["episodes"][1]["air_date"] == "2023-10-15"


class TestJaGitsRealSample:
    """攻殻機動隊 STAND ALONE COMPLEX (ja) - no 各話リスト section."""

    wt = _load("ja_gits.wikitext")

    def test_no_episode_section(self):
        assert parse_episode_list(self.wt) is None

    def test_plain_count_infobox(self):
        # ``|話数=全26話`` (no season marker, no spaces around '=') reads as a
        # single season. The manga 全5話 field follows and is ignored.
        assert parse_seasons_from_infobox(self.wt) == [
            {"season_number": 1, "episode_count": 26},
        ]


# ---------------------------------------------------------------------------
# Synthetic variants
# ---------------------------------------------------------------------------


class TestZhBookwormRealSample:
    """小書痴的下剋上 (zh) - novel 話數 decoy + TWO TVAnime blocks.

    Regression fixture for the multi-infobox bug: the web-novel block's
    全677話 must never be read as TV seasons. The TV blocks use the 集數
    field with kanji season ordinals (第一季：全14話 ...); the second TVAnime
    block (領主的養女, a separately-titled sequel) carries only a plain
    全24話預定 and is ignored because season-marked entries exist.
    """

    wt = _load("zh_bookworm.wikitext")

    def test_tv_block_only_extraction(self):
        assert parse_seasons_from_infobox(self.wt) == [
            {"season_number": 1, "episode_count": 14},
            {"season_number": 2, "episode_count": 12},
            {"season_number": 3, "episode_count": 10},
        ]


class TestZhSlimeRealSample:
    """關於我轉生變成史萊姆這檔事 (zh) - NO TVAnime block on the main page
    (the anime lives on a sub-page). Manga 話數 (122話/全51話) must not be
    misread as a single TV season."""

    wt = _load("zh_slime.wikitext")

    def test_no_tv_block_returns_none(self):
        assert parse_seasons_from_infobox(self.wt) is None


class TestInfoboxVariants:
    def test_simplified_and_fullwidth(self):
        wt = "{{Infobox animanga/TVAnime\n| 话数 = {{ubl|第１季:共１２话|第２季：全13話}}\n}}"
        assert parse_seasons_from_infobox(wt) == [
            {"season_number": 1, "episode_count": 12},
            {"season_number": 2, "episode_count": 13},
        ]

    def test_ja_ki_marker(self):
        wt = ("{{Infobox animanga/TVAnime\n"
              "| 話数 = 第1期：全23話+番外編1話<br />第2期：全25話\n}}")
        assert parse_seasons_from_infobox(wt) == [
            {"season_number": 1, "episode_count": 23},
            {"season_number": 2, "episode_count": 25},
        ]

    def test_no_season_marker(self):
        wt = "{{Infobox animanga/TVAnime\n| 話數 = 全12話\n}}"
        assert parse_seasons_from_infobox(wt) == [
            {"season_number": 1, "episode_count": 12}
        ]

    def test_kanji_season_ordinals(self):
        wt = ("{{Infobox animanga/TVAnime\n"
              "| 集數 = 第一季：全14話 <br />第二季：全12話 <br />第三季：全10話\n}}")
        assert parse_seasons_from_infobox(wt) == [
            {"season_number": 1, "episode_count": 14},
            {"season_number": 2, "episode_count": 12},
            {"season_number": 3, "episode_count": 10},
        ]

    def test_manga_block_ignored(self):
        # Manga/Novel blocks' 話數 must not leak into TV seasons.
        wt = ("{{Infobox animanga/Novel\n| 話數 = 全677話（web）\n}}"
              "{{Infobox animanga/Manga\n| 話數 = 全51話\n}}"
              "{{Infobox animanga/TVAnime\n| 話數 = {{ubl|第1季：全24話|第2季：全24話}}\n}}")
        assert parse_seasons_from_infobox(wt) == [
            {"season_number": 1, "episode_count": 24},
            {"season_number": 2, "episode_count": 24},
        ]

    def test_no_tv_block_returns_none(self):
        wt = ("{{Infobox animanga/Novel\n| 話數 = 全677話（web）\n}}"
              "{{Infobox animanga/Manga\n| 話數 = 全51話\n}}")
        assert parse_seasons_from_infobox(wt) is None

    def test_bare_field_without_infobox_returns_none(self):
        # 話數 outside any Infobox animanga structure is not TV evidence.
        assert parse_seasons_from_infobox("| 話數 = 253話{{Small|（2026年8月）}}") is None
        assert parse_seasons_from_infobox("") is None
        assert parse_seasons_from_infobox(None) is None

    def test_multiple_plain_tv_blocks_ambiguous(self):
        # Two plain-count TVAnime blocks are separate works - refuse to guess.
        wt = ("{{Infobox animanga/TVAnime\n| 話數 = 全24話\n}}"
              "{{Infobox animanga/TVAnime\n| 集數 = 全24話預定\n}}")
        assert parse_seasons_from_infobox(wt) is None


class TestEpisodeListVariants:
    def test_no_chapter_markers_single_season(self):
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
        assert [e["episode"] for e in data["episodes"]] == [1, 2]
        assert data["episodes"][1]["air_date"] == "2024-01-08"

    def test_no_section_returns_none(self):
        assert parse_episode_list("== 概要 ==\n本文") is None
        assert parse_episode_list(None) is None

    def test_section_ends_at_next_heading(self):
        wt = """=== 各話列表 ===
{{劇集列表/base
| Number = 第1話
| Title = 甲
}}
=== 播放平台 ===
{{劇集列表/base
| Number = 第99話
| Title = 不應收錄
}}
"""
        data = parse_episode_list(wt)
        assert len(data["episodes"]) == 1

    def test_wikilinks_in_titles(self):
        wt = """== 各話リスト ==
{{エピソードリスト/base
| Number = 第1話
| Title = [[騎士]]と[[魔法|魔術]]
| Subtitle = [[ナイト|夜]]の話
}}
"""
        data = parse_episode_list(wt)
        ep = data["episodes"][0]
        assert ep["title"] == "騎士と魔術"
        assert ep["subtitle"] == "夜の話"


class TestHelpers:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("第1話", 1),
            ("第13話", 13),
            ("3", 3),
            ("第１２話", 12),
            ("第一話", 1),
            ("第二十三話", 23),
            ("第零話", 0),
            ("第十話", 10),
            ("番外編", None),
            ("最終回", None),
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

    def test_air_date_year_carryover(self):
        d, year = _parse_air_date("'''2023年'''<br />10月8日", None)
        assert (d, year) == ("2023-10-08", 2023)
        d, year = _parse_air_date("10月15日", year)
        assert (d, year) == ("2023-10-15", 2023)
        d, year = _parse_air_date("{{nobr|'''2025年'''<br />1月12日}}", year)
        assert (d, year) == ("2025-01-12", 2025)

    def test_air_date_no_year(self):
        assert _parse_air_date("10月15日", None) == (None, None)
        assert _parse_air_date("{{Efn2|未放送}}", 2023) == (None, 2023)

    def test_clean_text(self):
        assert clean_text("{{ubl|甲|乙}}") == "甲 乙"
        assert clean_text("{{lang|ja|{{ubl|a|b}}}}") == "a b"
        assert clean_text("[[a|b]]と[[c]]") == "bとc"
        assert clean_text("'''bold'''") == "bold"
        assert clean_text("{{Sfnp|x|2022|p=6}}題") == "題"
        assert clean_text("") is None
        assert clean_text(None) is None
