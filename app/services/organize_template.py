"""命名模板引擎（内置整理子系统 organize 的规划层）。

``path_template`` 是相对 Library 根（``volume.mount_path + root_subpath``
解析结果）的格式串（``/`` 分隔），占位符
取值全部来自冻结的通知快照，穷举表见 docs/design/file-organization.md
「规则与命名模板」。两个入口：

- ``validate_template``：规则保存时调用——未知占位符 / 非法格式说明符 /
  绝对路径 / ``..`` 段 → ``ValueError``（上层转 422）。
- ``render_template``：规划时调用——引用的占位符缺必填值 →
  ``TemplateRenderError``（上层转规划失败，不落计划、下 tick 重试）。

渲染约定：

- ``[...]`` 是可选段标记（如预设中的 ``[ - {episode_title}]``）：段内渲染
  结果只剩空白/连字符时整段剔除，否则去掉方括号保留内容。
- 渲染结果逐分量过 ``sanitize_component``（剔除 ``/`` 与控制字符、去首尾
  空白与尾部点空格、截断 150 字符；清洗后为空 → 报错），与 vault-organizer
  namer 的行为对齐。
"""

from __future__ import annotations

import re
import string
from collections.abc import Mapping
from typing import Any

MAX_COMPONENT_LEN = 150

# 占位符穷举表（docs/design/file-organization.md「规则与命名模板」）。
# ``lang`` 不在文档主表内，仅服务于字幕预设 ``.{lang}{ext}``：规划器渲染字幕
# 目标时注入经 subtitle_lang_map 映射后的 Plex 语言后缀。
PLACEHOLDERS = frozenset(
    {
        "title",
        "title_en",
        "title_cn",
        "original_title",
        "year",
        "season",
        "episode",
        "episode_code",
        "episode_title",
        "category",
        "collection",
        "resolution",
        "container",
        "ext",
        "lang",
    }
)

# 允许携带格式说明符的整型占位符（如 ``{season:02d}``）。
_INT_FIELDS = frozenset({"year", "season", "episode"})
_INT_FORMAT_SPEC = re.compile(r"^(0?[1-9]\d*)?d$")

# 内置 Plex 兼容预设（与 docs/design/file-organization.md 一致）。
PRESET_TV = (
    "{title}/Season {season:02d}/{title} - {episode_code}"
    "[ - {episode_title}]{ext}"
)
PRESET_MOVIE = "{category}/{title} ({year})/{title} ({year}){ext}"
# 字幕预设：正片同主名 + `.{lang}{ext}`；lang 经 Library.subtitle_lang_map
# （BCP-47 → Plex 后缀；未命中查主标签，仍不中取主标签本身）。同集同语言
# 多份字幕第 2 份起由规划器在 lang 后追加序号。
PRESET_SUBTITLE = (
    "{title}/Season {season:02d}/{title} - {episode_code}"
    "[ - {episode_title}].{lang}{ext}"
)

# Library.subtitle_lang_map 为空时使用的内置默认表（移植自 vault-organizer）。
DEFAULT_SUBTITLE_LANG_MAP: dict[str, str] = {
    "zh-CN": "chs",
    "zh-TW": "cht",
    "zh-Hans": "chs",
    "zh-Hant": "cht",
    "ja": "ja",
    "en": "en",
}

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_SLASHES = re.compile(r"/+")
_OPTIONAL_GROUP = re.compile(r"\[([^\[\]]*)\]")

_FORMATTER = string.Formatter()


class TemplateRenderError(ValueError):
    """模板在运行时缺必填值或渲染结果非法（规划失败的直接原因）。"""


def sanitize_component(name: str, max_len: int = MAX_COMPONENT_LEN) -> str:
    """清洗单个路径分量：剔除 ``/`` 与控制字符、去尾部点和空格、长度截断。

    清洗后为空 → ``ValueError``（保存时转 422，规划时转规划失败）。
    """
    cleaned = _SLASHES.sub("", name)
    cleaned = _CONTROL_CHARS.sub("", cleaned)
    cleaned = cleaned.strip().rstrip(" .")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" .")
    if not cleaned:
        raise ValueError("路径分量清洗后为空")
    return cleaned


def map_subtitle_lang(tag: str, lang_map: Mapping[str, str] | None = None) -> str:
    """BCP-47 标签 → Plex 语言后缀；未命中取主标签（主标签在表中则先映射）。

    ``lang_map`` 为空时使用 ``DEFAULT_SUBTITLE_LANG_MAP``。
    """
    table = lang_map or DEFAULT_SUBTITLE_LANG_MAP
    for key, value in table.items():
        if key.casefold() == tag.casefold():
            return value
    primary = tag.split("-")[0]
    for key, value in table.items():
        if key.casefold() == primary.casefold():
            return value
    return primary.lower()


