"""Deterministic Wikipedia season/episode parser (P2) - pure functions, no IO, no LLM.

For wikipedia-primary channels the seasons/episodes CONTENT comes from the
Wikipedia page itself (source-consistency rule: never enrich content from
other sources). This module extracts that content from raw wikitext.

Supported page conventions (confirmed against real pages, 2026-08):

- zh anime pages (e.g. 超超超超超喜歡你的100個女朋友):
  infobox ``話數 = {{ubl|第1季：全12話|第2季：全12話}}``; episode section
  ``=== 各話列表 ===`` built from ``{{劇集列表/base}}`` templates with
  ``Chapter = 第N季`` season markers and rows carrying ``Number = 第M話`` /
  ``Title`` / ``Subtitle`` (``{{lang|ja|...}}``-wrapped) / ``Aux5`` (air date,
  ``'''2023年'''<br />10月8日`` then continuing-year ``10月15日`` forms).
- ja anime pages (e.g. 無職転生, 君のことが大大大大大好きな100人の彼女):
  same structure with the regular variant ``{{エピソードリスト/base}}``,
  ``Chapter = 第N期``, infobox ``話数 = 第1期：全23話<br />第2期：全25話``
  (``<br />``-separated instead of ``{{ubl|...}}``) and kanji-numeral
  ``Number = 第一話`` rows. Pages without a 各話リスト section (e.g.
  攻殻機動隊 STAND ALONE COMPLEX) simply yield ``None``.

Episode numbering: within a chapter, rows keep their printed number UNLESS
the chapter's first row starts above 1 (absolute numbering continuing across
seasons, e.g. 第2季 starting at 第13話) - then rows are renumbered from 1
(episode = M - first_M + 1), so keys stay per-season (series_id, season,
episode).
"""

from __future__ import annotations

import re

# Episode-list template family. zh uses 劇集列表 (traditional) / 剧集列表
# (simplified), ja uses the regular variant エピソードリスト.
_TEMPLATE_MARKER = re.compile(r"(劇集列表|剧集列表|エピソードリスト)\s*/\s*base")

_SECTION_RE = re.compile(r"^(={2,6})\s*各話\s*(列表|リスト)\s*\1\s*$", re.MULTILINE)
_HEADING_RE = re.compile(r"^(={2,6})[^=\n].*\1\s*$", re.MULTILINE)

# Any {{Infobox animanga/<sub> ...}} block opening (Header/Novel/Manga/
# TVAnime/Cast/Footer/...) - used to delimit the TVAnime block's extent.
_ANIMANGA_BLOCK_RE = re.compile(r"\{\{\s*Infobox\s+animanga/", re.IGNORECASE)

_FULLWIDTH_DIGITS = str.maketrans("０１２３４５６７８９", "0123456789")

_KANJI_DIGITS = {
    "零": 0, "〇": 0,
    "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9,
}
_KANJI_UNITS = {"十": 10, "百": 100}


def _kanji_to_int(s: str) -> int | None:
    """Parse simple kanji numerals (一..九十九, 零/〇) used in 第N話 labels."""
    if not s:
        return None
    result = 0
    num = 0
    for ch in s:
        if ch in _KANJI_DIGITS:
            num = _KANJI_DIGITS[ch]
        elif ch in _KANJI_UNITS:
            result += (num or 1) * _KANJI_UNITS[ch]
            num = 0
        else:
            return None
    return result + num


def _normalize_digits(s: str) -> str:
    return s.translate(_FULLWIDTH_DIGITS)


# ---------------------------------------------------------------------------
# Text cleaning
# ---------------------------------------------------------------------------

_REF_BLOCK_RE = re.compile(r"<ref[^>]*>.*?</ref>", re.DOTALL | re.IGNORECASE)
_REF_SELF_RE = re.compile(r"<ref[^>]*/>", re.IGNORECASE)
_TAG_RE = re.compile(r"</?(?:small|span|br|nowiki|poem)[^>]*>", re.IGNORECASE)
_INNERMOST_TEMPLATE_RE = re.compile(r"\{\{([^{}]*)\}\}")

# Templates whose (joined) parameters carry display text we keep.
_KEEP_FIRST = {"nobr", "nowrap", "small", "smaller", "em", "ruby", "0"}
_KEEP_LAST = {"lang"}
_KEEP_JOINED = {"ubl", "unbulleted list", "vlist", "spml", "hlist-comma", "hlist"}


def _resolve_template(inner: str) -> str:
    parts = [p.strip() for p in inner.split("|")]
    name = parts[0].lower() if parts else ""
    args = [p for p in parts[1:] if p]
    if not args:
        return ""
    if name in _KEEP_FIRST:
        return args[0]
    if name in _KEEP_LAST:
        return args[-1]
    if name in _KEEP_JOINED:
        return " ".join(args)
    # Unknown templates (citations like {{Sfnp|...}}, notes like {{Efn2|...}},
    # metadata markers) carry no episode content - drop them entirely.
    return ""


