"""One-off repair: re-run batch (合集) detection over existing FileResources.

Two mutually exclusive modes (default is the regex mode):

**Regex mode (default)** — re-run ``resource_parser.detect_batch`` over titles.

Why: ``detect_batch`` originally missed several common batch title shapes —
bracket-trailing ranges (``[...圣诞服女郎 01-13]``), bare ranges after a
season marker (``S01 | 01-24``), ``TV fin`` — so those torrents were stored
as single-episode resources, often with a garbage ``episode`` parsed from a
year/resolution/title number by the channel field_mapping. The pre-parser
now covers these shapes and clears ``episode`` at fetch time; this mode
repairs rows created before that.

For every non-batch resource whose ``title_raw`` now matches ``detect_batch``:
``is_batch=True``, ``episode=NULL`` (batch invariant), and
``episode_start``/``episode_end`` when the title spells out the boundaries.
Already-correct rows and non-matching titles are untouched.

**Torrent mode (``--from-torrent``)** — channel-A torrent content inspection
(``app.services.torrent_inspect.maybe_inspect_torrent``) over existing
non-batch resources: downloads each resource's .torrent file, parses the file
listing, and reclassifies via ``is_batch`` / ``batch_scope``
(season / multi_season / franchise) + episode range. Only plain http(s)
``torrent_url`` rows are eligible (magnets carry no listing and are skipped
by the inspector). The downloaded .torrent bytes are cached under
``settings.torrent_cache_dir`` and ``resource.torrent_file`` is recorded.
``maybe_inspect_torrent`` mutates the ORM row but never commits, so dry-run
is a plain rollback; note the on-disk .torrent cache files are still written
in dry-run (they are a reusable cache, not DB state).

NOTE on locking: the embedded-Turso backend holds a single-process exclusive
file lock, so STOP the app (``docker compose stop app``) before running this
against the dev database, then start it again afterwards.

Dry-run by default; pass ``--apply`` to write.

Usage:
    uv run python scripts/batch_redetect.py [--apply] [--channel-id UUID] [--limit N]
    uv run python scripts/batch_redetect.py --from-torrent [--apply] [--channel-id UUID] [--limit N] [--delay S]
"""

import argparse
import asyncio

from sqlalchemy import select

from app.database import async_session_factory
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.services.resource_parser import detect_batch
from app.services.torrent_inspect import maybe_inspect_torrent

APPLY_BATCH_SIZE = 50


async def run_regex(apply: bool, channel_id: str | None, limit: int | None) -> None:
    print(f"=== batch re-detection, regex mode ({'APPLY' if apply else 'DRY-RUN'}) ===")
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


async def run_from_torrent(
    apply: bool, channel_id: str | None, limit: int | None, delay: float
) -> None:
    print(f"=== batch re-detection, torrent mode ({'APPLY' if apply else 'DRY-RUN'}) ===")
    async with async_session_factory() as db:
        stmt = (
            select(FileResource)
            .where(
                FileResource.is_batch.is_(False),
                FileResource.torrent_url.like("http%"),
            )
            .order_by(FileResource.created_at)
        )
        if channel_id:
            stmt = stmt.where(FileResource.channel_id == channel_id)
        if limit:
            stmt = stmt.limit(limit)
        rows = (await db.execute(stmt)).scalars().all()
        print(f"inspecting {len(rows)} non-batch resources with http(s) torrent URLs")

        # Channel config drives franchise member-work matching; load it once
        # per channel instead of lazy-loading per resource.
        channels: dict[str, Channel] = {}

        reclassified = 0
        for i, r in enumerate(rows, 1):
            was_batch = r.is_batch
            # Mutates the row in-session (never commits). Failures (download,
            # parse, magnet) are silent inside the inspector and leave the
            # row untouched.
            if r.channel_id not in channels:
                channels[r.channel_id] = await db.get(Channel, r.channel_id)
            await maybe_inspect_torrent(db, r, channels[r.channel_id])
            if r.is_batch and not was_batch:
                reclassified += 1
                print(
                    f"  [{i}/{len(rows)}] -> {r.batch_scope} "
                    f"start={r.episode_start} end={r.episode_end} | {(r.title_raw or '')[:100]}"
                )
            if delay:
                await asyncio.sleep(delay)
            if apply and i % APPLY_BATCH_SIZE == 0:
                await db.commit()
                print(f"    ... committed {i}/{len(rows)}")
        if apply:
            await db.commit()
            print(f"applied: {reclassified} resources reclassified as batch")
        else:
            # maybe_inspect_torrent writes is_batch/batch_scope/torrent_file
            # in-session; roll back so dry-run leaves the DB untouched.
            await db.rollback()
            print(f"dry-run only ({reclassified} would be reclassified) - re-run with --apply to write")


async def main(
    apply: bool,
    channel_id: str | None,
    limit: int | None,
    from_torrent: bool,
    delay: float,
) -> None:
    if from_torrent:
        await run_from_torrent(apply, channel_id, limit, delay)
    else:
        await run_regex(apply, channel_id, limit)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write changes (default: dry-run)")
    parser.add_argument("--channel-id", default=None, help="restrict to one channel")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--from-torrent",
        action="store_true",
        help="torrent content inspection (channel A) instead of the default title-regex re-scan",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.2,
        help="seconds between .torrent downloads in --from-torrent mode (default: 0.2)",
    )
    args = parser.parse_args()
    asyncio.run(main(args.apply, args.channel_id, args.limit, args.from_torrent, args.delay))
