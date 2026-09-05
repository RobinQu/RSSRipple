"""Dynamic resource parser using per-channel field mappings.

Uses the new field_mapping format with list_locator + field_mappings.
Backward compatible with the old flat dict format.
"""

import logging
import re
from datetime import datetime
from typing import Any

from app.services.subtitle_groups import normalize_subtitle_groups

logger = logging.getLogger(__name__)


# Season suffixes baked into work titles by external sources (e.g. exa returns
# "That Time I Got Reincarnated as a Slime Season 4" for a show). We strip them
# so TVSeries stores the base show title and season lives on FileResource/Episode
# (where it belongs). Suffix-only + conservative patterns so legitimate titles
# like "Part II" or a trailing number aren't mangled; the season-suffixed form
# is still kept in series.aliases for matching.
_SEASON_SUFFIX_RE = re.compile(
    r"\s*("
    r"第[一二三四五六七八九十百零千两\d]+\s*[季期]"   # 第N季 / 第N期
    r"|\d{1,2}\s*[季期]"                             # bare N季/N期 (e.g. 3期, 2季)
    r"|Season\s*\d+"                                    # Season 4
    r"|\d+(?:st|nd|rd|th)\s+Season"                     # 4th Season
    r"|S\d{1,2}"                                        # S04
    r")\s*$",
    flags=re.IGNORECASE,
)


_FILENAME_GROUP_SUFFIX_RE = re.compile(
    r"(?:-|\[)([A-Za-z0-9][A-Za-z0-9._]{1,31})\]?$",
    flags=re.IGNORECASE,
)
_VIDEO_EXTENSION_RE = re.compile(
    r"\.(?:mkv|mp4|m4v|avi|mov|wmv|ts|m2ts|webm)$",
    flags=re.IGNORECASE,
)
_TECHNICAL_FILENAME_SUFFIXES = {
    "aac", "aac20", "ac3", "av1", "avc", "bluray", "bdrip", "cr",
    "ddp", "ddp51", "dts", "flac", "h264", "h265", "hevc", "opus",
    "remux", "truehd", "web", "webdl", "webrip", "x264", "x265",
}


def extract_subtitle_group_from_filename(file_path: str | None) -> str | None:
    """Extract a release group placed at the end of a video filename.

    Overseas release groups commonly use ``...-GROUP.mkv`` (or, less often,
    ``...[GROUP].mkv``), while Chinese/Japanese groups generally appear at the
    start of the RSS title.  This helper is intentionally filename-only and
    conservative: directory names, whitespace-bearing suffixes, numeric tags,
    and common codec/source tokens are rejected.
    """
    if not file_path:
        return None
    filename = re.split(r"[/\\]", str(file_path))[-1].strip()
    stem = _VIDEO_EXTENSION_RE.sub("", filename)
    match = _FILENAME_GROUP_SUFFIX_RE.search(stem)
    if not match:
        return None
    candidate = match.group(1).strip()
    normalized = re.sub(r"[._-]", "", candidate.casefold())
    if (
        candidate.isdigit()
        or normalized in _TECHNICAL_FILENAME_SUFFIXES
        or re.fullmatch(r"\d{3,4}p", normalized)
    ):
        return None
    return candidate


def strip_season_from_title(title: str | None) -> str | None:
    """Remove a trailing season suffix from a work title.

    Returns the base title (e.g. "关于我转生变成史莱姆这档事 第四季" ->
    "关于我转生变成史莱姆这档事"). If nothing matches, returns the title
    unchanged. Never returns empty - falls back to the original.
    """
    if not title:
        return title
    stripped = _SEASON_SUFFIX_RE.sub("", title).strip(" -:：·")
    return stripped or title


def season_from_title(title: str | None) -> int | None:
    """Season number carried by a work title (``第N季`` / ``Season N`` /
    ``N th Season`` / ``S04``), or None when the title has no season marker.

    Counterpart of :func:`strip_season_from_title`; used by the per-season
    upsert to pin the target season from a matched entity's titles.
    """
    if not title:
        return None
    return _season_marker(title.strip())


def parse_entry(entry: dict, field_mapping: dict | None, description: str | None = None) -> dict:
    """Parse a feedparser entry into FileResource fields.

    If field_mapping is None or empty, returns an empty dict.
    Supports two formats:
    - New format: {"list_locator": {...}, "field_mappings": {...}}
    - Old flat format: {"field_name": {"source": "...", ...}, ...}

    Args:
        entry: A feedparser entry as a plain dict.
        field_mapping: Channel-specific field mapping rules.
        description: Optional entry description (unused, kept for API compatibility).

    Returns:
        Dict of parsed FileResource fields.
    """
    if not field_mapping:
        return {}

    # New format: extract field_mappings from the wrapper
    if "field_mappings" in field_mapping:
        mappings = field_mapping["field_mappings"]
    else:
        # Backward compat: treat the whole dict as field_mappings
        mappings = field_mapping

    return _parse_with_mappings(entry, mappings)


