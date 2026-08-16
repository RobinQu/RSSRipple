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
2. **Wikipedia fetch**: for the remainder with a wikipedia identity
   (``external_id=wikipedia:<pageid>`` or ``wikipedia_url``), fetch wikitext
   and run the deterministic ``parse_episode_list``; earliest parsed
   ``air_date`` becomes ``start_date``. Series without parseable dates are
   reported and left untouched (run ``wikipedia_seasons_eval.py --apply``
   separately if their episode rows are also missing).

NOTE on locking: the embedded-Turso backend holds a single-process exclusive
file lock, so STOP the app (``docker compose stop app``) before running this
against the dev database, then start it again afterwards.

Dry-run by default; pass ``--apply`` to write.

Usage:
    uv run python scripts/start_date_backfill.py [--apply] [--limit N] [--delay S]
"""

import argparse
import asyncio
import re
from datetime import date
from urllib.parse import unquote

import httpx
from sqlalchemy import func, select

from app.database import async_session_factory
from app.models.episode import Episode
from app.models.series import TVSeries
from app.services.metadata_wikipedia_client import (
    _WIKIPEDIA_USER_AGENT,
    fetch_wikipedia_wikitext,
)
from app.services.wikipedia_episode_parser import parse_episode_list

APPLY_BATCH_SIZE = 20


def _page_locator(series: TVSeries) -> tuple[str | None, str | None, int | None]:
    """Derive (lang, title, page_id) for the work's wikipedia page.

    Same rule as ``wikipedia_seasons_eval._page_locator``.
    """
    url = series.wikipedia_url or ""
    m = re.search(r"https?://([a-z-]+)\.wikipedia\.org/wiki/([^#?]+)", url)
    if m:
        return m.group(1), unquote(m.group(2)).replace("_", " "), None
    ext = series.external_id or ""
    m = re.match(r"^wikipedia:(\d+)$", ext)
    if m:
        # Wikipedia page ids are per-language-wiki; without a URL we cannot
        # know the language - the zh catalog is the dominant case here.
        return "zh", None, int(m.group(1))
    return None, None, None


async def _title_from_page_id(page_id: int, lang: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=15) as client:
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
        for page in (data.get("query") or {}).get("pages", {}).values():
            if page.get("title"):
                return page["title"]
    except Exception as e:  # noqa: BLE001 - reported as unresolved reason
        print(f"    pageid resolve failed: {e}")
    return None


async def _derive_from_wikipedia(series: TVSeries) -> tuple[date | None, str | None]:
    """Fetch + parse the series' wikipedia page; earliest episode air_date.

    Returns ``(start_date, failure_reason)`` — exactly one is set. Pure read,
    no DB access.
    """
    lang, title, page_id = _page_locator(series)
    if not lang:
        return None, "no wikipedia locator"
    if title is None:
        title = await _title_from_page_id(page_id, lang)
        if not title:
            return None, f"pageid {page_id} unresolved ({lang})"
    wikitext = await fetch_wikipedia_wikitext(title, lang)
    if not wikitext:
        return None, "wikitext fetch failed"
    list_data = parse_episode_list(wikitext)
    air_dates = sorted(
        ep["air_date"]
        for ep in ((list_data or {}).get("episodes") or [])
        if ep.get("air_date")
    )
    if not air_dates:
        return None, "no episode air dates on page"
    try:
        return date.fromisoformat(air_dates[0]), None
    except ValueError:
        return None, f"unparseable air_date {air_dates[0]!r}"


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
            n_offline = 0
            for s in rows:
                d = ep_dates.get(s.id)
                if d is not None:
                    s.start_date = d
                    n_offline += 1
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
    session = async_session_factory() if apply else None
    try:
        for i, s in enumerate(needs_fetch, 1):
            if i > 1:
                await asyncio.sleep(delay)  # ~1 req/s, Wikimedia-friendly
            d, reason = await _derive_from_wikipedia(s)
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
    finally:
        if session is not None:
            await session.close()
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
