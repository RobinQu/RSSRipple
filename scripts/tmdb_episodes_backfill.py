"""P4 backfill: populate Episode rows for tmdb-primary TVSeries.

Symmetric with the wikipedia seasons backfill: series whose canonical
identity is a TMDB id get their per-episode data (title, air_date) from
``GET /tv/{tmdb_id}/season/{n}`` via ``fetch_tmdb_episode_list`` and are
upserted idempotently by ``upsert_episodes`` (additive, keyed by
(series_id, season, episode)). ``TVSeries.seasons`` supplies the season list;
when missing, the TMDB series-details endpoint is queried for it (read-only
input - this script does not rewrite series fields).

NOTE on locking: the embedded-Turso backend holds a single-process exclusive
file lock, so STOP the app (``docker compose stop app``) before running this
against the dev database, then start it again afterwards.

Dry-run by default; pass --apply to execute.

Usage:
    uv run python scripts/tmdb_episodes_backfill.py [--apply] [--limit N] [--delay S]
"""

import argparse
import asyncio
import re

from sqlalchemy import select

from app.config import settings
from app.database import async_session_factory
from app.models.series import TVSeries
from app.services.metadata_service import upsert_episodes
from app.services.metadata_source_io import (
    _execute_get_tmdb_details,
    fetch_tmdb_episode_list,
)

APPLY_BATCH_SIZE = 20


def tmdb_id_of(series: TVSeries) -> str | None:
    """Extract the numeric TMDB id from the canonical external_id, if any."""
    m = re.match(r"^tmdb:(\d+)$", series.external_id or "")
    return m.group(1) if m else None


async def select_tmdb_series(db) -> list[TVSeries]:
    """All TVSeries whose canonical identity is a TMDB id."""
    return (await db.execute(
        select(TVSeries)
        .where(TVSeries.external_id.like("tmdb:%"))
        .order_by(TVSeries.created_at)
    )).scalars().all()


async def _seasons_for(series: TVSeries, tmdb_id: str) -> list[dict] | None:
    """Season list for the fetch: persisted seasons, else TMDB details."""
    if series.seasons:
        return series.seasons
    details = await _execute_get_tmdb_details(tmdb_id, "tv")
    if details.get("success"):
        return (details.get("data") or {}).get("seasons") or None
    return None


async def main(apply: bool, limit: int | None, delay: float) -> None:
    print(f"=== tmdb episodes backfill ({'APPLY' if apply else 'DRY-RUN'}) ===")
    if not settings.tmdb_api_key:
        print("TMDB_API_KEY is not set; nothing to do.")
        return
    async with async_session_factory() as db:
        rows = await select_tmdb_series(db)
    print(f"{len(rows)} tmdb-identified series")
    if limit:
        rows = rows[:limit]
    ok = failed = skipped = 0
    applied = 0
    episodes_written = 0
    session = async_session_factory() if apply else None
    try:
        for i, s in enumerate(rows, 1):
            if i > 1:
                await asyncio.sleep(delay)
            title = s.title_cn or s.title_en or s.original_title
            tmdb_id = tmdb_id_of(s)
            if not tmdb_id:
                skipped += 1
                print(f"[{i}/{len(rows)}] -- {title!r}: unparseable external_id {s.external_id!r}")
                continue
            seasons = await _seasons_for(s, tmdb_id)
            if not seasons:
                failed += 1
                print(f"[{i}/{len(rows)}] -- {title!r} (tmdb:{tmdb_id}): no seasons data")
                continue
            episodes = await fetch_tmdb_episode_list(tmdb_id, seasons)
            if not episodes:
                failed += 1
                print(f"[{i}/{len(rows)}] -- {title!r} (tmdb:{tmdb_id}): no episodes fetched")
                continue
            ok += 1
            print(
                f"[{i}/{len(rows)}] OK {title!r} (tmdb:{tmdb_id}): "
                f"{len(episodes)} episodes across {len(seasons)} seasons"
            )
            if apply:
                series = await session.get(TVSeries, s.id)
                episodes_written += await upsert_episodes(session, series, episodes)
                applied += 1
                if applied % APPLY_BATCH_SIZE == 0:
                    await session.commit()
                    print(f"    ... committed batch ({applied} works)")
        if apply:
            await session.commit()
            print(
                f"applied: {applied} works, {episodes_written} episode entries "
                "upserted; committed."
            )
        else:
            print("dry-run: nothing written; re-run with --apply to execute.")
    finally:
        if session is not None:
            await session.close()
    print(f"\nsummary: ok={ok} no-data={failed} skipped={skipped} of {len(rows)}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--delay", type=float, default=0.25, help="seconds between works")
    args = parser.parse_args()
    asyncio.run(main(args.apply, args.limit, args.delay))
