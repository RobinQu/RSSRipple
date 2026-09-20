"""Review orphan identities; explicitly apply selected deletions with writers stopped."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.models  # noqa: F401
from app.config import settings
from app.database import apply_db_pragmas
from app.models.movie import Movie
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.models.work_external_id import WorkExternalId

OWNERS = {"series": TVSeries, "movie": Movie, "collection": WorkCollection}


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def fingerprint(report):
    return hashlib.sha256(canonical({k: report[k] for k in ("version", "orphans", "blocked")}).encode()).hexdigest()


async def export_review(db):
    owners = {kind: set(await db.scalars(select(model.id))) for kind, model in OWNERS.items()}
    rows = [
        dict(row) for row in (await db.execute(select(WorkExternalId.__table__).order_by(WorkExternalId.id))).mappings()
    ]
    report = {"version": 1, "orphans": [], "blocked": []}
    for row in rows:
        if row["work_type"] not in owners:
            report["blocked"].append(row)
        elif row["work_id"] not in owners[row["work_type"]]:
            report["orphans"].append(row)
    report = json.loads(canonical(report))
    report["fingerprint"] = fingerprint(report)
    return report


async def apply_review(db, review):
    if review.get("version") != 1 or review.get("fingerprint") != fingerprint(review):
        raise ValueError("Invalid review fingerprint")
    if review.get("approved_fingerprint") != review["fingerprint"]:
        raise ValueError("Explicit approved_fingerprint required")
    selected = review.get("selected_ids")
    if not isinstance(selected, list) or not selected or any(not isinstance(x, str) for x in selected):
        raise ValueError("Select a nonempty list of orphan row IDs")
    if len(selected) != len(set(selected)):
        raise ValueError("Duplicate selected IDs")
    originals = {row["id"]: row for row in review["orphans"]}
    if not set(selected) <= originals.keys() or review["blocked"]:
        raise ValueError("Unknown selection or blocked identity types")
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(
            text("LOCK TABLE movies, tv_series, work_collections, work_external_ids IN SHARE ROW EXCLUSIVE MODE")
        )
    current = await export_review(db)
    if current["blocked"]:
        raise ValueError("Unknown identity types require separate review")
    present = set(await db.scalars(select(WorkExternalId.id).where(WorkExternalId.id.in_(selected))))
    if not present:
        return {"deleted_ids": [], "already_absent_ids": selected}
    # A selected row gaining an owner disappears from the orphan report, so it
    # fails this comparison as well as explicit row edits and other drift.
    if current["fingerprint"] != review["fingerprint"]:
        raise ValueError("Database changed; export and review again")
    await db.execute(delete(WorkExternalId).where(WorkExternalId.id.in_(selected)))
    await db.flush()
    return {"deleted_ids": selected, "already_absent_ids": []}


async def run(args):
    engine = create_async_engine(settings.database_url)
    apply_db_pragmas(engine)
    try:
        async with AsyncSession(engine) as db:
            if args.export:
                report = await export_review(db)
                with Path(args.export).open("x", encoding="utf-8") as output:
                    output.write(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
                return {"orphans": len(report["orphans"]), "blocked": len(report["blocked"])}
            review = json.loads(Path(args.apply_review).read_text())
            if db.get_bind().dialect.name == "sqlite":
                await db.execute(text("BEGIN IMMEDIATE"))
            result = await apply_review(db, review)
            await db.commit()
            return result
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--export")
    mode.add_argument("--apply-review")
    parser.add_argument("--writers-stopped", action="store_true")
    parser.add_argument("--backup-confirmed", action="store_true")
    args = parser.parse_args()
    if args.apply_review and not (args.writers_stopped and args.backup_confirmed):
        parser.error("Applying requires --writers-stopped and --backup-confirmed")
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False))


if __name__ == "__main__":
    main()