def _parse_with_mappings(entry: dict, field_mappings: dict) -> dict:
    """Parse a feedparser entry dict using per-field extraction rules.

    Args:
        entry: A feedparser entry as a plain dict.
        field_mappings: Dict mapping FileResource field names to extraction rules.
            Each rule is a dict with keys: source, regex (optional),
            group (optional), transform (optional).

    Returns:
        Dict of parsed FileResource fields.
    """
    result = {}
    for field_name, rule in field_mappings.items():
        try:
            value = _extract_value(entry, rule)
            result[field_name] = value
        except Exception as e:
            logger.debug("Failed to extract field '%s': %s", field_name, e)
            result[field_name] = None
    _postprocess_parsed(result)
    return result


_RESOLUTION_P_RE = re.compile(r"^(\d{3,4})\s*[pP]$")


def _postprocess_parsed(result: dict) -> None:
    """Normalize parsed field values to canonical forms (in place).

    resolution: "1080P" / "1080 p" -> "1080p", so subscription conditions
    don't need to care about the publisher's casing. Only the plain
    ``<digits>p`` shape is touched; values like "1920x1080" pass through.
    """
    res = result.get("resolution")
    if isinstance(res, str):
        m = _RESOLUTION_P_RE.match(res.strip())
        if m:
            result["resolution"] = f"{m.group(1)}p"


def _extract_value(entry: dict, rule: dict) -> Any:
    """Extract a single value from an entry using a mapping rule."""
    source = rule.get("source", "")
    raw_value = _resolve_source(entry, source)

    if raw_value is None:
        return None

    raw_str = str(raw_value)

    # Apply regex extraction if specified. Case-insensitive: feed titles mix
    # cases freely ("1080p" vs "1080P", "WEB-DL" vs "web-dl"); extraction
    # should not depend on the publisher's casing. Normalization to a
    # canonical form happens in the transform step / post-processing below.
    regex = rule.get("regex")
    if regex:
        group = rule.get("group", 0)
        match = re.search(regex, raw_str, re.IGNORECASE)
        if match:
            raw_str = match.group(group)
        else:
            return None

    # Apply transform if specified
    transform = rule.get("transform")
    return _apply_transform(raw_str, transform)


def _resolve_source(entry: dict, source_path: str) -> Any:
    """Resolve a dotted/indexed path against a feedparser entry dict.

    Supports paths like: "title", "enclosures[0].url", "description"
    """
    if not source_path:
        return None

    parts = source_path.split(".")
    current = entry

    for part in parts:
        if current is None:
            return None

        # Handle array indexing: "enclosures[0]"
        bracket_match = re.match(r"^(.+?)\[(\d+)\]$", part)
        if bracket_match:
            key = bracket_match.group(1)
            index = int(bracket_match.group(2))
            if isinstance(current, dict):
                arr = current.get(key)
            else:
                arr = getattr(current, key, None)
            if arr and index < len(arr):
                current = arr[index]
            else:
                return None
        else:
            if isinstance(current, dict):
                current = current.get(part)
            else:
                current = getattr(current, part, None)

    return current


def _apply_transform(value: str, transform: str | None) -> Any:
    """Apply a type transformation to a string value."""
    if transform is None:
        return value

    if transform == "int":
        try:
            return int(value)
        except (ValueError, TypeError):
            return None
    elif transform == "float":
        try:
            return float(value)
        except (ValueError, TypeError):
            return None
    elif transform == "iso_datetime":
        try:
            return datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None
    elif transform == "lowercase":
        return value.lower()
    elif transform == "uppercase":
        return value.upper()

    return value


# ---------------------------------------------------------------------------
# Multi-episode batch (合集) detection
# ---------------------------------------------------------------------------

# Ordered pattern list: earlier patterns are tried first. Each pattern returns
# ``(is_batch, start, end)`` — start/end may be None when the title marks a
# batch (Season Pack / 全集 / Fin) without explicit boundaries.

# Range connectors: half-width tilde, hyphen, en-dash, full-width tilde
# (U+FF5E) and wave dash (U+301C).
_RANGE_DASH = r"[~\-–～〜]"
# Optional extras suffix after the end number, e.g. "01-28+SPx11".
_SP_SUFFIX = r"(?:\s*\+\s*SP\s*x?\s*\d+)?"

