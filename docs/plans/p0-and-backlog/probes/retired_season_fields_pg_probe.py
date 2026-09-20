"""Reviewed count cleanup: real PG CLI, rollback, and concurrent ownership."""

import asyncio
import json
import os
import sys
from pathlib import Path

from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url

url = make_url(os.environ["DATABASE_URL"])
assert url.drivername == "postgresql+asyncpg" and url.host in {"127.0.0.1", "localhost"}
assert (url.username, url.password, url.database) == ("organize_test",) * 3

from tests.unit.test_retired_season_cleanup import seed  # noqa: E402

import app.database as database  # noqa: E402
import app.models  # noqa: E402,F401
from app.models.channel import Channel  # noqa: E402
from app.models.series import TVSeries  # noqa: E402
from scripts import retired_season_fields as cleanup  # noqa: E402


async def main():
    original = cleanup.review_work
    report = Path(os.environ["REPORT_PATH"])
    reviewed = report.with_suffix(".reviewed.json")
    processes = []
    try:
        async with database.engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.drop_all)
            await conn.run_sync(database.Base.metadata.create_all)
        async with database.async_session_factory() as db:
            channel = Channel(name="Synthetic cleanup channel", url="https://example.invalid/rss", field_mapping={})
            db.add(channel)
            await db.flush()
            work, *_ = await seed(db, channel)
            identity = work.id

        async def cli(*args):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "scripts.retired_season_fields",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            processes.append(process)
            stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
            return process.returncode, stdout.decode(), stderr.decode()

        first = await cli("--export", str(report))
        assert first[0] == 0, first
        review = json.loads(report.read_text())
        assert "confirmed_season" not in review and review["blocked_reasons"] == []
        review["confirmed_season"] = 1
        reviewed.write_text(json.dumps(review))
        async with database.engine.begin() as conn:
            await conn.execute(update(TVSeries).where(TVSeries.id == identity).values(title_cn="New manual title"))
        stale = await cli("--apply-review", str(reviewed))
        assert stale[0] == 1 and "Review is stale" in stale[2], stale
        assert (await cli("--export", str(report)))[0] == 0
        review = json.loads(report.read_text())
        review["confirmed_season"] = 1
        reviewed.write_text(json.dumps(review))
        try:
            async with database.async_session_factory() as db:
                async with db.begin():
                    await cleanup.apply_review(db, review)
                    raise RuntimeError("injected after flush")
        except RuntimeError as exc:
            assert str(exc) == "injected after flush"
        async with database.async_session_factory() as db:
            assert (await original(db, identity))["fingerprint"] == review["fingerprint"]

        ready, release, second_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
        backend = {}

        async def pause_after_locked_snapshot(db, work_id, *, lock=False):
            result = await original(db, work_id, lock=lock)
            if lock and asyncio.current_task().get_name() == "first-cleanup":
                ready.set()
                await asyncio.wait_for(release.wait(), 10)
            return result

        async def apply_one():
            async with database.async_session_factory() as db:
                if asyncio.current_task().get_name() == "second-cleanup":
                    backend["pid"] = await db.scalar(text("SELECT pg_backend_pid()"))
                    second_ready.set()
                result = await cleanup.apply_review(db, review)
                await db.commit()
                return result

        cleanup.review_work = pause_after_locked_snapshot
        first_task = asyncio.create_task(apply_one(), name="first-cleanup")
        await asyncio.wait_for(ready.wait(), 10)
        second_task = asyncio.create_task(apply_one(), name="second-cleanup")
        await asyncio.wait_for(second_ready.wait(), 10)

        async def observe_blocker():
            async with database.engine.connect() as conn:
                while True:
                    if await conn.scalar(text("SELECT pg_blocking_pids(:pid)"), {"pid": backend["pid"]}):
                        return True
                    await asyncio.sleep(0.02)

        assert await asyncio.wait_for(observe_blocker(), 10)
        release.set()
        results = await asyncio.wait_for(asyncio.gather(first_task, second_task), 20)
        assert [r["changed"] for r in results] == [True, False], results
        cleanup.review_work = original
        async with database.async_session_factory() as db:
            after = (await original(db, identity))["snapshot"]
            for key in review["snapshot"].keys() - {"work"}:
                assert after[key] == review["snapshot"][key]
            for key in review["snapshot"]["work"].keys() - {
                "number_of_seasons",
                "manually_edited_fields",
                "updated_at",
            }:
                assert after["work"][key] == review["snapshot"]["work"][key]
            assert after["work"]["number_of_seasons"] is None
            assert after["work"]["manually_edited_fields"] == ["season_number", "title_cn"]
            assert await db.scalar(select(TVSeries.title_cn).where(TVSeries.id == identity)) == "New manual title"
        retry = await cli("--apply-review", str(reviewed))
        assert retry[0] == 0 and json.loads(retry[1])["changed"] is False, retry
        result = {
            "cli_export_exit": first[0],
            "stale_cli_exit": stale[0],
            "post_flush_rollback_preserved": True,
            "second_backend_blocked_observed": True,
            "concurrent_changed": [r["changed"] for r in results],
            "cli_idempotent_exit": retry[0],
            "related_rows_and_manual_title_preserved": True,
        }
        Path(os.environ["RESULT_PATH"]).write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result, indent=2))
    finally:
        cleanup.review_work = original
        for process in processes:
            if process.returncode is None:
                process.kill()
                await process.wait()
        await database.engine.dispose()


asyncio.run(main())
