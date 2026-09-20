# ruff: noqa: E402
import asyncio
import os

from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
assert url.drivername == "postgresql+asyncpg" and url.host == "127.0.0.1"
assert (url.username, url.password, url.database) == ("organize_test",) * 3
import json
import uuid
from pathlib import Path

from scripts.review_orphan_identities import apply_review, export_review
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

import app.database as database
import app.models  # noqa: F401
from app.models.movie import Movie
from app.models.work_external_id import WorkExternalId


async def main():
    async with database.engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    owner_id = str(uuid.uuid4())
    async with database.async_session_factory() as db:
        row = WorkExternalId(work_type="movie", work_id=owner_id, source="tmdb", external_id="tmdb:synthetic-orphan")
        db.add(row)
        await db.commit()
        oid = row.id
        review = await export_review(db)
        review.update(approved_fingerprint=review["fingerprint"], selected_ids=[oid])
    async with database.async_session_factory() as cleanup:
        result = await apply_review(cleanup, review)
        assert result["deleted_ids"] == [oid]
        async with database.async_session_factory() as writer:
            await writer.execute(text("SET LOCAL lock_timeout = '300ms'"))
            writer.add(Movie(id=owner_id, title_cn="Synthetic restored owner"))
            try:
                await writer.flush()
            except DBAPIError as exc:
                assert getattr(exc.orig, "sqlstate", None) == "55P03"
                await writer.rollback()
            else:
                raise AssertionError("owner recreation bypassed cleanup table lock")
        await cleanup.rollback()
    async with database.async_session_factory() as db:
        assert await db.scalar(select(WorkExternalId.id).where(WorkExternalId.id == oid)) == oid
        db.add(Movie(id=owner_id, title_cn="Synthetic restored owner"))
        await db.commit()
    async with database.async_session_factory() as cleanup:
        try:
            await apply_review(cleanup, review)
        except ValueError as exc:
            assert "changed" in str(exc)
            await cleanup.rollback()
        else:
            raise AssertionError("review deleted identity of a restored owner")
    async with database.async_session_factory() as db:
        assert await db.get(Movie, owner_id) is not None
        assert await db.get(WorkExternalId, oid) is not None
    result = {
        "owner_recreation_blocked_during_apply": True,
        "rollback_restores_orphan": True,
        "restored_owner_invalidates_review": True,
        "valid_identity_retained": True,
    }
    Path("/tmp/rssripple-v12-orphan-pg-hu.json").write_text(json.dumps(result, indent=2) + "\n")
    print(result)
    await database.engine.dispose()


asyncio.run(main())
