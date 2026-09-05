"""Wikipedia broadcast-date backfill: fill start_date/end_date for works
whose linked identity is a Wikipedia page but that still lack dates (the
``year`` required-field gate keeps their resources in 文件资源元数据确认).

Re-parses the TVAnime infobox broadcast fields (播放開始/放送開始 …) via
``parse_season_air_dates`` and derives each per-season work's OWN
premiere/finale through the same ``_work_start_date``/``_work_end_date``
semantics the upsert path uses. DRY-RUN by default; ``--apply`` writes only
NULL date fields (never overwrites an existing date) and respects
``manually_edited_fields``.

Identity resolution covers all stored Wikipedia id forms: canonical
``wikipedia:{lang}:{pageid}`` (``#s{N}`` season suffix stripped), legacy bare
``wikipedia:{pageid}`` (edition probed zh→en→ja), legacy title forms, plus
``wikipedia_url`` (both ``/wiki/<title>`` and ``?curid=`` forms) and the
work's identity bag (work_external_ids). When the linked edition's page
carries no dates, the zh/ja langlink counterpart is tried, then an anime
subpage probe (``<title> (アニメ)`` / `` (動畫)`` - franchise pages like
Re:Zero carry no TVAnime dates and langlinks never reach the anime article),
then sibling season works' locators (same collection = same series page).
Works carrying a Bangumi id (primary, or bagged by the identity-only web
fallback which strips content) fall back to the Bangumi subject API - exact
id lookup, season-granularity premiere date.

NOTE on locking: the embedded-Turso backend holds a single-process exclusive
file lock - STOP the app before running this against a Turso dev database.
Against PostgreSQL no stop is needed.

Usage:
    uv run python scripts/wikipedia_airdate_backfill.py [--apply] [--limit N] [--delay S]
"""
import argparse
import asyncio
import re
from datetime import date
from urllib.parse import unquote

import httpx
from sqlalchemy import or_, select

from app.database import async_session_factory
from app.models.series import TVSeries
from app.models.work_external_id import WorkExternalId
from app.services.bangumi_client import bangumi_configured, get_subject
from app.services.metadata_service import _work_end_date, _work_start_date
from app.services.metadata_wikipedia_client import (
    _WIKIPEDIA_USER_AGENT,
    fetch_wikipedia_wikitext,
)
from app.services.runtime_config import load_runtime_config
from app.services.wikipedia_episode_parser import parse_season_air_dates

APPLY_BATCH_SIZE = 20
EDITIONS = ("zh", "en", "ja")

_URL_RE = re.compile(r"https?://([a-z-]+)\.wikipedia\.org/wiki/([^#?]+)")
_URL_CURID_RE = re.compile(r"https?://([a-z-]+)\.wikipedia\.org/\?curid=(\d+)")
_QUALIFIED_RE = re.compile(r"^wikipedia:([a-z-]+):(\d+)$")
_QUALIFIED_TITLE_RE = re.compile(r"^wikipedia:([a-z-]+):(\D.*)$")
_BARE_PAGEID_RE = re.compile(r"^wikipedia:(\d+)$")
_BARE_TITLE_RE = re.compile(r"^wikipedia:(\D.*)$")


async def select_candidate_works(db) -> list[TVSeries]:
    """Season works still missing a start or end date, identified either by a
    Wikipedia identity (primary/url/bag) or by a primary Bangumi id
    (identity-only fallback rows carry no content)."""
    bagged = select(WorkExternalId.work_id).where(
        WorkExternalId.work_type == "series",
        WorkExternalId.source == "wikipedia",
    )
    return list((await db.execute(
        select(TVSeries).where(
            or_(
                TVSeries.external_id.like("wikipedia:%"),
                TVSeries.external_id.like("bangumi:%"),
                TVSeries.wikipedia_url.is_not(None),
                TVSeries.id.in_(bagged),
            ),
            or_(TVSeries.start_date.is_(None), TVSeries.end_date.is_(None)),
        ).order_by(TVSeries.created_at)
    )).scalars().all())


async def _bag_ids(db, work_id: str) -> dict[str, list[str]]:
    """The work's identity bag grouped by source (wikipedia/bangumi only)."""
    rows = (await db.execute(
        select(WorkExternalId.source, WorkExternalId.external_id).where(
            WorkExternalId.work_type == "series",
            WorkExternalId.work_id == work_id,
            WorkExternalId.source.in_(["wikipedia", "bangumi"]),
        )
    )).all()
    out: dict[str, list[str]] = {}
    for source, ext_id in rows:
        out.setdefault(source, []).append(ext_id)
    return out


