"""整理服务（organize_service）单元测试。

覆盖：notification→计划落库与幂等、regenerate 后 pending 计划重建（保留
人工 category）、done 短路、auto_execute 触发、待分类→classify→重渲染、
规划失败不落计划下 tick 重试、执行全链路
（移动/清理/审计）、冲突失败、待绑定计划（无 ops + 执行拒绝）、媒体服务器
刷新/任务清理失败不影响 done 状态、file_op=hardlink/copy 保种链路（源文件
保留、任务不删、恢复做种）。
DB 用 tests/unit/conftest.py 的 db_session；文件树用 tmp_path 真实目录。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.download_notification import DownloadNotification
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.library import Library
from app.models.media_server import MediaServerInstance
from app.models.organize_audit import OrganizeAuditEntry
from app.models.organize_plan import OrganizePlan
from app.models.organize_plan_op import OrganizePlanOp
from app.models.organize_rule import OrganizeRule
from app.services import organize_service
from app.services.organize_service import (
    OrganizeError,
    classify_plan,
    execute_plan,
    execute_plans,
    plan_for_notifications,
    replan_open_plans,
)


def _uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------- 造数据


def _series_payload(download_dir: str, torrent_name=None, files=None, task_id=""):
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
            "seasons": [{"season_number": 1, "episode_count": 10}],
            "episodes": [{"season": 1, "episode": 4, "title": "机器人回旋曲"}],
        },
    }
    if files is not None:
        payload["files"] = files
    return payload


def _movie_payload(download_dir: str, torrent_name=None, files=None, task_id=""):
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
        "seasons": None,
        "episodes": None,
    }
    return payload


async def _seed(db, payload: dict, *, volume=None, downloader_dir=None):
    """建 Channel/Downloader/FileResource/Task/Notification；返回 notification。

    ``volume``：StorageVolume（ORM），非空时绑定到 Downloader（卷绑定路径
    解析取代 P1 的 path_map）。
    """
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
        download_dir=downloader_dir or payload["task"]["download_dir"],
        volume_id=volume.id if volume is not None else None,
        status="disconnected",
    )
    resource = FileResource(
        id=_uuid(), channel_id=channel.id, guid=_uuid(), title_raw="raw",
        torrent_url="magnet:?xt=urn:btih:abc",
    )
    task = DownloadTask(
        id=_uuid(), file_resource_id=resource.id, downloader_id=dl.id,
        download_dir=payload["task"]["download_dir"], status="completed",
    )
    payload["task"]["download_task_id"] = task.id
    notification = DownloadNotification(
        id=_uuid(), agent_id=None, download_task_id=task.id, payload=payload,
    )
    db.add_all([channel, dl, resource, task, notification])
    await db.commit()
    return notification


async def _make_volume(db, mount_path: Path, name="vol"):
    from app.models.storage_volume import StorageVolume

    volume = StorageVolume(id=_uuid(), name=name, mount_path=str(mount_path))
    db.add(volume)
    await db.commit()
    return volume


async def _make_library(
    db, root: Path, name="TV", kind="tv", server=None, section_key=None,
    bound=True,
):
    """卷绑定形态的 Library（R2）：root 落为 StorageVolume.mount_path，
    Library 存 (volume_id, root_subpath)；bound=False 造待绑定行。"""
    from app.models.storage_volume import StorageVolume

    lib = Library(
        id=_uuid(), name=name, kind=kind,
        media_server_id=server.id if server is not None else None,
        section_key=section_key,
    )
    if bound:
        volume = StorageVolume(
            id=_uuid(), name=f"vol-{lib.id[:8]}", mount_path=str(root)
        )
        db.add(volume)
        lib.volume_id = volume.id
    db.add(lib)
    await db.commit()
    return lib


async def _make_rule(
    db, library_id, template, *, filter=None, auto_execute=False, priority=100,
    file_op="move",
):
    rule = OrganizeRule(
        id=_uuid(), name=f"rule-{template[:8]}", priority=priority, enabled=True,
        filter=filter, library_id=library_id, path_template=template,
        file_op=file_op, auto_execute=auto_execute,
    )
    db.add(rule)
    await db.commit()
    return rule


async def _plans(db):
    return (await db.execute(select(OrganizePlan))).scalars().all()


async def _audits(db, plan_id):
    return (
        await db.execute(
            select(OrganizeAuditEntry)
            .where(OrganizeAuditEntry.plan_id == plan_id)
            .order_by(OrganizeAuditEntry.created_at)
        )
    ).scalars().all()


TV_TEMPLATE = "{title}/Season {season:02d}/{title} - s{season:02d}e{episode:02d}{ext}"
MOVIE_TEMPLATE = "{category}/{title} ({year})/{title} ({year}){ext}"


# ---------------------------------------------------------------- 规划落库


async def test_plan_created_from_notification(db_session, tmp_path):
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(
        db_session, lib.id, TV_TEMPLATE,
        filter={"field": "series.is_anime", "operator": "eq", "value": False},
    )
    await _make_rule(db_session, lib.id, TV_TEMPLATE)
    payload = _series_payload(str(dl_dir), files=[{"name": "ep04.mkv"}])
    notification = await _seed(db_session, payload)

    stats = await plan_for_notifications(db_session, [notification])
    assert stats["planned"] == 1
    [plan] = await _plans(db_session)
    assert plan.status == "pending"
    assert plan.library_id == lib.id
    assert plan.rule_id is not None
    ops = (
        await db_session.execute(
            select(OrganizePlanOp).where(OrganizePlanOp.plan_id == plan.id)
        )
    ).scalars().all()
    assert len(ops) == 1
    assert ops[0].op_type == "move"
    assert ops[0].src == str(dl_dir / "ep04.mkv")
    assert ops[0].size == 300
    assert "Season 01" in ops[0].dst and ops[0].dst.endswith(".mkv")
    assert ops[0].seq == 0
    actions = [a.action for a in await _audits(db_session, plan.id)]
    assert "plan_created" in actions


async def test_plan_idempotent_on_repeat(db_session, tmp_path):
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(db_session, lib.id, TV_TEMPLATE)
    notification = await _seed(
        db_session, _series_payload(str(dl_dir), files=[{"name": "ep04.mkv"}])
    )
    await plan_for_notifications(db_session, [notification])
    stats = await plan_for_notifications(db_session, [notification])
    assert stats["skipped"] == 1 and stats["planned"] == 0
    assert len(await _plans(db_session)) == 1


async def test_volume_binding_translation(db_session, tmp_path):
    """daemon 视角 /downloads 经卷绑定解析到本进程视角目录。"""
    local_dir = tmp_path / "dl"
    _mkfile(local_dir / "Show.S01" / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(db_session, lib.id, TV_TEMPLATE)
    volume = await _make_volume(db_session, local_dir)
    payload = _series_payload("/downloads", torrent_name="Show.S01")
    notification = await _seed(
        db_session, payload, volume=volume, downloader_dir="/downloads",
    )
    stats = await plan_for_notifications(db_session, [notification])
    assert stats["planned"] == 1
    [plan] = await _plans(db_session)
    [op] = (
        await db_session.execute(
            select(OrganizePlanOp).where(OrganizePlanOp.plan_id == plan.id)
        )
    ).scalars().all()
    assert op.src.startswith(str(local_dir))


async def test_plan_failure_lands_failed_plan_and_recovers(db_session, tmp_path):
    """规划被确定性拒绝 → 落 failed 计划行（界面可见）；外部条件修复后
    重新触发规划（快照未变也重建 failed 计划）可恢复为 pending。"""
    dl_dir = tmp_path / "downloads"
    (dl_dir / "Show.S01").mkdir(parents=True)  # 空种子目录 → 规划失败
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(db_session, lib.id, TV_TEMPLATE)
    notification = await _seed(
        db_session, _series_payload(str(dl_dir), torrent_name="Show.S01")
    )
    stats = await plan_for_notifications(db_session, [notification])
    assert stats["failed"] == 1
    [plan] = await _plans(db_session)
    assert plan.status == "failed"
    assert plan.error_message
    assert plan.notification_id == notification.id
    actions = [a.action for a in await _audits(db_session, plan.id)]
    assert "plan_failed" in actions

    _mkfile(dl_dir / "Show.S01" / "ep04.mkv", 300)
    stats = await plan_for_notifications(db_session, [notification])
    assert stats["rebuilt"] == 1
    [plan] = await _plans(db_session)
    assert plan.status == "pending"
    assert plan.error_message is None
    assert plan.library_id == lib.id


async def test_no_matching_rule_creates_uncategorized_plan(db_session, tmp_path):
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(
        db_session, lib.id, TV_TEMPLATE,
        filter={"field": "series.is_anime", "operator": "eq", "value": False},
    )
    notification = await _seed(
        db_session, _series_payload(str(dl_dir), files=[{"name": "ep04.mkv"}])
    )
    stats = await plan_for_notifications(db_session, [notification])
    assert stats["uncategorized"] == 1
    [plan] = await _plans(db_session)
    assert plan.library_id is None and plan.rule_id is None
    assert plan.status == "pending"


async def test_plan_uses_first_genre_as_category(db_session, tmp_path):
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "hamnet.mkv", 500)
    lib = await _make_library(db_session, tmp_path / "movies", name="Movies", kind="movie")
    rule = await _make_rule(db_session, lib.id, MOVIE_TEMPLATE)
    notification = await _seed(
        db_session, _movie_payload(str(dl_dir), files=[{"name": "hamnet.mkv"}])
    )
    stats = await plan_for_notifications(db_session, [notification])
    assert stats["planned"] == 1
    [plan] = await _plans(db_session)
    assert plan.rule_id == rule.id and plan.library_id == lib.id
    assert plan.category == "Horror"
    assert len(plan.ops) > 0


async def test_auto_execute_scheduled(db_session, tmp_path, monkeypatch):
    mock = Mock()
    monkeypatch.setattr(organize_service, "schedule_auto_execute", mock)
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(db_session, lib.id, TV_TEMPLATE, auto_execute=True)
    notification = await _seed(
        db_session, _series_payload(str(dl_dir), files=[{"name": "ep04.mkv"}])
    )
    await plan_for_notifications(db_session, [notification])
    [plan] = await _plans(db_session)
    mock.assert_called_once_with(plan.id)


# ---------------------------------------------------------------- 重建（regenerate 语义）


async def test_rebuild_preserves_manual_category(db_session, tmp_path):
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "hamnet.mkv", 500)
    lib = await _make_library(db_session, tmp_path / "movies", name="Movies", kind="movie")
    await _make_rule(db_session, lib.id, MOVIE_TEMPLATE)
    notification = await _seed(
        db_session, _movie_payload(str(dl_dir), files=[{"name": "hamnet.mkv"}])
    )
    await plan_for_notifications(db_session, [notification])
    [plan] = await _plans(db_session)
    # 人工指定类别（classify）
    await classify_plan(db_session, plan.id, lib.id, category="Drama")

    # 模拟 regenerate：快照变化后重跑规划 → pending 计划重建，保留人工 category
    new_payload = {**notification.payload}
    new_payload["work"] = {**new_payload["work"], "title_en": "Hamnet (Re)"}
    notification.payload = new_payload
    await db_session.commit()
    stats = await plan_for_notifications(db_session, [notification])
    assert stats["rebuilt"] == 1
    [plan] = await _plans(db_session)
    assert plan.category == "Drama"
    assert plan.status == "pending"
    ops = (
        await db_session.execute(
            select(OrganizePlanOp).where(OrganizePlanOp.plan_id == plan.id)
        )
    ).scalars().all()
    assert len(ops) == 1
    assert "/Drama/" in ops[0].dst
    actions = [a.action for a in await _audits(db_session, plan.id)]
    assert "plan_rebuilt" in actions


async def test_done_plan_short_circuits(db_session, tmp_path):
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(db_session, lib.id, TV_TEMPLATE)
    notification = await _seed(
        db_session, _series_payload(str(dl_dir), files=[{"name": "ep04.mkv"}])
    )
    await plan_for_notifications(db_session, [notification])
    [plan] = await _plans(db_session)
    plan.status = "done"
    await db_session.commit()

    new_payload = {**notification.payload, "resource": {**notification.payload["resource"], "episode": 5}}
    notification.payload = new_payload
    await db_session.commit()
    stats = await plan_for_notifications(db_session, [notification])
    assert stats["skipped"] == 1
    await db_session.refresh(plan)
    assert plan.status == "done"
    assert plan.payload["resource"]["episode"] == 4  # 快照未重建


# ---------------------------------------------------------------- 配置变更重建


async def test_replan_open_plans_reroutes_on_rule_change(db_session, tmp_path):
    """规则目标库变更 → 未执行计划按当前规则重路由，op 目标重渲染。"""
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "ep04.mkv", 300)
    lib_a = await _make_library(db_session, tmp_path / "lib-a", name="A")
    lib_b = await _make_library(db_session, tmp_path / "lib-b", name="B")
    rule = await _make_rule(db_session, lib_a.id, TV_TEMPLATE)
    notification = await _seed(
        db_session, _series_payload(str(dl_dir), files=[{"name": "ep04.mkv"}])
    )
    await plan_for_notifications(db_session, [notification])
    [plan] = await _plans(db_session)
    assert plan.library_id == lib_a.id

    # 用户把规则改指到库 B（快照未变，plan_for_notifications 不会触发重建）
    rule.library_id = lib_b.id
    await db_session.commit()
    stats = await replan_open_plans(db_session, reason="规则更新")
    assert stats == {"rebuilt": 1, "failed": 0}
    await db_session.refresh(plan)
    assert plan.library_id == lib_b.id
    assert plan.rule_id == rule.id
    assert plan.status == "pending"
    ops = (
        await db_session.execute(
            select(OrganizePlanOp).where(OrganizePlanOp.plan_id == plan.id)
        )
    ).scalars().all()
    assert len(ops) == 1
    assert ops[0].dst.startswith(str(tmp_path / "lib-b"))


async def test_replan_open_plans_categorizes_uncategorized(db_session, tmp_path):
    """新建匹配规则 → 待分类（无规则命中）计划被新规则收编。"""
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib")
    # 先有一条永不匹配的规则（保证规划步激活），造出待分类计划
    await _make_rule(
        db_session, lib.id, TV_TEMPLATE,
        filter={"field": "series.is_anime", "operator": "eq", "value": False},
    )
    notification = await _seed(
        db_session, _series_payload(str(dl_dir), files=[{"name": "ep04.mkv"}])
    )
    stats = await plan_for_notifications(db_session, [notification])
    assert stats["uncategorized"] == 1
    [plan] = await _plans(db_session)
    assert plan.library_id is None and plan.rule_id is None

    rule = await _make_rule(db_session, lib.id, TV_TEMPLATE, priority=50)
    stats = await replan_open_plans(db_session, reason="规则创建")
    assert stats == {"rebuilt": 1, "failed": 0}
    await db_session.refresh(plan)
    assert plan.library_id == lib.id
    assert plan.rule_id == rule.id
    ops = (
        await db_session.execute(
            select(OrganizePlanOp).where(OrganizePlanOp.plan_id == plan.id)
        )
    ).scalars().all()
    assert len(ops) == 1


async def test_replan_open_plans_preserves_manual_and_done(db_session, tmp_path):
    """人工指定的 library/category 与 done 计划在配置重建中原样保留。"""
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "hamnet.mkv", 500)
    lib = await _make_library(db_session, tmp_path / "movies", name="Movies", kind="movie")
    await _make_rule(db_session, lib.id, MOVIE_TEMPLATE)
    notification = await _seed(
        db_session, _movie_payload(str(dl_dir), files=[{"name": "hamnet.mkv"}])
    )
    await plan_for_notifications(db_session, [notification])
    [plan] = await _plans(db_session)
    await classify_plan(db_session, plan.id, lib.id, category="Drama")

    # 第二条：done 计划
    _mkfile(dl_dir / "other.mkv", 400)
    notification2 = await _seed(
        db_session, _movie_payload(str(dl_dir), files=[{"name": "other.mkv"}])
    )
    await plan_for_notifications(db_session, [notification2])
    plans = await _plans(db_session)
    done_plan = next(p for p in plans if p.notification_id == notification2.id)
    done_plan.status = "done"
    await db_session.commit()

    stats = await replan_open_plans(db_session, reason="规则更新")
    assert stats == {"rebuilt": 1, "failed": 0}
    await db_session.refresh(plan)
    assert plan.library_id == lib.id
    assert plan.category == "Drama"
    ops = (
        await db_session.execute(
            select(OrganizePlanOp).where(OrganizePlanOp.plan_id == plan.id)
        )
    ).scalars().all()
    assert "/Drama/" in ops[0].dst
    await db_session.refresh(done_plan)
    assert done_plan.status == "done"


async def test_replan_open_plans_skips_without_enabled_rules(db_session, tmp_path):
    """规则全禁用时不做重建（既有计划保留，不清空）。"""
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib")
    rule = await _make_rule(db_session, lib.id, TV_TEMPLATE)
    notification = await _seed(
        db_session, _series_payload(str(dl_dir), files=[{"name": "ep04.mkv"}])
    )
    await plan_for_notifications(db_session, [notification])
    [plan] = await _plans(db_session)

    rule.enabled = False
    await db_session.commit()
    stats = await replan_open_plans(db_session, reason="规则更新")
    assert stats == {"rebuilt": 0, "failed": 0}
    await db_session.refresh(plan)
    assert plan.library_id == lib.id


async def test_replan_unmatched_rule_demotes_to_uncategorized(db_session, tmp_path):
    """规则收紧后不再命中：重建必须退回「待分类」（rule_id/library_id
    置空），绝不让规则指向停留在已不匹配的旧规则上。"""
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib")
    rule = await _make_rule(db_session, lib.id, TV_TEMPLATE)
    notification = await _seed(
        db_session, _series_payload(str(dl_dir), files=[{"name": "ep04.mkv"}])
    )
    await plan_for_notifications(db_session, [notification])
    [plan] = await _plans(db_session)
    assert plan.rule_id == rule.id and plan.library_id == lib.id

    # 收紧规则：加一条永不命中的过滤条件
    rule.filter = {"field": "series.is_anime", "operator": "eq", "value": False}
    await db_session.commit()
    stats = await replan_open_plans(db_session, reason="规则更新")
    assert stats == {"rebuilt": 1, "failed": 0}
    await db_session.refresh(plan)
    assert plan.rule_id is None
    assert plan.library_id is None
    assert plan.status == "pending"
    ops = (
        await db_session.execute(
            select(OrganizePlanOp).where(OrganizePlanOp.plan_id == plan.id)
        )
    ).scalars().all()
    assert ops == []
    audits = await _audits(db_session, plan.id)
    assert audits[-1].action == "plan_rebuilt"
    assert audits[-1].detail["uncategorized"] is True


# ---------------------------------------------------------------- 人工分类


async def test_classify_uncategorized_then_execute(db_session, tmp_path):
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(
        db_session, lib.id, TV_TEMPLATE,
        filter={"field": "series.is_anime", "operator": "eq", "value": False},
    )
    notification = await _seed(
        db_session, _series_payload(str(dl_dir), files=[{"name": "ep04.mkv"}])
    )
    await plan_for_notifications(db_session, [notification])
    [plan] = await _plans(db_session)
    assert plan.library_id is None

    plan = await classify_plan(db_session, plan.id, lib.id)
    assert plan.library_id == lib.id
    ops = (
        await db_session.execute(
            select(OrganizePlanOp).where(OrganizePlanOp.plan_id == plan.id)
        )
    ).scalars().all()
    assert len(ops) == 1
    assert ops[0].src == str(dl_dir / "ep04.mkv")  # src/size 不变
    assert ops[0].size == 300
    assert "Season 01" in ops[0].dst  # 预设 TV 模板渲染
    actions = [a.action for a in await _audits(db_session, plan.id)]
    assert "classify" in actions


async def test_classify_loads_volume_when_library_is_already_in_identity_map(
    db_session, tmp_path,
):
    """Regression for PostgreSQL MissingGreenlet from plan.library → db.get.

    The API detail loader places Library in the identity map without its
    volume; classify must explicitly execute the eager loader before resolving
    the library root.
    """
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(
        db_session, lib.id, TV_TEMPLATE,
        filter={"field": "series.is_anime", "operator": "eq", "value": False},
    )
    notification = await _seed(
        db_session, _series_payload(str(dl_dir), files=[{"name": "ep04.mkv"}])
    )
    await plan_for_notifications(db_session, [notification])
    [plan] = await _plans(db_session)
    await db_session.execute(
        select(OrganizePlan)
        .where(OrganizePlan.id == plan.id)
        .options(selectinload(OrganizePlan.library))
    )
    classified = await classify_plan(db_session, plan.id, lib.id)
    assert classified.library_id == lib.id


async def test_classify_without_explicit_category_uses_genre(db_session, tmp_path):
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "hamnet.mkv", 500)
    lib = await _make_library(db_session, tmp_path / "movies", name="Movies", kind="movie")
    await _make_rule(db_session, lib.id, MOVIE_TEMPLATE)
    notification = await _seed(
        db_session, _movie_payload(str(dl_dir), files=[{"name": "hamnet.mkv"}])
    )
    await plan_for_notifications(db_session, [notification])
    [plan] = await _plans(db_session)
    plan = await classify_plan(db_session, plan.id, lib.id, category=None)
    assert plan.category == "Horror"


# ---------------------------------------------------------------- 执行


async def _planned_series_plan(db_session, tmp_path, **rule_kw):
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "Show.S01" / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(db_session, lib.id, TV_TEMPLATE, **rule_kw)
    notification = await _seed(
        db_session,
        _series_payload(str(dl_dir), torrent_name="Show.S01",
                        files=[{"name": "ep04.mkv"}]),
    )
    await plan_for_notifications(db_session, [notification])
    [plan] = await _plans(db_session)
    return plan, dl_dir


async def test_execute_plan_full_flow(db_session, tmp_path):
    plan, dl_dir = await _planned_series_plan(db_session, tmp_path)
    plan = await execute_plan(db_session, plan.id)
    assert plan.status == "done"
    assert plan.executed_at is not None
    [op] = (
        await db_session.execute(
            select(OrganizePlanOp).where(OrganizePlanOp.plan_id == plan.id)
        )
    ).scalars().all()
    assert op.status == "done"
    assert Path(op.dst).exists() and Path(op.dst).stat().st_size == 300
    assert not Path(op.src).exists()
    # 空目录自底向上清空，下载根保留
    assert not (dl_dir / "Show.S01").exists()
    assert dl_dir.exists()
    # 审计落库
    actions = [a.action for a in await _audits(db_session, plan.id)]
    assert "move" in actions and "execute" in actions and "cleanup" in actions
    # 任务清理：status → cancelled
    task = await db_session.get(DownloadTask, plan.payload["task"]["download_task_id"])
    assert task.status == "cancelled"


async def test_execute_plan_precheck_conflict_fails(db_session, tmp_path):
    plan, dl_dir = await _planned_series_plan(db_session, tmp_path)
    [op] = (
        await db_session.execute(
            select(OrganizePlanOp).where(OrganizePlanOp.plan_id == plan.id)
        )
    ).scalars().all()
    _mkfile(Path(op.dst), 999)  # 规划后目标被占且 size 不符
    plan = await execute_plan(db_session, plan.id)
    assert plan.status == "failed"
    assert "前置门禁" in plan.error_message
    assert Path(op.src).exists()  # 整体放弃，不触碰任何文件
    assert Path(op.dst).stat().st_size == 999


async def test_execute_uncategorized_plan_rejected(db_session, tmp_path):
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(
        db_session, lib.id, TV_TEMPLATE,
        filter={"field": "series.is_anime", "operator": "eq", "value": False},
    )
    notification = await _seed(
        db_session, _series_payload(str(dl_dir), files=[{"name": "ep04.mkv"}])
    )
    await plan_for_notifications(db_session, [notification])
    [plan] = await _plans(db_session)
    with pytest.raises(OrganizeError, match="待分类"):
        await execute_plan(db_session, plan.id)
    await db_session.refresh(plan)
    assert plan.status == "pending"  # 拒绝执行不改变状态


async def test_execute_done_is_idempotent(db_session, tmp_path):
    plan, _ = await _planned_series_plan(db_session, tmp_path)
    plan = await execute_plan(db_session, plan.id)
    assert plan.status == "done"
    again = await execute_plan(db_session, plan.id)
    assert again.status == "done"


async def test_refresh_and_cleanup_failures_keep_done(db_session, tmp_path, monkeypatch):
    mock_cleanup = AsyncMock(side_effect=RuntimeError("rpc down"))
    mock_refresh = AsyncMock(side_effect=RuntimeError("server down"))
    monkeypatch.setattr(organize_service, "delete_task_after_organize", mock_cleanup)
    monkeypatch.setattr(organize_service, "refresh_library", mock_refresh)
    plan, _ = await _planned_series_plan(db_session, tmp_path)
    server = MediaServerInstance(
        id=_uuid(), name="plex", type="plex",
        url="http://plex:32400", token="tok",
    )
    db_session.add(server)
    lib = await db_session.get(Library, plan.library_id)
    lib.media_server_id = server.id
    lib.section_key = "2"
    await db_session.commit()

    plan = await execute_plan(db_session, plan.id)
    assert plan.status == "done"  # 失败只记日志，不改写计划状态
    mock_cleanup.assert_awaited_once()
    mock_refresh.assert_awaited_once()


async def test_unbound_library_plan_pending_and_execute_rejected(db_session, tmp_path):
    """命中规则的 Library 未绑定卷 → 落「待绑定」pending 计划（无 ops），
    执行门禁拒绝。"""
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "ep04.mkv", 300)
    lib = await _make_library(db_session, tmp_path / "lib", bound=False)
    await _make_rule(db_session, lib.id, TV_TEMPLATE)
    notification = await _seed(
        db_session, _series_payload(str(dl_dir), files=[{"name": "ep04.mkv"}])
    )
    stats = await plan_for_notifications(db_session, [notification])
    assert stats["planned"] == 1
    [plan] = await _plans(db_session)
    assert plan.status == "pending" and plan.library_id == lib.id
    ops = (
        await db_session.execute(
            select(OrganizePlanOp).where(OrganizePlanOp.plan_id == plan.id)
        )
    ).scalars().all()
    assert ops == []  # 待绑定：不渲染目标路径
    with pytest.raises(OrganizeError, match="待绑定"):
        await execute_plan(db_session, plan.id)
    await db_session.refresh(plan)
    assert plan.status == "pending"  # 拒绝执行不改变状态


async def test_execute_plans_batch_isolation(db_session, tmp_path):
    plan, _ = await _planned_series_plan(db_session, tmp_path)
    results = await execute_plans(db_session, ["missing-id", plan.id])
    assert results[0][0] == "missing-id" and "不存在" in results[0][1]
    assert results[1] == (plan.id, "done")


# ---------------------------------------------------------------- file_op: hardlink / copy（保种）


def _mock_downloader_client(monkeypatch):
    """Mock task_cleanup 的下载器客户端工厂，返回 (wrapper,) 供断言 RPC。"""
    wrapper = SimpleNamespace(
        resume_torrent=AsyncMock(return_value=True),
        remove_torrent=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.task_cleanup.get_downloader_client", lambda d: wrapper
    )
    return wrapper


async def test_execute_plan_hardlink_preserves_task_and_src(
    db_session, tmp_path, monkeypatch
):
    """hardlink 全链路：执行 done 后源文件保留、任务仍 completed（不删）、
    恢复快照时停过的做种、Plex 刷新照常。"""
    plan, dl_dir = await _planned_series_plan(
        db_session, tmp_path, file_op="hardlink"
    )
    task = await db_session.get(
        DownloadTask, plan.payload["task"]["download_task_id"]
    )
    task.transmission_torrent_id = 42
    server = MediaServerInstance(
        id=_uuid(), name="plex", type="plex",
        url="http://plex:32400", token="tok",
    )
    db_session.add(server)
    lib = await db_session.get(Library, plan.library_id)
    lib.media_server_id = server.id
    lib.section_key = "2"
    await db_session.commit()
    wrapper = _mock_downloader_client(monkeypatch)
    mock_refresh = AsyncMock()
    monkeypatch.setattr(organize_service, "refresh_library", mock_refresh)

    plan = await execute_plan(db_session, plan.id)
    assert plan.status == "done"
    [op] = (
        await db_session.execute(
            select(OrganizePlanOp).where(OrganizePlanOp.plan_id == plan.id)
        )
    ).scalars().all()
    assert op.status == "done"
    assert Path(op.dst).exists() and Path(op.dst).stat().st_size == 300
    assert Path(op.src).exists()  # 保种：源文件保留
    assert os.path.samefile(op.src, op.dst)  # 硬链接，不占双份存储
    assert (dl_dir / "Show.S01").exists()  # 源目录不清理
    # 保种：不删任务、恢复做种；Plex 刷新照常
    await db_session.refresh(task)
    assert task.status == "completed"
    wrapper.resume_torrent.assert_awaited_once_with(42)
    wrapper.remove_torrent.assert_not_awaited()
    mock_refresh.assert_awaited_once()
    actions = [a.action for a in await _audits(db_session, plan.id)]
    assert "hardlink" in actions and "cleanup" not in actions


async def test_execute_plan_copy_preserves_task_and_src(
    db_session, tmp_path, monkeypatch
):
    """copy 全链路：执行 done 后源文件保留（真复制）、任务不删、恢复做种。"""
    plan, _ = await _planned_series_plan(db_session, tmp_path, file_op="copy")
    task = await db_session.get(
        DownloadTask, plan.payload["task"]["download_task_id"]
    )
    task.transmission_torrent_id = 42
    await db_session.commit()
    wrapper = _mock_downloader_client(monkeypatch)

    plan = await execute_plan(db_session, plan.id)
    assert plan.status == "done"
    [op] = (
        await db_session.execute(
            select(OrganizePlanOp).where(OrganizePlanOp.plan_id == plan.id)
        )
    ).scalars().all()
    assert Path(op.dst).exists() and Path(op.dst).stat().st_size == 300
    assert Path(op.src).exists()  # 保种：源文件保留
    assert not os.path.samefile(op.src, op.dst)  # 真复制，两份 inode
    await db_session.refresh(task)
    assert task.status == "completed"
    wrapper.resume_torrent.assert_awaited_once_with(42)
    wrapper.remove_torrent.assert_not_awaited()
    actions = [a.action for a in await _audits(db_session, plan.id)]
    assert "copy" in actions and "cleanup" not in actions


async def test_execute_plan_hardlink_resume_failure_keeps_done(
    db_session, tmp_path, monkeypatch
):
    """恢复做种 RPC 失败只记日志，不改写计划 done 状态。"""
    plan, _ = await _planned_series_plan(
        db_session, tmp_path, file_op="hardlink"
    )
    task = await db_session.get(
        DownloadTask, plan.payload["task"]["download_task_id"]
    )
    task.transmission_torrent_id = 42
    await db_session.commit()
    wrapper = _mock_downloader_client(monkeypatch)
    wrapper.resume_torrent.side_effect = RuntimeError("rpc down")

    plan = await execute_plan(db_session, plan.id)
    assert plan.status == "done"
    await db_session.refresh(task)
    assert task.status == "completed"


# ---------------------------------------------------------------- 工具


def _mkfile(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


# ---------------------------------------------------------------- torrent 清单定位


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


async def test_plan_locates_flat_single_file_via_torrent_manifest(
    db_session, tmp_path
):
    """payload 无 files、torrent_name 为空（平铺在共享下载根的单文件种子）：
    回退 resource.torrent_file 的 torrent 清单做存在性精确匹配，照常出计划，
    且只触碰清单列出的文件。"""
    dl_dir = tmp_path / "downloads"
    _mkfile(dl_dir / "Hamnet.2025.1080p.mkv", 300)
    _mkfile(dl_dir / "other-task.mkv", 100)  # 共享根里的其他任务文件
    lib = await _make_library(db_session, tmp_path / "lib", name="Movies", kind="movie")
    await _make_rule(db_session, lib.id, "{title} ({year})/{title} ({year}){ext}")
    notification = await _seed(db_session, _movie_payload(str(dl_dir)))
    resource = (
        await db_session.execute(select(FileResource))
    ).scalars().one()
    resource.torrent_file = str(
        _write_torrent(tmp_path / "r.torrent", [("Hamnet.2025.1080p.mkv", 300)])
    )
    await db_session.commit()

    stats = await plan_for_notifications(db_session, [notification])
    assert stats["planned"] == 1
    [plan] = await _plans(db_session)
    ops = (
        await db_session.execute(
            select(OrganizePlanOp).where(OrganizePlanOp.plan_id == plan.id)
        )
    ).scalars().all()
    assert len(ops) == 1
    assert ops[0].op_type == "move"
    assert ops[0].src == str(dl_dir / "Hamnet.2025.1080p.mkv")
    # 共享根里的其他文件绝不出现在计划里
    assert "other-task.mkv" not in ops[0].src


async def test_manifest_fallback_prepends_torrent_root(db_session, tmp_path):
    """多文件 .torrent：清单回退补上 info/name 根目录分量，命中
    ``download_dir/<根目录>/<文件>`` 的落盘布局（快照 torrent_name 缺失时
    独立目录扫描不可用，只能靠清单精确匹配）。"""
    dl_dir = tmp_path / "downloads"
    root = "Group.Show.S01E04.1080p"
    _mkfile(dl_dir / root / "ep04.mkv", 300)
    _mkfile(dl_dir / root / "ep04.ass", 10)
    lib = await _make_library(db_session, tmp_path / "lib")
    await _make_rule(db_session, lib.id, TV_TEMPLATE)
    notification = await _seed(db_session, _series_payload(str(dl_dir)))
    resource = (
        await db_session.execute(select(FileResource))
    ).scalars().one()
    resource.torrent_file = str(
        _write_torrent(
            tmp_path / "r.torrent",
            [("ep04.mkv", 300), ("ep04.ass", 10)],
            root=root,
        )
    )
    await db_session.commit()

    stats = await plan_for_notifications(db_session, [notification])
    assert stats["planned"] == 1
    [plan] = await _plans(db_session)
    ops = (
        await db_session.execute(
            select(OrganizePlanOp).where(OrganizePlanOp.plan_id == plan.id)
        )
    ).scalars().all()
    srcs = {op.src for op in ops}
    assert str(dl_dir / root / "ep04.mkv") in srcs


async def test_manifest_fallback_filters_unsafe_paths(db_session, tmp_path):
    """torrent 清单里的绝对路径 / .. 分量被过滤，不会越界匹配共享根外的文件。"""
    from app.schemas.notification import NotificationPayload
    from app.services.organize_service import _resolve_manifest

    dl_dir = tmp_path / "downloads"
    outside = _mkfile(tmp_path / "outside.mkv", 100)
    notification = await _seed(db_session, _movie_payload(str(dl_dir)))
    resource = (
        await db_session.execute(select(FileResource))
    ).scalars().one()
    resource.torrent_file = str(
        _write_torrent(
            tmp_path / "r.torrent",
            [("../outside.mkv", 100), ("/etc/passwd", 1)],
        )
    )
    await db_session.commit()

    payload = NotificationPayload.model_validate(notification.payload)
    manifest = await _resolve_manifest(db_session, payload)
    # "../outside.mkv" 被过滤；"/etc/passwd" 经根目录前缀化为
    # "root/etc/passwd"（下载根内的相对路径，越界语义已消除）。
    names = [e["name"] for e in (manifest or [])]
    assert "../outside.mkv" not in names
    assert "/etc/passwd" not in names
    assert all(not Path(n).is_absolute() and ".." not in n.split("/") for n in names)
    assert outside.exists()  # 未被动到（只是存在性检查，但路径必须未入选）


def test_cleanup_paths_skips_shared_root_without_torrent_name():
    """torrent_name 为空（平铺种子）：空目录清理整体跳过，绝不以共享下载根
    为清理范围。"""
    from app.schemas.notification import NotificationPayload
    from app.services.organize_service import _cleanup_paths

    payload = NotificationPayload.model_validate(_movie_payload("/downloads"))
    assert _cleanup_paths(payload, None) == (None, None)
