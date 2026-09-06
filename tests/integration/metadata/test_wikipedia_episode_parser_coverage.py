"""Coverage-oriented integration tests for app/services/wikipedia_episode_parser.py.

Complements tests/integration/test_metadata_core_integration.py (which covers
the happy paths with real fixtures) by exercising error/boundary branches:
kanji numeral failures, template splitting with ``=`` inside links/templates,
unparseable chapter labels, infobox presence predicates, and the whole
broadcast-date parsing pipeline (parse_season_air_dates and helpers).

All functions under test are pure — no DB, no IO.
"""

from __future__ import annotations

from app.services.wikipedia_episode_parser import (
    _kanji_to_int,
    _parse_air_date,
    clean_text,
    has_animanga_film_infobox,
    has_tvanime_infobox,
    parse_episode_list,
    parse_season_air_dates,
    parse_seasons_from_infobox,
)

# ---------------------------------------------------------------------------
# Small helpers / edge inputs
# ---------------------------------------------------------------------------


class TestKanjiAndTemplates:
    def test_kanji_invalid_and_empty(self):
        assert _kanji_to_int("") is None
        # Unknown characters abort the parse instead of guessing.
        assert _kanji_to_int("X") is None
        assert _kanji_to_int("百零五") == 105

    def test_template_without_args_is_dropped(self):
        # {{lang}} / {{ubl}} with no parameters resolve to empty text.
        assert clean_text("{{lang}}{{ubl}}") is None

    def test_template_display_text_variants(self):
        assert clean_text("{{small|甲}}") == "甲"
        # {{lang|ja|...}} keeps the last parameter (the wrapped text).
        assert clean_text("{{lang|ja|テキスト}}") == "テキスト"
        # Unknown templates (citations/notes) carry no episode content.
        assert clean_text("{{Sfnp|a|b}}") is None

    def test_air_date_invalid_month_day(self):
        # A syntactically matched but out-of-range month/day yields no date,
        # while the running year is still carried forward.
        assert _parse_air_date("'''2023年'''<br />13月40日", None) == (None, 2023)


class TestSegmentSplitting:
    def test_equals_inside_wikilink_and_template_not_split(self):
        # ``=`` inside [[...]] and {{...}} must not be treated as the
        # name/value separator of a template parameter.
        wt = """=== 各話リスト ===
{{エピソードリスト/base
| Number = 第1話
| Title = [[File:a=b|x]] [[標題]]
| Aux5 = {{ubl|a=b|2024年1月7日}}
}}
"""
        data = parse_episode_list(wt)
        assert data is not None
        ep = data["episodes"][0]
        assert ep["title"] == "x 標題"
        assert ep["air_date"] == "2024-01-07"

    def test_positional_segments_with_nested_equals_are_ignored(self):
        # Positional (nameless) segments containing ``=`` inside nested
        # templates/wikilinks must not be mistaken for name=value params.
        wt = """=== 各話リスト ===
{{エピソードリスト/base
| {{0|a=b}}
| [[File:c=d]]
| Number = 第1話
}}
"""
        data = parse_episode_list(wt)
        assert data is not None
        assert data["episodes"][0]["episode"] == 1


class TestEpisodeListBoundaries:
    def test_unparseable_chapter_label_keeps_current_season(self):
        wt = """=== 各話列表 ===
{{劇集列表/base
| Chapter = 第1季
}}
{{劇集列表/base
| Chapter = 番外編
}}
{{劇集列表/base
| Number = 第1話
}}
"""
        data = parse_episode_list(wt)
        assert data is not None
        # 番外編 is unparseable: ignored, rows stay in season 1.
        assert data["episodes"][0]["season"] == 1

    def test_section_with_only_numberless_templates_returns_none(self):
        # Rows without a Number (番外編-style unaired rows) are skipped; a
        # section with no parseable rows yields None.
        wt = """=== 各話列表 ===
{{劇集列表/base
| Title = 番外篇
}}
"""
        assert parse_episode_list(wt) is None

    def test_falsy_wikitext_returns_none(self):
        assert parse_episode_list(None) is None
        assert parse_episode_list("") is None
        # Truthy page without an episode-list section also yields None.
        assert parse_episode_list("== 概要 ==\n本文") is None

    def test_same_level_heading_ends_section(self):
        wt = """=== 各話列表 ===
{{劇集列表/base
| Number = 第1話
}}
=== 外部連結 ===
{{劇集列表/base
| Number = 第2話
}}
"""
        data = parse_episode_list(wt)
        assert data is not None
        assert [e["episode"] for e in data["episodes"]] == [1]

    def test_deeper_heading_does_not_end_section(self):
        wt = """=== 各話列表 ===
==== 本編 ====
{{劇集列表/base
| Number = 第1話
}}
"""
        data = parse_episode_list(wt)
        assert data is not None
        assert len(data["episodes"]) == 1