def _raw_locators(work: TVSeries, bag_ids: list[str]) -> list[tuple[str | None, str]]:
    """(lang|None, pageid-or-title) candidates in priority order."""
    out: list[tuple[str | None, str]] = []
    seen: set[tuple[str | None, str]] = set()

    def add(lang: str | None, key: str | None) -> None:
        if key and (lang, key) not in seen:
            seen.add((lang, key))
            out.append((lang, key))

    for raw in [work.external_id, *bag_ids]:
        # Strip the synthetic per-season suffix - the page identity is the
        # series-level one.
        ext = re.sub(r"#s\d+$", "", raw or "")
        if not ext.startswith("wikipedia:"):
            continue
        m = _QUALIFIED_RE.match(ext)
        if m:
            add(m.group(1), m.group(2))
            continue
        m = _QUALIFIED_TITLE_RE.match(ext)
        if m:
            add(m.group(1), m.group(2).replace("_", " "))
            continue
        m = _BARE_PAGEID_RE.match(ext)
        if m:
            add(None, m.group(1))
            continue
        m = _BARE_TITLE_RE.match(ext)
        if m:
            add(None, m.group(1).replace("_", " "))
    m = _URL_RE.match(work.wikipedia_url or "")
    if m:
        add(m.group(1), unquote(m.group(2)).replace("_", " "))
    m = _URL_CURID_RE.match(work.wikipedia_url or "")
    if m:
        add(m.group(1), m.group(2))
    return out


async def _title_from_pageid(client: httpx.AsyncClient, pageid: str, lang: str) -> str | None:
    try:
        resp = await client.get(
            f"https://{lang}.wikipedia.org/w/api.php",
            params={"action": "query", "format": "json", "pageids": pageid, "redirects": 1},
        )
        resp.raise_for_status()
        for page in (resp.json().get("query") or {}).get("pages", {}).values():
            if "missing" not in page and page.get("title"):
                return page["title"]
    except Exception as e:  # noqa: BLE001 - reported as parse failure reason
        print(f"    pageid {pageid} resolve failed ({lang}): {e}")
    return None


async def _langlinks(client: httpx.AsyncClient, title: str, lang: str) -> dict[str, str]:
    """{lang: title} of the page's interlanguage links (empty on failure)."""
    try:
        resp = await client.get(
            f"https://{lang}.wikipedia.org/w/api.php",
            params={
                "action": "query", "format": "json", "titles": title,
                "prop": "langlinks", "lllimit": "max", "redirects": 1,
            },
        )
        resp.raise_for_status()
        for page in (resp.json().get("query") or {}).get("pages", {}).values():
            return {ll["lang"]: ll["*"] for ll in page.get("langlinks", [])}
    except Exception as e:  # noqa: BLE001 - best-effort fallback
        print(f"    langlinks failed ({lang}:{title}): {e}")
    return {}


async def _parse_page(title: str, lang: str, delay: float) -> dict[int, dict[str, str]] | None:
    """Fetch one page and extract per-season broadcast dates (None when the
    page is missing or carries none)."""
    wikitext = await fetch_wikipedia_wikitext(title, lang)
    await asyncio.sleep(delay)
    if not wikitext:
        return None
    return parse_season_air_dates(wikitext)


def _result_from_dates(work: TVSeries, dates: dict, page: str) -> dict:
    """Derive the work's own season dates through the production semantics
    (``_work_start_date``/``_work_end_date`` on a pseudo entity carrying only
    the parsed broadcast dates)."""
    report = {"id": work.id, "title": work.title_cn or work.title_en, "season": work.season_number}
    data = {
        "seasons": [{"season_number": n, **d} for n, d in sorted(dates.items())],
        "start_date": min(
            (d["air_date"] for d in dates.values() if d.get("air_date")),
            default=None,
        ),
    }
    start = _work_start_date(data, work.season_number, "series")
    end = _work_end_date(data, work.season_number, "series")
    if not start and not end:
        return report | {"ok": False, "reason": f"no date for season {work.season_number} ({page})"}
    return report | {
        "ok": True,
        "page": page,
        "start_date": str(start) if start else None,
        "end_date": str(end) if end else None,
    }


