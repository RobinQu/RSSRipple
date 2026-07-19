"""Wikipedia query cleaning + candidate-query generation.

Pure leaf module - no DB, no LLM. Extracted verbatim from metadata_agent.py
(Phase 0 leaf extraction): strip episode/season/quality markers from a title
fragment so the Wikipedia search targets the work name, and emit up to 6
(query, lang) candidate searches (zh/en/ja) for the single-LLM-judge path.
"""
from __future__ import annotations

import re
from typing import Any

from app.services.resource_parser import strip_season_from_title

# Episode/season/quality markers stripped from a Wikipedia search fragment so
# the query targets the work name, not a specific release. Applied to the
# per-fragment query, not to stored data, so aggressive is fine.
_QUERY_EPISODE_TAIL_RE = re.compile(r"\s*[-－]\s*\d+.*$")
_QUERY_QUALITY_TAIL_RE = re.compile(
    r"\s+(?:\d{3,4}p|4K|2160P?|1080[IP]?|720P?|HEVC|AVC|x26[45]|H\.?26[45]|"
    r"AAC|FLAC|MP[34]|WEB[- ]?DL|WebRip|BDRip|BluRay|10bit|8bit|GB|BIG5|CHS|CHT)\b.*$",
    flags=re.IGNORECASE,
)
_QUERY_EPISODE_MARKER_RE = re.compile(
    r"\s*[\[【]\s*\d{1,3}\s*(?:v\d+)?\s*[\]】]"   # [01] / [01v2]
    r"|\s*第\s*\d{1,3}\s*[话話集]"
    r"|\s*EP\s*\d{1,3}\b"
    r"|\s*[#＃]\s*\d{1,3}\b",
    flags=re.IGNORECASE,
)
_QUERY_DECORATIVE_TAIL_RE = re.compile(r"\s*[～~][^～~]*[～~]\s*$")
# Alt-title parenthetical "(新世紀エヴァンゲリオン)" / "(Neon Genesis Evangelion)"
# and description/arc tails after a colon "：通往大人的阶梯" / "：TV动画+剧场版".
_QUERY_PAREN_RE = re.compile(r"[（(][^）)]*[）)]")
_QUERY_COLON_TAIL_RE = re.compile(r"\s*[：:].*$")
# Trailing unicode roman numerals used as season markers (无职转生Ⅲ -> 无职转生).
_QUERY_ROMAN_TAIL_RE = re.compile(r"\s*[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+\s*$")
# First-occurrence season/episode marker; the prefix before it is the base
# work name (e.g. "无职转生 3期" -> "无职转生", "Mushoku Tensei S3 - 03" ->
# "Mushoku Tensei", "樱桃小丸子第二期 1538 ..." -> "樱桃小丸子"). Also splits
# off a trailing romaji/English alt-title appended to a CJK work name
# ("二十世纪电气目录 Nijusseiki Denki Mokuroku" -> "二十世纪电气目录").
_QUERY_SEASON_EP_SPLIT_RE = re.compile(
    r"\s*[-－]\s*\d"
    r"|\s+S\d{1,2}\b"
    r"|第[一二三四五六七八九十百零千两\d]+\s*[季期话話集]"
    r"|\d{1,2}\s*[季期]"
    r"|Season\s*\d+"
    r"|\s*[\[【]\s*\d"
    r"|\sEP\s*\d"
    r"|\s[#＃]\s*\d"
    r"|\s*[：:]"
    r"|\s*[（(]"
    r"|(?<=[一-鿿぀-ヿ])\s+[A-Za-z]",
    flags=re.IGNORECASE,
)
_KANA_RE = re.compile(r"[぀-ヿ]")
_CJK_RE = re.compile(r"[一-鿿぀-ヿ]")
# Bracket pair with content captured (both [] and 【】).
_BRACKET_PAIR_CAPTURE_RE = re.compile(r"[\[【]([^\]】]*)[\]】]")
# Hint that a bracket's content is release metadata (resolution / codec /
# subtitle lang / etc.) rather than a work-name fragment. Matched as a
# substring so "[简繁日内封字幕]" and "[AVC 8bit]" both drop, while
# "[樱桃小丸子第二期(Chibi Maruko-chan II)]" stays.
_BRACKET_METADATA_HINT_RE = re.compile(
    r"\d{3,4}p|4K|2160|1080|720|HEVC|AVC|x26[45]|H\.?26[45]|AAC|FLAC|MP[34]|"
    r"WEB[- ]?DL|WebRip|BDRip|BluRay|BD-?BOX|BIG5|CHS|CHT|"
    r"简体|繁体|简繁|简日|繁日|简中|繁中|内封|内嵌|内挂|外挂|双语|字幕|"
    r"Fin|Complete|Batch|合集|全集|招募|翻译|\bGB\b",
    flags=re.IGNORECASE,
)
_BRACKET_PURE_DIGIT_RE = re.compile(r"^\d{1,4}(?:v\d+)?$")
_BRACKET_DATE_RE = re.compile(r"^\d{4}[.\-/]\d{1,2}")
# Station / platform / broadcaster tokens that appear in fansub titles but
# are NOT work names. A field-mapping leak can surface one as the search_title
# (e.g. "[ViuTV粵語]" -> "ViuTV"), and its Wikipedia page (the ViuTV *television
# station*) title-matches the query well enough to slip past auto-link. Used
# two ways: A4 drops a bracket whose content contains one; A1 skips a query
# that IS one (fullmatch, so "Tokyo MX"/"BS 11" match but "Tokyo" alone won't).
_NON_WORK_NAME_TOKEN_RE = re.compile(
    r"ViuTV|TVB|CCTV|NHK|MBS|TBS|KBS|MBC|SBS|TVA|YTV|BS\s*11|AT-?X|"
    r"Fuji\s*TV|Nippon\s*TV|Tokyo\s*MX|"
    r"Bilibili|Netflix|Disney\+|Disney\s*Plus|Hulu|"
    r"Prime\s*Video|Amazon\s*Prime|HBO\s*Max|"
    r"Crunchyroll|Funimation|IQIYI|Youku|Tencent\s*Video|"
    r"爱奇艺|優酷|优酷|腾讯视频|"
    r"YouTube",
    flags=re.IGNORECASE,
)
# Any unbalanced bracket char left over after the pair-sub - an upstream
# regex that ate only the closing ']' (a field-mapping leak) leaves a stray
# '[' that would otherwise travel into the Wikipedia query.
_ORPHAN_BRACKET_RE = re.compile(r"[\[\]【】]")
# Alt-title separator: half-width " / " or full-width "／".
_ALT_TITLE_SPLIT_RE = re.compile(r"\s*[／/]\s*")


