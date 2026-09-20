"""Necessity probes: synthetic input, actual production grouping and Turso writes."""
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.models.pending_decision import PendingDecision
from app.services.agent_service import _batch_coverage_key, process_resources
from tests.unit.test_agent_service import (
    TestLinksOnlyMultiSeasonPack as PackFixture,
)
from tests.unit.test_agent_service import (
    _make_resource,
)
from tests.unit.test_agent_service import (
    channel as channel,
)
from tests.unit.test_agent_service import (
    downloader as downloader,
)


def test_different_episode_ranges_have_different_coverage():
    half = _make_resource("synthetic-channel", series_id="synthetic-season", is_batch=True,
                          episode=None, episode_start=1, episode_end=6)
    full = _make_resource("synthetic-channel", series_id="synthetic-season", is_batch=True,
                          episode=None, episode_start=1, episode_end=12)
    from app.models.series import TVSeries
    for pack in (half, full):
        pack.series = TVSeries(id="synthetic-season", season_number=1)
        pack.work_links = []
        pack.file_assignments = []
    assert _batch_coverage_key(half) != _batch_coverage_key(full)


async def test_distinct_link_sets_remain_two_decisions(db_session, channel, downloader):
    helper = PackFixture()
    first, second = await helper._make_season_works(db_session)
    third, fourth = await helper._make_season_works(db_session)
    agent = await helper._make_agent(db_session, channel, downloader, scope_channel_wide=True)
    group_a = [helper._pack(channel.id, [first, second]) for _ in range(2)]
    group_b = [helper._pack(channel.id, [third, fourth]) for _ in range(2)]
    db_session.add_all(group_a + group_b)
    await db_session.flush()
    with patch("app.services.agent_service._suggest_pick", AsyncMock(return_value=(None, None))):
        result = await process_resources(agent, group_a + group_b, db_session)
    assert not result.errors
    assert result.pending_decisions == 2
    rows = list(await db_session.scalars(select(PendingDecision).where(PendingDecision.agent_id == agent.id)))
    assert len(rows) == 2, [(row.id, row.candidates) for row in rows]
    assert {frozenset(row.candidates) for row in rows} == {
        frozenset(r.id for r in group_a), frozenset(r.id for r in group_b)
    }


async def test_completed_half_pack_does_not_suppress_full_pack(db_session, channel, downloader):
    from app.models.download_task import DownloadTask
    helper = PackFixture()
    first, _ = await helper._make_season_works(db_session)
    agent = await helper._make_agent(db_session, channel, downloader, scope_channel_wide=True)
    packs = [_make_resource(channel.id, series_id=first.id, is_batch=True, season=1,
                            episode=None, episode_start=1, episode_end=end) for end in (6, 12)]
    for resource in packs:
        resource.series = first
        resource.work_links = []
        resource.file_assignments = []
    db_session.add_all(packs)
    await db_session.flush()
    db_session.add(DownloadTask(agent_id=agent.id, file_resource_id=packs[0].id,
                               downloader_id=downloader.id, download_dir='/synthetic', status='completed'))
    await db_session.flush()
    with patch('app.services.agent_service.dispatch_download', AsyncMock()) as dispatch:
        result = await process_resources(agent, [packs[1]], db_session)
    assert result.duplicates_skipped == 0
    assert result.dispatched == 1
    dispatch.assert_awaited_once()


async def test_unknown_episode_coverage_is_held_before_dispatch(db_session, channel, downloader):
    helper = PackFixture()
    first, _ = await helper._make_season_works(db_session)
    agent = await helper._make_agent(db_session, channel, downloader, scope_channel_wide=True)
    pack = _make_resource(channel.id, series_id=first.id, is_batch=True, season=1,
                          episode=None, episode_start=None, episode_end=None)
    pack.series = first
    pack.work_links = []
    pack.file_assignments = []
    db_session.add(pack)
    await db_session.flush()
    with patch('app.services.agent_service.dispatch_download', AsyncMock()) as dispatch:
        result = await process_resources(agent, [pack], db_session)
    assert result.unrecognized == 1
    assert result.dispatched == 0
    dispatch.assert_not_awaited()


async def test_new_candidates_clear_old_recommendation_if_llm_fails(db_session, channel, downloader):
    from app.services.agent_service import create_pending_decision
    helper = PackFixture()
    first, _ = await helper._make_season_works(db_session)
    agent = await helper._make_agent(db_session, channel, downloader, scope_channel_wide=True)
    candidates = [_make_resource(channel.id, series_id=first.id, episode=1) for _ in range(3)]
    db_session.add_all(candidates)
    await db_session.flush()
    with patch("app.services.agent_service._suggest_pick", AsyncMock(return_value=(candidates[0].id, "old pick"))):
        row = await create_pending_decision(agent, ("series", first.id, 1), candidates[:2], db_session)
    assert row.llm_picked_resource_id == candidates[0].id
    with patch("app.services.agent_service._suggest_pick", AsyncMock(return_value=(None, None))):
        again = await create_pending_decision(agent, ("series", first.id, 1), candidates[1:], db_session)
    assert again.id == row.id
    assert set(again.candidates) == {c.id for c in candidates}
    assert again.llm_picked_resource_id is None


async def _candidate_group(db, channel, downloader, *, count=2, mode="ask"):
    helper = PackFixture()
    first, _ = await helper._make_season_works(db)
    agent = await helper._make_agent(db, channel, downloader, scope_channel_wide=True,
                                     conflict_resolution=mode)
    candidates = [_make_resource(channel.id, series_id=first.id, episode=1) for _ in range(count)]
    db.add_all(candidates)
    await db.flush()
    return agent, candidates


async def test_background_choice_retries_with_new_session(db_session, channel, downloader):
    from sqlalchemy.exc import OperationalError
    agent, candidates = await _candidate_group(db_session, channel, downloader)
    sessions = []
    async def write(agent, key, cands, db):
        sessions.append(db)
        if len(sessions) == 1:
            raise OperationalError("INSERT", {}, Exception("database is locked"))
        return True
    with patch("app.services.agent_service._process_candidate_group", write):
        result = await process_resources(agent, candidates, db_session, autocommit=True)
    assert result.pending_decisions == 1 and not result.errors
    assert len(sessions) == 2 and sessions[0] is not sessions[1]


async def test_request_choice_propagates_lock_for_whole_request_retry(db_session, channel, downloader):
    import pytest
    from sqlalchemy.exc import OperationalError
    agent, candidates = await _candidate_group(db_session, channel, downloader)
    with patch("app.services.agent_service._process_candidate_group",
               AsyncMock(side_effect=OperationalError("INSERT", {}, Exception("database is locked")))):
        with pytest.raises(OperationalError, match="database is locked"):
            await process_resources(agent, candidates, db_session)


async def test_background_dispatch_is_not_automatically_replayed(db_session, channel, downloader):
    from sqlalchemy.exc import OperationalError
    agent, candidates = await _candidate_group(db_session, channel, downloader, count=1)
    with patch("app.services.agent_service._process_candidate_group",
               AsyncMock(side_effect=OperationalError("COMMIT", {}, Exception("database is locked")))) as write:
        result = await process_resources(agent, candidates, db_session, autocommit=True)
    assert len(result.errors) == 1
    assert write.await_count == 1
