"""The existing offline entrypoint must not persist a partial season repair."""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

import app.database as database
from app.models.episode import Episode
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.dedup_metadata_policy import DedupConflictError
from scripts import post_split_leftover_repair as repair


@pytest.mark.parametrize('case', ['success', 'manual_conflict', 'late_failure'])
async def test_offline_repair_preserves_atomicity(db_session, monkeypatch, case):
    collection = WorkCollection(title_cn='合成离线修复合集')
    db_session.add(collection)
    await db_session.flush()
    survivor = TVSeries(title_cn='合成续篇', season_number=0, collection_id=collection.id,
                        description='curator source', manually_edited_fields=['description'])
    shell = TVSeries(title_cn='合成迁移壳', season_number=2, collection_id=collection.id)
    if case == 'manual_conflict':
        shell.description = 'different curator'
        shell.manually_edited_fields = ['description']
    db_session.add_all([survivor, shell])
    await db_session.flush()
    episodes = [Episode(series_id=survivor.id, season=0, episode=1, title='Synthetic original'),
                Episode(series_id=shell.id, season=2, episode=1, title='Synthetic shell')]
    db_session.add_all(episodes)
    await db_session.commit()
    source_id, shell_id = survivor.id, shell.id
    original_episodes = {(row.id, row.series_id, row.season, row.episode, row.title) for row in episodes}
    monkeypatch.setattr(repair, 'async_session_factory', database.async_session_factory)
    monkeypatch.setattr(repair, 'load_runtime_config', AsyncMock())
    monkeypatch.setattr(repair, 'bangumi_configured', lambda: False)
    monkeypatch.setattr(repair, '_OREGAIRU_PAIRS', [(source_id, shell_id, 2)])
    if case == 'late_failure':
        monkeypatch.setattr(repair, '_case_d_catseye', AsyncMock(side_effect=RuntimeError('Synthetic later repair failure')))
        with pytest.raises(RuntimeError, match='later repair failure'):
            await repair.main(apply=True)
    elif case == 'manual_conflict':
        with pytest.raises(DedupConflictError):
            await repair.main(apply=True)
    else:
        await repair.main(apply=True)
    db_session.expire_all()
    after = (await db_session.execute(select(Episode))).scalars().all()
    remaining = (await db_session.execute(select(TVSeries))).scalars().all()
    if case == 'success':
        assert len(remaining) == 1 and remaining[0].id == source_id
        assert remaining[0].season_number == 2
        assert len(after) == 1 and after[0].series_id == source_id and after[0].season == 2
        assert after[0].title == 'Synthetic original'
    else:
        assert len(remaining) == 2
        assert {row.id:row.season_number for row in remaining} == {source_id:0, shell_id:2}
        assert {(row.id, row.series_id, row.season, row.episode, row.title) for row in after} == original_episodes
    assert next(row for row in remaining if row.id == source_id).description == 'curator source'
