"""Independent-process first-use registration against isolated PostgreSQL."""
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

from sqlalchemy import delete
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.models  # noqa: F401
from app.database import Base
from app.models.organize_configuration import OrganizeConfiguration
from app.services.organize_ownership import OwnershipDomainError, registered_domain


async def main():
    url = make_url(os.environ["DATABASE_URL"])
    assert url.host == "127.0.0.1" and url.database == url.username == url.password == "organize_test"
    engine = create_async_engine(url)
    try:
        if len(sys.argv) > 1:
            directory, barrier, name = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
            (barrier / name).touch()
            until = time.monotonic() + 30
            while not (barrier / "go").exists():
                if time.monotonic() > until:
                    raise TimeoutError("registration barrier")
                await asyncio.sleep(0.02)
            async with AsyncSession(engine) as db:
                try:
                    await registered_domain(db, directory)
                except OwnershipDomainError:
                    raise SystemExit(12) from None
            return
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        root = Path(f"domain-evidence-{uuid.uuid4().hex[:8]}").resolve()
        root.mkdir()
        report = {}
        for shared in (True, False):
            async with engine.begin() as conn:
                await conn.execute(delete(OrganizeConfiguration))
            barrier = root / ("shared" if shared else "different")
            barrier.mkdir()
            processes = []
            try:
                for name in ("first", "second"):
                    directory = barrier / ("locks" if shared else name + "-locks")
                    processes.append(await asyncio.create_subprocess_exec(
                        sys.executable, __file__, str(directory), str(barrier), name,
                    ))
                until = time.monotonic() + 30
                while not all((barrier / name).exists() for name in ("first", "second")):
                    if time.monotonic() > until:
                        raise TimeoutError("actors did not reach barrier")
                    await asyncio.sleep(0.02)
                (barrier / "go").touch()
                codes = await asyncio.wait_for(asyncio.gather(*(p.wait() for p in processes)), 30)
                assert sorted(codes) == ([0, 0] if shared else [0, 12]), codes
                report["shared_directory" if shared else "different_directories"] = codes
            finally:
                for process in processes:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
        print(json.dumps(report, indent=2))
    finally:
        await engine.dispose()


asyncio.run(main())