_BATCH_PATTERNS: list[tuple[re.Pattern[str], int, int]] = [
    # SxxEyy~zz  /  SxxEyy-zz  /  SxxEyy–zz  (with optional Exx suffix on rhs)
    (re.compile(rf"S\d+\s*E(\d{{1,3}})\s*{_RANGE_DASH}\s*E?(\d{{1,3}})", re.IGNORECASE), 1, 2),
    # [01-12 合集] / [01~12 Fin] / [01-12] with batch keyword nearby
    (re.compile(
        rf"\[\s*(\d{{1,3}})\s*{_RANGE_DASH}\s*(\d{{1,3}})\s*(?:合集|Batch|Fin|完结|全集|完整|Complete)?\s*\]",
        re.IGNORECASE,
    ), 1, 2),
    # Bracket whose content *ends* in an episode range, even when the bracket
    # also holds title text: "[青春猪头少年不会梦到圣诞服女郎 01-13]".
    # Year pairs like "[2020-2021]" match the shape but are rejected by the
    # sanity cap below (end > 999).
    (re.compile(
        rf"[\[【][^\]】]*?(\d{{1,3}})\s*{_RANGE_DASH}\s*(\d{{1,3}}){_SP_SUFFIX}\s*[\]】]",
        re.IGNORECASE,
    ), 1, 2),
    # Bare range in the context of an explicit season marker:
    # "... S01 | 01-24 ..." / "... Season 2 01-12 ..." / "第2季 01-12".
    # Possessive ``\d++`` keeps "S04 - 05" (single episode) from matching:
    # without it the season number backtracks ("S0") so "4 - 05" looks like
    # a range.
    (re.compile(
        rf"(?:S\d++|Season\s*\d++|第\s*\d++\s*季).{{0,80}}?(\d{{1,3}})\s*{_RANGE_DASH}\s*(\d{{1,3}}){_SP_SUFFIX}",
        re.IGNORECASE,
    ), 1, 2),
    # 01-12 合集 (no bracket)
    (re.compile(
        rf"(\d{{1,3}})\s*{_RANGE_DASH}\s*(\d{{1,3}})\s*(?:合集|Batch|Fin|完结|全集|完整|Complete)",
        re.IGNORECASE,
    ), 1, 2),
    # 第01-第12话 / 第01~12話
    (re.compile(rf"第\s*(\d{{1,3}})\s*{_RANGE_DASH}\s*第?\s*(\d{{1,3}})\s*[话話集]"), 1, 2),
]

_BATCH_KEYWORD_RE = re.compile(
    r"(?:Season\s*Pack|Full\s*Season|Batch|BD-?BOX|BDBOX|BD\s*Rip\s*Box|"
    r"全集|全季|合集|完整|完结|Complete\s*Series|"
    # "TV fin" marks a completed TV run; bare "Fin" is *not* a keyword — it
    # is also used on single final-episode releases.
    r"TV[\s_-]?fin|"
    r"整理搬运|合集整理|资源整合|全集整理|打包)",
    re.IGNORECASE,
)

# --- Season-marked whole-disc (BD) season packs -----------------------------
# Titles like "[LinRip] Show Season 2 [BDRip 1080p ...]" carry an explicit
# season marker, no episode number at all, and a whole-disc release token —
# almost certainly a full-season BD pack even without a range/batch keyword.

# Whole-disc release tokens. ``BD`` must be a standalone token (word
# boundaries) so a stray "BD" substring inside another word never matches;
# ``BDRip``/``BD-Rip`` are listed explicitly because ``\bBD\b`` cannot match
# inside "BDRip" (no boundary between "BD" and "Rip").
_BATCH_DISC_TOKEN_RE = re.compile(
    r"\b(?:BD|BD-?Rip|BDMV|BDRemux|Blu-?ray|BD-?BOX)\b",
    re.IGNORECASE,
)
# Episode tail " - NN" on single-episode releases ("Show S02 - 03 (BDRip)").
# Requires whitespace (or string start) before the hyphen so codec strings
# like "HEVC-10bit" never count as an episode number. ``_EPISODE_TAIL_RE``
# (below) is NOT reused here precisely because it lacks that guard.
_BATCH_EPISODE_DASH_RE = re.compile(r"(?:^|\s)-\s*\d{1,3}(?:v\d+)?\b")


def _title_has_episode_number(title: str) -> bool:
    """True when the title carries an episode number in any common form:
    SxxExx, a bracketed ``[NN]``, a `` - NN`` tail, or the ``NN(MM)`` form.
    """
    if _SXXEXX_RE.search(title) or _BRACKET_EPISODE_RE.search(title):
        return True
    if _BATCH_EPISODE_DASH_RE.search(title):
        return True
    per_season, absolute = detect_absolute_episode(title)
    return per_season is not None and absolute is not None


def _is_season_marked_disc_pack(title: str) -> bool:
    """Full-season BD pack heuristic: explicit season marker (``S02`` /
    ``Season 2`` / ``2nd Season`` / ``第N季`` incl. kanji numerals), NO
    episode number anywhere, and a whole-disc token (BD / BDRip / BDMV /
    BDRemux / Blu-ray / BD-BOX). A season marker alone is not enough (WEB
    simulcast singles may only carry the season), and a disc token alone is
    not enough (movies and season-less shows ship on BD too).
    """
    if not _BATCH_DISC_TOKEN_RE.search(title):
        return False
    if _title_has_episode_number(title):
        return False
    return (
        _FB_SEASON_SUFFIX_RE.search(title) is not None
        or _FB_SEASON_ORDINAL_RE.search(title) is not None
        or _FB_SEASON_S_RE.search(title) is not None
        or _FB_SEASON_KANJI_RE.search(title) is not None
    )


# Leading tag marking a compilation/archive torrent that bundles an entire
# work (TV + movies + CDs + manga, e.g. "[整理搬运] 猫眼三姐妹／猫之眼：TV动画+剧场版...").
# Such torrents should link to the primary work and be flagged as a batch.
_COMPILATION_TAG_RE = re.compile(
    r"^[\[【]\s*(?:整理搬运|合集整理|资源整合|全集整理|打包整理|整理|搬运|打包)\s*[\]】]\s*"
)
# Delimiters that separate the primary work name from alt titles / description
# in a compilation title: full/half-width slash, colon, opening paren/bracket.
_COMPILATION_DELIM_RE = re.compile(r"[／/：:（(【\[]|\s{2,}")


