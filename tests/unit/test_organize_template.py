"""命名模板引擎（organize_template）单元测试。"""

from __future__ import annotations

import pytest

from app.services.organize_template import (
    DEFAULT_SUBTITLE_LANG_MAP,
    PRESET_MOVIE,
    PRESET_SUBTITLE,
    PRESET_TV,
    TemplateRenderError,
    map_subtitle_lang,
    render_template,
    sanitize_component,
    validate_template,
)

TV_CONTEXT = {
    "title": "攻壳机动队",
    "title_en": "THE GHOST IN THE SHELL",
    "title_cn": "攻壳机动队",
    "original_title": "攻殻機動隊 THE GHOST IN THE SHELL",
    "year": 2026,
    "season": 1,
    "episode": 4,
    "episode_title": "机器人回旋曲",
    "category": None,
    "collection": "攻壳机动队（系列）",
    "resolution": "1080p",
    "container": "MKV",
    "ext": ".mkv",
}

MOVIE_CONTEXT = {
    "title": "哈姆奈特",
    "title_en": "Hamnet",
    "title_cn": "哈姆奈特",
    "original_title": "Hamnet (2025)",
    "year": 2025,
    "season": None,
    "episode": None,
    "episode_title": None,
    "category": "Horror",
    "collection": None,
    "resolution": "2160p",
    "container": None,
    "ext": ".mkv",
}


class TestValidateTemplate:
    def test_builtin_presets_are_valid(self):
        validate_template(PRESET_TV)
        validate_template(PRESET_MOVIE)
        validate_template(PRESET_SUBTITLE)

    def test_unknown_placeholder(self):
        with pytest.raises(ValueError, match="未知占位符"):
            validate_template("{title}/{bogus}{ext}")

    def test_format_spec_on_string_field_rejected(self):
        with pytest.raises(ValueError, match="格式说明符"):
            validate_template("{title:02d}{ext}")

    def test_invalid_format_spec_rejected(self):
        with pytest.raises(ValueError, match="格式说明符非法"):
            validate_template("{season:xyz}{ext}")

    def test_conversion_rejected(self):
        with pytest.raises(ValueError, match="转换说明符"):
            validate_template("{title!r}{ext}")

    def test_unbalanced_braces(self):
        with pytest.raises(ValueError, match="模板语法错误"):
            validate_template("{title/{ext}")

    def test_empty_template(self):
        with pytest.raises(ValueError, match="不能为空"):
            validate_template("   ")

    def test_absolute_template_rejected(self):
        with pytest.raises(ValueError, match="相对路径"):
            validate_template("/{title}{ext}")

    def test_dotdot_segment_rejected(self):
        with pytest.raises(ValueError, match=r"'\.\.'"):
            validate_template("{title}/../evil{ext}")


class TestRenderTemplate:
    def test_tv_preset(self):
        assert render_template(PRESET_TV, TV_CONTEXT) == (
            "攻壳机动队/Season 01/"
            "攻壳机动队 - s01e04 - 机器人回旋曲.mkv"
        )

    def test_movie_preset(self):
        assert render_template(PRESET_MOVIE, MOVIE_CONTEXT) == (
            "Horror/哈姆奈特 (2025)/哈姆奈特 (2025).mkv"
        )

    def test_subtitle_preset(self):
        ctx = {**TV_CONTEXT, "ext": ".ass", "lang": "chs"}
        assert render_template(PRESET_SUBTITLE, ctx) == (
            "攻壳机动队/Season 01/"
            "攻壳机动队 - s01e04 - 机器人回旋曲.chs.ass"
        )

    def test_zero_padding(self):
        ctx = {**TV_CONTEXT, "season": 3, "episode": 7}
        out = render_template("{title}/Season {season:02d}/e{episode:02d}{ext}", ctx)
        assert out == "攻壳机动队/Season 03/e07.mkv"

    def test_episode_title_missing_renders_empty_segment(self):
        ctx = {**TV_CONTEXT, "episode_title": None}
        assert render_template(PRESET_TV, ctx) == (
            "攻壳机动队/Season 01/攻壳机动队 - s01e04.mkv"
        )

    def test_missing_required_value_raises(self):
        ctx = {**TV_CONTEXT, "season": None}
        with pytest.raises(TemplateRenderError, match="season"):
            render_template(PRESET_TV, ctx)

    def test_title_fallback_chain(self):
        ctx = {**MOVIE_CONTEXT, "title": None}
        with pytest.raises(TemplateRenderError, match="title"):
            render_template("{title}{ext}", ctx)

    def test_year_zero_padding_and_fallback(self):
        ctx = {**TV_CONTEXT, "year": 2026}
        assert render_template("{title} ({year}){ext}", ctx) == "攻壳机动队 (2026).mkv"

    def test_sanitize_strips_illegal_chars(self):
        ctx = {**MOVIE_CONTEXT, "title": 'A/B: "Hamnet"\x00 '}
        out = render_template("{title}{ext}", ctx)
        assert "/" not in out.split("/")[0]
        assert "\x00" not in out
        assert out.startswith('AB: "Hamnet"')

    def test_sanitize_empty_component_fails(self):
        ctx = {**MOVIE_CONTEXT, "title": "..."}
        with pytest.raises(TemplateRenderError, match="清洗后为空"):
            render_template("{title}/f{ext}", ctx)

    def test_long_component_truncated(self):
        ctx = {**MOVIE_CONTEXT, "title": "x" * 300}
        out = render_template("{title}{ext}", ctx)
        assert len(out.split("/")[0]) <= 150


class TestSanitizeComponent:
    def test_strips_slash_control_and_trailing_dots(self):
        assert sanitize_component("a/b\x01c .. ") == "abc"

    def test_truncates_to_max_len(self):
        assert len(sanitize_component("y" * 200)) == 150

    def test_empty_after_clean_raises(self):
        with pytest.raises(ValueError):
            sanitize_component(" / ")


class TestMapSubtitleLang:
    def test_default_map_exact_and_case(self):
        assert map_subtitle_lang("zh-CN") == "chs"
        assert map_subtitle_lang("ZH-cn") == "chs"

    def test_primary_tag_fallback(self):
        assert map_subtitle_lang("zh-Hans") == "chs"

    def test_unknown_tag_falls_back_to_primary(self):
        assert map_subtitle_lang("fr-FR") == "fr"

    def test_custom_library_map_wins(self):
        assert map_subtitle_lang("zh-CN", {"zh-cn": "sc"}) == "sc"

    def test_none_map_uses_default(self):
        assert map_subtitle_lang("ja", None) == DEFAULT_SUBTITLE_LANG_MAP["ja"]
