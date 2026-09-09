"""Unit tests for app/scripts/dedup_metadata.py.

Module-level lines (imports, logger) are covered by import. Here we exercise
``_run`` (the async body: session open, merge call, commit/rollback) and
``main`` (logging init + asyncio.run + sys.exit), stubbing the DB session and
the metadata_dedup service.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import app.scripts.dedup_metadata as dedup_mod
from app.services.metadata_dedup import DedupReport


def test_module_imports_and_logger():
    assert dedup_mod.logger.name == "app.scripts.dedup_metadata"
    assert dedup_mod.async_session_factory is not None


def _fake_db(report, merge_side_effect=None):
    """A fake session context manager + merge mock."""
    db = MagicMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()

    class _Ctx:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *a):
            return False

    merge = AsyncMock(return_value=report)
    if merge_side_effect is not None:
        merge = AsyncMock(side_effect=merge_side_effect)
    return _Ctx(), merge, db


@pytest.mark.asyncio
async def test_run_happy_path(monkeypatch, capsys):
    report = DedupReport(
        series_groups=1, series_removed=1,
        movie_groups=0, movies_removed=0,
        file_resources_updated=2, agent_works_updated=1,
        mappings_updated=0, pending_decisions_updated=0,
        episodes_updated=0, notes=["note"],
    )
    ctx, merge, db = _fake_db(report)
    monkeypatch.setattr(dedup_mod, "async_session_factory", lambda: ctx)
    monkeypatch.setattr(dedup_mod, "merge_duplicate_metadata", merge)

    code = await dedup_mod._run()

    assert code == 0
    db.commit.assert_awaited_once()
    out = capsys.readouterr().out
    assert "Metadata dedup complete:" in out
    assert "series groups merged: 1" in out
    assert "note" in out


@pytest.mark.asyncio
async def test_run_rolls_back_and_reraises(monkeypatch):
    ctx, merge, db = _fake_db(DedupReport(), merge_side_effect=RuntimeError("boom"))
    monkeypatch.setattr(dedup_mod, "async_session_factory", lambda: ctx)
    monkeypatch.setattr(dedup_mod, "merge_duplicate_metadata", merge)

    with pytest.raises(RuntimeError, match="boom"):
        await dedup_mod._run()

    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


def test_main_calls_asyncio_run_and_exit(monkeypatch):
    run = MagicMock(return_value=0)
    exit_mock = MagicMock()
    monkeypatch.setattr(dedup_mod, "asyncio", MagicMock(run=run))
    monkeypatch.setattr(dedup_mod, "sys", MagicMock(exit=exit_mock))
    monkeypatch.setattr(dedup_mod.logging, "basicConfig", MagicMock())

    dedup_mod.main()

    run.assert_called_once()
    exit_mock.assert_called_once_with(0)
    dedup_mod.logging.basicConfig.assert_called_once()


def test_main_uses_asyncio_run_real(monkeypatch):
    """Ensure the main entry still funnels through asyncio.run(_run())."""
    called = []

    async def _fake_run():
        called.append(True)
        return 3

    exit_mock = MagicMock()
    monkeypatch.setattr(dedup_mod, "_run", _fake_run)
    monkeypatch.setattr(dedup_mod, "sys", MagicMock(exit=exit_mock))
    monkeypatch.setattr(dedup_mod.logging, "basicConfig", MagicMock())

    dedup_mod.main()

    assert called == [True]
    exit_mock.assert_called_once_with(3)
