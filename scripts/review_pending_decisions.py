"""Export a read-only decision review or apply an explicitly approved offline review."""

import argparse
import asyncio
import json
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.models  # noqa: F401
from app.config import settings
from app.database import apply_db_pragmas
from app.services.decision_migration import apply_decision_review
from app.services.decision_review import export_decision_review


async def run(args):
    url = make_url(settings.database_url)
    if url.drivername not in {"sqlite+aioturso", "postgresql+asyncpg"}:
        raise ValueError("Unsupported database backend")
    if url.drivername == "sqlite+aioturso":
        url = url.difference_update_query(["isolation_level"])
    engine = create_async_engine(url)
    apply_db_pragmas(engine)
    try:
        if args.export:
            async with AsyncSession(engine) as db:
                review = await export_decision_review(db)
            # Never overwrite an earlier audit file or an existing user file.
            with Path(args.export).open("x", encoding="utf-8") as output:
                json.dump(review, output, indent=2, ensure_ascii=False)
                output.write("\n")
            return {
                "fingerprint": review["fingerprint"],
                "groups": len(review["proposed_groups"]),
                "blocked": len(review["blocked"]),
            }
        reviewed = json.loads(Path(args.apply_review).read_text())
        async with engine.begin() as conn:
            if conn.dialect.name == "sqlite":
                await conn.execute(text("BEGIN"))
            async with AsyncSession(bind=conn) as db:
                return await apply_decision_review(db, reviewed)
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--export", metavar="JSON")
    mode.add_argument("--apply-review", metavar="JSON")
    parser.add_argument("--writers-stopped", action="store_true")
    parser.add_argument("--backup-confirmed", action="store_true")
    args = parser.parse_args()
    if args.apply_review and not (args.writers_stopped and args.backup_confirmed):
        parser.error("--apply-review requires --writers-stopped and --backup-confirmed")
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False))


if __name__ == "__main__":
    main()
