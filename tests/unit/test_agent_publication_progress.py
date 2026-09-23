"""Actual transaction tests; synthetic identity and no downloader calls."""

from app.models.agent import Agent
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.services.agent_publication_progress import acknowledge_publications, reset_progress, snapshot_publications
from app.services.resource_publication import publish_resource
from tests.unit.test_resource_publication import resource


async def setup(db):
    old = await resource(db)
    downloader = DownloaderInstance(
        name="Synthetic", type="mock", url="mock://synthetic", download_dir="/tmp/synthetic"
    )
    db.add(downloader)
    await db.flush()
    agent = Agent(name="Synthetic", channel_id=old.channel_id, downloader_id=downloader.id)
    db.add(agent)
    await db.flush()
    await publish_resource(db, old.id, kind="created")
    await reset_progress(db, agent.id, old.channel_id)
    return agent, old


async def next_resource(db, channel_id):
    import uuid

    row = FileResource(channel_id=channel_id, guid=str(uuid.uuid4()), title_raw="Synthetic", torrent_url="synthetic")
    db.add(row)
    await db.flush()
    await publish_resource(db, row.id, kind="created")
    return row


async def test_late_metadata_retries_new_resource_but_preserves_history_exclusion(db_session):
    agent, old = await setup(db_session)
    new = await next_resource(db_session, old.channel_id)
    first = await snapshot_publications(db_session, agent.id, old.channel_id)
    assert first.resource_ids == (new.id,)
    assert await acknowledge_publications(db_session, first)
    await publish_resource(db_session, old.id, kind="metadata")
    await publish_resource(db_session, new.id, kind="metadata")
    second = await snapshot_publications(db_session, agent.id, old.channel_id)
    assert second.resource_ids == (new.id,)
    # Failed processing makes no acknowledgement and must replay unchanged.
    assert await snapshot_publications(db_session, agent.id, old.channel_id) == second
    assert await acknowledge_publications(db_session, second)
    assert not (await snapshot_publications(db_session, agent.id, old.channel_id)).resource_ids


async def test_backfill_reset_rejects_stale_run_acknowledgement(db_session):
    agent, old = await setup(db_session)
    new = await next_resource(db_session, old.channel_id)
    stale = await snapshot_publications(db_session, agent.id, old.channel_id)
    await reset_progress(db_session, agent.id, old.channel_id)
    assert not await acknowledge_publications(db_session, stale)
    await publish_resource(db_session, new.id, kind="metadata")
    assert not (await snapshot_publications(db_session, agent.id, old.channel_id)).resource_ids
    newest = await next_resource(db_session, old.channel_id)
    assert (await snapshot_publications(db_session, agent.id, old.channel_id)).resource_ids == (newest.id,)


async def test_completion_after_snapshot_survives_older_ack(db_session):
    agent, old = await setup(db_session)
    new = await next_resource(db_session, old.channel_id)
    before = await snapshot_publications(db_session, agent.id, old.channel_id)
    await publish_resource(db_session, new.id, kind="metadata")
    assert await acknowledge_publications(db_session, before)
    after = await snapshot_publications(db_session, agent.id, old.channel_id)
    assert after.resource_ids == (new.id,)
    assert after.through > before.through
    assert await acknowledge_publications(db_session, after)
    assert not await acknowledge_publications(db_session, before)


async def test_first_initialization_does_not_reset_existing_paused_progress(db_session):
    from app.services.agent_publication_progress import initialize_first_run

    agent, old = await setup(db_session)
    agent.status = "paused"
    new = await next_resource(db_session, old.channel_id)
    assert not await initialize_first_run(db_session, agent)
    agent.status = "active"
    assert (await snapshot_publications(db_session, agent.id, old.channel_id)).resource_ids == (new.id,)


