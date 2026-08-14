"""Organize 进程内集成测试的共享 fixtures。

DB fixtures 直接复用 tests/unit/conftest.py（导入即注册）：其 ``db_engine``
fixture 会把每测试独立的 Turso 引擎安装为 ``app.database`` 的全局
engine/session_factory——scheduler 的 notify tick（``committed_session``）
与 organize auto_execute 的后台任务都走这个全局 factory，因此
「completed 任务 → 通知 → 规划 → 执行 → 清理」全链路可以在进程内跑通，
不依赖 docker 栈。文件树用 ``tmp_path`` 模拟共享卷：Transmission 容器视角
``/downloads`` 与本进程视角 ``tmp_path/mnt/shared`` 刻意不同，以验证
``DownloaderInstance`` 卷绑定（StorageVolume + volume_id/volume_subpath）
路径解析。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from app.config import settings

# 复用 tests/unit/conftest.py 的 DB fixtures：pytest 按 conftest 命名空间里的
# 属性名注册 fixture，因此保持原名赋值（导入模块同时完成其环境准备：
# DATABASE_URL 兜底、app.models 注册、fast asyncio.sleep）。
from tests.unit import conftest as _unit_conftest

db_engine = _unit_conftest.db_engine
db_session = _unit_conftest.db_session


@pytest.fixture(autouse=True)
def _enable_organize(monkeypatch):
    monkeypatch.setattr(settings, "organize_enabled", True)


@pytest_asyncio.fixture
async def shared_volume(tmp_path, db_session):
    """共享卷：daemon 视角 ``/downloads`` ↔ 进程视角 ``tmp_path/mnt/shared``。

    落一条 StorageVolume 记录（mount_path=进程视角根），下载器经
    ``volume_id`` 绑定它做路径解析。
    """
    from app.models.storage_volume import StorageVolume

    process_root = tmp_path / "mnt" / "shared"
    (process_root / "complete").mkdir(parents=True)
    volume = StorageVolume(
        id=str(uuid.uuid4()),
        name=f"shared-{uuid.uuid4().hex[:8]}",
        mount_path=str(process_root),
    )
    db_session.add(volume)
    await db_session.commit()
    return SimpleNamespace(
        daemon="/downloads",
        process=process_root,
        complete_daemon="/downloads/complete",
        complete_process=process_root / "complete",
        volume=volume,
        media=tmp_path / "media",
    )


@pytest.fixture
def rpc_mocks(monkeypatch):
    """Mock 下载器 RPC：停种 / 恢复做种 / 文件清单 / 删除（文件清单返回值 per-test 配置）。"""
    pause = AsyncMock(return_value=True)
    resume = AsyncMock(return_value=True)
    remove = AsyncMock(return_value=True)
    get_files = AsyncMock(return_value={"name": None, "files": []})
    monkeypatch.setattr(
        "app.clients.transmission.TransmissionWrapper.pause_torrent", pause
    )
    monkeypatch.setattr(
        "app.clients.transmission.TransmissionWrapper.resume_torrent", resume
    )
    monkeypatch.setattr(
        "app.clients.transmission.TransmissionWrapper.remove_torrent", remove
    )
    monkeypatch.setattr(
        "app.clients.transmission.TransmissionWrapper.get_torrent_files", get_files
    )
    return SimpleNamespace(
        pause=pause, resume=resume, remove=remove, get_files=get_files
    )


@pytest.fixture
def refresh_mock(monkeypatch):
    """Mock 执行后的媒体服务器刷新（Library → MediaServerInstance 寻址）。"""
    mock = AsyncMock()
    monkeypatch.setattr("app.services.organize_service.refresh_library", mock)
    return mock


@pytest_asyncio.fixture
async def session_factory(db_engine):  # noqa: ARG001 — 仅保证全局 factory 已安装
    """``app.database`` 的全局 session factory（指向本测试的 Turso 引擎）。

    tick / 后台任务经 ``committed_session`` 写入的行与长生命周期
    ``db_session`` 的 MVCC 快照无关，断言一律走这里的新鲜会话（避免
    tests/api/test_organize_api.py 注释里记录的 MVCC 快照坑）。
    """
    import app.database as db_mod

    return db_mod.async_session_factory
