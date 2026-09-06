"""organize_template 命名模板引擎的集成侧补覆盖测试。

纯函数模块，不依赖 DB。补齐集成运行下未覆盖的分支：sanitize 截断与空分量、
map_subtitle_lang 主标签回退、模板语法/占位符/格式说明符校验、渲染期缺值、
format 失败、空渲染结果、渲染结果清洗失败。
"""

from __future__ import annotations

import pytest

from app.services.organize_template import (
    MAX_COMPONENT_LEN,
    TemplateRenderError,
    map_subtitle_lang,
    render_template,
    sanitize_component,
    validate_template,
)

# ---------------------------------------------------------------- sanitize_component


def test_sanitize_truncates_long_component():
    name = "a" * (MAX_COMPONENT_LEN + 50)
    cleaned = sanitize_component(name)
    assert len(cleaned) == MAX_COMPONENT_LEN


def test_sanitize_truncation_strips_trailing_dots():
    name = "a" * (MAX_COMPONENT_LEN - 1) + "..." + "b" * 50
    cleaned = sanitize_component(name)
    assert len(cleaned) <= MAX_COMPONENT_LEN
    assert not cleaned.endswith((".", " "))


def test_sanitize_empty_after_clean_raises():
    with pytest.raises(ValueError, match="清洗后为空"):
        sanitize_component("... ")
    with pytest.raises(ValueError, match="清洗后为空"):
        sanitize_component("/ /")


def test_sanitize_strips_slashes_and_control_chars():
    assert sanitize_component("a/b\x01c") == "abc"


# ---------------------------------------------------------------- map_subtitle_lang


def test_map_subtitle_lang_exact_case_insensitive():
    assert map_subtitle_lang("ZH-cn") == "chs"
    assert map_subtitle_lang("zh-Hant") == "cht"


def test_map_subtitle_lang_primary_tag_fallback():
    """完整标签未命中时按主标签再查表。"""
    assert map_subtitle_lang("JA-JP") == "ja"
    assert map_subtitle_lang("EN-US") == "en"


def test_map_subtitle_lang_unknown_returns_primary_lower():
    """主标签也不在表中 → 返回小写主标签本身。"""
    assert map_subtitle_lang("FR-CA") == "fr"


def test_map_subtitle_lang_custom_map():
    assert map_subtitle_lang("zh-CN", {"zh-cn": "sc"}) == "sc"


# ---------------------------------------------------------------- validate_template


def test_validate_template_ok():
    validate_template("{title}/Season {season:02d}/{title} - {episode_code}{ext}")
    validate_template("{{literal}}/{title}{ext}")  # 转义花括号产出字面量


@pytest.mark.parametrize(
    ("template", "match"),
    [
        ("", "不能为空"),
        ("   ", "不能为空"),
        ("{title}\x01", "控制字符"),
        ("/{title}", "相对路径"),
        ("{title", "语法错误"),
        ("{title!r}", "转换说明符"),
        ("{bogus}", "未知占位符"),
        ("{title:02d}", "不允许格式说明符"),
        ("{season:2f}", "格式说明符非法"),
        ("a/../{title}", "路径段"),
        ("./{title}", "路径段"),
    ],
)
def test_validate_template_rejects(template, match):
    with pytest.raises(ValueError, match=match):
        validate_template(template)


# ---------------------------------------------------------------- render_template


def test_render_preset_tv_optional_group_collapses():
    out = render_template(
        "{title}/Season {season:02d}/{title} - {episode_code}"
        "[ - {episode_title}]{ext}",
        {
            "title": "攻壳机动队",
            "season": 1,
            "episode_code": "s01e04",
            "episode_title": None,
            "ext": ".mkv",
        },
    )
    assert out == "攻壳机动队/Season 01/攻壳机动队 - s01e04.mkv"


def test_render_collection_none_collapses_level():
    out = render_template(
        "{collection}/{title}{ext}",
        {"collection": None, "title": "Show", "ext": ".mkv"},
    )
    assert out == "Show.mkv"  # 空合集层折叠，不产生 //


def test_render_missing_required_value_raises():
    with pytest.raises(TemplateRenderError, match="缺少取值"):
        render_template("{title} ({year}){ext}", {"title": "x", "ext": ".mkv"})


def test_render_format_failure_raises():
    """整型格式说明符作用于字符串取值 → format 抛错转 TemplateRenderError。"""
    with pytest.raises(TemplateRenderError, match="渲染失败"):
        render_template("{season:02d}{ext}", {"season": "abc", "ext": ".mkv"})


def test_render_empty_result_raises():
    with pytest.raises(TemplateRenderError, match="结果为空"):
        render_template("{collection}", {"collection": None})


def test_render_illegal_component_raises():
    """渲染值清洗后为空分量 → 渲染结果非法。"""
    with pytest.raises(TemplateRenderError, match="结果非法"):
        render_template("{title}{ext}", {"title": "...", "ext": ""})


def test_render_strips_slashes_from_values():
    out = render_template(
        "{title}{ext}", {"title": "a/b\x02c", "ext": ".mkv"}
    )
    assert out == "abc.mkv"
