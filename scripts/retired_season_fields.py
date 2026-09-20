"""Audit retired season counts, or apply one explicitly reviewed cleanup.

Stop application writers and back up the DB before applying a review. Export
is read-only and never runs startup. A review must add confirmed_season; it
cannot change the work's season or move/delete any associated business rows.
"""

import argparse
import asyncio
import copy
import hashlib
import json

from sqlalchemy import or_, select, update

import app.database as database
import app.models  # noqa: F401
from app.models.episode import Episode
from app.models.file_resource import FileResource
from app.models.resource_file_assignment import ResourceFileAssignment
from app.models.resource_work_link import ResourceWorkLink
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.models.work_external_id import WorkExternalId
from app.services.metadata_source_registry import split_season_identity


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def fingerprint(snapshot):
    return hashlib.sha256(canonical(snapshot).encode()).hexdigest()


async def review_work(db, work_id, *, lock=False):
    async def rows(model, condition, *, take_lock=True):
        query = select(model.__table__).where(condition).order_by(model.id)
        if lock and take_lock:
            query = query.with_for_update()
        return [dict(row) for row in (await db.execute(query)).mappings()]

    works = await rows(TVSeries, TVSeries.id == work_id, take_lock=False)
    if not works:
        raise ValueError(f"Work not found: {work_id}")
    work = works[0]
    collection = await rows(WorkCollection, WorkCollection.id == work["collection_id"])
    if lock:
        locked = await rows(TVSeries, TVSeries.id == work_id)
        if not locked or locked[0]["collection_id"] != work["collection_id"]:
            raise ValueError("Work changed collections; export and review again")
        work = locked[0]
    siblings = (
        select(TVSeries.id, TVSeries.season_number)
        .where(
            TVSeries.collection_id == work["collection_id"],
            TVSeries.id != work_id,
        )
        .order_by(TVSeries.id)
    )
    if lock:
        siblings = siblings.with_for_update()
    links = await rows(ResourceWorkLink, ResourceWorkLink.series_id == work_id)
    assignments = await rows(ResourceFileAssignment, ResourceFileAssignment.series_id == work_id)
    resource_ids = {row["resource_id"] for row in [*links, *assignments]}
    snapshot = {
        "work": work,
        "collection": collection,
        "siblings": [dict(row) for row in (await db.execute(siblings)).mappings()],
        "identities": await rows(
            WorkExternalId, (WorkExternalId.work_type == "series") & (WorkExternalId.work_id == work_id)
        ),
        "episodes": await rows(Episode, Episode.series_id == work_id),
        "resources": await rows(
            FileResource, or_(FileResource.series_id == work_id, FileResource.id.in_(resource_ids))
        ),
        "links": links,
        "assignments": assignments,
    }
    # Make the in-memory and exported/reloaded documents identical, including dates.
    snapshot = json.loads(canonical(snapshot))
    return {
        "version": 1,
        "work_id": work_id,
        "snapshot": snapshot,
        "fingerprint": fingerprint(snapshot),
        "blocked_reasons": blocked_reasons(snapshot),
    }


def blocked_reasons(snapshot):
    work = snapshot["work"]
    season = work["season_number"]
    reasons = []
    if type(season) is not int or season < 0:
        reasons.append("invalid current season")
    if not work["collection_id"] or len(snapshot["collection"]) != 1:
        reasons.append("missing collection; repair membership first")
    if any(row["season_number"] == season for row in snapshot["siblings"]):
        reasons.append("duplicate collection season slot; resolve before cleanup")
    protection = work["manually_edited_fields"]
    if protection is not None and (not isinstance(protection, list) or not all(isinstance(k, str) for k in protection)):
        reasons.append("malformed manual protection fields")
    declared = work["seasons"]
    if declared is not None:
        if not isinstance(declared, list):
            reasons.append("malformed legacy season evidence")
        elif any(
            not isinstance(item, dict) or type(item.get("season_number")) is not int or item["season_number"] != season
            for item in declared
        ):
            reasons.append("legacy season evidence conflicts; use reviewed season splitting")
    for row in snapshot["episodes"]:
        if row["season"] != season:
            reasons.append(f"cross-season episode: {row['id']}")
    for row in snapshot["resources"]:
        if (
            (not row["is_batch"] or row["batch_scope"] == "season")
            and row["season"] is not None
            and row["season"] != season
        ):
            reasons.append(f"conflicting single-season resource: {row['id']}")
    for row in snapshot["assignments"]:
        if row["season"] is not None and row["season"] != season:
            reasons.append(f"cross-season file assignment: {row['id']}")
    identities = [work["external_id"], *(row["external_id"] for row in snapshot["identities"])]
    for identity in identities:
        parsed = split_season_identity(identity)
        if parsed is not None and parsed[1] != season:
            reasons.append(f"conflicting season identity: {identity}")
    return reasons


