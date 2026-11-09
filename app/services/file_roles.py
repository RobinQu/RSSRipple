"""Shared torrent-file classification and filename matching helpers."""

from __future__ import annotations

import re
from typing import Literal

FileKind = Literal["video", "subtitle", "extra", "other"]
FileRole = Literal["episode", "subtitle", "extra", "excluded"]

VIDEO_EXTS = frozenset({
    "mkv", "mp4", "avi", "ts", "wmv", "flv", "mov", "webm", "mpg", "mpeg",
    "rmvb", "vob",
})
SUBTITLE_EXTS = frozenset({"ass", "ssa", "srt", "sub", "idx", "vtt", "sup", "mks"})
EXTRA_EXTS = frozenset({
    "bmp", "gif", "jpeg", "jpg", "nfo", "pdf", "png", "txt", "url", "webp",
})
MIN_EPISODE_VIDEO_SIZE = 50 * 1024 * 1024

_EXTRA_DIRS = frozenset({
    "sample", "samples", "特典", "映像特典", "sp", "sps", "special", "specials",
    "op", "ed", "ncop", "nced", "pv", "cm", "extra", "extras", "menu", "menus",
    "scan", "scans", "making", "making of", "interview", "interviews", "trailer",
    "trailers", "preview", "previews",
})
_EXTRA_MARKER_RE = re.compile(
    r"(?:^|[^a-z0-9])(?:nc(?:op|ed)|op|ed|pv|cm|menu)"
    r"(?:[_-]?\d+)?(?:v\d+)?(?=$|[^a-z0-9])",
    re.IGNORECASE,
)
_SUBTITLE_TAG_RE = re.compile(
    r"[\[【\(（][^\]】\)）]*(?:chs|cht|zh(?:[-_]?cn|[-_]?tw)?|sc|tc|"
    r"eng?|jpn|ja|kor|ko|简|繁|字幕|subtitle)[^\]】\)）]*[\]】\)）]",
    re.IGNORECASE,
)
_SUBTITLE_SUFFIX_RE = re.compile(
    r"(?:[\s._-]+(?:chs|cht|zh(?:[-_]?cn|[-_]?tw)?|sc|tc|eng?|jpn|ja|kor|ko|"
    r"简体|繁体|中文字幕|字幕|subs?|subtitles?|default|forced))+$",
    re.IGNORECASE,
)


def _parts(path: str | None) -> list[str]:
    return [part for part in re.split(r"[/\\]+", path or "") if part]


def _extension(filename: str) -> str:
    stem, dot, extension = filename.rpartition(".")
    return extension.casefold() if dot and stem else ""


def _has_extra_directory(parts: list[str]) -> bool:
    return any(part.strip().casefold() in _EXTRA_DIRS for part in parts[:-1])


def is_extra_video_path(path: str | None) -> bool:
    parts = _parts(path)
    if not parts or _extension(parts[-1]) not in VIDEO_EXTS:
        return False
    return _has_extra_directory(parts) or _EXTRA_MARKER_RE.search(parts[-1]) is not None


def classify_file(path: str | None, size: int | None = None) -> FileKind:
    """Classify one listing entry without consulting work/episode metadata."""
    parts = _parts(path)
    if not parts:
        return "other"
    extension = _extension(parts[-1])
    if extension in SUBTITLE_EXTS:
        return "subtitle"
    if extension in VIDEO_EXTS:
        if (
            is_extra_video_path(path)
            or size is not None and 0 <= size < MIN_EPISODE_VIDEO_SIZE
        ):
            return "extra"
        return "video"
    if _has_extra_directory(parts) or extension in EXTRA_EXTS:
        return "extra"
    return "other"


def assignment_file_role(path: str | None, size: int | None = None) -> FileRole:
    kind = classify_file(path, size)
    if kind == "video":
        return "episode"
    if kind == "other":
        return "excluded"
    return kind


def normalized_main_filename(path: str | None) -> str:
    """Normalize a video/subtitle basename for conservative exact matching."""
    parts = _parts(path)
    if not parts:
        return ""
    stem = parts[-1].rsplit(".", 1)[0]
    stem = _SUBTITLE_TAG_RE.sub("", stem)
    stem = _SUBTITLE_SUFFIX_RE.sub("", stem)
    return re.sub(r"[\W_]+", "", stem.casefold(), flags=re.UNICODE)


__all__ = [
    "FileKind",
    "FileRole",
    "MIN_EPISODE_VIDEO_SIZE",
    "assignment_file_role",
    "classify_file",
    "is_extra_video_path",
    "normalized_main_filename",
]
