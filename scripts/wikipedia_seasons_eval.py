"""Wikipedia seasons/episodes backfill (P2 eval + P4 apply).

Runs the deterministic Wikipedia season/episode parser over every
wikipedia-linked TVSeries and reports per-work coverage. DRY-RUN by default;
``--apply`` writes the parsed data: seasons / number_of_seasons /
number_of_episodes are OVERWRITTEN (wikipedia is the content source for
wikipedia-primary works) and Episode rows are upserted idempotently via
``upsert_episodes`` (additive, keyed by (series_id, season, episode)).
Anti-regression guard: parsed data with FEWER seasons than the existing row
is reported ``[guard-skip]`` and never applied (the rule formerly known as
``metadata_service.seasons_overwrite_allowed``, retired with the per-season
work model and inlined here).

With ``--apply`` this supersedes ``series_seasons_backfill.py`` phase 1 for
wikipedia-primary works (that script fills seasons from TMDB regardless of
the work's primary source).

NOTE on locking: the embedded-Turso backend holds a single-process exclusive
file lock, so STOP the app (``docker compose stop app``) before running this
against the dev database, then start it again afterwards.

Usage:
    uv run python scripts/wikipedia_seasons_eval.py [--apply] [--limit N] [--delay S]
"""
import argparse
import asyncio
import re
from urllib.parse import unquote

import httpx
from sqlalchemy import or_, select

from app.database import async_session_factory
from app.models.series import TVSeries
from app.services.metadata_service import upsert_episodes
from app.services.metadata_wikipedia_client import (
    _WIKIPEDIA_USER_AGENT,
    fetch_wikipedia_wikitext,
)
from app.services.wikipedia_episode_parser import (
    parse_episode_list,
    parse_seasons_from_infobox,
)

APPLY_BATCH_SIZE = 20


def seasons_overwrite_allowed(
    existing_seasons: list | None,
    existing_number_of_seasons: int | None,
    incoming_seasons: list | None,
) -> bool:
    """Anti-regression guard (inlined; retired from metadata_service with the
    per-season work model): overwrite allowed when no structure exists yet or
    the incoming data has at least as many seasons as the existing row."""
    existing_count = len(existing_seasons or []) or (existing_number_of_seasons or 0)
    if existing_count == 0:
        return True
    return len(incoming_seasons or []) >= existing_count


async def select_wikipedia_series(db) -> list[TVSeries]:
    """All TVSeries with a wikipedia identity (canonical id or page URL)."""
    return (await db.execute(
        select(TVSeries).where(
            or_(
                TVSeries.external_id.like("wikipedia:%"),
                TVSeries.wikipedia_url.is_not(None),
            )
        ).order_by(TVSeries.created_at)
    )).scalars().all()


def _page_locator(series: TVSeries) -> tuple[str | None, str | None, int | None]:
    """Derive (lang, title, page_id) for the work's wikipedia page."""
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
    except Exception as e:  # noqa: BLE001 - reported as parse failure reason
        print(f"    pageid resolve failed: {e}")
    return None


async def evaluate_series(series: TVSeries) -> dict:
    """Fetch + parse one series' wikipedia page. Pure read - no DB access,
    so the --apply path can call this then persist the result."""
    report = {"id": series.id, "title": series.title_cn or series.title_en}
    lang, title, page_id = _page_locator(series)
    if not lang:
        return report | {"ok": False, "reason": "no wikipedia locator"}
    if title is None:
        title = await _title_from_page_id(page_id, lang)
        if not title:
            return report | {"ok": False, "reason": f"pageid {page_id} unresolved ({lang})"}
    report["page"] = f"{lang}:{title}"
    wikitext = await fetch_wikipedia_wikitext(title, lang)
    if not wikitext:
        return report | {"ok": False, "reason": "wikitext fetch failed"}
    infobox = parse_seasons_from_infobox(wikitext)
    list_data = parse_episode_list(wikitext)
    seasons = (list_data or {}).get("seasons") or infobox
    episodes = (list_data or {}).get("episodes") or []
    if not seasons and not episodes:
        return report | {"ok": False, "reason": "no infobox seasons / no episode section"}
    # P2 anti-regression guard (same rule as metadata_service): parsed data
    # with FEWER seasons than the existing row must not overwrite it.
    guard_skip = bool(
        seasons
        and not seasons_overwrite_allowed(series.seasons, series.number_of_seasons, seasons)
    )
    return report | {
        "ok": True,
        "seasons": seasons or [],
        "episodes": episodes,
        "episode_count": len(episodes),
        "missing_air_dates": sum(1 for e in episodes if not e.get("air_date")),
        "guard_skip": guard_skip,
    }


