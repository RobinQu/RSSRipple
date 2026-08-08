"""One-off backfill: (1) populate ``TVSeries.seasons`` from TMDB for series
that lack per-season episode counts, (2) re-run cross-season episode
reconciliation for single-episode TV resources whose ``episode_confidence``
is NULL or ``raw`` using the linked series' persisted counts.

Why: episode reconciliation originally ran only inside the metadata agent's
apply path. Resources linked via the agent-free paths (known-work
short-circuit, ChannelRawTitleMapping, fuzzy auto-link) never got reconciled,
so absolute-numbered releases ("... 第四季 - 89") kept their absolute episode
number. The link paths now reconcile using ``TVSeries.seasons`` — this script
repairs the rows created before that, and fills the seasons data itself.

NOTE on locking: the embedded-Turso backend holds a single-process exclusive
file lock, so STOP the app (``docker compose stop app``) before running this
against the dev database, then start it again afterwards.

Dry-run by default; pass --apply to execute.
"""

import argparse
import asyncio
import re

import httpx
from sqlalchemy import or_, select

from app.config import settings
from app.database import async_session_factory
from app.models.file_resource import FileResource
from app.models.series import TVSeries
from app.services.metadata_episode_reconcile import (
    apply_episode_reconcile,
    seasons_map_from_list,
)

TMDB_BASE = "https://api.themoviedb.org/3"


def _tmdb_id(series: TVSeries) -> str | None:
    """Extract a TMDB numeric id from the canonical external_id, if any."""
    m = re.search(r"tmdb:(\d+)", series.external_id or "")
    return m.group(1) if m else None


async def _tmdb_get(client: httpx.AsyncClient, path: str, **params) -> dict | None:
    params["api_key"] = settings.tmdb_api_key
    try:
        r = await client.get(f"{TMDB_BASE}{path}", params=params, timeout=20)
        if r.status_code == 200:
            return r.json()
    except Exception as e:  # noqa: BLE001
        print(f"    [tmdb] {path} failed: {e}")
    return None


def _extract_seasons(details: dict) -> list | None:
    out = []
    for s in details.get("seasons") or []:
        num, cnt = s.get("season_number"), s.get("episode_count")
        if isinstance(num, int) and isinstance(cnt, int) and num >= 1 and cnt >= 1:
            out.append({"season_number": num, "episode_count": cnt})
    return out or None


async def _fetch_seasons_for_series(client: httpx.AsyncClient, series: TVSeries) -> list | None:
    """Fetch per-season counts: direct lookup by TMDB id, else title search."""
    tmdb_id = _tmdb_id(series)
    if tmdb_id:
        details = await _tmdb_get(client, f"/tv/{tmdb_id}")
        return _extract_seasons(details) if details else None
    for title in (series.title_en, series.original_title, series.title_cn):
        if not title:
            continue
        found = await _tmdb_get(client, "/search/tv", query=title)
        results = (found or {}).get("results") or []
        if not results:
            continue
        details = await _tmdb_get(client, f"/tv/{results[0]['id']}")
        seasons = _extract_seasons(details) if details else None
        if seasons:
            print(f"    [tmdb] search {title!r} -> id {results[0]['id']} ({results[0].get('name')!r})")
            return seasons
    return None


async def main(apply: bool) -> None:
    print(f"=== series seasons + reconcile backfill ({'APPLY' if apply else 'DRY-RUN'}) ===")
    if not settings.tmdb_api_key:
        print("TMDB_API_KEY is not set; phase 1 (seasons fetch) will skip everything.")

    async with async_session_factory() as db:
        # ── Phase 1: populate TVSeries.seasons ──
        missing = (await db.execute(
            select(TVSeries).where(TVSeries.seasons.is_(None))
        )).scalars().all()
        print(f"phase 1: {len(missing)} series without seasons data")
        filled = 0
        async with httpx.AsyncClient() as client:
            for s in missing:
                seasons = None
                if settings.tmdb_api_key:
                    seasons = await _fetch_seasons_for_series(client, s)
                    await asyncio.sleep(0.25)
                if seasons:
                    filled += 1
                    title = s.title_cn or s.title_en or s.original_title
                    print(f"  [fill] {title!r}: {seasons_map_from_list(seasons)}")
                    if apply:
                        s.seasons = seasons
                else:
                    title = s.title_cn or s.title_en or s.original_title
                    print(f"  [skip] {title!r}: no TMDB seasons found")
        print(f"phase 1: {filled}/{len(missing)} series filled")

        # ── Phase 2: reconcile stale resources ──
        stale = (await db.execute(
            select(FileResource).where(
                FileResource.series_id.isnot(None),
                FileResource.is_batch.is_(False),
                FileResource.episode.isnot(None),
                FileResource.season.isnot(None),
                or_(
                    FileResource.episode_confidence.is_(None),
                    FileResource.episode_confidence == "raw",
                ),
            )
        )).scalars().all()
        print(f"phase 2: {len(stale)} resources with confidence NULL/raw")
        series_cache: dict[str, TVSeries | None] = {}
        n_reconciled = n_ambiguous = n_no_seasons = n_already_ok = 0
        for r in stale:
            if r.series_id not in series_cache:
                series_cache[r.series_id] = await db.get(TVSeries, r.series_id)
            series = series_cache[r.series_id]
            smap = seasons_map_from_list(series.seasons if series else None)
            if not smap:
                n_no_seasons += 1
                continue
            before = (r.season, r.episode)
            if apply_episode_reconcile(r, smap):
                if r.episode_confidence == "ambiguous":
                    n_ambiguous += 1
                    print(f"  [ambiguous] S{before[0]}E{before[1]} stays | {r.title_raw[:70]}")
                else:
                    n_reconciled += 1
                    print(
                        f"  [reconciled] S{before[0]}E{before[1]} -> S{r.season}E{r.episode}"
                        f" (abs {r.absolute_episode}) | {r.title_raw[:60]}"
                    )
            else:
                n_already_ok += 1
        print(
            f"phase 2: reconciled={n_reconciled} ambiguous={n_ambiguous} "
            f"already-ok/marked-raw={n_already_ok} no-seasons-data={n_no_seasons}"
        )

        if apply:
            await db.commit()
            print("committed.")
        else:
            print("dry-run: nothing written; re-run with --apply to execute.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