def _without_update_time(snapshot):
    snapshot = copy.deepcopy(snapshot)
    snapshot["work"].pop("updated_at", None)
    return snapshot


async def apply_review(db, review):
    """Stage one cleanup; caller owns commit/rollback. Original review is retained."""
    if review.get("version") != 1 or fingerprint(review.get("snapshot")) != review.get("fingerprint"):
        raise ValueError("Invalid review fingerprint/version")
    confirmed = review.get("confirmed_season")
    if type(confirmed) is not int or confirmed < 0:
        raise ValueError("Explicit confirmed_season is required")
    current = await review_work(db, review["work_id"], lock=True)
    snapshot = current["snapshot"]
    if confirmed != snapshot["work"]["season_number"]:
        raise ValueError("Confirmation cannot change the existing season")
    if current["blocked_reasons"]:
        raise ValueError("; ".join(current["blocked_reasons"]))
    expected = copy.deepcopy(review["snapshot"])
    expected["work"]["number_of_seasons"] = None
    expected["work"]["manually_edited_fields"] = sorted(
        (set(expected["work"]["manually_edited_fields"] or []) - {"number_of_seasons"}) | {"season_number"}
    )
    if _without_update_time(snapshot) == _without_update_time(expected):
        return {"work_id": review["work_id"], "changed": False, "after_fingerprint": current["fingerprint"]}
    if current["fingerprint"] != review["fingerprint"]:
        raise ValueError("Review is stale; export and review the current evidence")
    if snapshot["work"]["number_of_seasons"] is None:
        raise ValueError("No retired count to clear")
    # Only retire these fields. ORM before_flush also normalizes search_text;
    # repairing unrelated legacy derived fields would invalidate the reviewed snapshot.
    await db.execute(
        update(TVSeries)
        .where(TVSeries.id == review["work_id"])
        .values(
            number_of_seasons=None,
            manually_edited_fields=expected["work"]["manually_edited_fields"],
        )
    )
    await db.flush()
    after = await review_work(db, review["work_id"])
    return {
        "work_id": review["work_id"],
        "changed": True,
        "before_fingerprint": current["fingerprint"],
        "after_fingerprint": after["fingerprint"],
    }


async def export_reviews(path):
    count, cursor = 0, ""
    async with database.async_session_factory() as db:
        with open(path, "w", encoding="utf-8") as output:
            while True:
                ids = list(
                    await db.scalars(
                        select(TVSeries.id)
                        .where(
                            TVSeries.number_of_seasons.is_not(None),
                            TVSeries.id > cursor,
                        )
                        .order_by(TVSeries.id)
                        .limit(100)
                    )
                )
                if not ids:
                    break
                for identity in ids:
                    output.write(canonical(await review_work(db, identity)) + "\n")
                    count += 1
                cursor = ids[-1]
    return count


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--export", metavar="JSONL")
    mode.add_argument("--apply-review", metavar="JSON", help="one exported record plus explicit confirmed_season")
    args = parser.parse_args()

    async def run():
        try:
            if args.export:
                print(canonical({"exported": await export_reviews(args.export), "path": args.export}))
            else:
                with open(args.apply_review, encoding="utf-8") as source:
                    review = json.load(source)
                async with database.async_session_factory() as db:
                    async with db.begin():
                        result = await apply_review(db, review)
                print(canonical(result))
        finally:
            await database.engine.dispose()

    asyncio.run(run())


if __name__ == "__main__":
    main()
