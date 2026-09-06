"""organize_service 的集成侧补覆盖测试（第二组）。

与 test_organize_pipeline.py（全链路 happy path）互补：这里集中打缺失的
错误/边界分支——_resolve_manifest 各回退层（torrent 缓存为空清单、拉取
失败、RPC 失败）、_collect_files 清单未命中回退/拒绝、_cleanup_paths 与
卷解析失败、_resolve_downloader 提前返回、规划的并发 IntegrityError 短路、
重建失败保留旧计划、replan 的孤儿通知/单条失败、execute_plan 全部门禁与
执行期异常、任务清理/刷新失败只记日志、classify 的全部错误分支、
schedule_auto_execute 后台失败。

DB 用 conftest 的 db_session（每测试独立 Turso 文件）；外部 RPC 一律 mock。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.models.download_notification import DownloadNotification
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.library import Library
from app.models.organize_audit import OrganizeAuditEntry
from app.models.organize_plan import OrganizePlan
from app.models.organize_plan_op import OrganizePlanOp
from app.models.organize_rule import OrganizeRule
from app.schemas.notification import NotificationPayload
from app.services import organize_service
from app.services.organize_executor import ExecOp
from app.services.organize_planner import PlanError
from app.services.organize_service import (
    OrganizeError,
    _cleanup_paths,
    _collect_files,
    _resolve_downloader,
    _resolve_manifest,
    _scoped_source_dir,
    _touched_path,
    classify_plan,
    execute_plan,
    execute_plans,
    is_plan_executing,
    plan_for_notifications,
    replan_open_plans,
    schedule_auto_execute,
)


def _uuid() -> str:
    return str(uuid.uuid4())


TV_TEMPLATE = (
    "{title}/Season {season:02d}/{title} - s{season:02d}e{episode:02d}{ext}"
)
MOVIE_TEMPLATE = "{category}/{title} ({year})/{title} ({year}){ext}"


# ---------------------------------------------------------------- 造数据


def _mkfile(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _write_torrent(path: Path, files: list[tuple[str, int]], root: str = "root") -> Path:
    """造一个最小 .torrent（多文件清单；单文件用 info/name 形式）。"""
    import bencodepy

    if len(files) == 1:
        name, size = files[0]
        info = {
            b"name": name.encode(), b"length": size,
            b"piece length": 16384, b"pieces": b"x" * 20,
        }
    else:
        info = {
            b"name": root.encode(),
            b"files": [
                {b"length": size, b"path": [p.encode() for p in name.split("/")]}
                for name, size in files
            ],
            b"piece length": 16384, b"pieces": b"x" * 20,
        }
    path.write_bytes(bencodepy.encode({b"info": info}))
    return path


def _series_payload(download_dir, torrent_name=None, files=None, task_id=""):
    payload = {
        "notification_id": "n-1",
        "agent": None,
        "task": {
            "download_task_id": task_id,
            "download_dir": download_dir,
            "torrent_name": torrent_name,
        },
        "resource": {
            "title_raw": "[Group] GITS - 04 [1080p]",
            "season": 1,
            "episode": 4,
            "is_batch": False,
            "episode_start": None,
            "episode_end": None,
            "subtitle_langs": [],
            "resolution": "1080p",
            "container": None,
            "title_year": None,
        },
        "work": {
            "type": "series",
            "series_id": "s-1",
            "title_en": "THE GHOST IN THE SHELL",
            "title_cn": "攻壳机动队",
            "original_title": "攻殻機動隊",
            "year": 2026,
            "content_type": "tv",
            "is_anime": True,
            "collection": None,
            "genre": ["Animation"],
            "season_number": 1,
            "number_of_episodes": 10,
            "episodes": [{"episode": 4, "title": "机器人回旋曲"}],
        },
    }
    if files is not None:
        payload["files"] = files
    return payload


def _movie_payload(download_dir, torrent_name=None, files=None, task_id=""):
    payload = _series_payload(download_dir, torrent_name, files, task_id)
    payload["resource"].update({"season": None, "episode": None})
    payload["work"] = {
        "type": "movie",
        "movie_id": "m-1",
        "title_en": "Hamnet",
        "title_cn": "哈姆奈特",
        "original_title": "Hamnet",
        "year": 2025,
        "content_type": "movie",
        "is_anime": False,
        "collection": None,
        "genre": ["Horror", "Drama"],
        "season_number": None,
        "number_of_episodes": None,
        "episodes": None,
    }
    return payload


async def _seed(db, payload: dict, *, resource_kw=None, task_kw=None):
    """建 Channel/Downloader/FileResource/Task/Notification。"""
    from app.models.channel import Channel

    channel = Channel(
        id=_uuid(), name="ch", type="rss_feed", url="https://example.com/rss",
        fetch_interval=1800, status="active",
        field_mapping={
            "list_locator": {"source": "entries"},
            "field_mappings": {"torrent_url": {"source": "link"}},
        },
        metadata_agent_enabled=False,
    )
    dl = DownloaderInstance(
        id=_uuid(), name="dl", type="transmission",
        url="http://127.0.0.1:9091/transmission/rpc",
        download_dir=payload["task"]["download_dir"] or "/downloads",
        status="disconnected",
    )
    resource_kw_all = dict(
        id=_uuid(), channel_id=channel.id, guid=_uuid(), title_raw="raw",
        torrent_url="magnet:?xt=urn:btih:abc",
    )
    resource_kw_all.update(resource_kw or {})
    resource = FileResource(**resource_kw_all)
    task = DownloadTask(
        id=_uuid(), file_resource_id=resource.id, downloader_id=dl.id,
        download_dir=payload["task"]["download_dir"], status="completed",
        **(task_kw or {}),
    )
    payload["task"]["download_task_id"] = task.id
    notification = DownloadNotification(
        id=_uuid(), agent_id=None, download_task_id=task.id, payload=payload,
    )
    db.add_all([channel, dl, resource, task, notification])
    await db.commit()
    return SimpleNamespace(
        channel=channel, downloader=dl, resource=resource,
        task=task, notification=notification,
    )


async def _make_library(db, root: Path, name="TV", kind="tv", bound=True):
    from app.models.storage_volume import StorageVolume

    lib = Library(id=_uuid(), name=name, kind=kind)
    if bound:
        volume = StorageVolume(
            id=_uuid(), name=f"vol-{lib.id[:8]}", mount_path=str(root)
        )
        db.add(volume)
        lib.volume_id = volume.id
    db.add(lib)
    await db.commit()
    return lib


async def _make_rule(db, library_id, template, *, filter=None, auto_execute=False):
    rule = OrganizeRule(
        id=_uuid(), name="rule", priority=100, enabled=True, filter=filter,
        library_id=library_id, path_template=template,
        file_op="move", auto_execute=auto_execute,
    )
    db.add(rule)
    await db.commit()
    return rule


async def _plans(db):
    return (await db.execute(select(OrganizePlan))).scalars().all()


async def _ops(db, plan_id):
    return (
        await db.execute(
            select(OrganizePlanOp)
            .where(OrganizePlanOp.plan_id == plan_id)
            .order_by(OrganizePlanOp.seq)
        )
    ).scalars().all()


async def _audit_actions(db, plan_id) -> list[str]:
    rows = (
        await db.execute(
            select(OrganizeAuditEntry)
            .where(OrganizeAuditEntry.plan_id == plan_id)
            .order_by(OrganizeAuditEntry.created_at)
        )
    ).scalars().all()
    return [r.action for r in rows]


async def _fk_off_write(db_engine, *statements: str):
    """以未启用 foreign_keys 的新引擎直连同一库文件，造 ORM 下违反 FK 的
    孤儿行（目标行被删/指向不存在记录这类防御分支的唯一稳定造法）。"""
    engine = create_async_engine(str(db_engine.url), echo=False)
    try:
        async with engine.begin() as conn:
            for stmt in statements:
                await conn.execute(text(stmt))
    finally:
        await engine.dispose()


# ---------------------------------------------------------------- 小工具函数


def test_is_plan_executing():
    plan_id = _uuid()
    assert is_plan_executing(plan_id) is False
    organize_service._executing_plan_ids.add(plan_id)
    try:
        assert is_plan_executing(plan_id) is True
    finally:
        organize_service._executing_plan_ids.discard(plan_id)
    assert is_plan_executing(plan_id) is False


def test_touched_path():
    # 无 move 目标 → None（整库刷新）
    assert _touched_path([ExecOp(op_type="keep", src="a", dst=None, size=1)]) is None
    # 公共前缀
    ops = [
        ExecOp(op_type="move", src="s1", dst="/lib/TV/Show/Season 01/a.mkv", size=1),
        ExecOp(op_type="move", src="s2", dst="/lib/TV/Show/Season 01/b.mkv", size=1),
    ]
    assert _touched_path(ops) == "/lib/TV/Show/Season 01"
    # 绝对/相对路径不可比 → ValueError 收敛为 None
    mixed = [
        ExecOp(op_type="move", src="s1", dst="/abs/a.mkv", size=1),
        ExecOp(op_type="move", src="s2", dst="rel/b.mkv", size=1),
    ]
    assert _touched_path(mixed) is None


# ---------------------------------------------------------------- _collect_files / 路径工具（无 DB）


def test_collect_files_volume_resolution_error():
    """下载器绑定了不存在的卷 → 卷解析失败转 PlanError（绝不静默恒等）。"""
    payload = NotificationPayload.model_validate(_series_payload("/downloads"))
    broken = SimpleNamespace(
        volume_id="missing-vol", volume=None, download_dir="/downloads",
        volume_subpath=None, name="dl",
    )
    with pytest.raises(PlanError, match="存储卷"):
        _collect_files(payload, broken)


def test_collect_files_skips_empty_manifest_names(tmp_path):
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "ep04.mkv", 300)
    payload = NotificationPayload.model_validate(
        _series_payload(
            str(dl_dir), files=[{"name": ""}, {"name": "ep04.mkv", "size": 1}]
        )
    )
    files = _collect_files(payload, None)
    assert [f.rel for f in files] == ["ep04.mkv"]
    assert files[0].size == 300  # 真实磁盘大小覆盖清单值


def test_collect_files_manifest_miss_falls_back_to_torrent_dir(tmp_path, caplog):
    """payload.files 全部未命中磁盘 → 回退扫描种子独立目录（绝不扫共享根）。"""
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "Show.S01" / "ep04.mkv", 300)
    payload = NotificationPayload.model_validate(
        _series_payload(
            str(dl_dir), torrent_name="Show.S01",
            files=[{"name": "missing.mkv"}],
        )
    )
    with caplog.at_level(logging.WARNING, logger="app.services.organize_service"):
        files = _collect_files(payload, None)
    assert [f.rel for f in files] == ["ep04.mkv"]
    assert "均未命中" in caplog.text


def test_collect_files_refuses_shared_root_scan(tmp_path):
    """清单未命中且种子独立目录不存在 → PlanError，绝不扫共享下载根。"""
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "other-task.mkv", 100)
    payload = NotificationPayload.model_validate(
        _series_payload(
            str(dl_dir), torrent_name="Nope", files=[{"name": "missing.mkv"}]
        )
    )
    with pytest.raises(PlanError, match="无法定位下载内容"):
        _collect_files(payload, None)


def test_collect_files_empty_scoped_dir(tmp_path):
    dl_dir = tmp_path / "downloads"
    (dl_dir / "Show.S01").mkdir(parents=True)
    payload = NotificationPayload.model_validate(
        _series_payload(str(dl_dir), torrent_name="Show.S01")
    )
    with pytest.raises(PlanError, match="无可整理文件"):
        _collect_files(payload, None)


def test_scoped_source_dir_volume_error_and_missing_dir(tmp_path):
    payload = NotificationPayload.model_validate(
        _series_payload("/downloads", torrent_name="Show.S01")
    )
    broken = SimpleNamespace(
        volume_id="missing-vol", volume=None, download_dir="/downloads",
        volume_subpath=None, name="dl",
    )
    with pytest.raises(PlanError, match="存储卷"):
        _scoped_source_dir(payload, broken)
    # 无独立目录 → None（绝不以共享下载根为 movedir 源）
    assert _scoped_source_dir(payload, None) is None


def test_cleanup_paths_skip_conditions(tmp_path):
    # download_dir 为空 → 跳过清理
    payload = NotificationPayload.model_validate(_series_payload(None))
    assert _cleanup_paths(payload, None) == (None, None)
    # torrent_name 为空（平铺种子）→ 绝不以共享下载根为清理范围
    payload = NotificationPayload.model_validate(_series_payload(str(tmp_path)))
    assert _cleanup_paths(payload, None) == (None, None)
    # 正常：清理范围 = 种子独立目录，保留边界 = 下载根
    payload = NotificationPayload.model_validate(
        _series_payload(str(tmp_path), torrent_name="Show.S01")
    )
    assert _cleanup_paths(payload, None) == (
        str(tmp_path / "Show.S01"), str(tmp_path)
    )


# ---------------------------------------------------------------- _resolve_downloader（假会话打提前返回）


class _FakeDB:
    """仅实现 _resolve_downloader 用到的最小会话接口。"""

    def __init__(self, *, task=None, downloader=None):
        self._task = task
        self._downloader = downloader

    async def get(self, model, pk):
        return self._task

    async def execute(self, stmt):
        return SimpleNamespace(scalar_one_or_none=lambda: self._downloader)


async def test_resolve_downloader_early_returns():
    # payload 无 task → None
    payload = NotificationPayload.model_validate(_series_payload("/d"))
    payload.task = None
    assert await _resolve_downloader(_FakeDB(), payload) is None

    payload = NotificationPayload.model_validate(
        _series_payload("/d", task_id="t-1")
    )
    # task 不存在 → None
    assert await _resolve_downloader(_FakeDB(task=None), payload) is None
    # task 无 downloader_id → None
    task = SimpleNamespace(downloader_id=None)
    assert await _resolve_downloader(_FakeDB(task=task), payload) is None
    # downloader 行不存在 → None
    task = SimpleNamespace(downloader_id="dl-1")
    assert (
        await _resolve_downloader(_FakeDB(task=task, downloader=None), payload)
        is None
    )


async def test_resolve_downloader_returns_snapshot(db_session):
    payload = _series_payload("/downloads")
    seed = await _seed(db_session, payload)
    snapshot = await _resolve_downloader(
        db_session, NotificationPayload.model_validate(payload)
    )
    assert snapshot is not None
    assert snapshot.id == seed.downloader.id
    assert snapshot.volume is None  # 未绑卷


# ---------------------------------------------------------------- _resolve_manifest


async def test_resolve_manifest_task_row_missing(db_session, tmp_path):
    """download_task_id 指向不存在任务 → None。"""
    payload = NotificationPayload.model_validate(
        _series_payload(str(tmp_path), task_id="missing-task")
    )
    assert await _resolve_manifest(db_session, payload) is None


async def test_resolve_manifest_empty_torrent_listing(db_session, tmp_path):
    """torrent 缓存存在但清单为空 → 该来源放弃，无其他来源 → None。"""
    payload = _series_payload(str(tmp_path / "downloads"))
    seed = await _seed(db_session, payload)
    torrent = _write_torrent(tmp_path / "empty.torrent", [], root="root")
    seed.resource.torrent_file = str(torrent)
    await db_session.commit()
    assert (
        await _resolve_manifest(
            db_session, NotificationPayload.model_validate(payload)
        )
        is None
    )


async def test_resolve_manifest_fetch_failure_falls_back_to_rpc(
    db_session, tmp_path, monkeypatch
):
    """torrent_url 拉取失败 → 回退下载器 RPC 清单；不安全条目被过滤。"""
    payload = _series_payload(str(tmp_path / "downloads"))
    seed = await _seed(
        db_session, payload,
        resource_kw={"torrent_url": "http://example.com/x.torrent"},
        task_kw={"transmission_torrent_id": 42},
    )
    fetch = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr("app.services.torrent_inspect.fetch_torrent_file", fetch)
    client = SimpleNamespace(
        get_torrent_files=AsyncMock(return_value={
            "files": [
                {"name": "ep04.mkv", "size": 100},
                {"name": "", "size": 1},          # 空名剔除
                {"name": "/abs/evil.mkv", "size": 1},  # 绝对路径剔除
                {"name": "../up.mkv", "size": 1},      # .. 分量剔除
            ]
        })
    )
    factory = lambda d: client  # noqa: E731
    monkeypatch.setattr("app.clients.downloader.get_downloader_client", factory)

    manifest = await _resolve_manifest(
        db_session, NotificationPayload.model_validate(payload)
    )
    assert manifest == [{"name": "ep04.mkv", "size": 100}]
    fetch.assert_awaited_once()
    # 拉取失败不写回 torrent_file 缓存
    await db_session.refresh(seed.resource)
    assert seed.resource.torrent_file is None


async def test_resolve_manifest_rpc_failure_returns_none(
    db_session, tmp_path, monkeypatch
):
    """全部来源不可用（缓存无、URL 非 http、RPC 异常）→ None。"""
    payload = _series_payload(str(tmp_path / "downloads"))
    await _seed(db_session, payload, task_kw={"transmission_torrent_id": 42})
    client = SimpleNamespace(
        get_torrent_files=AsyncMock(side_effect=RuntimeError("rpc down"))
    )
    monkeypatch.setattr(
        "app.clients.downloader.get_downloader_client", lambda d: client
    )
    assert (
        await _resolve_manifest(
            db_session, NotificationPayload.model_validate(payload)
        )
        is None
    )


# ---------------------------------------------------------------- 规划层分支


async def test_plan_for_notifications_empty_and_no_rules(db_session, tmp_path):
    stats = await plan_for_notifications(db_session, [])
    assert stats == {
        "planned": 0, "rebuilt": 0, "uncategorized": 0, "skipped": 0, "failed": 0,
    }
    # 无任何 enabled 规则 → 整步跳过
    payload = _series_payload(str(tmp_path), files=[{"name": "ep04.mkv"}])
    seed = await _seed(db_session, payload)
    stats = await plan_for_notifications(db_session, [seed.notification])
    assert stats["planned"] == 0 and stats["skipped"] == 0
    assert await _plans(db_session) == []


async def test_plan_one_unexpected_error_counts_failed(
    db_session, tmp_path, monkeypatch
):
    """单条通知规划抛非预期异常只记 failed，不炸整批、不落计划行。"""
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(db_session, lib.id, TV_TEMPLATE)
    payload = _series_payload(str(dl_dir), files=[{"name": "ep04.mkv"}])
    seed = await _seed(db_session, payload)

    def _boom(*a, **kw):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(organize_service, "_collect_and_plan", _boom)
    stats = await plan_for_notifications(db_session, [seed.notification])
    assert stats["failed"] == 1
    assert await _plans(db_session) == []  # 非预期异常不落计划行


async def test_existing_done_or_running_plan_skipped(db_session, tmp_path):
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(db_session, lib.id, TV_TEMPLATE)
    payload = _series_payload(str(dl_dir), files=[{"name": "ep04.mkv"}])
    seed = await _seed(db_session, payload)

    for status in ("done", "running"):
        db_session.add(OrganizePlan(
            id=_uuid(), notification_id=seed.notification.id, status=status,
            payload=payload,
        ))
        await db_session.commit()
        stats = await plan_for_notifications(db_session, [seed.notification])
        assert stats == {
            "planned": 0, "rebuilt": 0, "uncategorized": 0,
            "skipped": 1, "failed": 0,
        }
        await db_session.delete((await _plans(db_session))[0])
        await db_session.commit()


async def test_plan_idempotent_same_payload(db_session, tmp_path):
    """pending 计划且快照未变 → 短路 skipped，不重复规划。"""
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(db_session, lib.id, TV_TEMPLATE)
    payload = _series_payload(str(dl_dir), files=[{"name": "ep04.mkv"}])
    seed = await _seed(db_session, payload)
    assert (await plan_for_notifications(db_session, [seed.notification]))["planned"] == 1
    stats = await plan_for_notifications(db_session, [seed.notification])
    assert stats["skipped"] == 1
    assert len(await _plans(db_session)) == 1


async def test_plan_failure_lands_failed_plan(db_session, tmp_path):
    """确定性拒绝（种子目录无文件）→ 落 failed 计划行 + plan_failed 审计。"""
    dl_dir = tmp_path / "downloads"
    (dl_dir / "Show.S01").mkdir(parents=True)
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(db_session, lib.id, TV_TEMPLATE)
    seed = await _seed(
        db_session, _series_payload(str(dl_dir), torrent_name="Show.S01")
    )
    stats = await plan_for_notifications(db_session, [seed.notification])
    assert stats["failed"] == 1
    [plan] = await _plans(db_session)
    assert plan.status == "failed"
    assert plan.error_message
    assert "plan_failed" in await _audit_actions(db_session, plan.id)


async def test_plan_insert_integrity_race_returns_skipped(
    db_session, tmp_path, monkeypatch
):
    """并发规划竞争：输掉 notification_id 唯一约束 → skipped，不落重复行。"""
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(db_session, lib.id, TV_TEMPLATE)
    payload = _series_payload(str(dl_dir), files=[{"name": "ep04.mkv"}])
    seed = await _seed(db_session, payload)

    async def _dup_flush(self, *a, **kw):
        raise IntegrityError("INSERT", {}, Exception("unique constraint"))

    monkeypatch.setattr(AsyncSession, "flush", _dup_flush)
    stats = await plan_for_notifications(db_session, [seed.notification])
    assert stats["skipped"] == 1
    monkeypatch.undo()
    assert await _plans(db_session) == []


async def test_failed_plan_insert_integrity_race_returns_skipped(
    db_session, tmp_path, monkeypatch
):
    """failed 计划落库同样吸收唯一约束竞争。"""
    dl_dir = tmp_path / "downloads"
    (dl_dir / "Show.S01").mkdir(parents=True)
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(db_session, lib.id, TV_TEMPLATE)
    seed = await _seed(
        db_session, _series_payload(str(dl_dir), torrent_name="Show.S01")
    )

    async def _dup_flush(self, *a, **kw):
        raise IntegrityError("INSERT", {}, Exception("unique constraint"))

    monkeypatch.setattr(AsyncSession, "flush", _dup_flush)
    stats = await plan_for_notifications(db_session, [seed.notification])
    assert stats["skipped"] == 1
    monkeypatch.undo()
    assert await _plans(db_session) == []


async def test_plan_manifest_fallback_via_torrent_cache(db_session, tmp_path):
    """快照缺 files → 回退 resource.torrent_file 清单做精确匹配（473-475）。"""
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "Hamnet.2025.1080p.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib", name="Movies", kind="movie")
    await _make_rule(db_session, lib.id, "{title} ({year})/{title} ({year}){ext}")
    payload = _movie_payload(str(dl_dir))
    seed = await _seed(db_session, payload)
    seed.resource.torrent_file = str(
        _write_torrent(tmp_path / "r.torrent", [("Hamnet.2025.1080p.mkv", 300)])
    )
    await db_session.commit()

    stats = await plan_for_notifications(db_session, [seed.notification])
    assert stats["planned"] == 1
    [plan] = await _plans(db_session)
    [op] = await _ops(db_session, plan.id)
    assert op.src == str(dl_dir / "Hamnet.2025.1080p.mkv")


# ---------------------------------------------------------------- 重建分支


async def _planned_plan(db_session, tmp_path):
    dl_dir = tmp_path / "downloads"
    src = _mkfile(dl_dir / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib")
    rule = await _make_rule(db_session, lib.id, TV_TEMPLATE)
    payload = _series_payload(str(dl_dir), files=[{"name": "ep04.mkv"}])
    seed = await _seed(db_session, payload)
    await plan_for_notifications(db_session, [seed.notification])
    [plan] = await _plans(db_session)
    return SimpleNamespace(
        plan=plan, seed=seed, lib=lib, rule=rule, dl_dir=dl_dir, src=src,
    )


async def test_rebuild_on_payload_change(db_session, tmp_path):
    """快照 regenerate（与冻结快照不一致）→ pending 计划重建。"""
    ctx = await _planned_plan(db_session, tmp_path)
    new_payload = {**ctx.seed.notification.payload}
    new_payload["work"] = {**new_payload["work"], "title_cn": "攻壳机动队 新"}
    ctx.seed.notification.payload = new_payload
    await db_session.commit()

    stats = await plan_for_notifications(db_session, [ctx.seed.notification])
    assert stats["rebuilt"] == 1
    [plan] = await _plans(db_session)
    assert plan.status == "pending"
    assert plan.payload["work"]["title_cn"] == "攻壳机动队 新"
    [op] = await _ops(db_session, plan.id)
    assert "攻壳机动队 新" in op.dst
    assert "plan_rebuilt" in await _audit_actions(db_session, plan.id)


async def test_rebuild_failure_keeps_old_plan(db_session, tmp_path):
    """重建被确定性拒绝（文件消失）→ 旧计划原样保留，下 tick 可重试。"""
    ctx = await _planned_plan(db_session, tmp_path)
    ctx.src.unlink()
    new_payload = {**ctx.seed.notification.payload}
    new_payload["work"] = {**new_payload["work"], "title_cn": "变了"}
    ctx.seed.notification.payload = new_payload
    await db_session.commit()

    stats = await plan_for_notifications(db_session, [ctx.seed.notification])
    assert stats["failed"] == 1
    [plan] = await _plans(db_session)
    assert plan.status == "pending"  # 保留旧计划
    assert plan.payload["work"]["title_cn"] == "攻壳机动队"
    assert len(await _ops(db_session, plan.id)) == 1


async def test_rebuild_uses_manifest_when_files_missing(db_session, tmp_path):
    """重建时快照仍无 files → 走 torrent 清单回退（561-563）。"""
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "Hamnet.2025.1080p.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib", name="Movies", kind="movie")
    await _make_rule(db_session, lib.id, "{title} ({year})/{title} ({year}){ext}")
    payload = _movie_payload(str(dl_dir))
    seed = await _seed(db_session, payload)
    seed.resource.torrent_file = str(
        _write_torrent(tmp_path / "r.torrent", [("Hamnet.2025.1080p.mkv", 300)])
    )
    await db_session.commit()
    assert (await plan_for_notifications(db_session, [seed.notification]))["planned"] == 1

    new_payload = {**seed.notification.payload}
    new_payload["work"] = {**new_payload["work"], "title_cn": "哈姆奈特 导演剪辑版"}
    seed.notification.payload = new_payload
    await db_session.commit()
    stats = await plan_for_notifications(db_session, [seed.notification])
    assert stats["rebuilt"] == 1
    [plan] = await _plans(db_session)
    [op] = await _ops(db_session, plan.id)
    assert "哈姆奈特 导演剪辑版" in op.dst


async def test_rebuild_manual_library_uses_synthetic_rule(db_session, tmp_path):
    """人工分类过的计划（rule_id 为 null、library 已指定）重建时绕开规则
    匹配、以合成规则直指该库重渲染（567）。"""
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib")
    # 永不命中的规则 → 待分类计划
    await _make_rule(
        db_session, lib.id, TV_TEMPLATE,
        filter={"field": "series.is_anime", "operator": "eq", "value": False},
    )
    payload = _series_payload(str(dl_dir), files=[{"name": "ep04.mkv"}])
    seed = await _seed(db_session, payload)
    stats = await plan_for_notifications(db_session, [seed.notification])
    assert stats["uncategorized"] == 1
    [plan] = await _plans(db_session)
    await classify_plan(db_session, plan.id, lib.id)
    await db_session.refresh(plan)
    assert plan.library_id == lib.id and plan.rule_id is None

    new_payload = {**seed.notification.payload}
    new_payload["work"] = {**new_payload["work"], "title_cn": "攻壳机动队 新"}
    seed.notification.payload = new_payload
    await db_session.commit()
    stats = await plan_for_notifications(db_session, [seed.notification])
    assert stats["rebuilt"] == 1
    [plan] = await _plans(db_session)
    assert plan.library_id == lib.id and plan.rule_id is None
    [op] = await _ops(db_session, plan.id)
    assert "攻壳机动队 新" in op.dst


# ---------------------------------------------------------------- replan_open_plans 分支


async def test_replan_open_plans_early_returns(db_session, tmp_path):
    # 无 enabled 规则 → 整步跳过
    stats = await replan_open_plans(db_session, reason="测试")
    assert stats == {"rebuilt": 0, "failed": 0}
    # 有规则但无未执行计划 → 整步跳过
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(db_session, lib.id, TV_TEMPLATE)
    stats = await replan_open_plans(db_session, reason="测试")
    assert stats == {"rebuilt": 0, "failed": 0}


async def test_replan_skips_plan_with_missing_notification(
    db_session, db_engine, tmp_path
):
    """通知行已不存在（孤儿计划，仅 FK 未生效的存量库可能出现）→ 跳过。"""
    ctx = await _planned_plan(db_session, tmp_path)
    await _fk_off_write(
        db_engine,
        f"DELETE FROM download_notifications WHERE id = '{ctx.seed.notification.id}'",
    )
    stats = await replan_open_plans(db_session, reason="测试")
    assert stats == {"rebuilt": 0, "failed": 0}
    assert len(await _plans(db_session)) == 1  # 计划原样保留


async def test_replan_single_failure_isolated(db_session, tmp_path, monkeypatch):
    """单条计划重建抛非预期异常 → 记 failed，不影响其余计划。"""
    ctx = await _planned_plan(db_session, tmp_path)
    payload2 = _series_payload(str(ctx.dl_dir), files=[{"name": "ep04.mkv"}])
    seed2 = await _seed(db_session, payload2)
    db_session.add(OrganizePlan(
        id=_uuid(), notification_id=seed2.notification.id,
        status="pending", payload=payload2,
    ))
    await db_session.commit()

    original = organize_service._rebuild_plan

    async def _flaky(db, plan, notification, rules, libraries):
        if notification.id == seed2.notification.id:
            raise RuntimeError("unexpected")
        return await original(db, plan, notification, rules, libraries)

    monkeypatch.setattr(organize_service, "_rebuild_plan", _flaky)
    stats = await replan_open_plans(db_session, reason="测试")
    assert stats == {"rebuilt": 1, "failed": 1}
    await db_session.refresh(ctx.plan)
    assert ctx.plan.status == "pending"


# ---------------------------------------------------------------- schedule_auto_execute


async def test_schedule_auto_execute_background_failure_logged(db_session, caplog):
    """后台执行异常（计划不存在）只记日志，不抛出。"""
    with caplog.at_level(logging.ERROR, logger="app.services.organize_service"):
        schedule_auto_execute("missing-plan-id")
        for _ in range(100):
            if any(
                "自动执行计划" in r.getMessage() for r in caplog.records
            ):
                break
            await asyncio.sleep(0.05)
    assert any(
        "自动执行计划" in r.getMessage() and "missing-plan-id" in r.getMessage()
        for r in caplog.records
    )


# ---------------------------------------------------------------- execute_plan 门禁


async def test_execute_plan_state_guards(db_session, tmp_path):
    ctx = await _planned_plan(db_session, tmp_path)

    with pytest.raises(OrganizeError, match="不存在"):
        await execute_plan(db_session, "missing-id")

    # running 且本进程正在执行 → 拒绝
    ctx.plan.status = "running"
    await db_session.commit()
    organize_service._executing_plan_ids.add(ctx.plan.id)
    try:
        with pytest.raises(OrganizeError, match="正在执行中"):
            await execute_plan(db_session, ctx.plan.id)
    finally:
        organize_service._executing_plan_ids.discard(ctx.plan.id)

    # cancelled → 拒绝
    ctx.plan.status = "cancelled"
    await db_session.commit()
    with pytest.raises(OrganizeError, match="已取消"):
        await execute_plan(db_session, ctx.plan.id)


async def test_execute_plan_library_row_missing(db_session, db_engine, tmp_path):
    """plan.library_id 指向已不存在的库（孤儿引用）→ 拒绝执行。"""
    ctx = await _planned_plan(db_session, tmp_path)
    plan_id = ctx.plan.id
    await _fk_off_write(
        db_engine,
        f"UPDATE organize_plans SET library_id = 'gone-lib' "
        f"WHERE id = '{plan_id}'",
    )
    db_session.expire_all()
    with pytest.raises(OrganizeError, match="目标库不存在"):
        await execute_plan(db_session, plan_id)


async def test_execute_plan_unbound_library_rejected(db_session, tmp_path):
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib", bound=False)
    await _make_rule(db_session, lib.id, TV_TEMPLATE)
    payload = _series_payload(str(dl_dir), files=[{"name": "ep04.mkv"}])
    seed = await _seed(db_session, payload)
    await plan_for_notifications(db_session, [seed.notification])
    [plan] = await _plans(db_session)
    assert plan.library_id == lib.id
    with pytest.raises(OrganizeError, match="待绑定"):
        await execute_plan(db_session, plan.id)


async def test_execute_plan_cleanup_volume_error(db_session, db_engine, tmp_path):
    """下载器卷绑定损坏 → 清理范围解析失败转 OrganizeError，计划不卡在 running。"""
    ctx = await _planned_plan(db_session, tmp_path)
    plan_id = ctx.plan.id
    await _fk_off_write(
        db_engine,
        f"UPDATE downloader_instances SET volume_id = 'gone-vol' "
        f"WHERE id = '{ctx.seed.downloader.id}'",
    )
    db_session.expire_all()
    with pytest.raises(OrganizeError, match="存储卷"):
        await execute_plan(db_session, plan_id)
    plan = (await db_session.execute(
        select(OrganizePlan).where(OrganizePlan.id == plan_id)
    )).scalar_one()
    assert plan.status == "pending"  # 门禁失败不改变状态


async def test_execute_plan_internal_error_marks_failed(
    db_session, tmp_path, monkeypatch
):
    """执行段未预期异常 → 计划落 failed 可重试，绝不卡在 running。"""
    ctx = await _planned_plan(db_session, tmp_path)

    def _boom(*a, **kw):
        raise OSError("disk exploded")

    monkeypatch.setattr(organize_service, "run_execution", _boom)
    with pytest.raises(OrganizeError, match="内部错误"):
        await execute_plan(db_session, ctx.plan.id)
    await db_session.refresh(ctx.plan)
    assert ctx.plan.status == "failed"
    assert "内部错误" in ctx.plan.error_message
    assert not is_plan_executing(ctx.plan.id)  # finally 清理


async def test_execute_plan_conflict_marks_failed(db_session, tmp_path):
    """前置门禁冲突（目标已存在且大小不符）→ outcome 不 ok → failed。"""
    ctx = await _planned_plan(db_session, tmp_path)
    [op] = await _ops(db_session, ctx.plan.id)
    _mkfile(Path(op.dst), 999)
    plan = await execute_plan(db_session, ctx.plan.id)
    assert plan.status == "failed"
    assert plan.error_message
    assert Path(op.src).exists()  # 整体放弃，源未动
    actions = await _audit_actions(db_session, plan.id)
    assert "execute" in actions


async def test_execute_plans_batch_isolation(db_session, tmp_path):
    ctx = await _planned_plan(db_session, tmp_path)
    results = await execute_plans(db_session, ["missing-id", ctx.plan.id])
    assert results[0][0] == "missing-id" and "不存在" in results[0][1]
    assert results[1] == (ctx.plan.id, "done")
    # done 计划幂等短路：重复执行直接返回，不产生副作用
    again = await execute_plan(db_session, ctx.plan.id)
    assert again.status == "done"


# ---------------------------------------------------------------- 执行后清理/刷新失败只记日志


async def test_execute_done_but_cleanup_returns_false(
    db_session, tmp_path, monkeypatch, caplog
):
    """任务清理返回失败 → 只记日志，计划仍 done。"""
    ctx = await _planned_plan(db_session, tmp_path)
    cleanup = AsyncMock(return_value=False)
    monkeypatch.setattr(organize_service, "delete_task_after_organize", cleanup)
    with caplog.at_level(logging.ERROR, logger="app.services.organize_service"):
        plan = await execute_plan(db_session, ctx.plan.id)
    assert plan.status == "done"
    cleanup.assert_awaited_once()
    assert any("任务清理" in r.getMessage() for r in caplog.records)


async def test_execute_done_but_cleanup_and_refresh_raise(
    db_session, tmp_path, monkeypatch
):
    """任务清理异常 + 媒体服务器刷新异常均只记日志，不改写 done。"""
    ctx = await _planned_plan(db_session, tmp_path)
    cleanup = AsyncMock(side_effect=RuntimeError("rpc down"))
    refresh = AsyncMock(side_effect=RuntimeError("plex down"))
    monkeypatch.setattr(organize_service, "delete_task_after_organize", cleanup)
    monkeypatch.setattr(organize_service, "refresh_library", refresh)
    from app.models.media_server import MediaServerInstance

    server = MediaServerInstance(
        id=_uuid(), name="plex", type="plex",
        url="http://plex:32400", token="tok",
    )
    db_session.add(server)
    lib = await db_session.get(Library, ctx.lib.id)
    lib.media_server_id = server.id
    lib.section_key = "2"
    await db_session.commit()

    plan = await execute_plan(db_session, ctx.plan.id)
    assert plan.status == "done"
    cleanup.assert_awaited_once()
    refresh.assert_awaited_once()


# ---------------------------------------------------------------- classify_plan 分支


async def test_classify_plan_guards(db_session, tmp_path):
    ctx = await _planned_plan(db_session, tmp_path)
    with pytest.raises(OrganizeError, match="不存在"):
        await classify_plan(db_session, "missing-id", ctx.lib.id)
    with pytest.raises(OrganizeError, match="目标库不存在"):
        await classify_plan(db_session, ctx.plan.id, "missing-lib")

    ctx.plan.status = "done"
    await db_session.commit()
    with pytest.raises(OrganizeError, match="不可分类"):
        await classify_plan(db_session, ctx.plan.id, ctx.lib.id)


async def test_classify_uncategorized_uses_manifest_fallback(db_session, tmp_path):
    """待分类计划（无 ops）+ 快照无 files → classify 走 torrent 清单回退
    定位磁盘文件（933-935）。"""
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(
        db_session, lib.id, TV_TEMPLATE,
        filter={"field": "series.is_anime", "operator": "eq", "value": False},
    )
    payload = _series_payload(str(dl_dir))  # 无 files
    seed = await _seed(db_session, payload)
    seed.resource.torrent_file = str(
        _write_torrent(tmp_path / "r.torrent", [("ep04.mkv", 300)])
    )
    await db_session.commit()
    stats = await plan_for_notifications(db_session, [seed.notification])
    assert stats["uncategorized"] == 1
    [plan] = await _plans(db_session)
    assert await _ops(db_session, plan.id) == []

    plan = await classify_plan(db_session, plan.id, lib.id)
    assert plan.library_id == lib.id
    [op] = await _ops(db_session, plan.id)
    assert op.src == str(dl_dir / "ep04.mkv")
    assert "Season 01" in op.dst  # 预设 TV 模板渲染

    # 再次分类到另一个库：以现有 op 为磁盘清单重渲染，旧 op 被删除重建，
    # src/size 保持不变。
    lib2 = await _make_library(db_session, tmp_path / "lib2", name="TV2")
    plan = await classify_plan(db_session, plan.id, lib2.id)
    assert plan.library_id == lib2.id
    [op2] = await _ops(db_session, plan.id)
    assert op2.src == str(dl_dir / "ep04.mkv")
    assert op2.size == 300
    assert op2.dst.startswith(str(tmp_path / "lib2"))


async def test_classify_unlocatable_files_rejected(db_session, tmp_path):
    """无 ops、无 files、无任何清单来源 → 无法定位磁盘文件。"""
    dl_dir = tmp_path / "downloads"
    dl_dir.mkdir(parents=True)
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(
        db_session, lib.id, TV_TEMPLATE,
        filter={"field": "series.is_anime", "operator": "eq", "value": False},
    )
    payload = _series_payload(str(dl_dir), torrent_name="Nope")
    seed = await _seed(db_session, payload)
    await plan_for_notifications(db_session, [seed.notification])
    [plan] = await _plans(db_session)

    with pytest.raises(OrganizeError, match="无法定位磁盘文件"):
        await classify_plan(db_session, plan.id, lib.id)


async def test_classify_rerender_failure(db_session, tmp_path):
    """重渲染被确定性拒绝（合集缺集）→ OrganizeError，计划保留。"""
    dl_dir = tmp_path / "downloads"
    torrent_dir = dl_dir / "Show.S01"
    _mkfile(torrent_dir / "Show.S01E01.mkv", 200)
    _mkfile(torrent_dir / "Show.S01E02.mkv", 200)  # 缺 E03
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(db_session, lib.id, TV_TEMPLATE)
    payload = _series_payload(
        str(dl_dir), torrent_name="Show.S01",
        files=[{"name": "Show.S01E01.mkv"}, {"name": "Show.S01E02.mkv"}],
    )
    payload["resource"].update(
        {"is_batch": True, "episode": None, "episode_start": 1, "episode_end": 3}
    )
    seed = await _seed(db_session, payload)
    stats = await plan_for_notifications(db_session, [seed.notification])
    assert stats["failed"] == 1  # 覆盖度不足 → failed 计划
    [plan] = await _plans(db_session)
    assert plan.status == "failed"

    with pytest.raises(OrganizeError, match="重渲染失败"):
        await classify_plan(db_session, plan.id, lib.id)
    await db_session.refresh(plan)
    assert plan.status == "failed"  # 重渲染失败不改变状态


async def test_classify_needs_category_rejected(db_session, tmp_path):
    """模板含 {category} 且无法推导类别 → classify 必须同时指定类别。"""
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "hamnet.mkv", 500)
    lib = await _make_library(db_session, tmp_path / "movies", name="Movies", kind="movie")
    await _make_rule(db_session, lib.id, MOVIE_TEMPLATE)
    payload = _movie_payload(str(dl_dir), files=[{"name": "hamnet.mkv"}])
    payload["work"]["genre"] = []  # 无 genre → 类别无从推导
    seed = await _seed(db_session, payload)
    stats = await plan_for_notifications(db_session, [seed.notification])
    assert stats["planned"] == 1
    [plan] = await _plans(db_session)
    assert plan.category is None

    with pytest.raises(OrganizeError, match=r"\{category\}"):
        await classify_plan(db_session, plan.id, lib.id, category=None)
    # 指定类别后正常分类
    plan = await classify_plan(db_session, plan.id, lib.id, category="Horror")
    assert plan.category == "Horror"
    [op] = await _ops(db_session, plan.id)
    assert "/Horror/" in op.dst
