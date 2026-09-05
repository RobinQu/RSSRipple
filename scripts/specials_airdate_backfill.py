"""Specials/missing-date backfill: fill start_date for season works (notably
season-0 specials, whose own premiere is never discoverable) from the
earliest start_date of the SAME collection's non-specials members — the same
``_collection_fallback_start_date`` rule the upsert/refresh paths apply (the
``year`` required-field gate keeps resources of undated works in
文件资源元数据确认).

DRY-RUN by default; ``--apply`` writes only NULL start_date fields (never
overwrites an existing date) and respects ``manually_edited_fields``.

NOTE on locking: the embedded-Turso backend holds a single-process exclusive
file lock - STOP the app before running this against a Turso dev database.
Against PostgreSQL no stop is needed.

Usage:
    uv run python scripts/specials_airdate_backfill.py [--apply] [--limit N]
"""
import argparse
import asyncio
from datetime import date

from sqlalchemy import select

from app.database import async_session_factory
from app.models.series import TVSeries
from app.services.metadata_service import (
    _collection_fallback_start_date,
    manually_edited_fields,
)

APPLY_BATCH_SIZE = 20


async def select_candidate_works(db) -> list[TVSeries]:
    """Collection member works whose start_date is still NULL."""
    return list((await db.execute(
        select(TVSeries).where(
            TVSeries.start_date.is_(None),
            TVSeries.collection_id.is_not(None),
        ).order_by(TVSeries.created_at)
    )).scalars().all())


async def evaluate_work(db, work: TVSeries) -> dict:
    """Compute the fallback start_date for one work. Pure read - no DB
    writes, so the --apply path can call this then persist the result."""
    report = {
        "id": work.id,
        "title": work.title_cn or work.title_en,
        "season": work.season_number,
    }
    if "start_date" in manually_edited_fields(work):
        return report | {"ok": False, "reason": "start_date manually edited"}
    fallback = await _collection_fallback_start_date(db, work.collection_id, work.id)
    if not fallback:
        return report | {"ok": False, "reason": "no dated non-specials sibling"}
    return report | {"ok": True, "start_date": str(fallback)}


def apply_report(work: TVSeries, rep: dict) -> list[str]:
    """Write the fallback date onto the work (NULL field only). Returns the
    list of fields actually changed."""
    changed: list[str] = []
    if rep.get("start_date") and work.start_date is None:
        work.start_date = date.fromisoformat(rep["start_date"])
        changed.append("start_date")
    return changed


async def main(limit: int | None, apply: bool) -> None:
    async with async_session_factory() as db:
        works = await select_candidate_works(db)
        if limit:
            works = works[:limit]
        print(f"{len(works)} works missing start_date")
        ok = failed = changed_rows = 0
        for i, work in enumerate(works, 1):
            rep = await evaluate_work(db, work)
            if rep.get("ok"):
                ok += 1
                line = (f"[{i}/{len(works)}] OK {rep['title']} S{rep['season']}: "
                        f"{rep['start_date']}")
                if apply:
                    changed = apply_report(work, rep)
                    if changed:
                        changed_rows += 1
                        line += f"  <- wrote {','.join(changed)}"
                    else:
                        line += "  <- nothing to write (field already set)"
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
    args = parser.parse_args()
    asyncio.run(main(args.limit, args.apply))
