"""Unit tests for RSS title parser."""

import pytest
from app.services.title_parser import parse_title, ParsedTitle


class TestParseTitle:
    """Test title parsing with real mikanani RSS examples."""

    def test_lolihouse_standard(self):
        title = "[LoliHouse] 黄泉使者 / Yomi no Tsugai - 12 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕]"
        result = parse_title(title, "[LoliHouse] 黄泉使者 / Yomi no Tsugai - 12 [WebRip 1080p HEVC-10bit AAC][简繁内封字幕][685.16 MB]")

        assert result.subtitle_group == "LoliHouse"
        assert result.title_cn == "黄泉使者"
        assert result.title_en == "Yomi no Tsugai"
        assert result.episode == 12
        assert result.resolution == "1080p"
        assert result.video_codec == "HEVC-10bit"
        assert result.audio_codec == "AAC"
        assert result.subtitle_type == "简繁内封字幕"
        assert result.file_size == int(685.16 * 1024 * 1024)

    def test_skymoon_raws(self):
        title = "[Skymoon-Raws] 黄泉双使 (黄泉使者) / Daemons of the Shadow Realm - 12 [ViuTV][WEB-DL][CHT][1080p][AVC AAC]"
        result = parse_title(title)

        assert result.subtitle_group == "Skymoon-Raws"
        assert result.title_cn == "黄泉双使 (黄泉使者)"
        assert result.title_en == "Daemons of the Shadow Realm"
        assert result.episode == 12
        assert result.resolution == "1080p"
        assert result.source in ("WEB-DL", "ViuTV")  # Both are valid sources, parser picks first
        assert result.subtitle_type == "CHT"
        assert result.video_codec == "AVC"
        assert result.audio_codec == "AAC"

    def test_ani_format(self):
        title = "[ANi] Daemons of the Shadow Realm /  黄泉使者 - 12 [1080P][Baha][WEB-DL][AAC AVC][CHT][MP4]"
        result = parse_title(title)

        assert result.subtitle_group == "ANi"
        assert result.title_en == "Daemons of the Shadow Realm"
        assert result.title_cn == "黄泉使者"
        assert result.episode == 12
        assert result.resolution == "1080p"
        assert result.source in ("WEB-DL", "Baha")  # Baha is a source tag
        assert result.container == "MP4"
        assert result.subtitle_type == "CHT"

    def test_subtitle_group_extraction(self):
        result = parse_title("[SubGroup] Test Title - 01 [1080p]")
        assert result.subtitle_group == "SubGroup"

    def test_episode_extraction(self):
        result = parse_title("[Group] Anime / Name - 24 [720p]")
        assert result.episode == 24

    def test_single_digit_episode(self):
        result = parse_title("[Group] Anime / Name - 1 [1080p]")
        assert result.episode == 1

    def test_no_episode_number(self):
        result = parse_title("[Group] Movie Title [1080p][MKV]")
        assert result.episode is None

    def test_resolution_variants(self):
        assert parse_title("[G] T - 1 [720p]").resolution == "720p"
        assert parse_title("[G] T - 1 [1080P]").resolution == "1080p"

    def test_container_extraction(self):
        result = parse_title("[G] T - 1 [MP4]")
        assert result.container == "MP4"

    def test_file_size_from_description(self):
        result = parse_title("[G] T - 1", "[G] T - 1 [1.5 GB]")
        assert result.file_size == int(1.5 * 1024 * 1024 * 1024)

    def test_file_size_mb(self):
        result = parse_title("[G] T - 1", "[G] T - 1 [500 MB]")
        assert result.file_size == int(500 * 1024 * 1024)

    def test_empty_title(self):
        result = parse_title("")
        assert result.subtitle_group is None
        assert result.episode is None

    def test_chinese_only_title(self):
        result = parse_title("[字幕组] 测试动画 - 05 [1080p]")
        assert result.subtitle_group == "字幕组"
        assert result.title_cn == "测试动画"
        assert result.episode == 5


class TestParsedTitleDataclass:
    def test_defaults(self):
        p = ParsedTitle()
        assert p.raw == ""
        assert p.subtitle_group is None
        assert p.episode is None
        assert p.file_size is None
