"""Readers retain the last loaded configuration while a refresh awaits the DB."""

import asyncio

import pytest

from app.models.app_setting import AppSetting
from app.services import runtime_config as config


@pytest.mark.parametrize("query_fails", [False, True])
async def test_reload_keeps_previous_snapshot_until_query_completes(db_session, monkeypatch, query_fails):
    db_session.add(AppSetting(key="llm_model", value="empty-model"))
    await db_session.commit()
    await config.load_runtime_config(db_session)
    assert config.runtime_config.llm_model == "empty-model"
    entered, release = asyncio.Event(), asyncio.Event()
    original_execute = db_session.execute

    async def delayed_execute(*args, **kwargs):
        entered.set()
        await release.wait()
        if query_fails:
            raise RuntimeError("Synthetic configuration read failure")
        return await original_execute(*args, **kwargs)

    monkeypatch.setattr(db_session, "execute", delayed_execute)
    task = asyncio.create_task(config.load_runtime_config(db_session))
    try:
        await asyncio.wait_for(entered.wait(), 2)
        assert config.runtime_config.llm_model == "empty-model"
    finally:
        release.set()
        result = await asyncio.gather(task, return_exceptions=True)
    if query_fails:
        assert isinstance(result[0], RuntimeError)
    else:
        assert result == [None]
    assert config.runtime_config.llm_model == "empty-model"