def extract_compilation_work_title(raw: str | None) -> str | None:
    """Extract the primary work name from a compilation/archive title.

    ``"[整理搬运] 猫眼三姐妹／猫之眼 (キャッツ・アイ)：TV动画+剧场版+漫画+CD..."``
    -> ``"猫眼三姐妹"``. The torrent bundles an entire work, so the resource
    should link to that work and be flagged ``is_batch``. Returns ``None`` when
    ``raw`` is not a compilation title (no leading tag).
    """
    if not raw:
        return None
    m = _COMPILATION_TAG_RE.match(raw)
    if not m:
        return None
    rest = raw[m.end():]
    work = _COMPILATION_DELIM_RE.split(rest, maxsplit=1)[0].strip(" -·　")
    return work or None


def detect_batch(title: str | None) -> tuple[bool, int | None, int | None]:
    """Heuristically detect whether a raw RSS title represents a multi-episode
    batch (合集) resource.

    Returns ``(is_batch, episode_start, episode_end)``. When the title marks a
    batch without explicit boundaries (e.g. "Season Pack", "全集"), the two
    integers are None but ``is_batch`` is True.

    The MetadataAgent LLM may later refine or overwrite these values; the
    pre-parser exists so downstream logic stays safe even when the LLM path
    fails or is disabled.
    """
    if not title:
        return False, None, None

    for pattern, gstart, gend in _BATCH_PATTERNS:
        m = pattern.search(title)
        if not m:
            continue
        try:
            start = int(m.group(gstart))
            end = int(m.group(gend))
        except (TypeError, ValueError):
            continue
        if end < start:
            start, end = end, start
        # Filter obvious false positives: ranges that look like resolution
        # tokens (e.g. "1920x1080") or single-year matches would already be
        # excluded by the leading anchors, but keep a sanity cap.
        if end - start > 200 or start < 0 or end > 999:
            continue
        return True, start, end

    if _BATCH_KEYWORD_RE.search(title):
        return True, None, None

    # Season-marked whole-disc pack ("Show Season 2 [BDRip 1080p ...]"):
    # explicit season marker + no episode number + BD/Blu-ray disc token.
    if _is_season_marked_disc_pack(title):
        return True, None, None

    return False, None, None


# ---------------------------------------------------------------------------
# Cross-season episode reconciliation — pre-parser
# ---------------------------------------------------------------------------

# ``NN(MM)`` — per-season NN with an absolute MM in parens. Common on Chinese
# fansub packs, e.g. "13(85)" means "S4 episode 13, cumulative episode 85".
# The regex only matches when the inner number is ≥ the outer one, which is
# the shape that makes sense (absolute count is ≥ per-season count).
_EPISODE_NN_MM_RE = re.compile(
    r"(?<!\d)"           # not part of a larger number
    r"(\d{1,3})"          # per-season NN
    r"\s*\(\s*"
    r"(\d{2,4})"          # absolute MM (usually 2+ digits so we don't match
                          # runtimes like (24) accidentally — see filter below)
    r"\s*\)"
)


def detect_absolute_episode(title: str | None) -> tuple[int | None, int | None]:
    """Best-effort ``NN(MM)`` extraction from ``title``.

    Returns ``(per_season_ep, absolute_ep)``. Both are ``None`` when the
    title doesn't use the double-labeled form. When we do get a hit we
    require ``absolute > per_season`` and ``absolute - per_season ≥ 10`` —
    otherwise the parenthesized number is very likely a runtime, part
    number, or resolution decorator rather than an absolute episode count.
    """
    if not title:
        return None, None
    for m in _EPISODE_NN_MM_RE.finditer(title):
        try:
            per_season = int(m.group(1))
            absolute = int(m.group(2))
        except (TypeError, ValueError):
            continue
        # Sanity check — a real "13(85)" jump implies at least ~10 episodes
        # of earlier seasons. Small gaps (13(15)) are almost always something
        # else (e.g. resolution "1080p (15GB)" shreds).
        if absolute > per_season and absolute - per_season >= 10:
            return per_season, absolute
    return None, None


# ---------------------------------------------------------------------------
# Subtitle language detection
# ---------------------------------------------------------------------------

# Ordered from most-specific to least-specific so combos like "简繁日" hit
# before "简繁", and both hit before "简" / "繁" on their own.
_SUBTITLE_LANG_PATTERNS: list[tuple[re.Pattern[str], list[str]]] = [
    (re.compile(r"简繁日", re.IGNORECASE), ["zh-CN", "zh-TW", "ja"]),
    (re.compile(r"简繁英", re.IGNORECASE), ["zh-CN", "zh-TW", "en"]),
    (re.compile(r"简繁", re.IGNORECASE), ["zh-CN", "zh-TW"]),
    (re.compile(r"(?:多国字幕|多语言|多語言|Multi[-_ ]?Sub)", re.IGNORECASE), ["multi"]),
    (re.compile(r"(?:\bCHS\b|简中|简体|GB(?![A-Z]))"), ["zh-CN"]),
    (re.compile(r"(?:\bCHT\b|繁中|繁体|繁體|BIG5)"), ["zh-TW"]),
    (re.compile(r"(?:\bJPN?\b|\bJAP\b|日语|日文|Japanese)", re.IGNORECASE), ["ja"]),
    (re.compile(r"(?:\bENG?\b|英字|英文|English)", re.IGNORECASE), ["en"]),
]


