"""One-off backfill: file-level mappings for stock batch (合集) resources.

Repairs FileResource rows created before the batch enrichment pass existed:
``is_batch=true`` rows with no ``resource_file_assignments`` get their
torrent listing inspected (cache-first download), deterministic placements
written (source=auto, cluster titles as work_title_hint) and
``season_ranges`` recomputed. The LLM refinement is NOT invoked here — run
the wizard's 重新解析 for that; this script is fully offline apart from
fetching missing .torrent files.

Rows whose ``torrent_url`` is a magnet (no listing available) are reported
and skipped. Rows already carrying assignments are skipped.

NOTE on locking: the embedded-Turso backend holds a single-process exclusive
file lock — stop the app before running against a live database.

Dry-run by default; pass --apply to execute.
"""

import argparse
import asyncio

from sqlalchemy import func, select

from app.database import async_session_factory
from app.models.file_resource import FileResource
from app.models.resource_file_assignment import ResourceFileAssignment
from app.services.torrent_inspect import (
    analyze_torrent_files,
    ensure_torrent_cached,
    fetch_torrent_file,
    parse_torrent_files,
)


async def _assignment_counts(db) -> dict[str, int]:
    rows = await db.execute(
        select(
            ResourceFileAssignment.resource_id,
            func.count(ResourceFileAssignment.id),
        ).group_by(ResourceFileAssignment.resource_id)
    )
    return {rid: cnt for rid, cnt in rows.all()}


async def backfill(apply: bool, limit: int | None) -> None:
    from app.services import batch_content_analysis as bca

    async with async_session_factory() as db:
        counts = await _assignment_counts(db)
        rows = (await db.execute(
            select(FileResource)
            .where(FileResource.is_batch.is_(True))
            .order_by(FileResource.created_at.desc())
        )).scalars().all()

        targets = [r for r in rows if counts.get(r.id, 0) == 0]
        if limit is not None:
            targets = targets[:limit]
        print(f"batch rows total={len(rows)} without assignments={len(targets)}")

        enriched = 0
        parsed_fail = 0
        magnet_skipped = 0
        for resource in targets:
            url = resource.torrent_url or ""
            if not url.startswith(("http://", "https://")):
                magnet_skipped += 1
                continue
            path = await ensure_torrent_cached(resource)
            if not path:
                path = await fetch_torrent_file(url, resource.id)
                if path:
                    resource.torrent_file = path
            files = parse_torrent_files(path) if path else None
            if not files:
                parsed_fail += 1
                continue

            report = analyze_torrent_files(files)
            await db.refresh(resource, ["file_assignments"])
            bca.apply_auto_assignments(resource, report)
            resource.season_ranges = bca.compute_season_ranges(resource)

            sample = [
                (a.work_title_hint, a.season, a.episode_start)
                for a in list(resource.file_assignments)[:3]
            ]
            print(
                f"  [{resource.id[:8]}] scope={report.scope} "
                f"files={len(report.file_parses)} ranges={resource.season_ranges} "
                f"sample={sample} :: {resource.title_raw[:60]}"
            )
            enriched += 1

        print(
            f"=== batch assignment backfill "
            f"({'applied' if apply else 'dry-run — pass --apply to write'}) ===\n"
            f"enriched={enriched} parse_failed={parsed_fail} magnet_or_fetch_skipped={magnet_skipped}"
        )
        if apply and enriched:
            await db.commit()
            print("committed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write changes")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    asyncio.run(backfill(args.apply, args.limit))


if __name__ == "__main__":
    main()
