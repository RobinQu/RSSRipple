"""Review unowned legacy running histories with all writers stopped before apply."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import settings
from app.database import apply_db_pragmas, normalize_database_url
from app.models.agent_run import AgentRun
from app.models.agent_run_lease import AgentRunLease
from app.utils.time import utcnow

_REPORT_FIELDS = ("version", "after_id", "limit", "next_after_id", "records")


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


def fingerprint(report):
    return hashlib.sha256(canonical({key: report[key] for key in _REPORT_FIELDS}).encode()).hexdigest()


def retirement_marker(approved_hash, finished):
    return f"Legacy run retired by approved review {approved_hash} at {finished} UTC; counters may be incomplete."


async def export_review(db, *, after_id="", limit=100):
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        raise ValueError("Review page size must be between 1 and 1000")
    if not isinstance(after_id, str):
        raise ValueError("after_id must be a string")
    rows = list((await db.execute(
        select(AgentRun.__table__).outerjoin(AgentRunLease, AgentRunLease.run_id == AgentRun.id)
        .where(AgentRun.status == "running", AgentRun.finished_at.is_(None),
               AgentRunLease.id.is_(None), AgentRun.id > after_id)
        .order_by(AgentRun.id).limit(limit + 1)
    )).mappings())
    records = [dict(row) for row in rows[:limit]]
    report = json.loads(canonical({
        "version": 1, "after_id": after_id, "limit": limit,
        "next_after_id": records[-1]["id"] if len(rows) > limit else None,
        "records": records,
    }))
    report["fingerprint"] = fingerprint(report)
    return report


async def apply_review(db, review):
    if not isinstance(review, dict) or not all(key in review for key in _REPORT_FIELDS):
        raise ValueError("Invalid review structure")
    records = review["records"]
    if review["version"] != 1 or not isinstance(records, list) or not 1 <= len(records) <= 1000:
        raise ValueError("Invalid review version or record count")
    if any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in records):
        raise ValueError("Invalid review records")
    if review.get("fingerprint") != fingerprint(review):
        raise ValueError("Invalid review fingerprint")
    if review.get("approved_fingerprint") != review["fingerprint"]:
        raise ValueError("Explicit approved_fingerprint required")
    originals = {row["id"]: row for row in records}
    selected = review.get("selected_ids")
    if len(originals) != len(records) or not isinstance(selected, list) or not selected:
        raise ValueError("Select distinct reviewed run IDs")
    if any(not isinstance(item, str) for item in selected) or len(selected) != len(set(selected)):
        raise ValueError("Invalid or duplicate selected IDs")
    if not set(selected) <= originals.keys():
        raise ValueError("Selection includes an unreviewed run")
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(text("LOCK TABLE agent_runs, agent_run_leases IN SHARE ROW EXCLUSIVE MODE"))
    elif db.get_bind().dialect.name == "sqlite":
        await db.execute(text("BEGIN IMMEDIATE"))
    else:
        raise ValueError("Unsupported review database")
    rows = (await db.execute(select(AgentRun.__table__).where(AgentRun.id.in_(selected)))).mappings()
    current = {row["id"]: json.loads(canonical(dict(row))) for row in rows}
    leased = set(await db.scalars(select(AgentRunLease.run_id).where(AgentRunLease.run_id.in_(selected))))
    retire, already = [], []
    for run_id in selected:
        old, row = originals[run_id], current.get(run_id)
        if (old.get("status") != "running" or old.get("finished_at") is not None
                or not isinstance(old.get("errors"), list)):
            raise ValueError("Reviewed record is not an eligible unowned running history")
        if row is None or run_id in leased:
            raise ValueError("Run disappeared or acquired a lease; export and review again")
        if row == old:
            retire.append(run_id)
            continue
        expected = {**old, "status": "failed", "finished_at": row["finished_at"],
                    "errors": [*old["errors"], retirement_marker(review["fingerprint"], row["finished_at"])]}
        if row["finished_at"] is not None and row == expected:
            already.append(run_id)
            continue
        raise ValueError("Reviewed run changed; export and review again")
    # Validate every selected snapshot before writing any row. The caller owns
    # commit/rollback; no Agent summary, request or consumption state is changed.
    for run_id in retire:
        finished = utcnow()
        await db.execute(update(AgentRun).where(AgentRun.id == run_id).values(
            status="failed", finished_at=finished,
            errors=[*originals[run_id]["errors"], retirement_marker(review["fingerprint"], finished)],
        ).execution_options(synchronize_session=False))
    return {"retired_ids": retire, "already_retired_ids": already}


async def run(args):
    engine = create_async_engine(normalize_database_url(settings.database_url))
    apply_db_pragmas(engine)
    try:
        async with AsyncSession(engine) as db:
            if args.export:
                report = await export_review(db, after_id=args.after_id, limit=args.limit)
                with Path(args.export).open("x", encoding="utf-8") as output:
                    output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                return {"records": len(report["records"]), "next_after_id": report["next_after_id"]}
            review = json.loads(Path(args.apply_review).read_text(encoding="utf-8"))
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
    parser.add_argument("--after-id", default="")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--writers-stopped", action="store_true")
    parser.add_argument("--backup-confirmed", action="store_true")
    args = parser.parse_args()
    if args.apply_review and not (args.writers_stopped and args.backup_confirmed):
        parser.error("Applying requires --writers-stopped and --backup-confirmed")
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False))


if __name__ == "__main__":
    main()
