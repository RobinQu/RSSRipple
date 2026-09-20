"""Reviewed mixed legacy slots through actual ordinary Turso transactions."""

import json
import os
import uuid
from datetime import date

import pytest
from sqlalchemy import inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.database import Base
from app.models.agent import Agent
from app.models.channel import Channel
from app.models.decision_migration import DecisionMigration
from app.models.downloader import DownloaderInstance
from app.models.movie import Movie
from app.models.pending_decision import PendingDecision
from app.services.decision_migration import apply_decision_review
from app.services.decision_review import export_decision_review
from tests.unit.test_agent_service import _make_resource


@pytest.fixture
async def prepared(tmp_path):
    pg_url = os.environ.get("DECISION_MIGRATION_TEST_DATABASE_URL")
    schema = None
    if pg_url:
        url = make_url(pg_url)
        assert url.drivername == "postgresql+asyncpg" and url.host == "127.0.0.1"
        assert (url.username, url.password, url.database) == ("organize_test",) * 3
        schema = "decision_review_" + uuid.uuid4().hex
        engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
        async with engine.begin() as conn:
            await conn.execute(text(f"CREATE SCHEMA {schema}"))
    else:
        engine = create_async_engine(f"sqlite+aioturso:///{tmp_path / 'review.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("DROP TABLE pending_decisions"))
        status_type = "decision_status" if engine.dialect.name == "postgresql" else "TEXT"
        await conn.execute(
            text(
                "CREATE TABLE pending_decisions ("
                "id TEXT PRIMARY KEY,agent_id TEXT NOT NULL,series_id TEXT,movie_id TEXT,episode INTEGER,season INTEGER,"
                "candidates JSON NOT NULL,reason TEXT NOT NULL,llm_suggestion TEXT,llm_picked_resource_id TEXT,"
                f"decided_resource_id TEXT,status {status_type} NOT NULL,expires_at TIMESTAMP,decided_at TIMESTAMP,"
                "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
            )
        )
    async with engine.begin() as conn:
        async with AsyncSession(bind=conn, expire_on_commit=False) as db:
            channel = Channel(name="Synthetic", url="https://example.invalid/feed", field_mapping={})
            downloader = DownloaderInstance(
                name="Synthetic", type="mock", url="http://example.invalid", download_dir="/tmp"
            )
            movies = [Movie(title_cn=f"Synthetic {i}", release_date=date(2020, 1, 1), is_anime=False) for i in range(2)]
            db.add_all([channel, downloader, *movies])
            await db.flush()
            agent = Agent(name="Synthetic", channel_id=channel.id, downloader_id=downloader.id, scope_channel_wide=True)
            resources = [
                _make_resource(channel.id, movie_id=movie.id, season=None, episode=None, parsed_at=None)
                for movie in movies
                for _ in range(2)
            ]
            db.add_all([agent, *resources])
            await db.flush()
            for identity, ids, status in [
                ("old-a", [resources[0].id, resources[2].id], "pending"),
                ("old-b", [resources[1].id, resources[3].id], "pending"),
                ("history", [resources[0].id], "decided"),
            ]:
                await db.execute(
                    text(
                        "INSERT INTO pending_decisions(id,agent_id,candidates,reason,status) "
                        "VALUES (:id,:agent,:candidates,:reason,:status)"
                    ),
                    dict(
                        id=identity,
                        agent=agent.id,
                        candidates=json.dumps(ids),
                        reason="Original evidence",
                        status=status,
                    ),
                )
    async with AsyncSession(engine) as db:
        review = await export_decision_review(db)
    review.update(approved_fingerprint=review["fingerprint"], supersede_pending=True)
    try:
        yield engine, review
    finally:
        if schema:
            async with engine.begin() as conn:
                await conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await engine.dispose()


async def test_review_splits_merges_archives_and_is_idempotent(prepared):
    engine, review = prepared
    async with engine.begin() as conn:
        await conn.execute(text("BEGIN"))
        async with AsyncSession(bind=conn) as db:
            result = await apply_decision_review(db, review)
            assert len(result["created"]) == 2
            assert set(result["superseded_ids"]) == {"old-a", "old-b"}
            again = await apply_decision_review(db, review)
            assert again["already_applied"] and again["created"] == result["created"]
    async with AsyncSession(engine) as db:
        rows = list(await db.scalars(select(PendingDecision)))
        assert sum(r.status == "pending" for r in rows) == 2
        assert sum(r.status == "expired" for r in rows) == 2
        assert next(r for r in rows if r.id == "history").status == "decided"
        assert all(len(set(r.candidates)) == 2 for r in rows if r.status == "pending")
        archive = (await db.scalars(select(DecisionMigration))).one()
        assert archive.original_review["original_decisions"] == review["original_decisions"]


async def test_failed_apply_rolls_back_originals_new_columns_and_archive(prepared):
    engine, review = prepared
    with pytest.raises(RuntimeError, match="after apply"):
        async with engine.begin() as conn:
            await conn.execute(text("BEGIN"))
            async with AsyncSession(bind=conn) as db:
                await apply_decision_review(db, review)
                raise RuntimeError("after apply")
    async with AsyncSession(engine) as db:
        restored = await export_decision_review(db)
        assert restored["fingerprint"] == review["fingerprint"]
        assert not list(await db.scalars(select(DecisionMigration)))
        conn = await db.connection()
        columns = await conn.run_sync(lambda sync: [c["name"] for c in inspect(sync).get_columns("pending_decisions")])
        assert "decision_key" not in columns


async def test_stale_review_is_rejected_without_data_changes(prepared):
    engine, review = prepared
    async with engine.begin() as conn:
        await conn.execute(text("UPDATE pending_decisions SET reason='Concurrent edit' WHERE id='old-a'"))
    async with engine.begin() as conn:
        await conn.execute(text("BEGIN"))
        async with AsyncSession(bind=conn) as db:
            with pytest.raises(ValueError, match="stale"):
                await apply_decision_review(db, review)
            assert not list(await db.scalars(select(DecisionMigration)))
            assert (
                await db.execute(text("SELECT status FROM pending_decisions WHERE id='old-a'"))
            ).scalar_one() == "pending"


@pytest.mark.parametrize("missing", ["approved_fingerprint", "supersede_pending"])
async def test_explicit_review_approval_is_required(prepared, missing):
    engine, review = prepared
    del review[missing]
    async with engine.begin() as conn:
        await conn.execute(text("BEGIN"))
        async with AsyncSession(bind=conn) as db:
            with pytest.raises(ValueError, match="Explicit"):
                await apply_decision_review(db, review)
            assert (await export_decision_review(db))["fingerprint"] == review["fingerprint"]


@pytest.mark.parametrize("problem", ["missing", "unknown", "singleton"])
async def test_unresolved_groups_reject_whole_review_without_writes(prepared, problem):
    from app.models.file_resource import FileResource

    engine, _ = prepared
    async with engine.begin() as conn:
        async with AsyncSession(bind=conn) as db:
            ids = (
                json.loads(
                    (await db.execute(text("SELECT candidates FROM pending_decisions WHERE id='old-a'"))).scalar_one()
                )
                if conn.dialect.name == "sqlite"
                else (await db.execute(text("SELECT candidates FROM pending_decisions WHERE id='old-a'"))).scalar_one()
            )
            if problem == "unknown":
                resource = await db.get(FileResource, ids[0])
                resource.movie_id = None
                await db.flush()
            elif problem == "missing":
                ids[0] = "missing-synthetic-id"
                await db.execute(
                    text("UPDATE pending_decisions SET candidates=:ids WHERE id='old-a'"), {"ids": json.dumps(ids)}
                )
            else:
                # Remove one version from each work, leaving two known singleton groups.
                await db.execute(text("UPDATE pending_decisions SET status='expired' WHERE id='old-b'"))
    async with AsyncSession(engine) as db:
        review = await export_decision_review(db)
    review.update(approved_fingerprint=review["fingerprint"], supersede_pending=True)
    async with engine.begin() as conn:
        await conn.execute(text("BEGIN"))
        async with AsyncSession(bind=conn) as db:
            with pytest.raises(ValueError, match="Unresolved"):
                await apply_decision_review(db, review)
            assert (await export_decision_review(db))["fingerprint"] == review["fingerprint"]
            assert not list(await db.scalars(select(DecisionMigration)))


async def test_offline_cli_export_apply_and_rerun(tmp_path):
    import subprocess
    import sys

    if os.environ.get("DECISION_MIGRATION_TEST_DATABASE_URL"):
        pytest.skip("CLI process ownership test uses embedded Turso")
    seed = """import asyncio,sys
from pathlib import Path
from tests.unit.test_decision_migration import prepared
async def main():
    fixture=prepared.__wrapped__(Path(sys.argv[1]))
    await anext(fixture)
    await fixture.aclose()
asyncio.run(main())
"""
    # Seed in a process that exits before the CLI opens the embedded file.
    # Engine.dispose alone need not release every native Turso file handle.
    seeded = subprocess.run([sys.executable, "-c", seed, str(tmp_path)], capture_output=True, text=True, timeout=30)
    assert seeded.returncode == 0, seeded.stderr
    env = {**os.environ, "DATABASE_URL": f"sqlite+aioturso:///{tmp_path / 'review.db'}"}
    export = tmp_path / "review.json"
    command = [sys.executable, "-m", "scripts.review_pending_decisions"]
    result = subprocess.run([*command, "--export", str(export)], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    reviewed = json.loads(export.read_text())
    startup = [
        sys.executable,
        "-c",
        "import asyncio; import app.models; from app.database import create_tables; asyncio.run(create_tables())",
    ]
    blocked = subprocess.run(startup, env=env, capture_output=True, text=True, timeout=30)
    assert blocked.returncode == 1 and "reviewed identity migration" in blocked.stderr
    after_block = tmp_path / "after-block.json"
    unchanged = subprocess.run(
        [*command, "--export", str(after_block)], env=env, capture_output=True, text=True, timeout=30
    )
    assert unchanged.returncode == 0, unchanged.stderr
    assert json.loads(after_block.read_text())["fingerprint"] == reviewed["fingerprint"]
    assert "approved_fingerprint" not in reviewed
    reviewed.update(approved_fingerprint=reviewed["fingerprint"], supersede_pending=True)
    approved = tmp_path / "approved.json"
    approved.write_text(json.dumps(reviewed))
    denied = subprocess.run(
        [*command, "--apply-review", str(approved)], env=env, capture_output=True, text=True, timeout=30
    )
    assert denied.returncode == 2 and "--writers-stopped" in denied.stderr
    for repeated in [False, True]:
        applied = subprocess.run(
            [*command, "--apply-review", str(approved), "--writers-stopped", "--backup-confirmed"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert applied.returncode == 0, applied.stderr
        assert json.loads(applied.stdout)["already_applied"] == repeated
    started = subprocess.run(startup, env=env, capture_output=True, text=True, timeout=30)
    assert started.returncode == 0, started.stderr


@pytest.mark.parametrize("scope", ["season", "multi_season"])
async def test_batch_review_preserves_ranges_and_reuses_live_creation_key(prepared, scope):
    from app.models.file_resource import FileResource
    from app.models.resource_file_assignment import ResourceFileAssignment
    from app.models.resource_work_link import ResourceWorkLink
    from app.services.agent_service import _batch_coverage_key, create_pending_decision
    from app.services.resource_coverage import load_batch_coverage
    from tests.unit.test_agent_service import TestLinksOnlyMultiSeasonPack

    engine, _ = prepared
    async with engine.begin() as conn:
        async with AsyncSession(bind=conn, expire_on_commit=False) as db:
            await db.execute(text("UPDATE pending_decisions SET status='expired' WHERE status='pending'"))
            agent = (await db.execute(select(Agent.id, Agent.channel_id))).one()
            first, second = await TestLinksOnlyMultiSeasonPack()._make_season_works(db)
            works = [first] if scope == "season" else [first, second]
            groups = []
            for end in [6, 12]:
                versions = []
                for reverse in [False, True]:
                    resource = _make_resource(
                        agent.channel_id,
                        is_batch=True,
                        batch_scope=scope,
                        episode=None,
                        series_id=first.id if scope == "season" else None,
                        movie_id=None,
                        season=1 if scope == "season" else None,
                        batch_seasons=[1, 2] if scope == "multi_season" else [1],
                        episode_start=1,
                        episode_end=end,
                        parsed_at=None,
                    )
                    ordered = list(reversed(works)) if reverse else works
                    resource.work_links = [ResourceWorkLink(series_id=w.id) for w in ordered]
                    resource.file_assignments = [
                        ResourceFileAssignment(
                            series_id=w.id,
                            season=w.season_number,
                            file_path=f"{w.id}.mkv",
                            episode_start=1,
                            episode_end=end,
                        )
                        for w in ordered
                    ]
                    db.add(resource)
                    versions.append(resource)
                groups.append(versions)
            await db.flush()
            for index in [0, 1]:
                # Each old row incorrectly mixed a half-range and a full-range candidate.
                await db.execute(
                    text(
                        "INSERT INTO pending_decisions(id,agent_id,candidates,reason,status) "
                        "VALUES (:id,:agent,:ids,'Synthetic mixed coverage','pending')"
                    ),
                    dict(id=f"pack-{index}", agent=agent.id, ids=json.dumps([group[index].id for group in groups])),
                )
            agent_id = agent.id
    async with AsyncSession(engine) as db:
        review = await export_decision_review(db)
    assert len(review["proposed_groups"]) == 2 and not review["blocked"]
    review.update(approved_fingerprint=review["fingerprint"], supersede_pending=True)
    async with engine.begin() as conn:
        await conn.execute(text("BEGIN"))
        async with AsyncSession(bind=conn, expire_on_commit=False) as db:
            result = await apply_decision_review(db, review)
            assert len(result["created"]) == 2
    async with AsyncSession(engine) as db:
        agent = await db.get(Agent, agent_id)
        rows = list(await db.scalars(select(PendingDecision).where(PendingDecision.status == "pending")))
        assert len(rows) == 2 and len({row.decision_key for row in rows}) == 2
        for row in rows:
            assert row.episode == -1
            assert row.series_id == (first.id if scope == "season" else None)
            resources = list(await db.scalars(select(FileResource).where(FileResource.id.in_(row.candidates))))
            await load_batch_coverage(db, resources)
            coverage = _batch_coverage_key(resources[0])
            again = await create_pending_decision(
                agent, ("series", row.series_id, None, -1), resources, db, coverage=coverage, skip_llm=True
            )
            assert again.id == row.id
        await db.commit()


async def test_postgres_startup_rejects_unreviewed_then_accepts_review(prepared, monkeypatch):
    import app.database as database

    engine, review = prepared
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL startup DDL branch; embedded startup tested through CLI subprocess")
    monkeypatch.setattr(database, "engine", engine)
    with pytest.raises(ValueError, match="reviewed identity migration"):
        await database._create_tables_postgres()
    async with AsyncSession(engine) as db:
        assert (await export_decision_review(db))["fingerprint"] == review["fingerprint"]
    async with engine.begin() as conn:
        async with AsyncSession(bind=conn) as db:
            await apply_decision_review(db, review)
    await database._create_tables_postgres()
    async with AsyncSession(engine) as db:
        assert len(list(await db.scalars(select(PendingDecision).where(PendingDecision.status == "pending")))) == 2


async def test_old_schema_season_split_then_review(prepared):
    from scripts.season_split_migration import migrate_series
    from tests.unit.test_season_split_migration import _legacy_multi_season_series

    engine, _ = prepared
    async with engine.begin() as conn:
        await conn.execute(text("BEGIN"))
        async with AsyncSession(bind=conn, expire_on_commit=False) as db:
            series = await _legacy_multi_season_series(db)
            agent = (await db.execute(select(Agent.id, Agent.channel_id))).one()
            ids = []
            for season in (1, 2):
                for _ in range(2):
                    resource = _make_resource(agent.channel_id, series_id=series.id,
                                              season=season, episode=1, episode_confidence="manual", parsed_at=None)
                    db.add(resource)
                    await db.flush()
                    ids.append(resource.id)
            await db.execute(text(
                "INSERT INTO pending_decisions(id,agent_id,series_id,season,episode,candidates,reason,status) "
                "VALUES ('split-old',:agent,:series,2,1,:candidates,'Synthetic mixed legacy slot','pending')"
            ), dict(agent=agent.id, series=series.id, candidates=json.dumps(ids)))
            before = await export_decision_review(db)
            assert len(before['blocked']) == 4
            series_id = series.id
            with pytest.raises(RuntimeError, match="after old-table split"):
                async with db.begin_nested():
                    await migrate_series(db, series, apply=True)
                    raise RuntimeError("after old-table split")
            assert (await export_decision_review(db))["fingerprint"] == before["fingerprint"]
            from app.models.file_resource import FileResource
            from app.models.series import TVSeries

            restored = list(await db.execute(select(FileResource.series_id, FileResource.season)
                                             .where(FileResource.id.in_(ids))))
            assert len(restored) == 4
            assert {row.series_id for row in restored} == {series_id}
            assert sorted(row.season for row in restored) == [1, 1, 2, 2]
            assert len(list(await db.scalars(select(TVSeries.id)))) == 1
            series = await db.get(TVSeries, series_id, populate_existing=True)
            await migrate_series(db, series, apply=True)
            columns = await conn.run_sync(lambda sync: {c['name'] for c in inspect(sync).get_columns('pending_decisions')})
            assert 'decision_key' not in columns
            preserved = (await db.execute(text("SELECT candidates,status FROM pending_decisions WHERE id='split-old'"))).one()
            candidates = json.loads(preserved.candidates) if isinstance(preserved.candidates, str) else preserved.candidates
            assert candidates == ids and preserved.status == 'pending'
            review = await export_decision_review(db)
            assert not review['blocked']
            split_groups = [g for g in review['proposed_groups'] if 'split-old' in g['source_decision_ids']]
            assert len(split_groups) == 2
            assert {g['decision_scope']['season'] for g in split_groups} == {1, 2}
            assert set().union(*(set(g['candidates']) for g in split_groups)) == set(ids)
            review.update(approved_fingerprint=review['fingerprint'], supersede_pending=True)
            await apply_decision_review(db, review)
    async with AsyncSession(engine) as db:
        rows = list(await db.scalars(select(PendingDecision).where(PendingDecision.status == 'pending')))
        assert len(rows) == 4
        assert (await db.get(PendingDecision, 'split-old')).status == 'expired'
