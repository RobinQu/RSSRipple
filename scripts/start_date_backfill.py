"""One-off backfill: fill missing ``TVSeries.start_date`` (release year).

Why: before the fix in ``_attach_wikipedia_content`` / ``_normalize_finalize_dates``,
the wikipedia channel source never produced a ``start_date`` — the LLM judge
schema has no date field and the deterministic parser only merged
seasons/episodes — so wikipedia-matched series kept ``start_date=NULL``
forever (the update branch only writes when a value is present). New matches
derive it from the earliest episode ``air_date``; this script repairs rows
created before that.

Two phases per series with ``start_date IS NULL``:

1. **Offline**: derive from existing ``episodes`` rows (``MIN(air_date)``) —
   no network, covers works whose episode list was already upserted.
2. **Wikipedia fetch**: for the remainder, fetch wikitext and derive the
   start date by priority: (a) earliest ``air_date`` from the deterministic
   ``parse_episode_list``; (b) the same parser over episode-list sub-pages
   (long series split them out, e.g. "葬送的芙莉蓮各話列表" — located via
   ``{{main}}``/``{{see also}}`` hints and conventional naming, en tries
   "List of <title> episodes"); (c) earliest infobox broadcast-start field
   (放送開始/播放開始, en ``first``). Page location tries, in order:
   a. the exact ``wikipedia_url`` recorded in MetadataCache matched_entity
      (the pipeline knew the page's language at match time; the series row
      only kept the per-language pageid, which is meaningless without it);
   b. ``series.wikipedia_url``;
   c. last resort: resolve the pageid against zh/ja/en wikis in order.
   Series without parseable dates are reported and left untouched (run
   ``wikipedia_seasons_eval.py --apply`` separately if their episode rows
   are also missing).

NOTE on locking: the embedded-Turso backend holds a single-process exclusive
file lock, so STOP the app (``docker compose stop app``) before running this
against the dev database, then start it again afterwards.

Dry-run by default; pass ``--apply`` to write.

Usage:
    uv run python scripts/start_date_backfill.py [--apply] [--limit N] [--delay S]
"""

import argparse
import asyncio
import json
import re
from datetime import date
from urllib.parse import unquote

import httpx
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session_factory
from app.models.episode import Episode
from app.models.series import TVSeries
from app.services.metadata_wikipedia_client import (
    _WIKIPEDIA_USER_AGENT,
    fetch_wikipedia_wikitext,
)
from app.services.wikipedia_episode_parser import parse_episode_list

APPLY_BATCH_SIZE = 20
_WIKI_URL_RE = re.compile(r"https?://([a-z-]+)\.wikipedia\.org/wiki/([^#?]+)")
_PAGEID_RE = re.compile(r"^wikipedia:(\d+)$")
# Order for the last-resort pageid resolution; zh catalog dominates.
_PAGEID_LANGS = ("zh", "ja", "en")

# Long series keep their episode list on a sub-page instead of the work's
# main page (e.g. zh "葬送的芙莉蓮" -> "葬送的芙莉蓮各話列表"). Conventional
# sub-page suffixes per language, plus {{main}}/{{see also}} links whose
# target hints at an episode list or an anime-specific page.
_SUBPAGE_SUFFIXES = {
    "zh": ("各話列表", "各话列表", "剧集列表", "劇集列表"),
    "ja": ("のエピソード一覧", "エピソード一覧"),
}
_MAIN_LINK_RE = re.compile(r"\{\{(?:[Mm]ain|[Ss]ee ?also|参见|參見)\|([^}|]+)")
_SUBPAGE_HINT_RE = re.compile(r"各[話话]|[剧劇]集|エピソード|[(（][动動][画畫][)）]|episodes", re.IGNORECASE)

# Last-resort broadcast-start extraction from infobox fields
# (放送開始 / 播放開始 / 放送期間 / 播放期間; en uses "first" inside the
# animanga infobox). Dates may be "1998年4月3日", "{{Start date|1998|4|3}}"
# or "April 3, 1998". Extraction is scoped to the anime infobox blocks
# (TVAnime for zh/ja, Video for en) so a manga block's earlier serialization
# start is not picked up; unscoped fields would also hit "|first=" author
# names inside citation templates.
_ANIME_BLOCK_RE = re.compile(
    r"\{\{Infobox animanga/(?:TVAnime|Video).*?(?=\{\{Infobox animanga/|\Z)",
    re.DOTALL,
)
_BROADCAST_FIELD_RE = re.compile(
    r"(?:放送開始|播放開始|放送期間|播放期間)\s*=\s*(.{0,60})"
)
# en: "| first = <value>" must start with a date (real infobox dates do;
# citation "|first=Karen|title=..." does not).
_EN_FIRST_RE = re.compile(r"^\|\s*first\s*=\s*(.{0,60})$", re.MULTILINE)
_ZH_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日?")
_START_TPL_RE = re.compile(r"\{\{(?:[Ss]tart ?date|dts)\|(\d{4})\|(\d{1,2})\|(\d{1,2})")
_EN_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)
_EN_DATE_RE = re.compile(
    r"(" + "|".join(_EN_MONTHS) + r")\s+(\d{1,2}),\s*(\d{4})"
)
_BARE_YEAR_RE = re.compile(r"((?:19|20)\d{2})")