def detect_subtitle_langs(title: str | None) -> list[str]:
    """Return a de-duplicated list of BCP-47 language tags found in ``title``.

    An empty list means "parsed but no subtitle-language marker present". The
    caller decides whether to store ``None`` (never parsed) versus ``[]``
    (parsed, none found).

    Tags are appended in the order patterns match, preserving intent — e.g.
    ``"[CHS][CHT][ENG]"`` returns ``["zh-CN", "zh-TW", "en"]``.

    The sentinel tag ``"multi"`` is returned only when the title uses
    "multi-language" style shorthand without spelling out which languages.
    """
    if not title:
        return []
    seen: list[str] = []
    remaining = title
    for pattern, tags in _SUBTITLE_LANG_PATTERNS:
        if not pattern.search(remaining):
            continue
        for tag in tags:
            if tag not in seen:
                seen.append(tag)
        # Blank out matches so more general patterns don't re-fire on the same
        # substring (e.g. don't hit "CHS" inside a "简繁" span we already
        # translated to zh-CN + zh-TW).
        remaining = pattern.sub(" ", remaining)
    return seen


# ---------------------------------------------------------------------------
# Post-parse normalization
# ---------------------------------------------------------------------------

# The per-channel field_mapping regexes are LLM-generated and fragile. Two
# recurrent failure modes this normalizer repairs:
#   1. Multi-bracket titles ``[Group][Station]Work / Alt - EP``: the regex
#      strips only the first ``[...]`` so the second bracket leaks into
#      title_cn/title_en (e.g. ``"[ViuTV"``, ``"粵語]幪面超人 "``). That leaked
#      token then mis-directs the metadata agent (a TV-station name auto-links
#      to the station's Wikipedia article, spawning a bogus work).
#   2. Parenthetical tech blocks ``(WEB 1920x1080 AVC AACx2 ... CHT)``: the
#      WxH resolution, bare ``WEB`` source, and ``AACx2`` codec (the ``x``
#      breaks ``\bAAC\b``) are missed.
#
# The normalizer is CONSERVATIVE: it only repairs title fields that contain
# leaked brackets, and only fills tech fields that are None. Resources the
# field_mapping already parsed cleanly are untouched.

# All leading [..]/【..】 release-tag brackets (group / station / language).
_LEADING_BRACKETS_RE = re.compile(r"^(?:\s*[\[【][^\]】]*[\]】])+")
# Episode tail " - 42 ..." and any trailing [tech]/(tech) block, used to
# isolate the work-name segment(s) from a raw title.
_EPISODE_TAIL_RE = re.compile(r"\s*-\s*\d+\b.*$")
_TRAILING_TECH_RE = re.compile(r"\s*[\[【(（].*$")
_BILINGUAL_SPLIT_RE = re.compile(r"\s*[/／]\s*")
_CJK_LATIN_BOUNDARY_RE = re.compile(
    r"(?<=[\u3040-\u30ff\u3400-\u9fff])(?=[A-Za-z][A-Za-z0-9.' _-]{2,}$)"
)
_PACK_SUFFIX_RE = re.compile(
    r"\s+(?:S\d{1,2}|Season\s*\d+)\b(?:\s*[|｜].*)?$|"
    r"\s*[|｜]\s*\d{1,3}(?:\s*[-~～]\s*\d{1,3})?(?:\s*\+\s*SP\s*x?\d+)?\s*$",
    re.IGNORECASE,
)

_RESOLUTION_WXH_RE = re.compile(r"\b(\d{3,4})\s*[x×]\s*(\d{3,4})\b", re.IGNORECASE)
# Bare "1080p" / "1080P" form (the WXH regex only covers 1920x1080 shapes).
_RESOLUTION_BARE_P_RE = re.compile(r"\b(360|480|540|720|1080|1440|2160)\s*[pP]\b")
_RESOLUTION_BY_HEIGHT = {
    360: "360p", 480: "480p", 540: "540p", 720: "720p",
    1080: "1080p", 1440: "1440p", 2160: "2160p",
}
# Source tokens; bare ``WEB`` is recognized (the field_mapping only has WEB-DL).
_SOURCE_TOKEN_RE = re.compile(r"\b(WEB-DL|BDRip|WebRip|TVRip|WEB|BD-Rip|HDTV|DVD)\b", re.IGNORECASE)
# Codec tokens. The lookahead permits a trailing channel-count modifier
# (``AACx2``, ``AAC2.0``) without matching the codec inside a longer word
# (``AACoder``), which a plain ``\b...\b`` cannot do for ``AACx2``.
_AUDIO_CODEC_RE = re.compile(
    r"\b(AAC|FLAC|OPUS|AC-?3|E-?AC-?3|MP3|DTS|TrueHD)(?=[\s)\]x\d]|$)",
    re.IGNORECASE,
)
_VIDEO_CODEC_RE = re.compile(
    r"\b(AVC|x265|x264|HEVC|H\.?264|H\.?265|AV1|VP9)\b",
    re.IGNORECASE,
)
_CONTAINER_RE = re.compile(r"\b(MP4|MKV|AVI)\b", re.IGNORECASE)

