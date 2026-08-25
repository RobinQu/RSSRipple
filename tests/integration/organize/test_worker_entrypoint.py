"""Worker 进程入口（app/worker.py）进程内集成测试。

worker 是 ``python -m app.worker`` 的进程装配代码，无法在测试里真实拉起；
本文件拆掉进程级副作用（信号、无限等待、真实队列/调度器）后逐函数覆盖：
数据目录自举（poster 失败回退 / sqlite 父目录）、心跳循环触碰/异常/退出、
``_run`` 装配主链路与 ``main()`` 入口。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.worker as worker
from app.config import settings

# ---------------------------------------------------------------------------
# _ensure_data_dirs
# ---------------------------------------------------------------------------


def test_ensure_data_dirs_creates_poster_and_sqlite_parents(tmp_path, monkeypatch):
    poster = tmp_path / "posters"
    db_file = tmp_path / "nested" / "db" / "rss.db"
    monkeypatch.setattr(settings, "poster_cache_dir", str(poster))
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_file}")
    worker._ensure_data_dirs()
    assert poster.is_dir()
    assert db_file.parent.is_dir()


def test_ensure_data_dirs_poster_fallback_on_oserror(tmp_path, monkeypatch):
    """poster 目录创建失败（父级是文件）→ 回退 ./data/posters。"""
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setattr(settings, "poster_cache_dir", str(blocker / "nope"))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(settings, "database_url", "postgresql+asyncpg://x")
    worker._ensure_data_dirs()
    assert (cwd / "data" / "posters").is_dir()
    # 回退写入的是字面相对路径（与产品实现一致）
    assert settings.poster_cache_dir == "data/posters"


@pytest.mark.parametrize("url", [
    "sqlite:///abs/path/rss.db",
    "sqlite://abs/path/rss.db",
])
def test_ensure_data_dirs_sqlite_absolute_urls(tmp_path, monkeypatch, url):
    monkeypatch.setattr(settings, "poster_cache_dir", str(tmp_path / "p"))
    # 绝对路径形态：不抛错即可（父目录已存在）
    monkeypatch.setattr(settings, "database_url", url)
    worker._ensure_data_dirs()


def test_ensure_data_dirs_sqlite_relative_form(tmp_path, monkeypatch):
    """``sqlite:relative`` 形态按 CWD 相对路径推导父目录。"""
    monkeypatch.setattr(settings, "poster_cache_dir", str(tmp_path / "p"))
    monkeypatch.setattr(settings, "database_url", "sqlite:relative/data.db")
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    worker._ensure_data_dirs()
    assert (cwd / "relative").is_dir()


# ---------------------------------------------------------------------------
# _heartbeat_loop
# ---------------------------------------------------------------------------


async def test_heartbeat_loop_touches_until_stopped(tmp_path, monkeypatch):
    heartbeat_file = tmp_path / "hb"
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", heartbeat_file)
    monkeypatch.setattr(worker, "HEARTBEAT_INTERVAL", 0.01)
    stop = asyncio.Event()

    async def stop_later():
        await asyncio.sleep(0.05)
        stop.set()

    task = asyncio.create_task(worker._heartbeat_loop(stop))
    stopper = asyncio.create_task(stop_later())
    await asyncio.wait_for(task, timeout=2)
    await stopper
    assert heartbeat_file.exists()


async def test_heartbeat_loop_swallows_oserror(tmp_path, monkeypatch):
    """HEARTBEAT_PATH 父级是文件 → touch 抛 OSError，循环吞掉继续。"""
    blocker = tmp_path / "file"
    blocker.write_text("x")
    monkeypatch.setattr(worker, "HEARTBEAT_PATH", blocker / "hb")
    monkeypatch.setattr(worker, "HEARTBEAT_INTERVAL", 0.01)
    stop = asyncio.Event()

    async def stop_later():
        await asyncio.sleep(0.05)
        stop.set()

    task = asyncio.create_task(worker._heartbeat_loop(stop))
    stopper = asyncio.create_task(stop_later())
    await asyncio.wait_for(task, timeout=2)
    await stopper


# ---------------------------------------------------------------------------
# _run 主链路（全依赖 monkeypatch）与 main()
# ---------------------------------------------------------------------------


class _StubQueue:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.registered = []

    async def start(self, consume=True):
        self.started = True

    async def stop(self):
        self.stopped = True

    def register(self, job_type, handler):
        self.registered.append(job_type)


class _FakeSessionCtx:
    """``async with async_session_factory() as sess`` 的最小替身。"""

    def __init__(self):
        self.session = SimpleNamespace(commit=AsyncMock())

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def wired_worker(tmp_path, monkeypatch):
    """把 _run 的全部外部依赖替换为桩；stop_event 立即放行以走完整链路。"""
    monkeypatch.setattr(settings, "poster_cache_dir", str(tmp_path / "p"))
    calls: list = []
    stub = SimpleNamespace(queue=_StubQueue(), calls=calls)

    async def fake_create_tables():
        calls.append("create_tables")

    async def fake_load(sess):
        calls.append("load_runtime_config")

    async def fake_init_scheduler():
        calls.append("init_scheduler")

    async def fake_setup_channel_jobs(sess):
        calls.append("setup_channel_jobs")

    async def fake_shutdown_scheduler():
        calls.append("shutdown_scheduler")

    def fake_register_handlers(queue):
        calls.append("register_handlers")

    def fake_create_queue(backend="memory", **kw):
        calls.append(("create_queue", backend))
        return stub.queue

    monkeypatch.setattr(worker, "create_tables", fake_create_tables)
    monkeypatch.setattr(worker, "async_session_factory", _FakeSessionCtx)
    monkeypatch.setattr(
        "app.services.runtime_config.load_runtime_config", fake_load
    )
    monkeypatch.setattr(
        "app.services.scheduler.init_scheduler", fake_init_scheduler
    )
    monkeypatch.setattr(
        "app.services.scheduler.setup_channel_jobs", fake_setup_channel_jobs
    )
    monkeypatch.setattr(
        "app.services.scheduler.shutdown_scheduler", fake_shutdown_scheduler
    )
    monkeypatch.setattr(
        "app.job_handlers.register_all_handlers", fake_register_handlers
    )
    monkeypatch.setattr(
        "app.services.task_queue.create_queue", fake_create_queue
    )

    # stop_event.wait() 立即返回：_run 走完启动→等待→finally 清理后自然退出。
    # 注意 worker.asyncio 即全局 asyncio 模块，monkeypatch 结束后自动还原。
    real_event = asyncio.Event

    class _InstantEvent(real_event):
        async def wait(self):
            return True

    monkeypatch.setattr(worker.asyncio, "Event", _InstantEvent)
    return stub


async def test_run_full_wiring_and_graceful_shutdown(wired_worker):
    stub = wired_worker
    await worker._run()
    assert ("create_queue", settings.queue_backend) in stub.calls
    assert "register_handlers" in stub.calls
    assert stub.queue.started and stub.queue.stopped
    for step in (
        "create_tables", "load_runtime_config", "init_scheduler",
        "setup_channel_jobs", "shutdown_scheduler",
    ):
        assert step in stub.calls


async def test_run_warns_when_started_with_web_role(
    wired_worker, monkeypatch, caplog
):
    monkeypatch.setattr(settings, "app_role", "web")
    with caplog.at_level("WARNING", logger="app.worker"):
        await worker._run()
    assert any("APP_ROLE=web" in rec.message for rec in caplog.records)


def test_main_invokes_asyncio_run(monkeypatch):
    ran = {}
    monkeypatch.setattr(worker.asyncio, "run", lambda coro: ran.setdefault("coro", coro))
    worker.main()
    assert "coro" in ran
