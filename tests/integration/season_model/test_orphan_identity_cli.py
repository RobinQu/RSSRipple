"""Real CLI subprocesses against an isolated on-disk Turso database."""

import json
import os
import subprocess
import sys
import uuid


async def test_orphan_review_cli_export_apply_repeat_and_no_overwrite(tmp_path):
    url = "sqlite+aioturso:///" + str(tmp_path / "review.db")
    valid_id, orphan_id, owner_id = (str(uuid.uuid4()) for _ in range(3))
    # Turso's native file handle can outlive engine.dispose within a process.
    # Seed in a separate process so process exit proves file ownership ended.
    seed_code = """
import asyncio, json, os
from sqlalchemy.ext.asyncio import create_async_engine
import app.models
from app.database import Base, apply_db_pragmas
from app.models.movie import Movie
from app.models.work_external_id import WorkExternalId
async def seed():
    engine = create_async_engine(os.environ['DATABASE_URL'])
    apply_db_pragmas(engine)
    valid, orphan, owner, missing = json.loads(os.environ['SEED_IDS'])
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(Movie.__table__.insert().values(id=owner, title_cn='Synthetic valid owner'))
        await conn.execute(WorkExternalId.__table__.insert(), [
            dict(id=valid, work_type='movie', work_id=owner, source='tmdb', external_id='tmdb:valid'),
            dict(id=orphan, work_type='movie', work_id=missing, source='tmdb', external_id='tmdb:orphan')])
    await engine.dispose()
asyncio.run(seed())
"""
    seeded = subprocess.run(
        [sys.executable, "-c", seed_code],
        env=dict(os.environ, DATABASE_URL=url, SEED_IDS=json.dumps([valid_id, orphan_id, owner_id, str(uuid.uuid4())])),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert seeded.returncode == 0, seeded.stderr
    env = dict(os.environ, DATABASE_URL=url)

    def cli(*args):
        return subprocess.run(
            [sys.executable, "-m", "scripts.review_orphan_identities", *args],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    export = tmp_path / "original.json"
    result = cli("--export", str(export))
    assert result.returncode == 0, result.stderr
    original_bytes = export.read_bytes()
    review = json.loads(original_bytes)
    assert [row["id"] for row in review["orphans"]] == [orphan_id]
    assert cli("--export", str(export)).returncode != 0
    assert export.read_bytes() == original_bytes
    review.update(approved_fingerprint=review["fingerprint"], selected_ids=[orphan_id])
    approved = tmp_path / "approved.json"
    approved.write_text(json.dumps(review))
    assert cli("--apply-review", str(approved)).returncode != 0
    result = cli("--apply-review", str(approved), "--writers-stopped", "--backup-confirmed")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["deleted_ids"] == [orphan_id]
    result = cli("--apply-review", str(approved), "--writers-stopped", "--backup-confirmed")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["already_absent_ids"] == [orphan_id]
    after = tmp_path / "after.json"
    assert cli("--export", str(after)).returncode == 0
    assert json.loads(after.read_text())["orphans"] == []
    assert export.read_bytes() == original_bytes
