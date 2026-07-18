"""Dynamic resource parser using per-channel field mappings.

Uses the new field_mapping format with list_locator + field_mappings.
Backward compatible with the old flat dict format.
"""

import logging
import re
from datetime import datetime
from typing import Any

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
    return result


def _extract_value(entry: dict, rule: dict) -> Any:
    """Extract a single value from an entry using a mapping rule."""
    source = rule.get("source", "")
    raw_value = _resolve_source(entry, source)

    if raw_value is None:
        return None

    raw_str = str(raw_value)

    # Apply regex extraction if specified
    regex = rule.get("regex")
    if regex:
        group = rule.get("group", 0)
        match = re.search(regex, raw_str)
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

_BATCH_PATTERNS: list[tuple[re.Pattern[str], int, int]] = [
    # SxxEyy~zz  /  SxxEyy-zz  /  SxxEyy–zz  (with optional Exx suffix on rhs)
    (re.compile(r"S\d+\s*E(\d{1,3})\s*[~\-–]\s*E?(\d{1,3})", re.IGNORECASE), 1, 2),
    # [01-12 合集] / [01~12 Fin] / [01-12] with batch keyword nearby
    (re.compile(
        r"\[\s*(\d{1,3})\s*[~\-–]\s*(\d{1,3})\s*(?:合集|Batch|Fin|完结|全集|完整|Complete)?\s*\]",
        re.IGNORECASE,
    ), 1, 2),
    # 01-12 合集 (no bracket)
    (re.compile(
        r"(\d{1,3})\s*[~\-–]\s*(\d{1,3})\s*(?:合集|Batch|Fin|完结|全集|完整|Complete)",
        re.IGNORECASE,
    ), 1, 2),
    # 第01-第12话 / 第01~12話
    (re.compile(r"第\s*(\d{1,3})\s*[~\-–]\s*第?\s*(\d{1,3})\s*[话話集]"), 1, 2),
]

_BATCH_KEYWORD_RE = re.compile(
    r"(?:Season\s*Pack|Full\s*Season|Batch|BD-?BOX|BDBOX|BD\s*Rip\s*Box|"
    r"全集|全季|合集|完整|完结|Complete\s*Series|"
    r"整理搬运|合集整理|资源整合|全集整理|打包)",
    re.IGNORECASE,
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

_RESOLUTION_WXH_RE = re.compile(r"\b(\d{3,4})\s*[x×]\s*(\d{3,4})\b", re.IGNORECASE)
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
    return [s.strip() for s in core.split("/") if s.strip()]


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
    if not title_raw:
        return out

    if _has_bracket_leak(out.get("title_cn")) or _has_bracket_leak(out.get("title_en")):
        segments = _title_core_segments(title_raw)
        cjk_seg = next((s for s in segments if _CJK_RE.search(s)), None)
        lat_seg = next(
            (s for s in segments if _ASCII_ONLY_RE.fullmatch(s) and _LATIN_RE.search(s)),
            None,
        )
        if _has_bracket_leak(out.get("title_cn")):
            out["title_cn"] = cjk_seg
        if _has_bracket_leak(out.get("title_en")):
            out["title_en"] = lat_seg
        # Prefer the latin variant for search_title: series.title_en is the
        # romanized name local matching keys on, and bilingual titles bury the
        # searchable name in a later " / " segment.
        out["search_title"] = lat_seg or cjk_seg or out.get("search_title")

    if not out.get("resolution"):
        m = _RESOLUTION_WXH_RE.search(title_raw)
        if m:
            out["resolution"] = _RESOLUTION_BY_HEIGHT.get(int(m.group(2)))
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

    return out
