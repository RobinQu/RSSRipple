"""文件名分类与集号解析（内置整理子系统 organize 的规划层）。

原样移植自 vault-organizer ``parser.py``：纯正则、确定性、无 LLM。
规划器据此把磁盘文件分为主视频 / 字幕 / 其他三类，并为合集逐文件解析
``(season, episode)``；字幕语言识别配合 ``Library.subtitle_lang_map``
（map 为空时规划器回落到 ``organize_template.DEFAULT_SUBTITLE_LANG_MAP``）。
"""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path

VIDEO_EXTS = {".mkv", ".mp4", ".avi", ".m2ts", ".ts", ".wmv", ".mov", ".flv", ".webm"}
SUBTITLE_EXTS = {
    ".srt", ".smi", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup",
}
# Blu-ray remux/transcode releases often ship lossless or compatibility audio
# tracks beside the main MKV.  Plex does not attach sidecar audio to a movie,
# but the organizer still needs to recognise and preserve those source assets.
AUDIO_EXTS = {
    ".aac", ".ac3", ".dts", ".dtshd", ".eac3", ".ec3", ".flac", ".m4a",
    ".mka", ".mp3", ".ogg", ".opus", ".thd", ".truehd", ".wav",
}


class FileKind(StrEnum):
    VIDEO = "video"
    SUBTITLE = "subtitle"
    OTHER = "other"


def classify(name: str) -> FileKind:
    """按扩展名分类文件名（大小写不敏感）。"""
    ext = Path(name).suffix.lower()
    if ext in VIDEO_EXTS:
        return FileKind.VIDEO
    if ext in SUBTITLE_EXTS:
        return FileKind.SUBTITLE
    return FileKind.OTHER


def is_audio(name: str) -> bool:
    """Return whether *name* is a commonly distributed sidecar audio track."""
    return Path(name).suffix.lower() in AUDIO_EXTS


_SXXEXX = re.compile(r"[Ss](\d{1,2})[Ee](\d{1,4})")
_EP_TOKEN = re.compile(r"(?:^|[\s._\-\[])[Ee][Pp]?\.?\s*(\d{1,4})(?:\s*[vV]\d+)?(?=[\s._\-\]\)]|$)")
_CJK_EP = re.compile(r"第\s*(\d{1,4})\s*[話话集回]")
# 裸方括号集号（fansub 常见：``[Kisssub][Title][1080P][01][MP4].mp4``）——
# 与 ``resource_parser._BRACKET_EPISODE_RE`` 保持一致：1-3 位纯数字 +
# 可选 vN 修订号，``[1080P]``/``[2026]`` 这类技术标签不会匹配。
_BRACKET_EP = re.compile(r"\[(\d{1,3})(?:\s*[vV]\d+)?\]")
_ANIME_DASH = re.compile(r"-\s*(\d{1,4})(?:\s*[vV]\d+)?\s*(?=$|[\[\(.])")

_SEASON_WORD = re.compile(r"[Ss]eason\s*(\d{1,2})")
_SEASON_TOKEN = re.compile(r"[Ss](\d{1,2})")


def parse_episode(name: str) -> tuple[int | None, int | None]:
    """从文件名解析 (season, episode)；解析不出返回 (None, None)。

    支持：S04E09 / E09 / EP09 / 第09話 / 裸方括号 ``[09]``（含 v2 修订号）
    / 动画常见的 ``Title - 09 (1080p)``。
    """
    stem = Path(name).stem
    if m := _SXXEXX.search(stem):
        return int(m.group(1)), int(m.group(2))
    if m := _EP_TOKEN.search(stem):
        return None, int(m.group(1))
    if m := _CJK_EP.search(stem):
        return None, int(m.group(1))
    if m := _BRACKET_EP.search(stem):
        return None, int(m.group(1))
    if m := _ANIME_DASH.search(stem):
        return None, int(m.group(1))
    return None, None


def parse_season_from_path(rel: str) -> int | None:
    """从相对路径的目录分量解析季号（``Season 2/`` 或 ``S02/``）。"""
    for part in Path(rel).parts[:-1]:
        if m := _SEASON_WORD.search(part):
            return int(m.group(1))
        if m := _SEASON_TOKEN.fullmatch(part):
            return int(m.group(1))
    return None


# 字幕文件名中常见的语言标记 → 规范 BCP-47 标签
_LANG_TOKENS: dict[str, str] = {
    "zh-cn": "zh-CN",
    "zh-hans": "zh-CN",
    "chs": "zh-CN",
    "sc": "zh-CN",
    "gb": "zh-CN",
    "简体": "zh-CN",
    "简中": "zh-CN",
    "简": "zh-CN",
    "zh-tw": "zh-TW",
    "zh-hant": "zh-TW",
    "cht": "zh-TW",
    "tc": "zh-TW",
    "big5": "zh-TW",
    "繁体": "zh-TW",
    "繁體": "zh-TW",
    "繁中": "zh-TW",
    "繁": "zh-TW",
    "jpn": "ja",
    "jp": "ja",
    "ja": "ja",
    "日": "ja",
    "eng": "en",
    "english": "en",
    "en": "en",
    "fre": "fr",
    "fra": "fr",
    "french": "fr",
    "fr": "fr",
    "ger": "de",
    "deu": "de",
    "german": "de",
    "de": "de",
    "ita": "it",
    "italian": "it",
    "it": "it",
    "spa": "es",
    "spanish": "es",
    "es": "es",
}


def detect_subtitle_lang(name: str) -> str | None:
    """从字幕文件名识别语言标签（BCP-47）；识别不出返回 None。"""
    stem = Path(name).stem.lower()
    for token in sorted(_LANG_TOKENS, key=len, reverse=True):
        if token.isascii():
            if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", stem):
                return _LANG_TOKENS[token]
        elif token in stem:
            return _LANG_TOKENS[token]
    return None


def detect_subtitle_flags(name: str) -> tuple[str, ...]:
    """Return Plex-supported subtitle flags present in a sidecar filename."""
    tokens = {
        token.casefold()
        for token in re.split(r"[^a-zA-Z0-9]+", Path(name).stem)
        if token
    }
    flags: list[str] = []
    if "forced" in tokens or "force" in tokens:
        flags.append("forced")
    if "sdh" in tokens:
        flags.append("sdh")
    elif "cc" in tokens:
        flags.append("cc")
    return tuple(flags)
