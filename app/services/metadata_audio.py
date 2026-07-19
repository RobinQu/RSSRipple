"""AudioWork detection - non-TV/non-movie works (ASMR, music, drama CD, radio).

Pure leaf module - no DB, no LLM. Extracted verbatim from metadata_agent.py
(Phase 0 leaf extraction): pattern-match a raw RSS title to an AudioWork
sub-kind (asmr/music/drama_cd/radio/other), and flag software / non-media
titles that should never be matched.
"""
from __future__ import annotations

import re

AUDIO_CONTENT_TYPES: frozenset[str] = frozenset(
    {"asmr", "music", "drama_cd", "radio", "other"}
)


# Ordered: the first matching pattern wins. ASMR is the most specific (a
# standalone audio work with no TV/movie equivalent). Music markers target
# lossless/hi-res audio releases and OSTs - anime OP/ED themes carrying these
# tags still reach this path only when they did NOT short-circuit to a known
# series, which is the right fallback.
_AUDIO_TYPE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("asmr", re.compile(r"ASMR", re.IGNORECASE)),
    ("drama_cd", re.compile(r"ドラマ\s*CD|Drama\s*CD|广播剧|廣播劇")),
    ("radio", re.compile(r"ラジオ|Radio(?![a-z])|广播节目|廣播節目")),
    (
        "music",
        re.compile(
            r"\[FLAC|\bFLAC\b|\bALAC\b|96kHz|48kHz|24bit|"
            r"サントラ|Soundtrack|\bOST\b|シングル|\bSingle\b|"
            r"ボーカル|Vocal\b|キャラクターソング|Character\s*Song",
            re.IGNORECASE,
        ),
    ),
]


def _detect_audio_work_type(raw_title: str | None) -> str | None:
    """Return the AudioWork sub-kind for a title, or None if it isn't audio.

    Conservative: only flags titles with strong audio-only markers. A normal
    anime episode (``[WebRip 1080p HEVC AAC]``) is NOT flagged.
    """
    if not raw_title:
        return None
    for kind, pattern in _AUDIO_TYPE_PATTERNS:
        if pattern.search(raw_title):
            return kind
    return None


# Titles that are software / non-media releases (BitComet builds, cracked
# tools), not anime/TV/movie works. Marked non_work so they aren't retried.
_NON_MEDIA_RE = re.compile(
    r"BitComet|uTorrent|qBittorrent|比特彗星|aria2|解锁豪华版|破解版",
    re.IGNORECASE,
)


def _is_non_media(raw_title: str | None) -> bool:
    """True for software / non-media titles that should never be matched."""
    return bool(raw_title and _NON_MEDIA_RE.search(raw_title))
