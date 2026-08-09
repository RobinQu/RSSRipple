"""One-off backfill: link existing Movies to their TMDB collection.

Iterates movies whose ``external_id`` is in canonical ``tmdb:<digits>`` form
with a NULL ``collection_id`` and calls the deterministic
``link_movie_collection`` (TMDB movie details -> ``belongs_to_collection`` ->
upsert WorkCollection). New movies are linked at upsert time; this script
repairs the rows created before that.

NOTE on locking: the embedded-Turso backend holds a single-process exclusive
file lock, so STOP the app (``docker compose stop app``) before running this
against the dev database, then start it again afterwards.

Dry-run by default; pass --apply to execute.
"""

import argparse
import asyncio

from sqlalchemy import select

from app.config import settings
from app.database import async_session_factory
from app.models.movie import Movie
from app.services.collection_service import (
    _canonical_tmdb_id,
    fetch_tmdb_movie_collection,
    upsert_collection_from_tmdb,
)

BATCH_SIZE = 20


async def main(apply: bool) -> None:
    print(f"=== movie collection backfill ({'APPLY' if apply else 'DRY-RUN'}) ===")
    if not settings.tmdb_api_key:
        print("TMDB_API_KEY is not set; nothing to do.")
        return

    async with async_session_factory() as db:
        candidates = (await db.execute(
            select(Movie).where(Movie.collection_id.is_(None))
        )).scalars().all()
        candidates = [m for m in candidates if _canonical_tmdb_id(m.external_id)]
        print(f"{len(candidates)} movies with canonical tmdb: id and no collection")

        linked = no_collection = failed = 0
        pending = 0
        for m in candidates:
            title = m.title_cn or m.title_en or m.original_title
            coll = await fetch_tmdb_movie_collection(_canonical_tmdb_id(m.external_id))
            await asyncio.sleep(0.25)
            if coll is None:
                no_collection += 1
                print(f"  [skip] {title!r} (tmdb:{m.external_id}): no collection")
                continue
            print(f"  [link] {title!r} -> {coll.get('name')!r} (tmdb_collection:{coll['id']})")
            if apply:
                try:
                    collection = await upsert_collection_from_tmdb(db, coll)
                    m.collection_id = collection.id
                    linked += 1
                    pending += 1
                    if pending >= BATCH_SIZE:
                        await db.commit()
                        pending = 0
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    await db.rollback()
                    print(f"    [error] {title!r}: {e}")
            else:
                linked += 1

        if apply:
            await db.commit()
            print(f"committed. linked={linked} no-collection={no_collection} failed={failed}")
        else:
            print(
                f"dry-run: would link {linked}/{len(candidates)} "
                f"(no-collection={no_collection}); re-run with --apply to execute."
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
