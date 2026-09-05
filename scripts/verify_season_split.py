"""Read-only verification for the season-split migration (作品单季化 P5).

Run after ``scripts/season_split_migration.py --apply`` (and safe to run any
time — every check is a SELECT). Checks:

  1. No dangling FKs: resource_work_links / resource_file_assignments /
     episodes / agent_works / pending_decisions / file_resources /
     tv_series.collection_id / work_external_ids full-join counts.
  2. Row-count conservation against a pre-migration snapshot
     (``--snapshot counts.json``; capture one beforehand with
     ``--write-snapshot counts.json``). Counts the migration must conserve
     exactly (file_resources / agent_works / pending_decisions /
     channel_raw_title_mappings / movies) fail on any delta; tables that may
     legitimately grow (tv_series / work_collections / work_external_ids) or
     shrink by collision dedup (episodes / links / assignments) are reported
     as deltas without failing.
  3. Per-season consistency: every TVSeries belongs to a collection;
     ``(collection_id, season_number)`` is unique; ``Episode.season`` equals
     its work's ``season_number``.
  4. ``search_text`` has no NULLs (tv_series / movies / work_collections).
  5. Degraded dispatch-equivalence check: every metadata-matched resource is
     mounted on a work, a collection, or work links (links-only multi_season
     packs). Warning by default (unmatched resources are a legitimate state);
     ``--strict`` escalates to failure.

Exit code: 0 = all checks passed, 1 = any failure.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from sqlalchemy import func, select

import app.database as app_database
from app.models.agent_work import AgentWork
from app.models.channel_raw_title_mapping import ChannelRawTitleMapping
from app.models.episode import Episode
from app.models.file_resource import FileResource
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.models.work_external_id import WorkExternalId


# child table → [(child column, parent table)]
def _build_fk_pairs():
    return [
        (ResourceWorkLink, ResourceWorkLink.series_id, TVSeries),
        (ResourceWorkLink, ResourceWorkLink.movie_id, Movie),
        (ResourceFileAssignment, ResourceFileAssignment.series_id, TVSeries),
        (ResourceFileAssignment, ResourceFileAssignment.movie_id, Movie),
        (Episode, Episode.series_id, TVSeries),
        (AgentWork, AgentWork.series_id, TVSeries),
        (AgentWork, AgentWork.movie_id, Movie),
        (PendingDecision, PendingDecision.series_id, TVSeries),
        (PendingDecision, PendingDecision.movie_id, Movie),
        (FileResource, FileResource.series_id, TVSeries),
        (FileResource, FileResource.movie_id, Movie),
        (FileResource, FileResource.collection_id, WorkCollection),
        (TVSeries, TVSeries.collection_id, WorkCollection),
    ]


_SNAPSHOT_TABLES = {
    "tv_series": TVSeries,
    "movies": Movie,
    "work_collections": WorkCollection,
    "episodes": Episode,
    "file_resources": FileResource,
    "resource_work_links": ResourceWorkLink,
    "resource_file_assignments": ResourceFileAssignment,
    "agent_works": AgentWork,
    "pending_decisions": PendingDecision,
    "channel_raw_title_mappings": ChannelRawTitleMapping,
    "work_external_ids": WorkExternalId,
}

# Counts the migration must conserve exactly; everything else is delta-only.
_CONSERVED_TABLES = {
    "movies",
    "file_resources",
    "agent_works",
    "pending_decisions",
    "channel_raw_title_mappings",
}


async def _count(db, model) -> int:
    return int((await db.execute(select(func.count()).select_from(model))).scalar_one())


async def _snapshot_counts(db) -> dict[str, int]:
    return {name: await _count(db, model) for name, model in _SNAPSHOT_TABLES.items()}


async def _check_dangling_fks(db) -> list[str]:
    failures: list[str] = []
    for child, column, parent in _build_fk_pairs():
        dangling = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(child)
                    .outerjoin(parent, column == parent.id)
                    .where(column.is_not(None), parent.id.is_(None))
                )
            ).scalar_one()
        )
        if dangling:
            failures.append(
                f"{child.__tablename__}.{column.key} → {parent.__tablename__}: "
                f"{dangling} dangling"
            )
    # Identity bag rows whose owning work is gone (per work_type).
    for work_type, parent in (
        ("series", TVSeries),
        ("movie", Movie),
        ("collection", WorkCollection),
    ):
        dangling = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(WorkExternalId)
                    .outerjoin(
                        parent,
                        (WorkExternalId.work_id == parent.id)
                        & (WorkExternalId.work_type == work_type),
                    )
                    .where(WorkExternalId.work_type == work_type, parent.id.is_(None))
                )
            ).scalar_one()
        )
        if dangling:
            failures.append(f"work_external_ids({work_type}): {dangling} dangling")
    return failures


async def _check_consistency(db) -> list[str]:
    failures: list[str] = []
    unattached = int(
        (
            await db.execute(
                select(func.count())
                .select_from(TVSeries)
                .where(TVSeries.collection_id.is_(None))
            )
        ).scalar_one()
    )
    if unattached:
        failures.append(f"tv_series without collection: {unattached}")
    dup_rows = (
        await db.execute(
            select(TVSeries.collection_id, TVSeries.season_number, func.count())
            .where(TVSeries.collection_id.is_not(None))
            .group_by(TVSeries.collection_id, TVSeries.season_number)
            .having(func.count() > 1)
        )
    ).all()
    for collection_id, season, cnt in dup_rows:
        failures.append(
            f"duplicate (collection {str(collection_id)[:8]}, season {season}): {cnt} works"
        )
    mismatched = int(
        (
            await db.execute(
                select(func.count())
                .select_from(Episode)
                .join(TVSeries, Episode.series_id == TVSeries.id)
                .where(Episode.season != TVSeries.season_number)
            )
        ).scalar_one()
    )
    if mismatched:
        failures.append(f"episodes with season != work.season_number: {mismatched}")
    return failures


async def _check_search_text(db) -> list[str]:
    failures: list[str] = []
    for model in (TVSeries, Movie, WorkCollection):
        nulls = int(
            (
                await db.execute(
                    select(func.count()).select_from(model).where(model.search_text.is_(None))
                )
            ).scalar_one()
        )
        if nulls:
            failures.append(f"{model.__tablename__}.search_text NULL: {nulls}")
    return failures


async def _unmounted_matched_resources(db) -> int:
    """Matched resources with no work FK, no collection FK, and no work links.

    Links count as a mount: links-only multi_season packs (work FKs cleared by
    design in the per-season model) are dispatched through their
    ``resource_work_links`` rows.
    """
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(FileResource)
                .where(
                    FileResource.metadata_matched_at.is_not(None),
                    FileResource.series_id.is_(None),
                    FileResource.movie_id.is_(None),
                    FileResource.audio_work_id.is_(None),
                    FileResource.collection_id.is_(None),
                    ~select(ResourceWorkLink.id)
                    .where(ResourceWorkLink.resource_id == FileResource.id)
                    .exists(),
                )
            )
        ).scalar_one()
    )


def _check_snapshot(current: dict[str, int], snapshot_path: str) -> tuple[list[str], list[str]]:
    with open(snapshot_path) as f:
        before = json.load(f)
    failures: list[str] = []
    notes: list[str] = []
    for name in _SNAPSHOT_TABLES:
        old = int(before.get(name, 0))
        new = current[name]
        delta = new - old
        if name in _CONSERVED_TABLES:
            if delta != 0:
                failures.append(f"{name}: {old} → {new} (delta {delta:+d}, must conserve)")
        elif delta != 0:
            notes.append(f"{name}: {old} → {new} (delta {delta:+d})")
    return failures, notes


async def verify(*, snapshot: str | None, strict: bool) -> int:
    failures: list[str] = []
    async with app_database.async_session_factory() as db:
        print("== 1. dangling FK checks ==")
        found = await _check_dangling_fks(db)
        failures += found
        print("  ok" if not found else "\n".join(f"  FAIL {f}" for f in found))

        print("== 3. per-season consistency ==")
        found = await _check_consistency(db)
        failures += found
        print("  ok" if not found else "\n".join(f"  FAIL {f}" for f in found))

        print("== 4. search_text NULLs ==")
        found = await _check_search_text(db)
        failures += found
        print("  ok" if not found else "\n".join(f"  FAIL {f}" for f in found))

        print("== 5. matched resources mounted on a work/collection ==")
        unmounted = await _unmounted_matched_resources(db)
        if unmounted and strict:
            failures.append(f"metadata-matched but unmounted resources: {unmounted}")
            print(f"  FAIL {unmounted} matched resources have no work/collection mount")
        else:
            print(f"  {unmounted} matched-but-unmounted resources (warning)")

        print("== 2. row-count conservation ==")
        if snapshot:
            found, notes = _check_snapshot(await _snapshot_counts(db), snapshot)
            failures += found
            for note in notes:
                print(f"  delta {note}")
            print("  ok" if not found else "\n".join(f"  FAIL {f}" for f in found))
        else:
            print("  skipped (pass --snapshot counts.json to compare)")

    if failures:
        print(f"\nVERIFY FAILED: {len(failures)} problem(s)")
        return 1
    print("\nVERIFY OK")
    return 0


async def _write_snapshot(path: str) -> None:
    async with app_database.async_session_factory() as db:
        counts = await _snapshot_counts(db)
    with open(path, "w") as f:
        json.dump(counts, f, indent=2, sort_keys=True)
    print(f"snapshot written to {path}: {counts}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", default=None, help="pre-migration counts JSON to compare")
    parser.add_argument("--write-snapshot", default=None, help="capture current counts and exit")
    parser.add_argument("--strict", action="store_true", help="escalate warnings to failures")
    args = parser.parse_args()
    if args.write_snapshot:
        asyncio.run(_write_snapshot(args.write_snapshot))
        return
    sys.exit(asyncio.run(verify(snapshot=args.snapshot, strict=args.strict)))


if __name__ == "__main__":
    main()