# Episode/season fallbacks for formats the LLM-generated per-channel regexes
# commonly miss: bracketed episode numbers (``[03]``) and SxxExx / "Season 3"
# / "3rd Season" / 第N季 season markers. Applied only when the field_mapping
# left the field empty - a correctly parsed value is never overwritten.
#
# Fansub re-releases carry a version tag right after the episode number —
# ``[02v2]`` / ``S03E06v2`` mean episode 2 / 6, second revised release. The
# ``vN`` suffix is tolerated and dropped: the version is not part of the
# episode number and (for now) does not participate in dedup either.
_SXXEXX_RE = re.compile(r"\bS(\d{1,2})E(\d{1,3})(?:v\d+)?\b", re.IGNORECASE)
_SPECIAL_EPISODE_RE = re.compile(
    r"(?:^|[\s._\-\[(])(?:SP|SPECIAL|OVA|OAD)\s*[-_. ]?\s*(\d{1,3})"
    r"(?=[\s._\-\])]|$)",
    re.IGNORECASE,
)
_BRACKET_EPISODE_RE = re.compile(
    r"\[(\d{1,3})(?:v\d+|[a-z])?\]", re.IGNORECASE
)
_FB_SEASON_SUFFIX_RE = re.compile(r"\bSeason\s*(\d{1,2})\b", re.IGNORECASE)
_FB_SEASON_ORDINAL_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)\s+Season\b", re.IGNORECASE)
_FB_SEASON_S_RE = re.compile(r"\bS(\d{1,2})\b(?!E)", re.IGNORECASE)
_FB_SEASON_KANJI_RE = re.compile(r"第([一二三四五六七八九十\d]{1,3})季")
_KANJI_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# Release-year detection. Bracketed ``[2026]`` is preferred; otherwise a
# standalone 4-digit token. The lookarounds reject resolution (``1920x1080``)
# and codec-adjacent (``x264``… ok, but ``2026x`` / ``x2026``) contexts.
_YEAR_BRACKET_RE = re.compile(r"\[(19\d{2}|20\d{2})\]")
_YEAR_TOKEN_RE = re.compile(r"(?<![\dxX])(19\d{2}|20\d{2})(?![\dxX\d])")
_YEAR_MIN, _YEAR_MAX = 1950, 2100


def extract_title_year(title_raw: str) -> int | None:
    """Best-effort release year parsed from a raw title.

    Prefers a bracketed year (``[2026]``), else a standalone token
    (``Koukaku Kidoutai 2026``). ``[1080p]`` (only 1-3 digit episode brackets
    aside, ``1080`` fails the ``19xx|20xx`` prefix) and ``1920x1080`` never
    match. Values outside the 1950..2100 sanity range return ``None``.
    """
    if not title_raw:
        return None
    m = _YEAR_BRACKET_RE.search(title_raw)
    if m is None:
        m = _YEAR_TOKEN_RE.search(title_raw)
    if m is None:
        return None
    year = int(m.group(1))
    return year if _YEAR_MIN <= year <= _YEAR_MAX else None


def _kanji_to_int(text: str) -> int | None:
    if text.isdigit():
        return int(text)
    if text in _KANJI_DIGITS:
        return _KANJI_DIGITS[text]
    if len(text) == 2 and text[0] == "十":  # 十一..十九
        return 10 + _KANJI_DIGITS.get(text[1], 0)
    if len(text) == 2 and text[1] == "十":  # 二十..九十
        return _KANJI_DIGITS.get(text[0], 0) * 10
    return None


def extract_episode_fallback(title_raw: str) -> tuple[int | None, int | None]:
    """Best-effort (episode, season) from common fansub numbering formats.

    Returns ``(None, None)`` when nothing matches. ``[NN]`` is capped at
    three digits so tech brackets like ``[1080p]``/``[2026]`` never match.
    A re-release version tag glued to the number (``[02v2]``, ``S03E06v2``)
    is dropped — the episode number alone is returned.
    """
    m = _SXXEXX_RE.search(title_raw)
    if m:
        return int(m.group(2)), int(m.group(1))
    m = _BRACKET_EPISODE_RE.search(title_raw)
    episode = int(m.group(1)) if m else None
    season = None
    m = _FB_SEASON_SUFFIX_RE.search(title_raw)
    if m:
        season = int(m.group(1))
    elif (m := _FB_SEASON_ORDINAL_RE.search(title_raw)):
        season = int(m.group(1))
    elif (m := _FB_SEASON_S_RE.search(title_raw)):
        season = int(m.group(1))
    elif (m := _FB_SEASON_KANJI_RE.search(title_raw)):
        season = _kanji_to_int(m.group(1))
    return episode, season