def _dates_in_text(text: str) -> list[date]:
    """All parseable dates in a wikitext snippet, most specific first."""
    out: list[date] = []
    for m in _ZH_DATE_RE.finditer(text):
        out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    for m in _START_TPL_RE.finditer(text):
        out.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    for m in _EN_DATE_RE.finditer(text):
        out.append(
            date(int(m.group(3)), _EN_MONTHS.index(m.group(1)) + 1, int(m.group(2)))
        )
    if not out:
        for m in _BARE_YEAR_RE.finditer(text):
            out.append(date(int(m.group(1)), 1, 1))
    return out


def _infobox_start_date(wikitext: str) -> date | None:
    """Earliest broadcast-start date in the anime infobox blocks.

    Without an animanga block, zh/ja broadcast fields (放送開始…) are still
    specific enough to scan unscoped; the en ``first`` field is not (it also
    names citation authors, and manga blocks carry the earlier serialization
    start), so en pages without a Video block yield nothing.
    """
    blocks = _ANIME_BLOCK_RE.findall(wikitext)
    candidates: list[date] = []
    if blocks:
        for block in blocks:
            for m in _BROADCAST_FIELD_RE.finditer(block):
                candidates.extend(_dates_in_text(m.group(1)))
            for m in _EN_FIRST_RE.finditer(block):
                value = m.group(1).lstrip()
                # The value must *start* with a date-ish token, otherwise
                # this is a citation author field or similar.
                if re.match(r"(?:\{\{|\d{4}|" + "|".join(_EN_MONTHS) + r")", value):
                    candidates.extend(_dates_in_text(value))
    else:
        for m in _BROADCAST_FIELD_RE.finditer(wikitext):
            candidates.extend(_dates_in_text(m.group(1)))
    return min(candidates) if candidates else None


def _episode_start_date(wikitext: str) -> date | None:
    """Earliest air_date from the deterministic episode-list parser."""
    list_data = parse_episode_list(wikitext)
    air_dates = sorted(
        ep["air_date"]
        for ep in ((list_data or {}).get("episodes") or [])
        if ep.get("air_date")
    )
    if not air_dates:
        return None
    try:
        return date.fromisoformat(air_dates[0])
    except ValueError:
        return None


async def _episode_start_via_subpages(title: str, lang: str, wikitext: str) -> date | None:
    """Try episode-list sub-pages: {{main}} hints, then conventional names."""
    subpages: list[str] = []
    for m in _MAIN_LINK_RE.finditer(wikitext):
        target = m.group(1).strip()
        if _SUBPAGE_HINT_RE.search(target) and target not in subpages:
            subpages.append(target)
    if lang == "en" and not title.lower().startswith("list of"):
        subpages.append(f"List of {title} episodes")
    for suffix in _SUBPAGE_SUFFIXES.get(lang, ()):
        subpages.append(f"{title}{suffix}")
    for sub in subpages:
        sub_wt = await fetch_wikipedia_wikitext(sub, lang)
        if not sub_wt:
            continue
        d = _episode_start_date(sub_wt)
        if d is not None:
            return d
        # The anime sub-page may lack a parseable episode list but still
        # carry an infobox broadcast start (e.g. "Re:從零開始的異世界生活
        # (動畫)").
        d = _infobox_start_date(sub_wt)
        if d is not None:
            return d
    return None


def _lang_title_from_url(url: str) -> tuple[str, str] | None:
    m = _WIKI_URL_RE.search(url or "")
    if not m:
        return None
    return m.group(1), unquote(m.group(2)).replace("_", " ")


async def _cache_wikipedia_urls(db: AsyncSession, series: TVSeries) -> list[str]:
    """wikipedia_urls recorded in MetadataCache entities for this work.

    The cache payload remembers the exact page (language + title) the
    pipeline selected; the series row only kept the bare pageid.
    """
    m = _PAGEID_RE.match(series.external_id or "")
    if not m:
        return []
    pageid = m.group(1)
    rows = (
        await db.execute(
            text(
                "SELECT metadata_json FROM metadata_cache "
                "WHERE source = 'metadata_agent:wikipedia' "
                "AND metadata_json LIKE :pat"
            ),
            {"pat": f"%{pageid}%"},
        )
    ).scalars().all()
    urls: list[str] = []
    wanted = {series.external_id}
    for payload in rows:
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                continue
        me = (payload or {}).get("matched_entity") or {}
        ids = {me.get("external_id")} | {
            a.get("id") for a in (me.get("alt_external_ids") or [])
        }
        if not (wanted & ids):
            continue
        url = me.get("wikipedia_url")
        if url and url not in urls:
            urls.append(url)
    return urls