async def test_missing_legacy_progress_is_not_silently_reset(db_session):
    import pytest
    from sqlalchemy import delete

    from app.models.agent_publication_progress import AgentPublicationProgress
    from app.services.agent_publication_progress import initialize_first_run
    from app.utils.time import utcnow

    agent, old = await setup(db_session)
    await db_session.execute(delete(AgentPublicationProgress).where(AgentPublicationProgress.agent_id == agent.id))
    agent.last_consumed_at = utcnow()
    with pytest.raises(ValueError, match="migration"):
        await initialize_first_run(db_session, agent)
    agent.last_consumed_at = None
    assert await initialize_first_run(db_session, agent)
    assert not (await snapshot_publications(db_session, agent.id, old.channel_id)).resource_ids
    new = await next_resource(db_session, old.channel_id)
    assert (await snapshot_publications(db_session, agent.id, old.channel_id)).resource_ids == (new.id,)


async def test_channel_switch_retains_old_time_scope_and_accepts_late_new_publication(db_session):
    from datetime import timedelta

    from app.services.agent_publication_progress import switch_channel_progress
    from app.utils.time import utcnow

    agent, _ = await setup(db_session)
    old_snapshot = await snapshot_publications(db_session, agent.id, agent.channel_id)
    other = await resource(db_session)
    now = utcnow()
    other.created_at = now - timedelta(days=2)
    await publish_resource(db_session, other.id, kind="created")
    eligible = await next_resource(db_session, other.channel_id)
    eligible.created_at = now
    agent.last_consumed_at = now - timedelta(days=1)
    agent.channel_id = other.channel_id
    await db_session.flush()
    await switch_channel_progress(db_session, agent)
    assert not await acknowledge_publications(db_session, old_snapshot)
    first = await snapshot_publications(db_session, agent.id, agent.channel_id)
    assert first.resource_ids == (eligible.id,)
    await acknowledge_publications(db_session, first)
    late = await next_resource(db_session, agent.channel_id)
    late.created_at = now - timedelta(days=3)
    await db_session.flush()
    assert (await snapshot_publications(db_session, agent.id, agent.channel_id)).resource_ids == (late.id,)
    await reset_progress(db_session, agent.id, agent.channel_id)
    await publish_resource(db_session, eligible.id, kind="metadata")
    assert not (await snapshot_publications(db_session, agent.id, agent.channel_id)).resource_ids


async def test_window_completion_scope_and_post_snapshot_events(db_session):
    from datetime import timedelta

    from app.services.agent_publication_progress import acknowledge_window
    from app.utils.time import utcnow

    agent, excluded = await setup(db_session)
    now = utcnow()
    excluded.created_at = now - timedelta(days=2)
    selected = await next_resource(db_session, agent.channel_id)
    selected.created_at = now
    await db_session.flush()
    await reset_progress(db_session, agent.id, agent.channel_id)
    window = await snapshot_publications(db_session, agent.id, agent.channel_id)
    await publish_resource(db_session, selected.id, kind="metadata")
    await publish_resource(db_session, excluded.id, kind="metadata")
    assert await acknowledge_window(db_session, window, now - timedelta(days=1))
    after = await snapshot_publications(db_session, agent.id, agent.channel_id)
    assert after.resource_ids == (selected.id,)
    await reset_progress(db_session, agent.id, agent.channel_id)
    assert not await acknowledge_window(db_session, window, None)
    await publish_resource(db_session, selected.id, kind="metadata")
    assert not (await snapshot_publications(db_session, agent.id, agent.channel_id)).resource_ids


async def test_prepared_scan_survives_missing_ack_but_backfill_cancels_scope(db_session):
    from app.services.agent_publication_progress import prepare_window_retry

    agent, old = await setup(db_session)
    original = await snapshot_publications(db_session, agent.id, agent.channel_id)
    prepared = await prepare_window_retry(db_session, original, [old.id], None)
    assert prepared.generation != original.generation
    retry = await snapshot_publications(db_session, agent.id, agent.channel_id)
    assert retry.resource_ids == (old.id,)
    assert not await acknowledge_publications(db_session, original)
    await reset_progress(db_session, agent.id, agent.channel_id)
    assert not await acknowledge_publications(db_session, prepared)
    assert not (await snapshot_publications(db_session, agent.id, agent.channel_id)).resource_ids