def _iter_fields(template: str):
    """产出模板中全部 (literal_text, field_name, format_spec)；语法错误 → ValueError。"""
    try:
        parsed = list(_FORMATTER.parse(template))
    except ValueError as exc:
        raise ValueError(f"模板语法错误：{exc}") from exc
    for literal_text, field_name, format_spec, conversion in parsed:
        if field_name is None:
            continue
        yield literal_text, field_name, format_spec, conversion


def _validate_field(field_name: str, format_spec: str, conversion: str | None) -> None:
    if conversion is not None:
        raise ValueError(f"占位符 {{{field_name}}} 不允许使用转换说明符 !{conversion}")
    if not field_name or field_name not in PLACEHOLDERS:
        raise ValueError(f"未知占位符 {{{field_name}}}")
    if format_spec:
        if field_name not in _INT_FIELDS:
            raise ValueError(
                f"占位符 {{{field_name}}} 不允许格式说明符（仅 year/season/episode 支持）"
            )
        if not _INT_FORMAT_SPEC.match(format_spec):
            raise ValueError(f"占位符 {{{field_name}}} 的格式说明符非法：{format_spec!r}")


def validate_template(template: str) -> None:
    """保存时校验：未知占位符 / 非法格式说明符 / 绝对路径 / ``..`` 段 → ValueError。"""
    if not template or not template.strip():
        raise ValueError("模板不能为空")
    if _CONTROL_CHARS.search(template):
        raise ValueError("模板不允许包含控制字符")
    if template.startswith("/"):
        raise ValueError("模板必须是相对 Library 根的相对路径")
    for _literal, field_name, format_spec, conversion in _iter_fields(template):
        _validate_field(field_name, format_spec, conversion)
    # 纯字面量部分也不能含 ``..`` 段（占位符渲染值中的 ``..`` 会被
    # sanitize_component 清洗成空分量而报错，无需在此检查）。
    for part in template.split("/"):
        if "{" not in part and part in (".", ".."):
            raise ValueError("模板不允许包含 '.' 或 '..' 路径段")


def _collapse_optional_groups(rendered: str) -> str:
    """处理 ``[...]`` 可选段：内容仅剩空白/连字符则整段剔除，否则去括号。"""

    def _sub(m: re.Match[str]) -> str:
        inner = m.group(1)
        return inner if inner.strip(" -–—") else ""

    return _OPTIONAL_GROUP.sub(_sub, rendered)


def render_template(template: str, context: Mapping[str, Any]) -> str:
    """按快照上下文渲染模板，返回清洗后的相对路径（``/`` 分隔）。

    ``episode_title`` 缺失渲染为空段；``collection`` 无取值（作品无合集）
    渲染为空串并折叠该目录层级（不产生 ``//``）；其余被引用的占位符取值
    为 None → ``TemplateRenderError``（规划失败，不落计划）。
    """
    values: dict[str, Any] = {}
    for _literal, field_name, format_spec, _conversion in _iter_fields(template):
        value = context.get(field_name)
        if value is None:
            if field_name in ("episode_title", "collection"):
                value = ""
            else:
                raise TemplateRenderError(f"模板占位符 {{{field_name}}} 缺少取值")
        # 字符串取值先剔除 ``/`` 与控制字符，避免取值引入额外路径分量；
        # 逐分量的完整清洗在 format 之后统一做。
        if isinstance(value, str):
            value = _CONTROL_CHARS.sub("", _SLASHES.sub("", value))
        values[field_name] = value
    try:
        rendered = template.format(**values)
    except (ValueError, KeyError, IndexError) as exc:
        raise TemplateRenderError(f"模板渲染失败：{exc}") from exc
    rendered = _collapse_optional_groups(rendered)
    # 空分量（空的可选变量，如无合集时的 ``{collection}``）整层折叠；
    # 折叠后结果不可能以 ``/`` 开头（模板级的绝对路径在保存时已拒绝）。
    parts = [part for part in rendered.split("/") if part != ""]
    if not parts:
        raise TemplateRenderError("模板渲染结果为空")
    try:
        components = [sanitize_component(part) for part in parts]
    except ValueError as exc:
        raise TemplateRenderError(f"模板渲染结果非法：{exc}") from exc
    return "/".join(components)