async def _title_from_page_id(page_id: str) -> tuple[str, str] | None:
    """Last-resort pageid resolution, trying zh/ja/en in order."""
    async with httpx.AsyncClient(timeout=15) as client:
        for lang in _PAGEID_LANGS:
            try:
                resp = await client.get(
                    f"https://{lang}.wikipedia.org/w/api.php",
                    params={
                        "action": "query",
                        "format": "json",
                        "pageids": page_id,
                        "redirects": 1,
                    },
                    headers={"User-Agent": _WIKIPEDIA_USER_AGENT},
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:  # noqa: BLE001 - try the next language
                print(f"    pageid resolve failed ({lang}): {e}")
                continue
            for page in (data.get("query") or {}).get("pages", {}).values():
                if page.get("title"):
                    return lang, page["title"]
    return None


async def _derive_from_wikipedia(
    db: AsyncSession, series: TVSeries
) -> tuple[date | None, str | None]:
    """Fetch + parse the series' wikipedia page; earliest episode air_date.

    Returns ``(start_date, failure_reason)`` — exactly one is set. Writes
    nothing.
    """
    candidates: list[tuple[str, str]] = []
    for url in await _cache_wikipedia_urls(db, series):
        lt = _lang_title_from_url(url)
        if lt and lt not in candidates:
            candidates.append(lt)
    lt = _lang_title_from_url(series.wikipedia_url or "")
    if lt and lt not in candidates:
        candidates.append(lt)
    m = _PAGEID_RE.match(series.external_id or "")
    if not candidates and m:
        resolved = await _title_from_page_id(m.group(1))
        if resolved:
            candidates.append(resolved)
    if not candidates:
        return None, "no wikipedia locator"

    last_reason = "no wikipedia locator"
    for lang, title in candidates:
        wikitext = await fetch_wikipedia_wikitext(title, lang)
        if not wikitext:
            last_reason = f"wikitext fetch failed ({lang}:{title})"
            continue
        d = _episode_start_date(wikitext)
        if d is not None:
            return d, None
        # Long series keep the episode list on a sub-page.
        d = await _episode_start_via_subpages(title, lang, wikitext)
        if d is not None:
            return d, None
        # Last resort: infobox broadcast-start field (year precision is
        # enough for the works-library year display).
        d = _infobox_start_date(wikitext)
        if d is not None:
            return d, None
        last_reason = f"no air dates anywhere ({lang}:{title})"
    return None, last_reason


async def main(limit: int | None, delay: float, apply: bool) -> None:
    print(f"=== series start_date backfill ({'APPLY' if apply else 'DRY-RUN'}) ===")
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                select(TVSeries).where(TVSeries.start_date.is_(None)).order_by(TVSeries.created_at)
            )
        ).scalars().all()
        # Phase 1 offline evidence: MIN(air_date) from existing episode rows.
        ep_dates = dict(
            (
                await db.execute(
                    select(Episode.series_id, func.min(Episode.air_date))
                    .where(Episode.air_date.is_not(None))
                    .group_by(Episode.series_id)
                )
            ).all()
        )
        if apply:
            for s in rows:
                d = ep_dates.get(s.id)
                if d is not None:
                    s.start_date = d
            await db.commit()
    if limit:
        rows = rows[:limit]
    print(f"series with start_date NULL: {len(rows)}")

    n_offline = 0
    needs_fetch: list[TVSeries] = []
    for s in rows:
        d = ep_dates.get(s.id)
        if d is not None:
            n_offline += 1
            print(f"[offline] {s.title_cn or s.title_en!r} -> {d}")
        else:
            needs_fetch.append(s)
    print(f"phase 1 (existing episodes): {n_offline} resolved, {len(needs_fetch)} need wikipedia fetch")

    n_fetched = 0
    failures: dict[str, int] = {}
    async with async_session_factory() as session:
        for i, s in enumerate(needs_fetch, 1):
            if i > 1:
                await asyncio.sleep(delay)  # ~1 req/s, Wikimedia-friendly
            d, reason = await _derive_from_wikipedia(session, s)
            title = s.title_cn or s.title_en
            if d is None:
                failures[reason] = failures.get(reason, 0) + 1
                print(f"[{i}/{len(needs_fetch)}] --  {title!r}: {reason}")
                continue
            n_fetched += 1
            print(f"[{i}/{len(needs_fetch)}] OK  {title!r} -> {d}")
            if apply:
                obj = await session.get(TVSeries, s.id)
                if obj is not None and obj.start_date is None:
                    obj.start_date = d
                if n_fetched % APPLY_BATCH_SIZE == 0:
                    await session.commit()
                    print(f"    ... committed batch ({n_fetched} works)")
        if apply:
            await session.commit()
    print(f"phase 2 (wikipedia fetch): {n_fetched} resolved")
    if failures:
        print("unresolved (left untouched):")
        for reason, count in sorted(failures.items(), key=lambda kv: -kv[1]):
            print(f"  {count:4d}  {reason}")
    if not apply:
        print("dry-run only - re-run with --apply to write")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between wikipedia fetches")
    args = parser.parse_args()
    asyncio.run(main(args.limit, args.delay, args.apply))
