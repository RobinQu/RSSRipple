"""One-off backfill: group existing TVSeries into Wikidata franchise collections.

TV has no TMDB collection equivalent, so series grouping resolves each
unlinked TVSeries to a Wikidata entity (stored ``wikipedia_url`` /
``wikipedia_page_id`` first, exact-match ``wbsearchentities`` fallback),
reads its "part of the series" claim (P179), and upserts a WorkCollection
keyed by ``(external_source="wikidata", external_id=<franchise QID>)``.
Precision over recall: ambiguous entities and works with multiple P179 values
are SKIPPED, never guessed.

NOTE on locking: the embedded-Turso backend holds a single-process exclusive
file lock, so STOP the app (``docker compose stop app``) before running this
against the dev database, then start it again afterwards.

Dry-run by default; pass --apply to execute.
"""

import argparse
import asyncio

from sqlalchemy import select

from app.database import async_session_factory
from app.models.series import TVSeries
from app.services import wikidata_collection as wc

BATCH_SIZE = 20
RATE_LIMIT_SLEEP = 0.5  # seconds between series; each may make up to ~3 API calls


async def main(apply: bool) -> None:
    print(f"=== TV collection backfill via Wikidata ({'APPLY' if apply else 'DRY-RUN'}) ===")

    async with async_session_factory() as db:
        candidates = (await db.execute(
            select(TVSeries).where(TVSeries.collection_id.is_(None))
        )).scalars().all()
        print(f"{len(candidates)} series without collection")

        counts = {
            wc.STATUS_LINKED: 0,
            wc.STATUS_NO_P179: 0,
            wc.STATUS_NO_ENTITY: 0,
            wc.STATUS_AMBIGUOUS: 0,
            wc.STATUS_FAILED: 0,
        }
        pending = 0
        for s in candidates:
            title = s.title_cn or s.title_en or s.original_title
            try:
                status = await wc.link_series_wikidata_collection(db, s, apply=apply)
            except Exception as e:  # noqa: BLE001
                status = wc.STATUS_FAILED
                await db.rollback()
                print(f"  [error] {title!r}: {e}")
            counts[status] = counts.get(status, 0) + 1
            if status == wc.STATUS_LINKED:
                print(f"  [link] {title!r} (collection_id={'pending-commit' if apply else 'dry-run'})")
            else:
                print(f"  [skip:{status}] {title!r}")
            if apply and status == wc.STATUS_LINKED:
                pending += 1
                if pending >= BATCH_SIZE:
                    await db.commit()
                    pending = 0
            await asyncio.sleep(RATE_LIMIT_SLEEP)

        if apply:
            await db.commit()
            print(
                f"committed. linked={counts[wc.STATUS_LINKED]} "
                f"no-p179={counts[wc.STATUS_NO_P179]} "
                f"no-entity={counts[wc.STATUS_NO_ENTITY]} "
                f"ambiguous-skipped={counts[wc.STATUS_AMBIGUOUS]} "
                f"failed={counts[wc.STATUS_FAILED]}"
            )
        else:
            print(
                f"dry-run: would link {counts[wc.STATUS_LINKED]}/{len(candidates)} "
                f"(no-p179={counts[wc.STATUS_NO_P179]} "
                f"no-entity={counts[wc.STATUS_NO_ENTITY]} "
                f"ambiguous-skipped={counts[wc.STATUS_AMBIGUOUS]} "
                f"failed={counts[wc.STATUS_FAILED]}); re-run with --apply to execute."
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