async def test_stale_scan_cannot_reopen_new_backfill(db_session):
    import pytest

    from app.services.agent_publication_progress import prepare_window_retry

    agent, old = await setup(db_session)
    stale = await snapshot_publications(db_session, agent.id, agent.channel_id)
    await reset_progress(db_session, agent.id, agent.channel_id)
    with pytest.raises(ValueError, match="scope changed"):
        await prepare_window_retry(db_session, stale, [old.id], None)
    assert not (await snapshot_publications(db_session, agent.id, agent.channel_id)).resource_ids


async def test_replacing_completion_cannot_be_consumed_by_an_older_snapshot(db_session):
    agent, old = await setup(db_session)
    new = await next_resource(db_session, agent.channel_id)
    await publish_resource(db_session, new.id, kind="metadata")
    before = await snapshot_publications(db_session, agent.id, agent.channel_id)
    latest = await publish_resource(db_session, new.id, kind="metadata")
    assert latest.sequence > before.through
    await acknowledge_publications(db_session, before)
    after = await snapshot_publications(db_session, agent.id, agent.channel_id)
    assert after.resource_ids == (new.id,)
    assert after.through == latest.sequence


async def test_history_reconciliation_publishes_only_admitted_work(db_session, monkeypatch):
    """Synthetic numbering history; actual sweep, publication and admission."""
    from app.models.series import TVSeries
    from app.services.fetch_service import reconcile_stale_raw_episodes

    agent, excluded = await setup(db_session)
    admitted = await next_resource(db_session, excluded.channel_id)
    anchor = await next_resource(db_session, excluded.channel_id)
    series = TVSeries(title_cn="Synthetic history", season_number=4, number_of_episodes=24)
    db_session.add(series)
    await db_session.flush()
    for row in (excluded, admitted, anchor):
        row.series_id = series.id
        row.season = 4
        row.subtitle_group = "GROUP"
        row.episode = 18
        row.absolute_episode = 90
        row.episode_confidence = "raw"
    anchor.absolute_episode = 89
    anchor.episode_confidence = "manual"
    first = await snapshot_publications(db_session, agent.id, excluded.channel_id)
    await acknowledge_publications(db_session, first)
    await db_session.commit()
    # A failed event insert must roll back numbering changes and publication.
    import pytest

    from app.services import resource_publication

    ids = (excluded.id, admitted.id)
    real_publish = resource_publication.publish_resource

    async def fail_after_publish(*args, **kwargs):
        await real_publish(*args, **kwargs)
        raise RuntimeError("Synthetic publication failure")

    with monkeypatch.context() as patcher:
        patcher.setattr(resource_publication, "publish_resource", fail_after_publish)
        with pytest.raises(RuntimeError, match="Synthetic publication failure"):
            await reconcile_stale_raw_episodes(db_session, return_resource_ids=True)
        await db_session.rollback()
    for row in (excluded, admitted, anchor, agent):
        await db_session.refresh(row)
    assert excluded.episode == admitted.episode == 18
    assert not (await snapshot_publications(db_session, agent.id, excluded.channel_id)).resource_ids
    # Exercise the scheduled handler with its own committed sessions. Only
    # unrelated network backfill and the queue transport are substituted.
    from unittest.mock import AsyncMock, patch

    from app.job_handlers import _handle_backfill_metadata
    from app.services import task_queue

    await db_session.commit()
    enqueue = AsyncMock()
    with (
        patch("app.job_handlers._refresh_runtime_config", AsyncMock()),
        patch("app.services.fetch_service.backfill_unmatched_resources_global", AsyncMock(return_value=0)),
        patch.object(task_queue.task_queue, "enqueue", enqueue),
    ):
        result = await _handle_backfill_metadata({})
    assert result == {"status": "done", "processed": 0, "reconciled": len(ids)}
    enqueue.assert_awaited_once_with(
        "run_agent", f"agent:{agent.id}", {"agent_id": agent.id, "automatic": True}
    )
    for row in (excluded, admitted):
        await db_session.refresh(row)
    assert excluded.episode == admitted.episode == 19
    pending = await snapshot_publications(db_session, agent.id, excluded.channel_id)
    assert pending.resource_ids == (admitted.id,)
