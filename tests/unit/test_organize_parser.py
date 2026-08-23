"""文件名解析器（organize_parser，移植自 vault-organizer）单元测试。"""

from __future__ import annotations

from app.services.organize_parser import (
    FileKind,
    classify,
    detect_subtitle_flags,
    detect_subtitle_lang,
    is_audio,
    parse_episode,
    parse_season_from_path,
)


class TestClassify:
    def test_video(self):
        assert classify("Show.S01E04.1080p.mkv") == FileKind.VIDEO
        assert classify("movie.MP4") == FileKind.VIDEO

    def test_subtitle(self):
        assert classify("Show.S01E04.chs.ass") == FileKind.SUBTITLE
        assert classify("movie.srt") == FileKind.SUBTITLE
        assert classify("movie.PGS.sup") == FileKind.SUBTITLE
        assert classify("movie.idx") == FileKind.SUBTITLE

    def test_audio_sidecar(self):
        assert is_audio("track.truehd")
        assert is_audio("commentary.MKA")
        assert not is_audio("movie.mkv")

    def test_other(self):
        assert classify("info.nfo") == FileKind.OTHER
        assert classify("poster.jpg") == FileKind.OTHER
        assert classify("README") == FileKind.OTHER


class TestParseEpisode:
    def test_sxxexx(self):
        assert parse_episode("Show.S04E09.1080p.mkv") == (4, 9)
        assert parse_episode("show.s1e12.mkv") == (1, 12)

    def test_ep_token(self):
        assert parse_episode("Show E09.mkv") == (None, 9)
        assert parse_episode("Show.EP09.mkv") == (None, 9)
        assert parse_episode("Show - e09v2.mkv") == (None, 9)

    def test_cjk_episode(self):
        assert parse_episode("某动画 第09話.mkv") == (None, 9)
        assert parse_episode("某剧 第12集.mkv") == (None, 12)

    def test_bracket_episode(self):
        # fansub 裸方括号集号（与 resource_parser._BRACKET_EPISODE_RE 对齐）
        assert parse_episode(
            "[Kisssub][Tefuda ga Oome no Victoria][1080P][BIG5][01][MP4].mp4"
        ) == (None, 1)
        assert parse_episode("[Group] Title [12v2].mkv") == (None, 12)

    def test_bracket_tech_tags_not_episode(self):
        # 4 位年份 / 带字母的技术标签不匹配裸方括号集号
        assert parse_episode("Movie [2026].mkv") == (None, None)
        assert parse_episode("[Group] Title [1080P].mkv") == (None, None)

    def test_anime_dash(self):
        assert parse_episode("Title - 09 (1080p).mkv") == (None, 9)
        assert parse_episode("Title - 09v2 [ABC123].mkv") == (None, 9)

    def test_unparseable(self):
        assert parse_episode("Making of the show.mkv") == (None, None)


class TestParseSeasonFromPath:
    def test_season_word_dir(self):
        assert parse_season_from_path("Season 2/Show - 01.mkv") == 2

    def test_sxx_dir(self):
        assert parse_season_from_path("S02/Show - 01.mkv") == 2

    def test_no_season_dir(self):
        assert parse_season_from_path("Show - 01.mkv") is None


class TestDetectSubtitleLang:
    def test_common_tokens(self):
        assert detect_subtitle_lang("Show.S01E04.chs.ass") == "zh-CN"
        assert detect_subtitle_lang("Show.S01E04.简体.ass") == "zh-CN"
        assert detect_subtitle_lang("Show.S01E04.cht.ass") == "zh-TW"
        assert detect_subtitle_lang("Show.S01E04.繁體.srt") == "zh-TW"
        assert detect_subtitle_lang("Show.S01E04.jpn.srt") == "ja"
        assert detect_subtitle_lang("Show.S01E04.eng.srt") == "en"
        assert detect_subtitle_lang("Movie UHD BluRay French Forced.sup") == "fr"
        assert detect_subtitle_lang("Movie UHD BluRay German Forced.sup") == "de"
        assert detect_subtitle_lang("Movie UHD BluRay Italian Forced.sup") == "it"
        assert detect_subtitle_lang("Movie UHD BluRay Spanish Forced.sup") == "es"

    def test_word_boundary(self):
        # "en" 是 "Title" 的一部分，不应命中
        assert detect_subtitle_lang("Golden.Kamuy.srt") is None

    def test_unknown(self):
        assert detect_subtitle_lang("Show.S01E04.srt") is None


class TestDetectSubtitleFlags:
    def test_plex_flags(self):
        assert detect_subtitle_flags("movie.eng.forced.sup") == ("forced",)
        assert detect_subtitle_flags("movie.en.SDH.srt") == ("sdh",)
        assert detect_subtitle_flags("movie.en.cc.ass") == ("cc",)