async def apply_report(db, series_id: str, rep: dict) -> int:
    """Write one parsed report: override seasons fields + upsert Episode rows.

    Returns the number of episode entries upserted. Caller commits. A report
    flagged ``guard_skip`` (parsed seasons strictly poorer than the existing
    structure) is NOT applied - seasons fields and episode rows both come
    from the same suspect parse.
    """
    series = await db.get(TVSeries, series_id)
    if series is None or rep.get("guard_skip"):
        return 0
    seasons = rep.get("seasons") or []
    if seasons:
        series.seasons = seasons
        series.number_of_seasons = len(seasons)
        series.number_of_episodes = sum(x["episode_count"] for x in seasons)
    episodes = rep.get("episodes") or []
    if episodes:
        return await upsert_episodes(db, series, episodes)
    return 0


async def main(limit: int | None, delay: float, apply: bool) -> None:
    async with async_session_factory() as db:
        rows = await select_wikipedia_series(db)
    print(
        f"=== wikipedia seasons backfill: {len(rows)} wikipedia-linked series "
        f"({'APPLY' if apply else 'DRY-RUN'}) ==="
    )
    if limit:
        rows = rows[:limit]
    ok = 0
    failures: dict[str, int] = {}
    applied = 0
    episodes_written = 0
    session = async_session_factory() if apply else None
    try:
        for i, s in enumerate(rows, 1):
            if i > 1:
                await asyncio.sleep(delay)  # ~1 req/s, Wikimedia-friendly
            rep = await evaluate_series(s)
            if rep.get("ok"):
                ok += 1
                seasons = ", ".join(
                    f"S{x['season_number']}:{x['episode_count']}" for x in rep["seasons"]
                )
                guard = " [guard-skip]" if rep.get("guard_skip") else ""
                print(
                    f"[{i}/{len(rows)}] OK  {rep['title']!r} ({rep.get('page')}) "
                    f"seasons=[{seasons}] episodes={rep['episode_count']} "
                    f"no-date={rep['missing_air_dates']}{guard}"
                )
                if apply and not rep.get("guard_skip"):
                    episodes_written += await apply_report(session, s.id, rep)
                    applied += 1
                    if applied % APPLY_BATCH_SIZE == 0:
                        await session.commit()
                        print(f"    ... committed batch ({applied} works)")
            else:
                reason = rep.get("reason", "unknown")
                failures[reason] = failures.get(reason, 0) + 1
                print(f"[{i}/{len(rows)}] --  {rep['title']!r}: {reason}")
        if apply:
            await session.commit()
            print(
                f"applied: {applied} works updated, {episodes_written} "
                "episode entries upserted; committed."
            )
        else:
            print("dry-run: nothing written; re-run with --apply to execute.")
    finally:
        if session is not None:
            await session.close()
    print("\n=== coverage summary ===")
    print(f"parsed: {ok}/{len(rows)}")
    for reason, n in sorted(failures.items(), key=lambda kv: -kv[1]):
        print(f"  {reason}: {n}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="write parsed data to the DB")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--delay", type=float, default=1.0, help="seconds between fetches")
    args = p.parse_args()
    asyncio.run(main(args.limit, args.delay, args.apply))
