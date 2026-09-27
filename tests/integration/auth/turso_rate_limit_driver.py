"""Real Turso burst reservations with production backoff, outside pytest sleep patches."""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

from sqlalchemy import delete, select, text

from app import database
from app.models.auth_rate_limit import AuthRateLimitBucket
from app.services import auth_rate_limit as limiter


async def main():
    url = os.environ["AUTH_LIMIT_DATABASE_URL"]
    assert url == os.environ["DATABASE_URL"]
    assert url.startswith("sqlite+aioturso:///")
    assert Path(urlsplit(url).path).name == "auth_limit_test.db"
    limiter.utcnow = lambda: datetime(2026, 9, 27, 12)
    records = []
    try:
        async with database.engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.create_all)
            await conn.execute(text("PRAGMA journal_mode='mvcc'"))
        for count in (12, 40) * 5:
            async with database.async_session_factory() as db, db.begin():
                await db.execute(delete(AuthRateLimitBucket))
            results = await asyncio.gather(
                *(limiter.reserve_otp_attempt("192.0.2.1") for _ in range(count)),
                return_exceptions=True,
            )
            async with database.async_session_factory() as db:
                attempts = sorted((await db.scalars(select(AuthRateLimitBucket.attempts))).all())
            errors = [type(r).__name__ for r in results if isinstance(r, BaseException)]
            records.append({"concurrent": count, "allowed": results.count(0),
                            "denied": sum(isinstance(r, int) and r > 0 for r in results),
                            "errors": errors, "attempts": attempts})
        Path(os.environ["AUTH_LIMIT_RESULT"]).write_text(json.dumps({"cases": records}, indent=2))
        assert all(not r["errors"] and r["allowed"] == 5 and r["denied"] == r["concurrent"] - 5
                   and r["attempts"] == [5, min(r["concurrent"], 30)] for r in records), records
    finally:
        await database.engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