async def _bangumi_leg(work: TVSeries, bag_ids: list[str], report: dict) -> dict | None:
    """Bangumi leg: exact subject-id lookup (no title guessing). Bangumi
    entries are season-granular, so the subject date IS the work's own
    premiere (no end date available there). Primary id first (same-source);
    a bagged id only when the wikipedia chain failed (cross-source repair,
    still id-exact). Returns a report dict when the leg applies, else None.
    """
    candidates = []
    if re.fullmatch(r"bangumi:\d+", work.external_id or ""):
        candidates.append((work.external_id, False))
    candidates += [(b, True) for b in bag_ids if re.fullmatch(r"bangumi:\d+", b or "")]
    if not candidates or not bangumi_configured():
        return None
    async with httpx.AsyncClient(
        timeout=15, headers={"User-Agent": _WIKIPEDIA_USER_AGENT}
    ) as client:
        for ext_id, from_bag in candidates:
            subject_id = ext_id.split(":", 1)[1]
            try:
                subj = await get_subject(client, subject_id)
            except Exception as e:  # noqa: BLE001 - try the next candidate
                print(f"    bangumi lookup failed ({ext_id}): {e}")
                continue
            d = (subj.get("date") or "").strip()
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
                return report | {
                    "ok": True,
                    "page": f"bangumi:{subject_id}" + (" (bag)" if from_bag else ""),
                    "start_date": d,
                    "end_date": None,
                }
    if candidates:
        return report | {"ok": False, "reason": f"bangumi subject(s) have no full date ({len(candidates)} tried)"}
    return None


async def evaluate_work(
    work: TVSeries,
    wikipedia_bag: list[str],
    bangumi_bag: list[str],
    delay: float,
    extra_locators: list[tuple[str | None, str]] | None = None,
) -> dict:
    """Fetch + parse one work's wikipedia page(s). Pure read - no DB writes,
    so the --apply path can call this then persist the result.

    Fallback chain: own locators → ``extra_locators`` (sibling season works
    of the same collection share the series-level page) → langlink
    counterpart (zh↔ja, and en→zh/ja for legacy en-slug identities) →
    bangumi exact-id leg.
    """
    report = {"id": work.id, "title": work.title_cn or work.title_en, "season": work.season_number}
    locators = _raw_locators(work, wikipedia_bag)
    extra = [loc for loc in (extra_locators or []) if loc not in locators]
    if not locators and not extra:
        bangumi = await _bangumi_leg(work, bangumi_bag, report)
        return bangumi or report | {"ok": False, "reason": "no wikipedia locator"}
    async with httpx.AsyncClient(
        timeout=15, headers={"User-Agent": _WIKIPEDIA_USER_AGENT}
    ) as client:
        attempts: list[tuple[str, str]] = []
        for lang, key in [*locators, *extra]:
            for lg in ((lang,) if lang else EDITIONS):
                title = key if not key.isdigit() else await _title_from_pageid(client, key, lg)
                if title and (lg, title) not in attempts:
                    attempts.append((lg, title))
        tried: set[tuple[str, str]] = set()
        for lg, title in attempts:
            tried.add((lg, title))
            dates = await _parse_page(title, lg, delay)
            if dates:
                return _result_from_dates(work, dates, f"{lg}:{title}")
        # Langlink fallback: the linked edition's page carries no dates, but
        # the zh/ja counterpart often does.
        for lg, title in attempts:
            links = await _langlinks(client, title, lg)
            await asyncio.sleep(delay)
            alts = ("zh", "ja") if lg not in ("zh", "ja") else ({"zh": "ja", "ja": "zh"}[lg],)
            for alt in alts:
                alt_title = links.get(alt)
                if not alt_title or (alt, alt_title) in tried:
                    continue
                tried.add((alt, alt_title))
                dates = await _parse_page(alt_title, alt, delay)
                if dates:
                    return _result_from_dates(work, dates, f"{alt}:{alt_title} (via {lg})")
        # Anime-subpage probe: franchise pages (Re:Zero, 俺妹 …) carry no
        # TVAnime dates; the anime article lives at ``<title> (アニメ)`` /
        # ``<title> (動畫)`` which langlinks never reach.
        suffixes = {"ja": [" (アニメ)"], "zh": [" (動畫)", " (动画)"]}
        for lg, title in list(tried):
            if lg not in suffixes or "(" in title:
                continue
            for suffix in suffixes[lg]:
                sub = title + suffix
                if (lg, sub) in tried:
                    continue
                tried.add((lg, sub))
                dates = await _parse_page(sub, lg, delay)
                if dates:
                    return _result_from_dates(work, dates, f"{lg}:{sub} (subpage)")
        # Bangumi leg (see _bangumi_leg): exact id lookup when the wikipedia
        # chain found nothing.
        bangumi = await _bangumi_leg(work, bangumi_bag, report)
        if bangumi:
            return bangumi
    return report | {"ok": False, "reason": f"no parseable dates ({len(locators) + len(extra)} locators)"}


