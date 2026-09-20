"""Disposable PostgreSQL proof of startup and collection-season uniqueness."""

import asyncio
import json
import os
import sys
import uuid

from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
assert url.drivername == "postgresql+asyncpg"
assert url.host in {"127.0.0.1", "localhost"}
assert (url.username, url.password, url.database) == ("organize_test",) * 3

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402

import app.database as database  # noqa: E402
import app.models  # noqa: E402,F401
from app.models.episode import Episode  # noqa: E402
from app.models.series import TVSeries  # noqa: E402
from app.models.work_collection import WorkCollection  # noqa: E402
from app.models.work_external_id import WorkExternalId  # noqa: E402


async def start_child():
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        __file__,
        "--startup",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 45)
    assert process.returncode == 0, (stdout.decode(), stderr.decode())
    return process.returncode


async def main():
    engine = database.engine
    factory = database.async_session_factory
    result = {"fixture": "synthetic works, real disposable PostgreSQL and production startup"}
    try:
        async with engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.drop_all)
        result["fresh_concurrent_process_startup"] = await asyncio.gather(start_child(), start_child())
        async with factory() as db:
            parent = WorkCollection(title_cn="Synthetic PostgreSQL collection")
            db.add(parent)
            await db.flush()
            work = TVSeries(
                title_cn="Synthetic protected S3",
                collection_id=parent.id,
                season_number=3,
                manually_edited_fields=["title_cn"],
            )
            db.add(work)
            await db.flush()
            episode = Episode(series_id=work.id, season=3, episode=1)
            bag = WorkExternalId(work_type="series", work_id=work.id, source="tmdb", external_id="tmdb:900001#s3")
            db.add_all([episode, bag])
            await db.commit()
            keys = (parent.id, work.id, episode.id, bag.id)
        async with engine.begin() as conn:
            await conn.execute(text("DROP INDEX uq_tv_series_collection_season"))
        result["populated_concurrent_process_upgrade"] = await asyncio.gather(start_child(), start_child())
        async with factory() as db:
            work = await db.get(TVSeries, keys[1])
            assert (work.collection_id, work.season_number, work.title_cn, work.manually_edited_fields) == (
                keys[0],
                3,
                "Synthetic protected S3",
                ["title_cn"],
            )
            ep = await db.get(Episode, keys[2])
            bag = await db.get(WorkExternalId, keys[3])
            assert (ep.series_id, ep.season, bag.work_id) == (keys[1], 3, keys[1])
            result["populated_identity_and_episode_preserved"] = True
            for season in (0, 1):
                db.add(TVSeries(title_cn="Synthetic valid season", collection_id=keys[0], season_number=season))
            await db.commit()
            for season in (0, 1, 3):
                try:
                    async with db.begin_nested():
                        db.add(
                            TVSeries(
                                title_cn="Synthetic rejected duplicate", collection_id=keys[0], season_number=season
                            )
                        )
                        await db.flush()
                except IntegrityError as exc:
                    assert exc.orig.sqlstate == "23505"
                else:
                    raise AssertionError(f"duplicate S{season} accepted")
            result["duplicate_seasons_rejected"] = [0, 1, 3]
        # Hold one uncommitted index entry and prove the second backend waits
        # on it before allowing the first transaction to commit.
        second_ready = asyncio.Event()
        backend = {}

        async def contender():
            async with factory() as db:
                backend["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                second_ready.set()
                db.add(TVSeries(title_cn="Synthetic losing S5", collection_id=keys[0], season_number=5))
                try:
                    await db.commit()
                except IntegrityError as exc:
                    await db.rollback()
                    return exc.orig.sqlstate
                return "unexpected success"

        async with factory() as first:
            first.add(TVSeries(title_cn="Synthetic winning S5", collection_id=keys[0], season_number=5))
            await first.flush()
            task = asyncio.create_task(contender())
            try:
                await asyncio.wait_for(second_ready.wait(), 10)

                async def observe_blocker():
                    async with engine.connect() as observer:
                        while True:
                            pids = await observer.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": backend["pid"]})
                            if pids:
                                return True
                            await asyncio.sleep(0.05)

                result["second_writer_blocked"] = await asyncio.wait_for(observe_blocker(), 10)
                await first.commit()
                result["second_writer_sqlstate"] = await asyncio.wait_for(task, 10)
                assert result["second_writer_sqlstate"] == "23505"
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
        async with factory() as db:
            rows = list(
                await db.scalars(select(TVSeries).where(TVSeries.collection_id == keys[0], TVSeries.season_number == 5))
            )
            assert len(rows) == 1 and rows[0].title_cn == "Synthetic winning S5"
        # Build an old conflicting schema explicitly; startup must neither
        # discard the conflicting work nor rewrite either manual title.
        async with engine.begin() as conn:
            await conn.execute(text("DROP INDEX uq_tv_series_collection_season"))
        async with factory() as db:
            db.add(
                TVSeries(
                    id=str(uuid.uuid4()),
                    title_cn="Synthetic protected duplicate S3",
                    collection_id=keys[0],
                    season_number=3,
                    manually_edited_fields=["title_cn"],
                )
            )
            await db.commit()
            before = {(r.id, r.title_cn, r.collection_id, r.season_number) for r in await db.scalars(select(TVSeries))}
        try:
            await database.create_tables()
        except IntegrityError as exc:
            assert exc.orig.sqlstate == "23505"
        else:
            raise AssertionError("conflicting upgrade was accepted without constraint")
        async with factory() as db:
            after = {(r.id, r.title_cn, r.collection_id, r.season_number) for r in await db.scalars(select(TVSeries))}
            assert before == after
            result["conflicting_upgrade_preserves_all_rows"] = True
        print(json.dumps(result, indent=2))
    finally:
        await engine.dispose()


async def startup():
    try:
        await database.create_tables()
    finally:
        await database.engine.dispose()


asyncio.run(startup() if "--startup" in sys.argv else main())