def _strip_refs(s: str) -> str:
    s = _REF_BLOCK_RE.sub("", s)
    return _REF_SELF_RE.sub("", s)


def clean_text(s: str | None) -> str | None:
    """Reduce wikitext to its display text: refs/templates stripped, wikilinks
    unwrapped (``[[a|b]]`` -> ``b``, ``[[a]]`` -> ``a``), bold/italic marks and
    simple HTML tags removed."""
    if not s:
        return None
    s = _strip_refs(s)
    prev = None
    while "{{" in s and s != prev:
        prev = s
        s = _INNERMOST_TEMPLATE_RE.sub(lambda m: _resolve_template(m.group(1)), s)
    s = re.sub(r"\[\[([^\]|]*)\|([^\]|]*)\]\]", r"\2", s)
    s = re.sub(r"\[\[([^\]|]*)\]\]", r"\1", s)
    s = s.replace("'''", "").replace("''", "")
    s = _TAG_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s or None


# ---------------------------------------------------------------------------
# Template block scanning (balanced braces, link-aware splitting)
# ---------------------------------------------------------------------------


def _iter_top_templates(text: str):
    """Yield inner bodies of top-level ``{{...}}`` invocations in ``text``."""
    depth = 0
    start = -1
    i = 0
    n = len(text)
    while i < n - 1:
        two = text[i:i + 2]
        if two == "{{":
            if depth == 0:
                start = i
            depth += 1
            i += 2
            continue
        if two == "}}" and depth:
            depth -= 1
            i += 2
            if depth == 0 and start >= 0:
                yield text[start + 2:i - 2]
                start = -1
            continue
        i += 1


def _split_segments(inner: str) -> list[str]:
    """Split a template body on top-level ``|`` (respecting nested templates
    and wikilinks)."""
    parts: list[str] = []
    depth = 0
    ldepth = 0
    cur: list[str] = []
    i = 0
    n = len(inner)
    while i < n:
        two = inner[i:i + 2]
        if two == "{{":
            depth += 1
            cur.append(two)
            i += 2
            continue
        if two == "}}" and depth:
            depth -= 1
            cur.append(two)
            i += 2
            continue
        if two == "[[":
            ldepth += 1
            cur.append(two)
            i += 2
            continue
        if two == "]]" and ldepth:
            ldepth -= 1
            cur.append(two)
            i += 2
            continue
        if inner[i] == "|" and depth == 0 and ldepth == 0:
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(inner[i])
        i += 1
    parts.append("".join(cur))
    return parts


def _split_name_value(segment: str) -> tuple[str, str] | None:
    """Split ``name = value`` on the first top-level ``=``."""
    depth = 0
    ldepth = 0
    i = 0
    n = len(segment)
    while i < n:
        two = segment[i:i + 2]
        if two == "{{":
            depth += 1
            i += 2
            continue
        if two == "}}" and depth:
            depth -= 1
            i += 2
            continue
        if two == "[[":
            ldepth += 1
            i += 2
            continue
        if two == "]]" and ldepth:
            ldepth -= 1
            i += 2
            continue
        if segment[i] == "=" and depth == 0 and ldepth == 0:
            return segment[:i].strip(), segment[i + 1:].strip()
        i += 1
    return None


def _parse_episode_template(inner: str) -> dict | None:
    """Classify one top-level template. Returns::

        {"kind": "chapter", "season": int}
        {"kind": "row", "number": int, "title": str|None,
         "subtitle": str|None, "date_raw": str|None}

    or ``None`` for unrelated templates and header/footer blocks.
    """
    segments = _split_segments(inner)
    marker_seg = None
    for seg in segments[:2]:
        if "=" not in seg and _TEMPLATE_MARKER.search(seg):
            marker_seg = seg
            break
    if marker_seg is None:
        return None
    if re.search(r"/\s*(header|footer)", marker_seg):
        return None

    params: dict[str, str] = {}
    for seg in segments:
        nv = _split_name_value(seg)
        if nv:
            params.setdefault(nv[0].lower(), nv[1])

    chapter = params.get("chapter")
    if chapter:
        m = re.search(r"第\s*([0-9０-９]+)\s*[季期]", chapter)
        if m:
            return {"kind": "chapter", "season": int(_normalize_digits(m.group(1)))}
        return None  # unparseable chapter label - ignore, keep current season

    number = params.get("number")
    if number is None:
        return None
    num = _parse_episode_number(number)
    if num is None:
        return None  # 番外編 / unaired rows without a number
    return {
        "kind": "row",
        "number": num,
        "title": clean_text(params.get("title")),
        "subtitle": clean_text(params.get("subtitle")),
        "date_raw": params.get("aux5"),
    }


