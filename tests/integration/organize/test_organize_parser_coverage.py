"""organize_parser 确定性解析的集成侧补覆盖测试。

纯函数模块（文件名分类 / 集号季号解析 / 字幕语言与标志识别），不依赖 DB。
补齐集成运行下未覆盖的分支：OTHER 分类、is_audio、parse_episode 各回退
分支、parse_season_from_path 目录季号、非 ASCII 语言 token、字幕 flags。
"""

from __future__ import annotations

import pytest

from app.services.organize_parser import (
    FileKind,
    classify,
    detect_subtitle_flags,
    detect_subtitle_lang,
    is_audio,
    parse_episode,
    parse_season_from_path,
)

# ---------------------------------------------------------------- 分类


def test_classify_video_subtitle_other():
    assert classify("Show.S01E04.MKV") is FileKind.VIDEO  # 大小写不敏感
    assert classify("ep04.ass") is FileKind.SUBTITLE
    assert classify("readme.nfo") is FileKind.OTHER
    assert classify("no_extension") is FileKind.OTHER


def test_is_audio():
    assert is_audio("Show.S01E04.flac") is True
    assert is_audio("track.AC3") is True
    assert is_audio("Show.S01E04.mkv") is False


# ---------------------------------------------------------------- 集号解析


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Show.S04E09.1080p.mkv", (4, 9)),
        ("Show E09.mkv", (None, 9)),
        ("Show.EP09.v2.mkv", (None, 9)),
        ("[Group] Show 第09話 [1080p].mkv", (None, 9)),
        ("[Kisssub][Title][01][MP4].mp4", (None, 1)),
        ("Title [1080P] [09v2].mkv", (None, 9)),
        ("Title - 09 (1080p).mkv", (None, 9)),
        ("Title - 12 [BDRip].mkv", (None, 12)),
        ("plain movie.mkv", (None, None)),
    ],
)
def test_parse_episode_branches(name, expected):
    assert parse_episode(name) == expected


def test_parse_episode_technical_tags_not_episode():
    """[1080P] 这类技术标签不会被当作裸方括号集号。"""
    assert parse_episode("Title [1080P].mkv") == (None, None)


# ---------------------------------------------------------------- 目录季号


def test_parse_season_from_path():
    assert parse_season_from_path("Season 2/ep01.mkv") == 2
    assert parse_season_from_path("S02/ep01.mkv") == 2
    assert parse_season_from_path("show.S01/Season 10/ep01.mkv") == 10
    assert parse_season_from_path("ep01.mkv") is None
    assert parse_season_from_path("SeasonX/ep01.mkv") is None


# ---------------------------------------------------------------- 字幕语言


def test_detect_subtitle_lang_ascii_token():
    assert detect_subtitle_lang("ep04.chs.srt") == "zh-CN"
    assert detect_subtitle_lang("ep04.CHT.ass") == "zh-TW"
    assert detect_subtitle_lang("ep04.jpn.srt") == "ja"


def test_detect_subtitle_lang_cjk_token():
    """非 ASCII token 走子串匹配分支。"""
    assert detect_subtitle_lang("ep04.简体.srt") == "zh-CN"
    assert detect_subtitle_lang("ep04.繁體.ass") == "zh-TW"


def test_detect_subtitle_lang_no_match():
    assert detect_subtitle_lang("ep04.srt") is None
    # 嵌入更长词中的片段不算命中（ascii token 需边界）
    assert detect_subtitle_lang("ep04.scandinavian.srt") is None


# ---------------------------------------------------------------- 字幕 flags


def test_detect_subtitle_flags():
    assert detect_subtitle_flags("ep04.forced.sdh.srt") == ("forced", "sdh")
    assert detect_subtitle_flags("ep04.force.cc.srt") == ("forced", "cc")
    assert detect_subtitle_flags("ep04.sdh.srt") == ("sdh",)
    assert detect_subtitle_flags("ep04.srt") == ()
