"""Entry point: ``python -m app.scripts.dedup_metadata``.

Merges TVSeries/Movie rows that duplicate the same real work (created before
the canonical-external-id upsert was added). See
``app.services.metadata_dedup`` for the merge logic.

Prints a summary to stdout. Idempotent — a second run is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from app.database import async_session_factory
from app.services.metadata_dedup import merge_duplicate_metadata

logger = logging.getLogger(__name__)


async def _run() -> int:
    async with async_session_factory() as db:
        try:
            report = await merge_duplicate_metadata(db)
            await db.commit()
        except Exception:
            await db.rollback()
            raise

    print("Metadata dedup complete:")
    print(f"  series groups merged: {report.series_groups}")
    print(f"  series rows removed:  {report.series_removed}")
    print(f"  movie  groups merged: {report.movie_groups}")
    print(f"  movie  rows removed:  {report.movies_removed}")
    print(f"  file_resources re-pointed: {report.file_resources_updated}")
    print(f"  agent_works    re-pointed: {report.agent_works_updated}")
    print(f"  channel_mappings re-pointed: {report.mappings_updated}")
    print(f"  pending_decisions re-pointed: {report.pending_decisions_updated}")
    print(f"  episodes re-pointed: {report.episodes_updated}")
    for note in report.notes:
        print(f"  {note}")
    return 0


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