class TestInfoboxPredicates:
    def test_tvanime_infobox_presence(self):
        assert has_tvanime_infobox(None) is False
        assert has_tvanime_infobox("") is False
        assert has_tvanime_infobox("{{Infobox animanga/TVAnime\n| 話数 = 全12話\n}}") is True
        # A Novel block alone does not count as a TV anime page.
        assert has_tvanime_infobox("{{Infobox animanga/Novel\n}}") is False

    def test_film_infobox_presence(self):
        assert has_animanga_film_infobox(None) is False
        assert has_animanga_film_infobox("") is False
        assert has_animanga_film_infobox("{{Infobox animanga/Movie\n}}") is True
        assert has_animanga_film_infobox("{{Infobox animanga/OVA\n}}") is True
        assert has_animanga_film_infobox("{{Infobox animanga/TVAnime\n}}") is False

    def test_parse_seasons_falsy_input(self):
        assert parse_seasons_from_infobox(None) is None
        assert parse_seasons_from_infobox("") is None


# ---------------------------------------------------------------------------
# Broadcast dates: parse_season_air_dates + helpers
# ---------------------------------------------------------------------------


class TestSeasonAirDates:
    def test_falsy_and_no_tv_block(self):
        assert parse_season_air_dates(None) is None
        assert parse_season_air_dates("") is None
        assert parse_season_air_dates("== 概要 ==\n本文") is None

    def test_plain_single_block_full_range(self):
        wt = """{{Infobox animanga/TVAnime
| 播放開始 = 2019年10月2日－12月25日
}}
"""
        assert parse_season_air_dates(wt) == {
            1: {"air_date": "2019-10-02", "end_date": "2019-12-25"},
        }

    def test_labelled_ubl_items_map_per_season(self):
        wt = """{{Infobox animanga/TVAnime
| 播放開始 = {{ubl|第一季：2019年10月2日－12月25日|第二季：2020年4月5日－6月21日}}
}}
"""
        assert parse_season_air_dates(wt) == {
            1: {"air_date": "2019-10-02", "end_date": "2019-12-25"},
            2: {"air_date": "2020-04-05", "end_date": "2020-06-21"},
        }

    def test_end_field_inherits_start_year_and_rolls_over_new_year(self):
        wt = """{{Infobox animanga/TVAnime
| 放送開始 = 2021年10月4日
| 放送終了 = 3月28日
}}
"""
        # 3月 < start month 10月 → the end rolls over into 2022.
        assert parse_season_air_dates(wt) == {
            1: {"air_date": "2021-10-04", "end_date": "2022-03-28"},
        }

    def test_month_only_fragments_degrade_to_period_bounds(self):
        wt = """{{Infobox animanga/TVAnime
| 播放開始 = 2012年4月
| 播放結束 = 2012年9月
}}
"""
        assert parse_season_air_dates(wt) == {
            1: {"air_date": "2012-04-01", "end_date": "2012-09-30"},
        }

    def test_year_only_fragments_degrade_to_year_bounds(self):
        wt = """{{Infobox animanga/TVAnime
| 播放開始 = 1998年
| 播放結束 = 1999年
}}
"""
        assert parse_season_air_dates(wt) == {
            1: {"air_date": "1998-01-01", "end_date": "1999-12-31"},
        }

    def test_invalid_fragments_yield_nothing(self):
        wt = """{{Infobox animanga/TVAnime
| 播放開始 = 2019年13月40日
| 播放結束 = 13月
}}
"""
        assert parse_season_air_dates(wt) is None

    def test_end_field_month_day_without_known_start_is_unparseable(self):
        wt = """{{Infobox animanga/TVAnime
| 放送終了 = 6月28日
}}
"""
        # No start year anywhere to inherit → no usable date.
        assert parse_season_air_dates(wt) is None

    def test_end_field_month_only_without_known_start_is_unparseable(self):
        wt = """{{Infobox animanga/TVAnime
| 放送終了 = 6月
}}
"""
        assert parse_season_air_dates(wt) is None

    def test_dateless_fragment_is_unparseable(self):
        wt = """{{Infobox animanga/TVAnime
| 放送終了 = 不定期
}}
"""
        assert parse_season_air_dates(wt) is None

    def test_month_only_end_rolls_over_new_year(self):
        wt = """{{Infobox animanga/TVAnime
| 放送開始 = 2012年10月
| 放送終了 = 3月
}}
"""
        # Month-only end fragments also roll over when before the start month.
        assert parse_season_air_dates(wt) == {
            1: {"air_date": "2012-10-01", "end_date": "2013-03-31"},
        }

    def test_empty_items_between_separators_are_skipped(self):
        wt = """{{Infobox animanga/TVAnime
| 放送開始 = {{ubl|第1期：2018年10月4日|}}
}}
"""
        assert parse_season_air_dates(wt) == {1: {"air_date": "2018-10-04"}}

    def test_label_only_item_sets_pending_season(self):
        wt = """{{Infobox animanga/TVAnime
| 放送開始 = 第1期：<br />2018年10月4日－12月27日
}}
"""
        assert parse_season_air_dates(wt) == {
            1: {"air_date": "2018-10-04", "end_date": "2018-12-27"},
        }

    def test_open_range_is_a_start_not_an_end(self):
        wt = """{{Infobox animanga/TVAnime
| 放送開始 = 2026年8月12日 -
}}
"""
        assert parse_season_air_dates(wt) == {1: {"air_date": "2026-08-12"}}

    def test_open_range_in_end_field_counts_as_start(self):
        wt = """{{Infobox animanga/TVAnime
| 放送終了 = 2026年 -
}}
"""
        # A dangling separator marks an open range even in the end field —
        # the date is a start, never an end.
        assert parse_season_air_dates(wt) == {1: {"air_date": "2026-01-01"}}

    def test_range_misplaced_into_end_field(self):
        wt = """{{Infobox animanga/TVAnime
| 放送開始 = 第1期：2012年4月10日－6月26日
| 放送終了 = 第2期：2013年4月 - 6月
}}
"""
        assert parse_season_air_dates(wt) == {
            1: {"air_date": "2012-04-10", "end_date": "2012-06-26"},
            2: {"air_date": "2013-04-01", "end_date": "2013-06-30"},
        }

    def test_cross_field_continuation_closes_incomplete_season(self):
        # Re:Zero shape: an unlabelled end item closes the one marked season
        # that still lacks an end; a later split-cour range keeps the
        # earliest air_date and the latest end_date.
        wt = """{{Infobox animanga/TVAnime
| 放送開始 = {{ubl|第1期：2018年10月4日－12月27日|第2期前半クール：2020年7月8日}}
| 放送終了 = 9月19日<br />第2期後半クール：2021年1月6日－3月24日
}}
"""
        assert parse_season_air_dates(wt) == {
            1: {"air_date": "2018-10-04", "end_date": "2018-12-27"},
            2: {"air_date": "2020-07-08", "end_date": "2021-03-24"},
        }

    def test_marked_dates_win_over_plain_blocks(self):
        # When any block carries season labels, plain dates in other blocks
        # belong to separately-titled works and are ignored.
        wt = """{{Infobox animanga/TVAnime
| 播放開始 = 第1季：2019年10月2日
}}
{{Infobox animanga/TVAnime
| 播放開始 = 2021年4月1日
}}
"""
        assert parse_season_air_dates(wt) == {1: {"air_date": "2019-10-02"}}

    def test_multiple_plain_blocks_are_ambiguous(self):
        wt = """{{Infobox animanga/TVAnime
| 播放開始 = 2019年10月2日
}}
{{Infobox animanga/TVAnime
| 播放開始 = 2021年4月1日
}}
"""
        assert parse_season_air_dates(wt) is None
