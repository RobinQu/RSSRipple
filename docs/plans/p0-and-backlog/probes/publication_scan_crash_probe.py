"""Kill a real scanner after its first commit, recover in a fresh process."""

import asyncio
import json
import os
import select
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch


async def child(mode):
    from datetime import date

    from sqlalchemy import select as sql_select

    import app.database as database
    import app.models  # noqa: F401
    from app.job_handlers import _handle_run_agent
    from app.models.download_task import DownloadTask
    from app.models.movie import Movie
    from app.services.agent_publication_progress import snapshot_publications
    from app.services.resource_publication import publish_resource
    from tests.unit.test_agent_publication_progress import setup
    from tests.unit.test_agent_service import _make_resource

    state_path = Path(os.environ["PROBE_STATE"])
    if mode == "seed":
        async with database.engine.begin() as conn:
            await conn.run_sync(database.Base.metadata.create_all)
        async with database.async_session_factory() as db:
            agent, excluded = await setup(db)
            movie = Movie(title_cn="Synthetic crash movie", release_date=date(2020, 1, 1), is_anime=False)
            db.add(movie)
            await db.flush()
            resource = _make_resource(agent.channel_id, movie_id=movie.id, season=None, episode=None, parsed_at=None)
            db.add(resource)
            agent.scope_channel_wide = True
            await db.flush()
            await publish_resource(db, resource.id, kind="created")
            from app.api.v1.agents import _apply_backfill

            await _apply_backfill(agent, [], db)
            await db.commit()
            state_path.write_text(
                json.dumps({"agent_id": agent.id, "channel_id": agent.channel_id, "resource_id": resource.id})
            )
    else:
        state = json.loads(state_path.read_text())
        if mode == "crash":

            async def stop_before_processing(*args, **kwargs):
                print("SCAN_INTENT_COMMITTED", flush=True)
                await asyncio.Event().wait()

            with patch("app.services.agent_service.process_resources", stop_before_processing):
                await _handle_run_agent({"agent_id": state["agent_id"], "scan_since": None})
        else:
            async with database.async_session_factory() as db:
                snapshot = await snapshot_publications(db, state["agent_id"], state["channel_id"])
                assert state["resource_id"] in snapshot.resource_ids
            result = await _handle_run_agent({"agent_id": state["agent_id"]})
            assert result["dispatched"] == 1 and not result["errors"], result
            again = await _handle_run_agent({"agent_id": state["agent_id"]})
            assert again["total_resources"] == 0, again
            async with database.async_session_factory() as db:
                tasks = list(
                    await db.scalars(sql_select(DownloadTask).where(DownloadTask.agent_id == state["agent_id"]))
                )
                assert len(tasks) == 1 and tasks[0].file_resource_id == state["resource_id"]
            print(json.dumps({"recovered": result, "next_run": again, "task_count": len(tasks)}))
    await database.engine.dispose()


def parent():
    with tempfile.TemporaryDirectory(prefix="rssripple-v13-crash-jb-") as directory:
        root = Path(directory)
        env = dict(
            os.environ,
            DATABASE_URL=f"sqlite+aioturso:///{root / 'test.db'}?isolation_level=DEFERRED",
            PROBE_STATE=str(root / "state.json"),
        )

        def run(mode):
            result = subprocess.run(
                [sys.executable, __file__, mode], env=env, capture_output=True, text=True, timeout=30
            )
            assert result.returncode == 0, (mode, result.returncode, result.stderr)
            return result

        run("seed")
        with (root / "crash.log").open("w") as error:
            process = subprocess.Popen(
                [sys.executable, __file__, "crash"], env=env, stdout=subprocess.PIPE, stderr=error, text=True
            )
            try:
                ready, _, _ = select.select([process.stdout], [], [], 20)
                assert ready, "scanner never reached committed barrier"
                assert process.stdout.readline().strip() == "SCAN_INTENT_COMMITTED"
                assert process.poll() is None
                process.kill()
                assert process.wait(timeout=10) == -9
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=10)
        recovered = json.loads(run("recover").stdout)
        print(
            json.dumps(
                {
                    "scanner_signal": 9,
                    "fresh_process_recovery": recovered,
                    "data": "synthetic; actual durable Turso file, handler, task and mock downloader",
                    "cleanup": "temporary directory removed",
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    if len(sys.argv) > 1:
        asyncio.run(child(sys.argv[1]))
    else:
        parent()
