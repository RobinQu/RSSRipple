"""Real PG disconnect, SIGKILL, file publication, and coroutine cancellation."""
import asyncio
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

from ownership_guard import OwnershipBusyError, finish_before_cancel, plan_lock
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.services.organize_executor import ExecOp, run_execution


def publish(directory):
    result = run_execution([ExecOp(op_type="move", src=str(directory / "source.mkv"),
                                   dst=str(directory / "target.mkv"), size=1048577)], file_op="hardlink")
    assert result.ok, result.error


async def actor(engine, directory, plan_id):
    with plan_lock(directory, plan_id):
        async with engine.connect() as conn:
            pid = await conn.scalar(text("SELECT pg_backend_pid()"))
            await conn.execute(text("SELECT pg_advisory_lock(919191)"))
            (directory / "backend").write_text(str(pid))
            # The process intentionally remains alive after DB termination.
            def blocked_thread():
                (directory / "entered").touch()
                until = time.monotonic() + 45
                while not (directory / "release").exists():
                    if time.monotonic() > until:
                        raise TimeoutError("parent did not release")
                    time.sleep(0.05)
                publish(directory)
            await asyncio.to_thread(blocked_thread)


async def main():
    url = make_url(os.environ["DATABASE_URL"])
    assert url.host == "127.0.0.1" and url.database == url.username == url.password == "organize_test"
    engine = create_async_engine(url)
    try:
        if len(sys.argv) > 1:
            await actor(engine, Path(sys.argv[1]), sys.argv[2])
            return
        directory = Path(f"lock-evidence-{uuid.uuid4().hex[:8]}").resolve()
        directory.mkdir()
        (directory / "source.mkv").write_bytes(b"x" * 1048577)
        plan_id = str(uuid.uuid4())
        worker = await asyncio.create_subprocess_exec(sys.executable, __file__, str(directory), plan_id)
        try:
            deadline = time.monotonic() + 30
            while not (directory / "entered").exists():
                assert worker.returncode is None
                if time.monotonic() > deadline:
                    raise TimeoutError("worker did not enter")
                await asyncio.sleep(0.05)
            async with engine.connect() as conn:
                assert await conn.scalar(
                    text("SELECT pg_terminate_backend(:pid)"),
                    {"pid": int((directory / "backend").read_text())},
                )
                # This is the unsafe takeover signal if advisory lock alone is used.
                assert await conn.scalar(text("SELECT pg_try_advisory_lock(919191)"))
                await conn.execute(text("SELECT pg_advisory_unlock(919191)"))
            try:
                with plan_lock(directory, plan_id):
                    raise AssertionError("live disconnected owner was stolen")
            except OwnershipBusyError:
                pass
            assert worker.returncode is None and not (directory / "target.mkv").exists()
            worker.kill()
            assert await worker.wait() == -9
            with plan_lock(directory, plan_id):
                publish(directory)
            assert os.path.samefile(directory / "source.mkv", directory / "target.mkv")
        finally:
            if worker.returncode is None:
                worker.kill()
                await worker.wait()

        entered = threading.Event()
        release = threading.Event()
        async def guarded_operation():
            with plan_lock(directory, plan_id):
                def thread():
                    entered.set()
                    if not release.wait(15):
                        raise TimeoutError("thread not released")
                    publish(directory)
                await asyncio.to_thread(thread)
                (directory / "finalized").touch()
        caller = asyncio.create_task(finish_before_cancel(guarded_operation()))
        try:
            assert await asyncio.to_thread(entered.wait, 15)
            caller.cancel()
            await asyncio.sleep(0)
            caller.cancel()
            await asyncio.sleep(0)
            assert not caller.done()
            try:
                with plan_lock(directory, plan_id):
                    raise AssertionError("cancelled caller released a live thread's lock")
            except OwnershipBusyError:
                pass
        finally:
            release.set()
        try:
            await caller
            raise AssertionError("caller cancellation was lost")
        except asyncio.CancelledError:
            pass
        assert (directory / "finalized").exists()
        with plan_lock(directory, plan_id):
            pass
        report = {"db_disconnect_releases_advisory_but_not_file_lock": True,
                  "live_owner_takeover_rejected": True, "sigkill_exit": worker.returncode,
                  "recovery_published_real_hardlink": True,
                  "repeated_cancellation_waits_for_thread_and_finalization": True,
                  "media": "synthetic 1048577 bytes", "scope": "local shared filesystem; not remote lock certification"}
        (directory / "result.json").write_text(json.dumps(report, indent=2))
        print(json.dumps(report, indent=2))
    finally:
        await engine.dispose()


asyncio.run(main())
