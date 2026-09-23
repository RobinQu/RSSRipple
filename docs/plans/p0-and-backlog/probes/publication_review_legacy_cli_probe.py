"""Subprocess CLI smoke test on a fresh, disposable Turso file."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

with tempfile.TemporaryDirectory(prefix="rssripple-v13-review-io-") as directory:
    root = Path(directory)
    env = dict(os.environ, DATABASE_URL=f"sqlite+aioturso:///{root / 'test.db'}?isolation_level=DEFERRED")
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
    print(
        json.dumps(
            {
                "export": exported,
                "apply": first,
                "repeat": second,
                "legacy_missing_tables_prepared_twice": True,
                "export_overwrite_rejected": True,
                "missing_flags_rejected": True,
                "database": "temporary synthetic Turso file; removed after process exits",
            },
            indent=2,
        )
    )
