"""Subprocess CLI smoke test on a fresh, disposable Turso file."""

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

with tempfile.TemporaryDirectory(prefix="rssripple-v13-review-io-") as directory:
    root = Path(directory)
    url = os.environ["PROBE_DATABASE_URL"]
    assert url.startswith("postgresql+asyncpg://organize_test:organize_test@127.0.0.1:") and url.endswith(
        "/organize_test"
    )
    env = dict(
        os.environ,
        DATABASE_URL=url,
        DB_MIGRATE_ON_STARTUP="false",
        SCHEDULER_ENABLED="false",
        QUEUE_BACKEND="memory",
        AUTH_ENABLED="false",
        POSTER_CACHE_DIR=str(root / "posters"),
        APP_ROLE="web",
    )
    seed = """
import asyncio
import app.database as d
import app.models
from tests.unit.test_publication_migration import legacy
async def main():
    async with d.engine.begin() as c:
        await c.run_sync(d.Base.metadata.create_all)
    async with d.async_session_factory() as s:
        await legacy(s)
        await s.commit()
    from app.models.agent_publication_progress import AgentPublicationProgress
    from app.models.resource_publication import ResourcePublication, ChannelPublicationCounter
    async with d.engine.begin() as c:
        for model in (AgentPublicationProgress, ResourcePublication, ChannelPublicationCounter):
            await c.run_sync(model.__table__.drop)
    await d.engine.dispose()
asyncio.run(main())
"""

    def run(args, expected=0):
        result = subprocess.run([sys.executable, *args], env=env, capture_output=True, text=True, timeout=60)
        assert result.returncode == expected, (args, result.returncode, result.stdout, result.stderr)
        return result

    run(["-c", seed])
    cli = ["-m", "scripts.review_publication_migration"]
    run([*cli, "--prepare-schema"], 2)
    prepare = [*cli, "--prepare-schema", "--writers-stopped", "--backup-confirmed"]
    assert json.loads(run(prepare).stdout) == {"status": "schema_prepared"}
    assert json.loads(run(prepare).stdout) == {"status": "schema_prepared"}

    def startup(role, ready):
        child_env = dict(env, APP_ROLE=role)
        if role == "web":
            args = ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "0"]
            expected = "Application startup complete"
        else:
            code = (
                "from pathlib import Path; import app.worker as w; "
                f"w.HEARTBEAT_PATH=Path({str(root / 'heartbeat')!r}); w.main()"
            )
            args = ["-c", code]
            expected = "Worker started"
        path = root / f"{role}-{ready}.log"
        with path.open("w") as log:
            process = subprocess.Popen([sys.executable, *args], env=child_env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    content = path.read_text()
                    if ready and expected in content:
                        process.send_signal(signal.SIGINT)
                        assert process.wait(timeout=10) == 0, path.read_text()
                        return
                    if process.poll() is not None:
                        assert not ready and process.returncode != 0, path.read_text()
                        assert "Publication migration required" in content, content
                        return
                    time.sleep(0.05)
                raise AssertionError("startup deadline: " + path.read_text())
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)

    startup("web", False)
    startup("worker", False)
    review = root / "review.json"
    exported = json.loads(run([*cli, "--export", str(review)]).stdout)
    assert exported["agents"] == 1 and exported["resources"] == 3
    assert "FileExistsError" in run([*cli, "--export", str(review)], 1).stderr
    run([*cli, "--apply-review", str(review)], 2)
    data = json.loads(review.read_text())
    data["approved_fingerprint"] = data["fingerprint"]
    review.write_text(json.dumps(data))
    apply = [*cli, "--apply-review", str(review), "--writers-stopped", "--backup-confirmed"]
    first = json.loads(run(apply).stdout)
    second = json.loads(run(apply).stdout)
    assert first == {"status": "applied", "resources": 3, "agents": 1}
    assert second == {"status": "already_applied"}
    startup("web", True)
    startup("worker", True)
    print(
        json.dumps(
            {
                "startup_before_migration_rejected": ["web", "worker"],
                "startup_after_migration_and_shutdown_passed": ["web", "worker"],
                "export": exported,
                "apply": first,
                "repeat": second,
                "legacy_missing_tables_prepared_twice": True,
                "export_overwrite_rejected": True,
                "missing_flags_rejected": True,
                "database": "dedicated synthetic PostgreSQL database; Compose cleanup by caller",
            },
            indent=2,
        )
    )