# Episode-number forms trusted on a *filename* component only (directory
# names often carry batch ranges like "01-12", so bare/bracketed numbers in
# directories are not treated as episode markers):
#   "Show - 01.mkv"            -> 1   (dash form)
#   "Show 第03話.mkv"          -> 3   (kanji form)
_PATH_SPLIT_RE = re.compile(r"[/\\]+")
_DASH_EPISODE_RE = re.compile(r"\s-\s(\d{1,3})(?:v\d+)?(?=[\s.\[\(（【]|$)")
_KANJI_EPISODE_RE = re.compile(r"第\s*(\d{1,3})\s*[话話集]")
# Bare leading number in the FILENAME ("BD-folder convention": "S01/01 Title.mkv").
# Anchored at the name start so stray tech numbers deeper in the name
# ("... BDRip 1080p.mkv") never match; the trailing guard rejects resolution
# tokens ("1080p") and longer digit runs (dates).
_FILENAME_LEADING_EP_RE = re.compile(
    r"^(?:\s*\[[^\]]*\])*\s*(?:EP|E)?(\d{1,3})(?:v\d+)?(?=\s|\.|\[|$)",
    re.IGNORECASE,
)
_SEMICOLON_EP_RE = re.compile(r"\s(\d{1,3})(?:v\d+)?\s*;", re.IGNORECASE)


def _season_marker(text: str) -> int | None:
    """Season number from a single path component, or None."""
    m = _FB_SEASON_SUFFIX_RE.search(text)
    if m:
        return int(m.group(1))
    if (m := _FB_SEASON_ORDINAL_RE.search(text)):
        return int(m.group(1))
    if (m := _FB_SEASON_KANJI_RE.search(text)):
        return _kanji_to_int(m.group(1))
    if (m := _FB_SEASON_S_RE.search(text)):
        return int(m.group(1))
    return None


def extract_season_episode_from_path(path: str) -> tuple[int | None, int | None]:
    """Best-effort ``(season, episode)`` from every component of a file path.

    Season markers (``S01`` directory, ``Season 2``, ``第3季``) may live in
    ANY component — directory or filename. Episode numbers are only trusted
    from the filename component: directory names frequently carry batch
    ranges (``01-12``) that would otherwise be misread as episode 12.
    ``SxxEyy`` is recognized anywhere and yields both values. The filename
    episode forms cover ``[NN]``, ``- NN``, ``第N话`` and the BD-folder bare
    leading number (``S01/01 Title.mkv``). Either side of the returned pair
    may be None; both are None when nothing matches.
    """
    if not path:
        return None, None
    components = [c for c in _PATH_SPLIT_RE.split(path) if c]
    if not components:
        return None, None

    season: int | None = None
    episode: int | None = None
    for comp in components:
        m = _SXXEXX_RE.search(comp)
        if m:
            if season is None:
                season = int(m.group(1))
            if episode is None:
                episode = int(m.group(2))
        if season is None:
            season = _season_marker(comp)

    filename = components[-1]
    # Explicit special numbering is already a media-library canonical index,
    # so it is safe to map directly to Plex's Specials season.
    special = _SPECIAL_EPISODE_RE.search(filename)
    if special:
        return 0, int(special.group(1))
    if episode is None:
        m = _SEMICOLON_EP_RE.search(filename)
        if m:
            episode = int(m.group(1))
    if episode is None:
        m = _BRACKET_EPISODE_RE.search(filename)
        if m:
            episode = int(m.group(1))
    if episode is None:
        m = _DASH_EPISODE_RE.search(filename)
        if m:
            episode = int(m.group(1))
    if episode is None:
        m = _KANJI_EPISODE_RE.search(filename)
        if m:
            episode = int(m.group(1))
    if episode is None:
        # BD-folder convention: the season lives on the directory and the
        # filename is a bare number ("Frieren S01/01 [VOSTFR] ....mkv").
        m = _FILENAME_LEADING_EP_RE.match(filename)
        if m:
            episode = int(m.group(1))
    return season, episode

_CJK_RE = re.compile(r"[一-鿿]")
_ASCII_ONLY_RE = re.compile(r"[\x00-\x7f\s]+")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _has_bracket_leak(value: Any) -> bool:
    """True when an extracted field leaked bracket characters (``[ViuTV``)."""
    return value is not None and ("[" in str(value) or "]" in str(value))


def _title_core_segments(title_raw: str) -> list[str]:
    """Split a raw title into its work-name variant segments.

    Strips ALL leading release-tag brackets, drops the episode tail and any
    trailing tech block, then splits on `` / `` alt-title separators::

        "[jibaketa..][ViuTV粵語]幪面超人 / 假面騎士ZEZTZ - 42 [..] (..)"
        -> ["幪面超人", "假面騎士ZEZTZ"]
    """
    core = _LEADING_BRACKETS_RE.sub("", title_raw).strip()
    core = _EPISODE_TAIL_RE.sub("", core)
    core = _TRAILING_TECH_RE.sub("", core)
    core = _PACK_SUFFIX_RE.sub("", core).strip()
    return [s.strip() for s in _BILINGUAL_SPLIT_RE.split(core) if s.strip()]


