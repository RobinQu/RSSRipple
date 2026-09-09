"""Unit tests for the worker entry point helpers.

The module-level lines (imports, logger, heartbeat constants) are covered by
importing ``app.worker``. Here we exercise the two runnable helpers:
``_ensure_data_dirs`` (poster + sqlite db dir creation, including the
OSError fallbacks) and ``_heartbeat_loop`` (heartbeat file touching,
timeout-continue, and clean stop).
"""

from __future__ import annotations

import asyncio

import pytest

import app.worker as worker


def test_module_imports_and_constants():
    """Cover module-level imports, logger and heartbeat constants."""
    assert worker.HEARTBEAT_INTERVAL > 0
    assert str(worker.HEARTBEAT_PATH)
    assert worker.logger.name == "app.worker"


def test_ensure_data_dirs_sqlite_file_url(monkeypatch, tmp_path):
    """sqlite:/// absolute path creates the poster dir and DB parent dir."""
    poster = tmp_path / "posters"
    db = tmp_path / "nested" / "db" / "test.db"
    monkeypatch.setattr(worker.settings, "poster_cache_dir", str(poster))
    monkeypatch.setattr(worker.settings, "database_url", f"sqlite:///{db}")

    worker._ensure_data_dirs()

    assert poster.exists()
    assert db.parent.exists()


def test_ensure_data_dirs_sqlite_relative_url(monkeypatch, tmp_path):
    """sqlite: relative form (no ///) still derives and creates the dir."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(worker.settings, "poster_cache_dir", str(tmp_path / "p"))
    monkeypatch.setattr(worker.settings, "database_url", "sqlite:rel/db/test.db")

    worker._ensure_data_dirs()

    assert (tmp_path / "rel" / "db").exists()


def test_ensure_data_dirs_poster_fallback_on_oserror(monkeypatch, tmp_path):
    """When the configured poster dir cannot be created, fall back to
    ./data/posters and rewrite settings.poster_cache_dir."""
    from pathlib import Path

    import app.worker as worker_mod

    real_mkdir = Path.mkdir
    calls = {"count": 0}

    class _FlakyPath(Path):
        def mkdir(self, *a, **kw):
            calls["count"] += 1
            if calls["count"] == 1:
                raise OSError("permission denied")
            return real_mkdir(self, *a, **kw)

    monkeypatch.setattr(worker_mod, "Path", _FlakyPath)
    monkeypatch.setattr(worker_mod.settings, "poster_cache_dir", str(tmp_path / "bad"))
    monkeypatch.setattr(worker_mod.settings, "database_url", "postgresql+asyncpg://x")

    worker_mod._ensure_data_dirs()

    assert worker_mod.settings.poster_cache_dir.endswith("data/posters")


def test_ensure_data_dirs_db_oserror_swallowed(monkeypatch, tmp_path):
    """A failure to create the DB parent dir is logged and swallowed."""
    from pathlib import Path

    import app.worker as worker_mod

    real_mkdir = Path.mkdir

    def _flaky_mkdir(self, *a, **kw):
        if "nope" in str(self):
            raise OSError("no space")
        return real_mkdir(self, *a, **kw)

    monkeypatch.setattr(worker_mod.settings, "poster_cache_dir", str(tmp_path / "ok"))
    monkeypatch.setattr(worker_mod.settings, "database_url", "sqlite:///nope/db/test.db")
    monkeypatch.setattr(Path, "mkdir", _flaky_mkdir)

    worker_mod._ensure_data_dirs()

    assert (tmp_path / "ok").exists()


@pytest.mark.asyncio
async def test_heartbeat_loop_touches_and_stops(tmp_path, monkeypatch):
    hb = tmp_path / "heartbeat"
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", hb)
    monkeypatch.setattr(worker, "HEARTBEAT_INTERVAL", 1)

    stop = asyncio.Event()
    task = asyncio.create_task(worker._heartbeat_loop(stop))
    await asyncio.sleep(0.02)
    assert hb.exists()
    stop.set()
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_heartbeat_loop_continues_on_timeout(tmp_path, monkeypatch):
    """The wait_for TimeoutError path (line 140-142) — keep looping."""
    hb = tmp_path / "heartbeat"
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", hb)
    monkeypatch.setattr(worker, "HEARTBEAT_INTERVAL", 0.01)

    stop = asyncio.Event()
    task = asyncio.create_task(worker._heartbeat_loop(stop))
    # Let it time out a few times before stopping.
    await asyncio.sleep(0.05)
    assert hb.exists()
    stop.set()
    await asyncio.wait_for(task, timeout=2)


@pytest.mark.asyncio
async def test_heartbeat_loop_oserror_logged(monkeypatch, caplog):
    class _FakePath:
        def touch(self):
            raise OSError("read-only fs")

    monkeypatch.setattr(worker, "HEARTBEAT_PATH", _FakePath())
    monkeypatch.setattr(worker, "HEARTBEAT_INTERVAL", 1)

    stop = asyncio.Event()
    task = asyncio.create_task(worker._heartbeat_loop(stop))
    await asyncio.sleep(0.02)
    assert "Cannot write worker heartbeat" in caplog.text
    stop.set()
    await asyncio.wait_for(task, timeout=2)