def _clean_query(part: str) -> str:
    """Strip episode/quality/season markers from a title fragment so the
    Wikipedia search query targets the work name, not a specific release."""
    if not part:
        return ""
    part = _BRACKET_PAIR_CAPTURE_RE.sub(" ", part)  # drop [metadata] / 【metadata】
    part = _ORPHAN_BRACKET_RE.sub(" ", part)  # drop unbalanced [】left by an upstream leak
    part = _QUERY_PAREN_RE.sub(" ", part)  # drop （alt-title） parentheticals
    part = _QUERY_COLON_TAIL_RE.sub("", part)  # drop ：arc/description tail
    part = _QUERY_EPISODE_TAIL_RE.sub("", part)
    part = _QUERY_QUALITY_TAIL_RE.sub("", part)
    part = _QUERY_EPISODE_MARKER_RE.sub("", part)
    part = _QUERY_DECORATIVE_TAIL_RE.sub("", part)
    part = _QUERY_ROMAN_TAIL_RE.sub("", part)  # trailing Ⅲ season marker
    part = strip_season_from_title(part)
    return part.strip(" -/|:：·～~。.")


def _work_name_prefix(part: str) -> str:
    """Return the work-name prefix before the first season/episode marker.

    ``"无职转生 3期"`` -> ``"无职转生"``; ``"Mushoku Tensei S3 - 03"`` ->
    ``"Mushoku Tensei"``. Returns empty when the marker starts the fragment
    (nothing useful before it). Caller already ran :func:`_clean_query`.
    """
    m = _QUERY_SEASON_EP_SPLIT_RE.search(part)
    if not m:
        return ""
    return part[: m.start()].strip(" -/|:：·～~")


def _candidate_queries(raw_title: str, resource: Any | None = None) -> list[tuple[str, str]]:
    """Up to 6 (query, lang) wikipedia searches for the judge path.

    Prefers pre-parser hints (search_title / title_cn -> zh, title_en -> en)
    when a resource is available, then derives more from the raw title (drop
    [subtitle groups], split on " / "). For each fragment it emits BOTH the
    cleaned form AND the season-stripped work-name prefix, so a noisy
    "无职转生 3期" still queries the base "无职转生". CJK fragments search
    Chinese Wikipedia, and Japanese Wikipedia too when the fragment carries
    kana (the canonical anime page usually lives on ja). Dedupes.
    """
    queries: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(q: str | None, lang: str) -> None:
        q = _clean_query(q or "")
        if not q:
            return
        if _NON_WORK_NAME_TOKEN_RE.fullmatch(q):
            return  # A1: a bare station/platform token is not a work name
        key = (q.lower(), lang)
        if key in seen:
            return
        seen.add(key)
        queries.append((q, lang))

    def add_variants(part: str, lang: str) -> None:
        cleaned = _clean_query(part)
        if not cleaned:
            return
        add(cleaned, lang)
        prefix = _work_name_prefix(cleaned)
        if prefix and prefix.lower() != cleaned.lower():
            add(prefix, lang)

    if resource is not None:
        st = getattr(resource, "search_title", None)
        if st:
            add_variants(st, "zh" if _CJK_RE.search(st) else "en")
        if getattr(resource, "title_cn", None):
            add_variants(resource.title_cn, "zh")
        if getattr(resource, "title_en", None):
            add_variants(resource.title_en, "en")

    # Build candidate fragments from the raw title. Brackets usually hold
    # release metadata ([1080p], [01], [CHS], ...), so their content is dropped
    # - UNLESS a bracket's content looks like a work name (no metadata hint,
    # not a pure episode number / date), in which case it's kept as an extra
    # fragment. This recovers the work name on multi-bracket titles like
    # "[SweetSub][小書痴...][S04][13]" where dropping every bracket would
    # leave nothing to search.
    bracket_parts: list[str] = []

    def _strip_brackets(m: re.Match) -> str:
        content = m.group(1).strip()
        if (
            content
            and not _BRACKET_METADATA_HINT_RE.search(content)
            and not _NON_WORK_NAME_TOKEN_RE.search(content)  # A4: [ViuTV粵語]/[TVB]/[Netflix]
            and not _BRACKET_PURE_DIGIT_RE.match(content)
            and not _BRACKET_DATE_RE.match(content)
        ):
            bracket_parts.append(content)
        return " "

    outside = _BRACKET_PAIR_CAPTURE_RE.sub(_strip_brackets, raw_title)
    fragments = [p.strip() for p in _ALT_TITLE_SPLIT_RE.split(outside) if p.strip()]
    fragments.extend(bracket_parts)

    for part in fragments:
        if not part:
            continue
        if _CJK_RE.search(part):
            add_variants(part, "zh")
            if _KANA_RE.search(part):
                add_variants(part, "ja")
        else:
            add_variants(part, "en")

    return queries[:6]