def normalize_parsed_fields(title_raw: str | None, parsed: dict) -> dict:
    """Conservatively repair field_mapping output for common regex misses.

    See the module section header for the two failure modes this addresses.
    Only repairs title fields that leaked brackets and only fills tech fields
    that are ``None`` - a no-op for cleanly-parsed resources. When a title
    field is repaired, ``search_title`` is set to the latin variant if present
    (the best local-match signal for bilingual fansub titles such as
    "Ultraman Teo"), else the CJK variant.

    Tech values preserve the casing found in the title (matching the
    field_mapping's behavior); only ``resolution`` is canonicalized to the
    ``Np`` form.
    """
    out = dict(parsed)
    # Keep the legacy scalar mapping usable while exposing the canonical list
    # to downstream filters and API serializers.  A mapping may already emit
    # ``subtitle_groups``; in that case it is authoritative.
    if "subtitle_groups" not in out and out.get("subtitle_group"):
        out["subtitle_groups"] = normalize_subtitle_groups(out["subtitle_group"])
    elif "subtitle_groups" in out and out.get("subtitle_groups") is not None:
        out["subtitle_groups"] = normalize_subtitle_groups(out["subtitle_groups"])
    if not title_raw:
        return out

    segments = _title_core_segments(title_raw)
    cjk_seg = next((s for s in segments if _CJK_RE.search(s)), None)
    lat_seg = next(
        (s for s in segments if _ASCII_ONLY_RE.fullmatch(s) and _LATIN_RE.search(s)),
        None,
    )
    bilingual_title = cjk_seg is not None and lat_seg is not None
    cn_leaked = _has_bracket_leak(out.get("title_cn"))
    en_leaked = _has_bracket_leak(out.get("title_en"))
    if not out.get("title_cn") and cjk_seg:
        # Some feeds provide no title mapping at all. Recover the obvious CJK
        # work-name prefix from the cleaned release title; an adjacent latin
        # alias such as ``攻壳机动队THE.GHOST...`` is kept out of title_cn.
        title_parts = _CJK_LATIN_BOUNDARY_RE.split(cjk_seg, maxsplit=1)
        out["title_cn"] = title_parts[0].strip()
        adjacent_latin = title_parts[1].strip() if len(title_parts) == 2 else None
        if adjacent_latin and not out.get("title_en"):
            out["title_en"] = adjacent_latin
        if not out.get("search_title"):
            out["search_title"] = adjacent_latin or out["title_cn"]
    if (
        cn_leaked
        or en_leaked
        or (bilingual_title and not out.get("title_en"))
    ):
        if cn_leaked:
            out["title_cn"] = cjk_seg
        if en_leaked:
            out["title_en"] = lat_seg
        elif bilingual_title and not out.get("title_en"):
            out["title_en"] = lat_seg
        # Prefer the latin variant for search_title: series.title_en is the
        # romanized name local matching keys on, and bilingual titles bury the
        # searchable name in a later " / " segment.
        if bilingual_title or cn_leaked or en_leaked:
            out["search_title"] = lat_seg or cjk_seg or out.get("search_title")

    if not out.get("resolution"):
        m = _RESOLUTION_WXH_RE.search(title_raw)
        if m:
            out["resolution"] = _RESOLUTION_BY_HEIGHT.get(int(m.group(2)))
        else:
            m = _RESOLUTION_BARE_P_RE.search(title_raw)
            if m:
                out["resolution"] = f"{m.group(1)}p"
    if not out.get("source"):
        m = _SOURCE_TOKEN_RE.search(title_raw)
        if m:
            out["source"] = m.group(1)
    if not out.get("audio_codec"):
        m = _AUDIO_CODEC_RE.search(title_raw)
        if m:
            out["audio_codec"] = m.group(1)
    if not out.get("video_codec"):
        m = _VIDEO_CODEC_RE.search(title_raw)
        if m:
            out["video_codec"] = m.group(1)
    if not out.get("container"):
        m = _CONTAINER_RE.search(title_raw)
        if m:
            out["container"] = m.group(1)

    # Fansub group fallback: a leading "[Group]" / "【Group】" bracket is the
    # near-universal release convention, but per-channel regexes may only
    # cover one bracket style (or omit subtitle_group entirely).
    if not out.get("subtitle_group"):
        m = re.match(
            r"^\s*(?:\[([^\]]+)\]|【([^】]+)】|［([^］]+)］)",
            title_raw,
        )
        if m:
            candidate = next(group for group in m.groups() if group is not None).strip()
            # Pure-number brackets are years/tags, not group names.
            if candidate and not candidate.isdigit():
                out["subtitle_group"] = candidate
                out["subtitle_groups"] = normalize_subtitle_groups(candidate)

    # Episode/season fallbacks (bracket "[03]", SxxExx, "Season 3", 第N季) —
    # the per-channel episode regexes typically only cover the "- NN" form.
    if out.get("episode") is None or out.get("season") is None:
        fb_episode, fb_season = extract_episode_fallback(title_raw)
        if out.get("episode") is None and fb_episode is not None:
            out["episode"] = fb_episode
        if out.get("season") is None and fb_season is not None:
            out["season"] = fb_season

    # Release year ("[2026]" / standalone token) — guards local auto-link
    # against same-title remakes from a different year.
    if out.get("title_year") is None:
        year = extract_title_year(title_raw)
        if year is not None:
            out["title_year"] = year

    return out
