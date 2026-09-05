"""One-off repair: split SAC_2045 out of the "攻壳机动队（系列）" collection.

Damage (from the season-split migration's "absorbed" path, prod_apply.log
[13/123]): the legacy series-level row 攻壳机动队：SAC_2045 (TMDB 90293, a
*different series* — 2020/2022) was absorbed into the franchise collection of
the 2026 series 攻殻機動隊 THE GHOST IN THE SHELL. Concretely:

- ``cfc75752…`` became the collection's season-2 member even though the 2026
  series has no season 2 — it is SAC_2045's OWN season 2 (2022-05-23);
- SAC_2045 S1's episodes 11-12 ("14 岁革命", "全部成为N。") were inserted into
  the 2026 season-1 work (its E1-E10 already existed and were dropped);
- SAC_2045 S1's episodes 1-10 were dropped entirely (no S1 work exists).

Repair (dry-run default, ``--apply`` to execute):

1. Get-or-create a dedicated ``series_group`` collection for SAC_2045 with the
   series-level identity ``tmdb:90293`` on the collection bag.
2. Move the season-2 work into it.
3. Create the missing season-1 work (``tmdb:90293#s1``) in it, re-point the
   two stray episodes off the 2026 work, and re-fetch the full SAC_2045 S1
   episode list from TMDB to restore the dropped rows.

Usage:
    uv run python scripts/fix_sac2045_collection_split.py           # dry-run
    uv run python scripts/fix_sac2045_collection_split.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import select

from app.database import async_session_factory
from app.models.episode import Episode
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.external_ids import add_external_id, find_work_by_external_id
from app.services.metadata_service import _parse_date, upsert_episodes
from app.services.metadata_source_io import fetch_tmdb_episode_list
from app.services.runtime_config import load_runtime_config

logger = logging.getLogger("fix_sac2045_collection_split")

FRANCHISE_COLLECTION_ID = "813af37b-a832-45ae-a4d5-ca91714b3a89"
GITS_2026_S1_WORK = "1dd3a4cc-63ba-43b1-9a21-7d0634fa81e2"
SAC2045_S2_WORK = "cfc75752-de32-4c61-9194-35fbbeeba05a"
SAC2045_TMDB_ID = "90293"


async def _get_or_create_sac_collection(db, apply: bool) -> WorkCollection | None:
    owner = await find_work_by_external_id(
        db, "collection", "tmdb", f"tmdb:{SAC2045_TMDB_ID}"
    )
    if owner is not None:
        logger.info("[coll] reuse existing collection %s (%r)", owner.id, owner.title_cn)
        return owner
    existing = (await db.execute(
        select(WorkCollection).where(WorkCollection.title_cn == "攻壳机动队：SAC_2045")
    )).scalar_one_or_none()
    if existing is not None:
        logger.info("[coll] reuse title-matched collection %s", existing.id)
        return existing
    logger.info("[coll] create collection '攻壳机动队：SAC_2045'%s", "" if apply else " [dry-run]")
    if not apply:
        return None
    coll = WorkCollection(
        title_cn="攻壳机动队：SAC_2045",
        title_en="Ghost in the Shell: SAC_2045",
        external_source="series_group",
    )
    db.add(coll)
    await db.flush()
    await add_external_id(db, "collection", coll.id, "tmdb", f"tmdb:{SAC2045_TMDB_ID}")
    return coll


async def main(apply: bool) -> None:
    async with async_session_factory() as db:
        await load_runtime_config(db)

        coll_old = await db.get(WorkCollection, FRANCHISE_COLLECTION_ID)
        gits_s1 = await db.get(TVSeries, GITS_2026_S1_WORK)
        sac_s2 = await db.get(TVSeries, SAC2045_S2_WORK)
        if not (coll_old and gits_s1 and sac_s2):
            logger.error("expected rows not found — already repaired?")
            return
        if sac_s2.collection_id != FRANCHISE_COLLECTION_ID:
            logger.error("SAC_2045 S2 work is no longer in the franchise collection — already repaired?")
            return

        # TMDB ground truth for SAC_2045 (both seasons).
        ep_list = await fetch_tmdb_episode_list(
            SAC2045_TMDB_ID, [{"season_number": 1}, {"season_number": 2}]
        )
        if not ep_list:
            logger.error("TMDB episode fetch failed — aborting (no writes)")
            return
        s1_eps = [e for e in ep_list if e["season"] == 1]
        s1_dates = sorted(e["air_date"] for e in s1_eps if e.get("air_date"))
        logger.info(
            "[tmdb] SAC_2045 S1: %d eps, premiere %s; S2: %d eps",
            len(s1_eps), s1_dates[0] if s1_dates else None,
            len([e for e in ep_list if e["season"] == 2]),
        )

        # Stray SAC_2045 S1 episodes sitting on the 2026 work.
        stray = [
            ep for ep in (await db.execute(
                select(Episode).where(Episode.series_id == gits_s1.id)
            )).scalars().all()
            if ep.episode > (gits_s1.number_of_episodes or 10)
        ]
        logger.info(
            "[stray] %d episodes on the 2026 work beyond its %s-episode run: %s",
            len(stray), gits_s1.number_of_episodes,
            [(e.episode, e.title) for e in stray],
        )

        coll = await _get_or_create_sac_collection(db, apply)
        coll_id = coll.id if coll else "<new>"

        # Move the S2 work.
        logger.info(
            "[move] S2 work %s → collection %s%s",
            sac_s2.id, coll_id, "" if apply else " [dry-run]",
        )
        if apply:
            sac_s2.collection_id = coll.id

        # Create the missing S1 work (unless some work already owns the id).
        s1_owner = await find_work_by_external_id(
            db, "series", "tmdb", f"tmdb:{SAC2045_TMDB_ID}#s1"
        )
        s1_work = s1_owner
        if s1_work is None:
            logger.info("[s1] create SAC_2045 S1 work%s", "" if apply else " [dry-run]")
            if apply:
                s1_work = TVSeries(
                    title_cn="攻壳机动队：SAC_2045",
                    title_en="Ghost in the Shell: SAC_2045",
                    original_title="攻殻機動隊 SAC_2045",
                    external_id=f"tmdb:{SAC2045_TMDB_ID}#s1",
                    external_source="tmdb",
                    season_number=1,
                    collection_id=coll.id,
                    content_type="tv",
                    start_date=_parse_date(s1_dates[0]) if s1_dates else None,
                    number_of_episodes=len(s1_eps) or None,
                    is_anime=sac_s2.is_anime,
                )
                db.add(s1_work)
                await db.flush()
                await add_external_id(
                    db, "series", s1_work.id, "tmdb", f"tmdb:{SAC2045_TMDB_ID}#s1"
                )
        else:
            logger.info("[s1] work already exists: %s", s1_work.id)

        if apply and s1_work is not None:
            moved = 0
            for ep in stray:
                ep.series_id = s1_work.id
                moved += 1
            logger.info("[stray] re-pointed %d episodes to the S1 work", moved)
            n = await upsert_episodes(db, s1_work, s1_eps, entity_granularity="series")
            logger.info("[s1] upserted %d episode rows from TMDB", n)

        if apply:
            await db.commit()
            logger.info("[done] committed")
        else:
            logger.info("[done] dry-run — pass --apply to execute")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(main(args.apply))