def _parse_episode_number(raw: str) -> int | None:
    text = _normalize_digits(_strip_refs(raw)).strip()
    m = re.match(r"^第?\s*([0-9]+)\s*話?$", text)
    if m:
        return int(m.group(1))
    m = re.match(r"^第\s*([零〇一二三四五六七八九十百]+)\s*話$", text)
    if m:
        return _kanji_to_int(m.group(1))
    return None


# ---------------------------------------------------------------------------
# Air dates
# ---------------------------------------------------------------------------

_YEAR_RE = re.compile(r"([0-9０-９]{4})\s*年")
_MONTH_DAY_RE = re.compile(r"([0-9０-９]{1,2})\s*月\s*([0-9０-９]{1,2})\s*日")


def _parse_air_date(date_raw: str | None, current_year: int | None) -> tuple[str | None, int | None]:
    """Parse Aux5 into (ISO date, updated current_year).

    Handles ``'''2023年'''<br />10月8日`` (sets the year) and the
    continuing-year form ``10月15日`` (reuses the running year).
    """
    if not date_raw:
        return None, current_year
    text = clean_text(date_raw) or ""
    text = _normalize_digits(text)
    year = current_year
    m_year = _YEAR_RE.search(text)
    if m_year:
        year = int(m_year.group(1))
    m_md = _MONTH_DAY_RE.search(text)
    if m_md and year:
        month, day = int(m_md.group(1)), int(m_md.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return f"{year:04d}-{month:02d}-{day:02d}", year
    return None, year


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def has_tvanime_infobox(wikitext: str | None) -> bool:
    """True when the page carries a ``{{Infobox animanga/TVAnime}}`` block.

    This is a deterministic anime signal for ``is_anime`` (see
    ``anime_signals``): only anime TV works use the animanga TVAnime infobox
    on zh/ja Wikipedia. Unlike :func:`parse_seasons_from_infobox` (which
    collapses "no TV block" and "unparseable counts" into the same None),
    this answers the plain presence question.
    """
    if not wikitext:
        return False
    return any(
        re.match(
            r"\{\{\s*Infobox\s+animanga/TVAnime",
            wikitext[m.start():],
            flags=re.IGNORECASE,
        )
        for m in _ANIMANGA_BLOCK_RE.finditer(wikitext)
    )


_ANIMANGA_FILM_RE = re.compile(
    r"\{\{\s*Infobox\s+animanga/(?:Movie|Film|OVA)", re.IGNORECASE
)


def has_animanga_film_infobox(wikitext: str | None) -> bool:
    """True when the page carries an animanga film block
    (``{{Infobox animanga/Movie|Film|OVA}}``) — the deterministic anime
    signal for theatrical/OVA works, complementing
    :func:`has_tvanime_infobox` (TV only)."""
    if not wikitext:
        return False
    return bool(_ANIMANGA_FILM_RE.search(wikitext))


def parse_seasons_from_infobox(wikitext: str | None) -> list[dict] | None:
    """Extract ``[{season_number, episode_count}]`` from the TV-anime infobox
    block ONLY.

    zh pages stack multiple ``{{Infobox animanga/...}}`` blocks (Novel/Manga/
    TVAnime), each carrying its own 話數/集數 field - the novel/manga counts
    (e.g. 小書痴's web-novel 全677話) must never be read as TV seasons. Only
    ``{{Infobox animanga/TVAnime ...}}`` blocks (the same name on both zh and
    ja Wikipedia) are scanned; a block runs from its opening marker to the
    next ``{{Infobox animanga/`` sub-template. Pages without a TVAnime block
    (e.g. the 史萊姆 main page, whose anime lives on a sub-page) return
    ``None``.

    Handles ``{{ubl|...}}`` wrappers and ``<br />``-separated variants (ja),
    全/共 count markers, 話數/话数/話数/集數 field names, digit and kanji
    season ordinals (第1季/第1期/第一季), and half/fullwidth digits. A plain
    ``全26話`` (no season marker) reads as a single season - but only when NO
    TV block carries season markers; plain counts in additional TVAnime
    blocks of season-marked pages belong to separately-titled sequel works
    (e.g. 小書痴 領主的養女) and are ignored.
    """
    if not wikitext:
        return None
    markers = [m.start() for m in _ANIMANGA_BLOCK_RE.finditer(wikitext)]
    tv_starts = [
        pos for pos in markers
        if re.match(r"\{\{\s*Infobox\s+animanga/TVAnime", wikitext[pos:], flags=re.IGNORECASE)
    ]
    if not tv_starts:
        return None
    block_fields: list[list[str]] = []
    for i, start in enumerate(tv_starts):
        following = [p for p in markers if p > start]
        end = following[0] if following else len(wikitext)
        block = wikitext[start:end]
        block_fields.append(
            re.findall(r"^\|\s*(?:[話话][數数]|集[數数])\s*=\s*(.+)$", block, flags=re.MULTILINE)
        )
    seasons: list[dict] = []
    seen: set[int] = set()
    plain_blocks: list[int] = []  # first plain count per block that has one
    for field_values in block_fields:
        block_plain: list[int] = []
        for value in field_values:
            value = _strip_refs(value)
            # Split into per-season items: ubl-style parameters or <br /> breaks.
            items: list[str] = []
            m_ubl = re.match(r"^\s*\{\{\s*(?:ubl|unbulleted list|vlist)\s*\|(.*)\}\}\s*$",
                             value, flags=re.DOTALL | re.IGNORECASE)
            if m_ubl:
                items = [p for p in _split_segments(m_ubl.group(1))]
            else:
                items = re.split(r"<br\s*/?>", value)
            for item in items:
                text = _normalize_digits(clean_text(item) or "")
                m = re.search(
                    r"第\s*([0-9]+|[一二三四五六七八九十]+)\s*[季期]\s*[：:]?\s*[全共]\s*(\d+)\s*[话話]",
                    text,
                )
                if m:
                    num = int(m.group(1)) if m.group(1).isdigit() else _kanji_to_int(m.group(1))
                    if num is not None and num not in seen:
                        seen.add(num)
                        seasons.append({"season_number": num, "episode_count": int(m.group(2))})
                    continue
                m_plain = re.match(r"^[全共]\s*(\d+)\s*[话話]", text)
                if m_plain:
                    block_plain.append(int(m_plain.group(1)))
        if block_plain:
            plain_blocks.append(block_plain[0])
    if seasons:
        return seasons
    # No season-marked entry anywhere: exactly one TVAnime block with a
    # plain count reads as one season (GiTS ``全26話``); several plain-only
    # blocks are separate works - too ambiguous to model.
    if len(plain_blocks) == 1:
        return [{"season_number": 1, "episode_count": plain_blocks[0]}]
    return None


def parse_episode_list(wikitext: str | None) -> dict | None:
    """Parse the 各話列表/各話リスト episode-list section.

    Returns::

        {
          "seasons": [{"season_number": int, "episode_count": int}, ...],
          "episodes": [{"season": int, "episode": int, "title": str|None,
                        "subtitle": str|None, "air_date": "YYYY-MM-DD"|None}, ...],
        }

    ``Chapter = 第N季/第N期`` markers set the current season; without any
    chapter marker all rows fall into season 1. Returns ``None`` when the
    page has no episode-list section (or no parseable rows).
    """
    if not wikitext:
        return None
    m_sec = _SECTION_RE.search(wikitext)
    if not m_sec:
        return None
    level = len(m_sec.group(1))
    start = m_sec.end()
    end = len(wikitext)
    for m_head in _HEADING_RE.finditer(wikitext, start):
        if len(m_head.group(1)) <= level:
            end = m_head.start()
            break
    section = wikitext[start:end]

    episodes: list[dict] = []
    current_season = 1
    chapter_first_number: int | None = None
    current_year: int | None = None
    saw_chapter = False
    for inner in _iter_top_templates(section):
        parsed = _parse_episode_template(inner)
        if not parsed:
            continue
        if parsed["kind"] == "chapter":
            current_season = parsed["season"]
            chapter_first_number = None
            saw_chapter = True
            continue
        number = parsed["number"]
        if chapter_first_number is None:
            chapter_first_number = number
        # Per-chapter renumbering: chapters continuing absolute numbering
        # (first row > 1) are rebased to episode 1; chapters starting at 0/1
        # keep their printed numbers.
        episode_num = (
            number - chapter_first_number + 1 if chapter_first_number > 1 else number
        )
        air_date, current_year = _parse_air_date(parsed["date_raw"], current_year)
        episodes.append({
            "season": current_season,
            "episode": episode_num,
            "title": parsed["title"],
            "subtitle": parsed["subtitle"],
            "air_date": air_date,
        })

    if not episodes:
        return None
    counts: dict[int, int] = {}
    for ep in episodes:
        counts[ep["season"]] = counts.get(ep["season"], 0) + 1
    seasons = [
        {"season_number": s, "episode_count": counts[s]} for s in sorted(counts)
    ]
    if not saw_chapter:
        seasons = [{"season_number": 1, "episode_count": len(episodes)}]
    return {"seasons": seasons, "episodes": episodes}
