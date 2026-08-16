"""One-off repair: re-run batch (合集) detection over existing FileResources.

Why: ``detect_batch`` originally missed several common batch title shapes —
bracket-trailing ranges (``[...圣诞服女郎 01-13]``), bare ranges after a
season marker (``S01 | 01-24``), ``TV fin`` — so those torrents were stored
as single-episode resources, often with a garbage ``episode`` parsed from a
year/resolution/title number by the channel field_mapping. The pre-parser
now covers these shapes and clears ``episode`` at fetch time; this script
repairs rows created before that.

For every non-batch resource whose ``title_raw`` now matches ``detect_batch``:
``is_batch=True``, ``episode=NULL`` (batch invariant), and
``episode_start``/``episode_end`` when the title spells out the boundaries.
Already-correct rows and non-matching titles are untouched.

NOTE on locking: the embedded-Turso backend holds a single-process exclusive
file lock, so STOP the app (``docker compose stop app``) before running this
against the dev database, then start it again afterwards.

Dry-run by default; pass ``--apply`` to write.

Usage:
    uv run python scripts/batch_redetect.py [--apply] [--channel-id UUID] [--limit N]
"""

import argparse
import asyncio

from sqlalchemy import select

from app.database import async_session_factory
from app.models.file_resource import FileResource
from app.services.resource_parser import detect_batch

APPLY_BATCH_SIZE = 50


async def main(apply: bool, channel_id: str | None, limit: int | None) -> None:
    print(f"=== batch re-detection ({'APPLY' if apply else 'DRY-RUN'}) ===")
    async with async_session_factory() as db:
        stmt = (
            select(FileResource)
            .where(FileResource.is_batch.is_(False))
            .order_by(FileResource.created_at)
        )
        if channel_id:
            stmt = stmt.where(FileResource.channel_id == channel_id)
        rows = (await db.execute(stmt)).scalars().all()
        print(f"scanning {len(rows)} non-batch resources")

        hits: list[tuple[FileResource, int | None, int | None]] = []
        for r in rows:
            is_batch, start, end = detect_batch(r.title_raw or "")
            if is_batch:
                hits.append((r, start, end))
        if limit:
            hits = hits[:limit]
        print(f"newly detected batches: {len(hits)}")

        for r, start, end in hits:
            print(
                f"  ep={r.episode} -> start={start} end={end} | {(r.title_raw or '')[:100]}"
            )
            if apply:
                r.is_batch = True
                r.episode = None
                if start is not None:
                    r.episode_start = start
                if end is not None:
                    r.episode_end = end
        if apply:
            for i in range(0, len(hits), APPLY_BATCH_SIZE):
                await db.commit()
                if hits:
                    print(f"    ... committed {min(i + APPLY_BATCH_SIZE, len(hits))}/{len(hits)}")
            print(f"applied: {len(hits)} resources marked as batch")
        else:
            print("dry-run only - re-run with --apply to write")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    parser.add_argument("--channel-id", default=None, help="restrict to one channel")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(main(args.apply, args.channel_id, args.limit))