def apply_report(work: TVSeries, rep: dict) -> list[str]:
    """Write the parsed dates onto the work (NULL fields only). Returns the
    list of fields actually changed."""
    changed: list[str] = []
    edited = set(work.manually_edited_fields or [])
    if rep.get("start_date") and work.start_date is None and "start_date" not in edited:
        work.start_date = date.fromisoformat(rep["start_date"])
        changed.append("start_date")
    if rep.get("end_date") and work.end_date is None and "end_date" not in edited:
        work.end_date = date.fromisoformat(rep["end_date"])
        changed.append("end_date")
    return changed


async def main(limit: int | None, delay: float, apply: bool) -> None:
    async with async_session_factory() as db:
        await load_runtime_config(db)
        works = await select_candidate_works(db)
        if limit:
            works = works[:limit]
        print(f"{len(works)} works missing dates")
        ok = failed = changed_rows = 0
        sibling_cache: dict[str, list[tuple[str | None, str]]] = {}

        async def _sibling_locators(work: TVSeries) -> list[tuple[str | None, str]]:
            """Locators of the work's collection siblings (they share the
            series-level wikipedia page - e.g. a work whose own legacy id is
            unusable can still parse via a sibling's page URL)."""
            if not work.collection_id:
                return []
            if work.collection_id not in sibling_cache:
                siblings = list((await db.execute(
                    select(TVSeries).where(
                        TVSeries.collection_id == work.collection_id,
                        TVSeries.id != work.id,
                    )
                )).scalars().all())
                locs: list[tuple[str | None, str]] = []
                for sib in siblings:
                    locs += _raw_locators(sib, (await _bag_ids(db, sib.id)).get("wikipedia", []))
                sibling_cache[work.collection_id] = locs
            return sibling_cache[work.collection_id]

        for i, work in enumerate(works, 1):
            bag = await _bag_ids(db, work.id)
            rep = await evaluate_work(
                work, bag.get("wikipedia", []), bag.get("bangumi", []), delay,
            )
            if not rep.get("ok"):
                siblings = await _sibling_locators(work)
                if siblings:
                    rep = await evaluate_work(
                        work, bag.get("wikipedia", []), bag.get("bangumi", []),
                        delay, extra_locators=siblings,
                    )
                    if rep.get("ok"):
                        rep["page"] += " (via sibling)"
            if rep.get("ok"):
                ok += 1
                line = (f"[{i}/{len(works)}] OK {rep['title']} S{rep['season']}: "
                        f"{rep['start_date']} .. {rep['end_date']} ({rep['page']})")
                if apply:
                    changed = apply_report(work, rep)
                    if changed:
                        changed_rows += 1
                        line += f"  <- wrote {','.join(changed)}"
                    else:
                        line += "  <- nothing to write (fields set/protected)"
                print(line)
            else:
                failed += 1
                print(f"[{i}/{len(works)}] FAIL {rep['title']} S{rep['season']}: {rep['reason']}")
            if apply and i % APPLY_BATCH_SIZE == 0:
                await db.commit()
                print(f"  ... committed batch ({i} evaluated)")
        if apply:
            await db.commit()
        print(f"done: {ok} resolved, {failed} failed"
              + (f", {changed_rows} rows updated" if apply else " (dry-run)"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write results (default: dry-run)")
    parser.add_argument("--limit", type=int, default=None, help="evaluate at most N works")
    parser.add_argument("--delay", type=float, default=0.5, help="seconds between page fetches")
    args = parser.parse_args()
    asyncio.run(main(args.limit, args.delay, args.apply))
