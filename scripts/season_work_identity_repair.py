"""Repair per-season works damaged by the season-blind refresh/auto-link.

Background: before the season-aware bangumi auto-link and the season-scoped
refresh fill, a season>1 work could absorb the season-1 entry's identity and
premiere date (the refresh searched the base title with no season context and
the bangumi auto-link always pinned the base-named = season-1 subject). Two
distinct damage shapes result:

1. **Stolen identity** — a season>1 work carries the *same* ``external_id``
   as another work (the season-1 entry), plus that entry's dates / episode
   count / rating / genre / description / poster. Phase 1 (offline, always
   runs) strips every entity-sourced field from the non-owner rows (owner =
   the lowest-season row sharing the id) and removes the colliding
   ``WorkExternalId`` bag rows, so a later refresh can re-resolve cleanly.
2. **Missing season premiere** — season works with ``start_date IS NULL``
   (typ. lazily created shells) never resolve the channel-required ``year``
   field. Phase 2 (``--backfill``, network + LLM) re-runs
   ``refresh_work_metadata`` per work; the refresh now carries the work's
   ``season_number`` as ``season_hint`` so season-granular sources (bangumi)
   pick the correct season's entry and series-level sources only fill
   season-scoped dates.

Dry-run by default; pass --apply to execute. Phase 2 needs the same
credentials as the runtime (LLM key; bangumi/tmdb tokens as applicable).

Usage:
    uv run python scripts/season_work_identity_repair.py                 # dry-run
    uv run python scripts/season_work_identity_repair.py --apply         # phase 1 only
    uv run python scripts/season_work_identity_repair.py --apply --backfill [--limit N] [--delay S]
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import select

from app.database import async_session_factory
from app.models.series import TVSeries
from app.models.work_external_id import WorkExternalId
from app.services.metadata_service import refresh_work_metadata
from app.services.runtime_config import load_runtime_config

logger = logging.getLogger("season_work_identity_repair")

# Fields the wrong-entity refresh could have filled. Titles/aliases are kept:
# they follow the base-title convention and are not entity-exclusive.
_ENTITY_FIELDS = (
    "external_id",
    "external_source",
    "start_date",
    "end_date",
    "number_of_episodes",
    "rating",
    "genre",
    "description",
    "poster_url",
    "status",
)


def _title_of(work: TVSeries) -> str:
    return work.title_cn or work.title_en or work.original_title or work.id


async def _find_identity_collisions(db) -> list[list[TVSeries]]:
    """Groups of TVSeries rows sharing one external_id, lowest season first."""
    rows = list((await db.execute(select(TVSeries))).scalars().all())
    by_ext: dict[str, list[TVSeries]] = {}
    for w in rows:
        if w.external_id:
            by_ext.setdefault(w.external_id, []).append(w)
    groups = [g for g in by_ext.values() if len(g) > 1]
    for g in groups:
        # Owner = lowest regular season; S0 specials sort last (a specials work
        # must never win the main entry's identity over season 1).
        g.sort(key=lambda w: (w.season_number == 0, w.season_number, w.created_at))
    return groups


async def _strip_stolen_identity(db, work: TVSeries, owner: TVSeries) -> list[str]:
    """Clear entity-sourced fields on a non-owner row; returns cleared names."""
    manual = set(work.manually_edited_fields or [])
    cleared: list[str] = []
    for attr in _ENTITY_FIELDS:
        if attr in manual:
            continue
        if getattr(work, attr) is not None:
            setattr(work, attr, None)
            cleared.append(attr)
    bag_rows = (await db.execute(
        select(WorkExternalId).where(
            WorkExternalId.work_type == "series",
            WorkExternalId.work_id == work.id,
            WorkExternalId.external_id == owner.external_id,
        )
    )).scalars().all()
    for row in bag_rows:
        await db.delete(row)
        cleared.append(f"bag:{row.external_id}")
    return cleared


async def _backfill_targets(db) -> list[dict]:
    """Season works missing start_date, with the source a refresh should use."""
    works = list((await db.execute(
        select(TVSeries).where(TVSeries.start_date.is_(None)).order_by(TVSeries.created_at)
    )).scalars().all())
    members_by_collection: dict[str, list[TVSeries]] = {}
    collection_ids = {w.collection_id for w in works if w.collection_id}
    if collection_ids:
        members = list((await db.execute(
            select(TVSeries).where(TVSeries.collection_id.in_(collection_ids))
        )).scalars().all())
        for m in members:
            members_by_collection.setdefault(m.collection_id, []).append(m)
    out: list[dict] = []
    for w in works:
        source = w.external_source
        if source not in ("wikipedia", "tmdb", "bangumi"):
            # Fall back to a collection sibling's identity source (the season-1
            # member is the most reliable carrier of the series' home source).
            siblings = members_by_collection.get(w.collection_id or "", [])
            source = next(
                (
                    m.external_source
                    for m in sorted(siblings, key=lambda m: (m.season_number, m.created_at))
                    if m.external_source in ("wikipedia", "tmdb", "bangumi")
                ),
                None,
            )
        out.append({
            "id": w.id,
            "title": _title_of(w),
            "season_number": w.season_number,
            "external_id": w.external_id,
            "source": source,
        })
    return out


async def main(apply: bool, backfill: bool, limit: int | None, delay: float) -> None:
    async with async_session_factory() as db:
        await load_runtime_config(db)
        # ── Phase 1: stolen-identity collisions (offline) ──
        groups = await _find_identity_collisions(db)
        if not groups:
            logger.info("[phase1] no duplicate external_id groups")
        for group in groups:
            owner = group[0]
            logger.info(
                "[phase1] %r shared by %d works; owner = %r S%s (%s)",
                owner.external_id, len(group),
                _title_of(owner), owner.season_number, owner.id,
            )
            for work in group[1:]:
                if apply:
                    cleared = await _strip_stolen_identity(db, work, owner)
                    logger.info(
                        "[phase1]   stripped %r S%s (%s): %s",
                        _title_of(work), work.season_number, work.id, cleared,
                    )
                else:
                    logger.info(
                        "[phase1]   would strip %r S%s (%s)",
                        _title_of(work), work.season_number, work.id,
                    )
        if apply and groups:
            await db.commit()
            logger.info("[phase1] committed")

        if not backfill:
            return

        # ── Phase 2: re-refresh works missing a season premiere ──
        targets = await _backfill_targets(db)
        logger.info("[phase2] %d works missing start_date", len(targets))
        done = 0
        for t in targets:
            if limit is not None and done >= limit:
                logger.info("[phase2] --limit %d reached", limit)
                break
            if not t["source"]:
                logger.info(
                    "[phase2]   skip %r S%s (%s): no usable source",
                    t["title"], t["season_number"], t["id"],
                )
                continue
            logger.info(
                "[phase2]   refresh %r S%s (%s) via %s%s",
                t["title"], t["season_number"], t["id"], t["source"],
                "" if apply else " [dry-run]",
            )
            if not apply:
                done += 1
                continue
            try:
                result = await refresh_work_metadata(db, t["id"], "tv", t["source"])
                logger.info(
                    "[phase2]     -> found=%s filled=%s (%s)",
                    result.get("found"), result.get("filled"), result.get("message"),
                )
            except Exception as e:  # noqa: BLE001 — one failure never aborts the run
                logger.warning("[phase2]     -> failed: %s", e)
                await db.rollback()
            done += 1
            await asyncio.sleep(delay)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    parser.add_argument("--backfill", action="store_true", help="phase 2: re-refresh works missing start_date")
    parser.add_argument("--limit", type=int, default=None, help="max phase-2 refreshes")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between refreshes")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(main(args.apply, args.backfill, args.limit, args.delay))
