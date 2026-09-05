"""Repair post-season-split leftovers found by the 2026-09 audit.

All damage traces back to the season-split migration (P8) and the
season-blind corruption era it cleaned up after. Cases handled:

A. **Oregairu 续/完 mislabeled as season 0** — the works carrying the real
   bangumi identities (102134=续/S2, 277954=完/S3) were created with
   ``season_number=0``; the migration then created identity-less S2/S3 shells
   in the same collections. Repair: drop the shell's duplicate episode rows,
   merge the shell into the real work (``_merge_series_group`` re-points
   links/assignments/decisions and unions bags), then renumber the survivor
   to its true season and re-tag its episode rows.

B. **芙莉蓮 S2 stale episodes** — the migration copied S1's 28 bangumi
   episodes into the S2 work. The real S2 (bangumi:515759) lists episodes
   29-38 (bangumi continuation numbering). Replace.

C. **史萊姆 S4 stale episodes** — episodes 1-24 are S1's titles (upserted
   while the work wrongly carried S1's bangumi id); episodes 73-96 match the
   real S4 entry (bangumi:515594, continuation numbering). Delete the 1-24
   block, upsert the authoritative list to refresh titles.

D. **猫眼三姐妹 S1 count** — bangumi:9304 is a combined 73-episode entry; the
   work's episode rows (1-73) are consistent with it, only
   ``number_of_episodes`` lagged at 36 (wikipedia S1-only figure). Fix the
   count to match the identity source.

E. **俺春物原系列 S0 identity** — the series-level wikipedia id sits on the
   season-0 specials work; move it to the collection bag and clear the work
   column (series-level ids belong on the collection).

F. **S0 specials works groomed** — season-0 works with no episode rows got
   the MAIN entry's ``number_of_episodes``/``start_date``/``end_date`` from
   season-blind refreshes; clear them back to unknown (batch-pack links are
   unrelated to those scalars and stay untouched).

Dry-run by default; pass ``--apply`` to execute.

Usage:
    uv run python scripts/post_split_leftover_repair.py
    uv run python scripts/post_split_leftover_repair.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import select

from app.database import async_session_factory
from app.models.episode import Episode
from app.models.series import TVSeries
from app.models.work_external_id import WorkExternalId
from app.services.bangumi_client import bangumi_configured, get_subject_episodes
from app.services.external_ids import add_external_id
from app.services.metadata_bangumi import _episode_list_from
from app.services.metadata_dedup import DedupReport, _merge_series_group
from app.services.metadata_service import upsert_episodes
from app.services.runtime_config import load_runtime_config

logger = logging.getLogger("post_split_leftover_repair")

# Case A: (survivor S0 work, migration shell, true season)
_OREGAIRU_PAIRS = [
    ("09fc280c-b80f-4af7-844e-fa8a39bd65f1", "e8c3f2d1-a326-429b-b3df-0fa2c57c0fcf", 2),  # 续
    ("04db47a0-0809-4cd8-be7a-582d0ade614a", "2f48f108-8266-4f58-893f-d147917b0b2e", 3),  # 完
]
# Case B/C: (work, bangumi subject, stale episode predicate description)
_FRIEREN_S2 = ("712d437c-4449-4e8b-8fb4-e539e0d5dcec", 515759)
_SLIME_S4 = ("4538a0ec-4a48-4a78-8522-ec16520340d5", 515594)
_CATSEYE_S1 = "a6b8c770-8f24-4826-a18c-a9157178cf54"
_OREGAIRU_S0 = "f0fa3466-23b7-486a-a2b4-07349e9e7cbc"


async def _case_a_oregairu(db, apply: bool) -> None:
    for survivor_id, shell_id, season in _OREGAIRU_PAIRS:
        survivor = await db.get(TVSeries, survivor_id)
        shell = await db.get(TVSeries, shell_id)
        if survivor is None or shell is None:
            logger.info("[A] %s: rows missing — already repaired?", survivor_id)
            continue
        if survivor.season_number == season:
            logger.info("[A] %r already season %d — skip", survivor.title_cn, season)
            continue
        logger.info(
            "[A] %r: merge shell %s into survivor %s, renumber S0 -> S%d%s",
            survivor.title_cn, shell_id, survivor_id, season, "" if apply else " [dry-run]",
        )
        if not apply:
            continue
        # The shell's episode rows duplicate the same season's content (the
        # survivor already carries the authoritative bangumi-numbered rows);
        # drop them before the merge so no mixed-numbering rows survive.
        shell_eps = (await db.execute(
            select(Episode).where(Episode.series_id == shell.id)
        )).scalars().all()
        for ep in shell_eps:
            await db.delete(ep)
        report = DedupReport()
        await _merge_series_group(db, [survivor, shell], report, survivor=survivor)
        await db.flush()  # shell row gone → (collection, season) slot is free
        survivor.season_number = season
        for ep in (await db.execute(
            select(Episode).where(Episode.series_id == survivor.id)
        )).scalars().all():
            ep.season = season
        logger.info("[A]   merged (%s), renumbered to S%d", report.notes[-1], season)


async def _replace_episodes_from_bangumi(
    db, work_id: str, subject_id: int, season: int, drop_predicate, label: str, apply: bool
) -> None:
    """Drop stale episode rows and upsert the authoritative bangumi list."""
    import httpx

    work = await db.get(TVSeries, work_id)
    if work is None:
        logger.info("[%s] work %s missing — already repaired?", label, work_id)
        return
    rows = (await db.execute(
        select(Episode).where(Episode.series_id == work_id)
    )).scalars().all()
    stale = [r for r in rows if drop_predicate(r)]
    logger.info(
        "[%s] %r: %d stale episode rows to delete%s",
        label, work.title_cn, len(stale), "" if apply else " [dry-run]",
    )
    if not apply:
        return
    async with httpx.AsyncClient(timeout=15) as client:
        raw = await get_subject_episodes(client, subject_id)
    ep_list = _episode_list_from(raw, season)
    for r in stale:
        await db.delete(r)
    await db.flush()
    n = await upsert_episodes(db, work, ep_list, entity_granularity="season")
    logger.info("[%s]   upserted %d bangumi episode rows", label, n)


async def _case_d_catseye(db, apply: bool) -> None:
    work = await db.get(TVSeries, _CATSEYE_S1)
    if work is None or work.number_of_episodes == 73:
        logger.info("[D] 猫眼三姐妹: already consistent — skip")
        return
    logger.info(
        "[D] 猫眼三姐妹: number_of_episodes %s -> 73 (bangumi:9304 combined entry)%s",
        work.number_of_episodes, "" if apply else " [dry-run]",
    )
    if apply:
        work.number_of_episodes = 73


async def _case_e_oregairu_s0_identity(db, apply: bool) -> None:
    work = await db.get(TVSeries, _OREGAIRU_S0)
    if work is None or not (work.external_id or "").startswith("wikipedia:"):
        logger.info("[E] 俺春物 S0 identity already moved — skip")
        return
    ext_id, ext_src = work.external_id, work.external_source
    logger.info(
        "[E] move %r from S0 work to collection %s bag%s",
        ext_id, work.collection_id, "" if apply else " [dry-run]",
    )
    if not apply:
        return
    if work.collection_id:
        await add_external_id(db, "collection", work.collection_id, ext_src, ext_id)
    bag_rows = (await db.execute(
        select(WorkExternalId).where(
            WorkExternalId.work_type == "series",
            WorkExternalId.work_id == work.id,
            WorkExternalId.external_id == ext_id,
        )
    )).scalars().all()
    for row in bag_rows:
        await db.delete(row)
    work.external_id = None
    work.external_source = None


async def _case_f_s0_shells(db, apply: bool) -> None:
    """Clear main-series scalar fields on season-0 specials works.

    Only works without episode rows are touched; batch-pack links are
    unrelated to these scalars and stay untouched."""
    works = (await db.execute(
        select(TVSeries).where(TVSeries.season_number == 0)
    )).scalars().all()
    for work in works:
        manual = set(work.manually_edited_fields or [])
        eps = (await db.execute(
            select(Episode.id).where(Episode.series_id == work.id).limit(1)
        )).first()
        if eps:
            continue  # has real episode rows — not an empty shell
        cleared = []
        for attr in ("number_of_episodes", "start_date", "end_date"):
            if attr not in manual and getattr(work, attr) is not None:
                if apply:
                    setattr(work, attr, None)
                cleared.append(attr)
        if cleared:
            logger.info(
                "[F] %r S0 shell: clear %s%s",
                work.title_cn, cleared, "" if apply else " [dry-run]",
            )


async def main(apply: bool) -> None:
    async with async_session_factory() as db:
        await load_runtime_config(db)
        await _case_a_oregairu(db, apply)
        if bangumi_configured():
            await _replace_episodes_from_bangumi(
                db, *_FRIEREN_S2, season=2,
                drop_predicate=lambda r: True,  # all 28 rows are S1's list
                label="B/frieren-s2", apply=apply,
            )
            await _replace_episodes_from_bangumi(
                db, *_SLIME_S4, season=4,
                drop_predicate=lambda r: r.episode <= 24,  # S1-titled junk block
                label="C/slime-s4", apply=apply,
            )
        else:
            logger.warning("[B/C] bangumi token not configured — skipped")
        await _case_d_catseye(db, apply)
        await _case_e_oregairu_s0_identity(db, apply)
        await _case_f_s0_shells(db, apply)
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
