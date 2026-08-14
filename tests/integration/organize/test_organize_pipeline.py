"""Organize 子系统的进程内集成测试（全链路，不依赖 docker 栈）。

与 tests/unit/test_organize_service.py 的分工：单元测试用手工构造的
payload 直测服务函数；这里从**真实通知生成链**开始——completed
DownloadTask 经 scheduler 的 notify tick（``_process_download_notifications``，
含停种 + RPC 文件清单快照 + mock webhook fan-out + organize 规划步）或
``create_notification_for_task`` 生成冻结快照，再走
``plan_for_notifications`` → ``execute_plan``。文件系统用 tmp_path 模拟
「Transmission 容器挂载点 /downloads ≠ RSSRipple 进程挂载点
tmp_path/mnt/shared」的共享卷，验证下载器卷绑定（StorageVolume +
volume_id）路径解析、模板落位、源目录自底向上清理（下载根保留）、
任务清理（remove_torrent delete_data=False + 任务置 cancelled）与
媒体服务器刷新（Library → MediaServerInstance 寻址）。

覆盖点：单集剧集（scheduler tick + 手动执行）、auto_execute 全自动链路、
hardlink 规则（源文件保留保种 + 任务不删 + 恢复做种）、合集（batch 覆盖度
校验 + 字幕随正片 + 特典 keep）、电影 category
（needs_category → classify → 执行）、待分类 → classify → 执行、
无卷绑定恒等。

独立运行：``uv run pytest tests/integration/organize -q``
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models.agent import Agent
from app.models.agent_webhook import AgentWebhook
from app.models.channel import Channel
from app.models.download_notification import DownloadNotification
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.episode import Episode
from app.models.file_resource import FileResource
from app.models.library import Library
from app.models.media_server import MediaServerInstance
from app.models.movie import Movie
from app.models.organize_audit import OrganizeAuditEntry
from app.models.organize_plan import OrganizePlan
from app.models.organize_plan_op import OrganizePlanOp
from app.models.organize_rule import OrganizeRule
from app.models.series import TVSeries
from app.models.webhook_delivery import WebhookDelivery
from app.services.notify_service import create_notification_for_task
from app.services.organize_service import (
    OrganizeError,
    classify_plan,
    execute_plan,
    plan_for_notifications,
)
from app.services.scheduler import _process_download_notifications
from app.utils.time import utcnow


def _uuid() -> str:
    return str(uuid.uuid4())


TV_TEMPLATE = (
    "{title}/Season {season:02d}/"
    "{title} - s{season:02d}e{episode:02d}[ - {episode_title}]{ext}"
)
MOVIE_TEMPLATE = "{category}/{title} ({year})/{title} ({year}){ext}"


# ---------------------------------------------------------------------------
# 数据与文件 seed
# ---------------------------------------------------------------------------


def _mkfile(path: Path, size: int) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _series() -> TVSeries:
    return TVSeries(
        id=_uuid(), title_cn="攻壳机动队", title_en="Ghost in the Shell",
        original_title="攻殻機動隊", content_type="tv", is_anime=True,
        genre=["Animation"], start_date=date(2026, 4, 1),
        seasons=[{"season_number": 1, "episode_count": 10}],
    )


def _movie() -> Movie:
    return Movie(
        id=_uuid(), title_cn="哈姆奈特", title_en="Hamnet",
        original_title="Hamnet", content_type="movie", is_anime=False,
        genre=["Horror", "Drama"], release_date=date(2025, 12, 1),
    )


async def _seed_chain(
    db,
    *,
    work,
    resource_kw: dict,
    download_dir: str,
    volume=None,
    downloader_dir: str | None = None,
) -> SimpleNamespace:
    """建 Channel/Downloader/Agent/mock Webhook/Resource/completed Task。

    ``volume``：StorageVolume（ORM），非空时经 ``volume_id`` 绑定到
    Downloader；``downloader_dir`` 为 daemon 视角下载根（绑卷时与任务的
    ``download_dir`` 不同，如 ``/downloads`` vs ``/downloads/complete``）。
    """
    channel = Channel(
        id=_uuid(), name="ch", type="rss_feed", url="https://example.com/rss",
        fetch_interval=1800, status="active",
        field_mapping={
            "list_locator": {"source": "entries"},
            "field_mappings": {"torrent_url": {"source": "link"}},
        },
        metadata_agent_enabled=False,
    )
    downloader = DownloaderInstance(
        id=_uuid(), name="dl", type="transmission",
        url="http://127.0.0.1:9091/transmission/rpc",
        download_dir=downloader_dir or download_dir,
        volume_id=volume.id if volume is not None else None,
        status="disconnected",
    )
    agent = Agent(
        id=_uuid(), name="agent", channel_id=channel.id,
        downloader_id=downloader.id,
    )
    db.add_all([channel, downloader, work, agent])
    await db.flush()
    webhook = AgentWebhook(
        id=_uuid(), agent_id=agent.id, url="http://consumer.invalid/hook",
        mock=True, enabled=True,
    )
    db.add(webhook)
    resource_kw = dict(resource_kw)
    work_link = (
        {"series_id": work.id} if isinstance(work, TVSeries)
        else {"movie_id": work.id}
    )
    resource = FileResource(
        id=_uuid(), channel_id=channel.id, guid=_uuid(),
        title_raw=resource_kw.pop("title_raw", "raw"),
        torrent_url="magnet:?xt=urn:btih:abc", **work_link, **resource_kw,
    )
    task = DownloadTask(
        id=_uuid(), agent_id=agent.id, file_resource_id=resource.id,
        downloader_id=downloader.id, download_dir=download_dir,
        transmission_torrent_id=42, status="completed", completed_at=utcnow(),
    )
    db.add_all([resource, task])
    await db.commit()
    return SimpleNamespace(
        channel=channel, downloader=downloader, agent=agent, webhook=webhook,
        resource=resource, task=task, work=work,
    )


async def _library(db, root: Path, *, name="TV", kind="tv", server=None,
                   section_key=None):
    """卷绑定形态的 Library（R2）：root 落为 StorageVolume.mount_path。"""
    from app.models.storage_volume import StorageVolume

    volume = StorageVolume(
        id=_uuid(), name=f"vol-{_uuid()[:8]}", mount_path=str(root)
    )
    lib = Library(
        id=_uuid(), name=name, kind=kind, volume_id=volume.id,
        media_server_id=server.id if server is not None else None,
        section_key=section_key,
    )
    db.add_all([volume, lib])
    await db.commit()
    return lib


async def _rule(db, library_id, template, *, filter=None, auto_execute=False,
                file_op="move"):
    rule = OrganizeRule(
        id=_uuid(), name="rule", priority=100, enabled=True, filter=filter,
        library_id=library_id, path_template=template, file_op=file_op,
        auto_execute=auto_execute,
    )
    db.add(rule)
    await db.commit()
    return rule


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


# ---------------------------------------------------------------------------
# 1. scheduler tick 全链路：单集剧集（卷绑定解析 + 手动执行）
# ---------------------------------------------------------------------------


async def test_tick_full_chain_single_episode(
    db_session, session_factory, shared_volume, rpc_mocks, refresh_mock
):
    series = _series()
    db_session.add(
        Episode(id=_uuid(), series_id=series.id, season=1, episode=4,
                title="机器人回旋曲")
    )
    server = MediaServerInstance(
        id=_uuid(), name="plex", type="plex",
        url="http://plex:32400", token="tok",
    )
    db_session.add(server)
    chain = await _seed_chain(
        db_session, work=series,
        resource_kw=dict(season=1, episode=4, is_batch=False,
                         subtitle_langs=["zh-CN"], resolution="1080p",
                         container="mkv"),
        download_dir=shared_volume.complete_daemon,
        volume=shared_volume.volume,
        downloader_dir=shared_volume.daemon,
    )
    # 磁盘：种子独立目录（本进程视角的共享卷路径）
    torrent_dir = shared_volume.complete_process / "Show.S01"
    _mkfile(torrent_dir / "ep04.mkv", 300)
    _mkfile(torrent_dir / "ep04.chs.srt", 40)
    rpc_mocks.get_files.return_value = {
        "name": "Show.S01",
        "files": [
            {"name": "ep04.mkv", "length": 300},
            {"name": "ep04.chs.srt", "length": 40},
        ],
    }
    lib = await _library(
        db_session, shared_volume.media / "tv", server=server, section_key="3"
    )
    rule = await _rule(
        db_session, lib.id, TV_TEMPLATE,
        filter={"field": "series.is_anime", "operator": "eq", "value": True},
    )

    # 单次 notify tick：completed 任务 → 停种+快照通知 → organize 规划
    # → mock webhook fan-out 投递，一条链路跑完。
    await _process_download_notifications()

    async with session_factory() as s:
        notification = (
            await s.execute(select(DownloadNotification))
        ).scalar_one()
        rpc_mocks.pause.assert_awaited_once_with(42)
        # 快照冻结的是 daemon 视角路径
        assert notification.payload["task"]["download_dir"] == (
            shared_volume.complete_daemon
        )
        assert notification.payload["task"]["torrent_name"] == "Show.S01"
        assert notification.payload["work"]["title_cn"] == "攻壳机动队"
        # organize 规划步已在 tick 内落计划
        plan = (
            await s.execute(
                select(OrganizePlan).where(
                    OrganizePlan.notification_id == notification.id
                )
            )
        ).scalar_one()
        assert plan.status == "pending"
        assert plan.rule_id == rule.id and plan.library_id == lib.id
        ops = await _ops(s, plan.id)
        moves = {Path(o.src).name: o for o in ops if o.op_type == "move"}
        assert set(moves) == {"ep04.mkv", "ep04.chs.srt"}
        video = moves["ep04.mkv"]
        # src 已按卷绑定解析为本进程视角
        assert video.src == str(torrent_dir / "ep04.mkv")
        assert video.src.startswith(str(shared_volume.process))
        lib_root = shared_volume.media / "tv"
        expected_dst = (
            lib_root / "攻壳机动队" / "Season 01"
            / "攻壳机动队 - s01e04 - 机器人回旋曲.mkv"
        )
        assert video.dst == str(expected_dst)
        assert moves["ep04.chs.srt"].dst == str(
            expected_dst.with_suffix(".chs.srt")
        )
        # mock webhook fan-out 投递成功
        delivery = (await s.execute(select(WebhookDelivery))).scalar_one()
        assert delivery.status == "done"
        plan_id = plan.id

    # 执行：文件落 Library、源目录自底向上清空、下载根保留、任务清理、媒体服务器刷新
    async with session_factory() as s:
        plan = await execute_plan(s, plan_id)
        assert plan.status == "done" and plan.executed_at is not None
        assert expected_dst.exists()
        assert expected_dst.stat().st_size == 300
        assert expected_dst.with_suffix(".chs.srt").exists()
        assert not (torrent_dir / "ep04.mkv").exists()
        assert not torrent_dir.exists()  # 空目录自底向上清空
        assert shared_volume.complete_process.exists()  # 下载根保留
        task = await s.get(DownloadTask, chain.task.id)
        assert task.status == "cancelled"
        rpc_mocks.remove.assert_awaited_once_with(42, delete_data=False)
        refresh_mock.assert_awaited_once()
        # partial refresh 的触及目录 = move 目标父目录公共前缀
        assert refresh_mock.await_args.kwargs["path"].endswith("Season 01")
        actions = await _audit_actions(s, plan_id)
        assert "plan_created" in actions and "move" in actions
        assert "execute" in actions and "cleanup" in actions


# ---------------------------------------------------------------------------
# 1b. hardlink 规则：tick→计划→执行，文件落库且源保留保种、任务不删
# ---------------------------------------------------------------------------


async def test_tick_hardlink_rule_keeps_src_and_task(
    db_session, session_factory, shared_volume, rpc_mocks
):
    chain = await _seed_chain(
        db_session, work=_series(),
        resource_kw=dict(season=1, episode=4, is_batch=False,
                         subtitle_langs=[], resolution="1080p",
                         container="mkv"),
        download_dir=shared_volume.complete_daemon,
        volume=shared_volume.volume,
        downloader_dir=shared_volume.daemon,
    )
    torrent_dir = shared_volume.complete_process / "Show.S01"
    src = _mkfile(torrent_dir / "ep04.mkv", 300)
    rpc_mocks.get_files.return_value = {
        "name": "Show.S01",
        "files": [{"name": "ep04.mkv", "length": 300}],
    }
    lib = await _library(db_session, shared_volume.media / "tv")
    await _rule(db_session, lib.id, TV_TEMPLATE, file_op="hardlink")

    await _process_download_notifications()

    async with session_factory() as s:
        notification = (
            await s.execute(select(DownloadNotification))
        ).scalar_one()
        plan = (
            await s.execute(
                select(OrganizePlan).where(
                    OrganizePlan.notification_id == notification.id
                )
            )
        ).scalar_one()
        assert plan.status == "pending"
        plan_id = plan.id

    async with session_factory() as s:
        plan = await execute_plan(s, plan_id)
        assert plan.status == "done", plan.error_message
        [op] = await _ops(s, plan_id)
        # 文件已落库（硬链接）且源文件保留保种
        assert Path(op.dst).exists()
        assert src.exists()
        assert os.path.samefile(src, op.dst)
        assert torrent_dir.exists()  # 源目录不清理
        # 保种：任务不删（仍 completed）、不 remove torrent、恢复做种
        task = await s.get(DownloadTask, chain.task.id)
        assert task.status == "completed"
        rpc_mocks.remove.assert_not_awaited()
        rpc_mocks.resume.assert_awaited_once_with(42)
        actions = await _audit_actions(s, plan_id)
        assert "hardlink" in actions and "cleanup" not in actions


# ---------------------------------------------------------------------------
# 2. auto_execute：tick 内规划落库后后台自动执行，无需人工点击
# ---------------------------------------------------------------------------


async def test_tick_auto_execute_moves_files(
    db_session, session_factory, shared_volume, rpc_mocks
):
    chain = await _seed_chain(
        db_session, work=_series(),
        resource_kw=dict(season=1, episode=4, is_batch=False,
                         subtitle_langs=[], resolution="1080p",
                         container="mkv"),
        download_dir=shared_volume.complete_daemon,
        volume=shared_volume.volume,
        downloader_dir=shared_volume.daemon,
    )
    torrent_dir = shared_volume.complete_process / "Show.S01"
    _mkfile(torrent_dir / "ep04.mkv", 300)
    rpc_mocks.get_files.return_value = {
        "name": "Show.S01",
        "files": [{"name": "ep04.mkv", "length": 300}],
    }
    lib = await _library(db_session, shared_volume.media / "tv")
    await _rule(db_session, lib.id, TV_TEMPLATE, auto_execute=True)

    await _process_download_notifications()

    plan = None
    for _ in range(200):
        async with session_factory() as s:
            plan = (await s.execute(select(OrganizePlan))).scalar_one()
            if plan.status in ("done", "failed"):
                break
        await asyncio.sleep(0.05)
    assert plan is not None
    assert plan.status == "done", plan.error_message

    dst = (
        shared_volume.media / "tv" / "攻壳机动队" / "Season 01"
        / "攻壳机动队 - s01e04.mkv"
    )
    assert dst.exists()
    assert not torrent_dir.exists()
    # 任务清理在计划转 done 之后同一代码路径内完成（best-effort），轮询收敛
    task = None
    for _ in range(100):
        async with session_factory() as s:
            task = await s.get(DownloadTask, chain.task.id)
            if task.status == "cancelled":
                break
        await asyncio.sleep(0.05)
    assert task is not None and task.status == "cancelled"
    rpc_mocks.remove.assert_awaited_once_with(42, delete_data=False)


# ---------------------------------------------------------------------------
# 3. 合集（batch）：覆盖度校验 + 字幕随正片 + 特典 keep
# ---------------------------------------------------------------------------


async def test_batch_season_full_chain(
    db_session, shared_volume, rpc_mocks
):
    chain = await _seed_chain(
        db_session, work=_series(),
        resource_kw=dict(season=1, episode=None, is_batch=True,
                         episode_start=1, episode_end=3,
                         subtitle_langs=["zh-CN"], resolution="1080p",
                         container="mkv"),
        download_dir=shared_volume.complete_daemon,
        volume=shared_volume.volume,
        downloader_dir=shared_volume.daemon,
    )
    torrent_dir = shared_volume.complete_process / "Show.S01"
    disk_files = {
        "Show.S01E01.mkv": 200,
        "Show.S01E02.mkv": 210,
        "Show.S01E03.mkv": 220,
        "Show.S01E01.chs.srt": 30,
        "Making of.mkv": 50,  # 解析不出集号 → 特典 keep
    }
    for name, size in disk_files.items():
        _mkfile(torrent_dir / name, size)
    rpc_mocks.get_files.return_value = {
        "name": "Show.S01",
        "files": [{"name": n, "length": s} for n, s in disk_files.items()],
    }
    lib = await _library(db_session, shared_volume.media / "tv")
    await _rule(
        db_session, lib.id, TV_TEMPLATE,
        filter={"field": "is_batch", "operator": "eq", "value": True},
    )

    notification, created = await create_notification_for_task(
        db_session, chain.task
    )
    assert created
    stats = await plan_for_notifications(db_session, [notification])
    assert stats["planned"] == 1

    plan = (
        await db_session.execute(select(OrganizePlan))
    ).scalar_one()
    ops = await _ops(db_session, plan.id)
    moves = {Path(o.src).name: o for o in ops if o.op_type == "move"}
    keeps = [o for o in ops if o.op_type == "keep"]
    assert set(moves) == {
        "Show.S01E01.mkv", "Show.S01E02.mkv", "Show.S01E03.mkv",
        "Show.S01E01.chs.srt",
    }
    assert [Path(o.src).name for o in keeps] == ["Making of.mkv"]
    lib_root = shared_volume.media / "tv"
    for i in (1, 2, 3):
        assert moves[f"Show.S01E0{i}.mkv"].dst == str(
            lib_root / "攻壳机动队" / "Season 01"
            / f"攻壳机动队 - s01e0{i}.mkv"
        )
    assert moves["Show.S01E01.chs.srt"].dst == str(
        lib_root / "攻壳机动队" / "Season 01" / "攻壳机动队 - s01e01.chs.srt"
    )

    plan = await execute_plan(db_session, plan.id)
    assert plan.status == "done", plan.error_message
    for i in (1, 2, 3):
        dst = (
            lib_root / "攻壳机动队" / "Season 01"
            / f"攻壳机动队 - s01e0{i}.mkv"
        )
        assert dst.exists()
    assert (lib_root / "攻壳机动队" / "Season 01"
            / "攻壳机动队 - s01e01.chs.srt").exists()
    # keep 文件原地保留 → 种子目录非空，自底向上清理自然跳过它
    assert (torrent_dir / "Making of.mkv").exists()
    assert not (torrent_dir / "Show.S01E01.mkv").exists()
    assert shared_volume.complete_process.exists()
    task = await db_session.get(DownloadTask, chain.task.id)
    assert task.status == "cancelled"
    rpc_mocks.remove.assert_awaited_once_with(42, delete_data=False)


# ---------------------------------------------------------------------------
# 4. 电影 category：{category} 模板 → 人工指定类别 → 执行
# ---------------------------------------------------------------------------


async def test_movie_category_classify_then_execute(
    db_session, shared_volume, rpc_mocks
):
    chain = await _seed_chain(
        db_session, work=_movie(),
        resource_kw=dict(season=None, episode=None, is_batch=False,
                         resolution="1080p", container="mkv",
                         title_year=2025),
        download_dir=shared_volume.complete_daemon,
        volume=shared_volume.volume,
        downloader_dir=shared_volume.daemon,
    )
    # 单文件种子：文件平铺在下载目录（非独立目录形态）
    src = _mkfile(
        shared_volume.complete_process / "Hamnet.2025.1080p.mkv", 500
    )
    rpc_mocks.get_files.return_value = {
        "name": "Hamnet.2025.1080p.mkv",
        "files": [{"name": "Hamnet.2025.1080p.mkv", "length": 500}],
    }
    lib = await _library(
        db_session, shared_volume.media / "movies", name="Movies", kind="movie"
    )
    rule = await _rule(
        db_session, lib.id, MOVIE_TEMPLATE,
        filter={"field": "movie.genre", "operator": "contains",
                "value": "Horror"},
    )

    notification, _ = await create_notification_for_task(db_session, chain.task)
    stats = await plan_for_notifications(db_session, [notification])
    assert stats["planned"] == 1
    plan = (await db_session.execute(select(OrganizePlan))).scalar_one()
    assert plan.rule_id == rule.id and plan.library_id == lib.id
    assert plan.category is None  # {category} 未定 → 待人工指定
    assert await _ops(db_session, plan.id) == []

    with pytest.raises(OrganizeError, match="类别"):
        await execute_plan(db_session, plan.id)

    plan = await classify_plan(db_session, plan.id, lib.id, category="Horror")
    [op] = await _ops(db_session, plan.id)
    lib_root = shared_volume.media / "movies"
    expected_dst = (
        lib_root / "Horror" / "哈姆奈特 (2025)" / "哈姆奈特 (2025).mkv"
    )
    assert op.dst == str(expected_dst)
    assert op.src == str(src)  # src 已翻译、classify 保持不变

    plan = await execute_plan(db_session, plan.id)
    assert plan.status == "done", plan.error_message
    assert expected_dst.exists()
    assert not src.exists()
    assert shared_volume.complete_process.exists()
    task = await db_session.get(DownloadTask, chain.task.id)
    assert task.status == "cancelled"


# ---------------------------------------------------------------------------
# 5. 待分类：无规则匹配 → library_id=null → classify → 执行
# ---------------------------------------------------------------------------


async def test_uncategorized_classify_then_execute(
    db_session, shared_volume, rpc_mocks
):
    chain = await _seed_chain(
        db_session, work=_series(),
        resource_kw=dict(season=1, episode=4, is_batch=False,
                         subtitle_langs=[], resolution="1080p",
                         container="mkv"),
        download_dir=shared_volume.complete_daemon,
        volume=shared_volume.volume,
        downloader_dir=shared_volume.daemon,
    )
    torrent_dir = shared_volume.complete_process / "Show.S01"
    _mkfile(torrent_dir / "ep04.mkv", 300)
    rpc_mocks.get_files.return_value = {
        "name": "Show.S01",
        "files": [{"name": "ep04.mkv", "length": 300}],
    }
    lib = await _library(db_session, shared_volume.media / "tv")
    # is_anime=True 的作品不匹配该规则 → 待分类
    await _rule(
        db_session, lib.id, TV_TEMPLATE,
        filter={"field": "series.is_anime", "operator": "eq", "value": False},
    )

    notification, _ = await create_notification_for_task(db_session, chain.task)
    stats = await plan_for_notifications(db_session, [notification])
    assert stats["uncategorized"] == 1
    plan = (await db_session.execute(select(OrganizePlan))).scalar_one()
    assert plan.library_id is None and plan.rule_id is None

    with pytest.raises(OrganizeError, match="待分类"):
        await execute_plan(db_session, plan.id)

    plan = await classify_plan(db_session, plan.id, lib.id)
    [op] = await _ops(db_session, plan.id)
    assert op.src == str(torrent_dir / "ep04.mkv")
    assert op.dst.startswith(str(shared_volume.media / "tv"))
    assert "Season 01" in op.dst  # 预设 TV 模板渲染

    plan = await execute_plan(db_session, plan.id)
    assert plan.status == "done", plan.error_message
    assert Path(op.dst).exists()
    assert not torrent_dir.exists()
    assert shared_volume.complete_process.exists()


# ---------------------------------------------------------------------------
# 6. 无卷绑定恒等：两容器路径一致的部署无需解析（volume_id=null）
# ---------------------------------------------------------------------------


async def test_no_volume_binding_is_identity(
    db_session, session_factory, shared_volume, rpc_mocks
):
    await _seed_chain(
        db_session, work=_series(),
        resource_kw=dict(season=1, episode=4, is_batch=False,
                         subtitle_langs=[], resolution="1080p",
                         container="mkv"),
        download_dir=str(shared_volume.complete_process),
        volume=None,  # 不绑卷 → 恒等
    )
    torrent_dir = shared_volume.complete_process / "Show.S01"
    _mkfile(torrent_dir / "ep04.mkv", 300)
    rpc_mocks.get_files.return_value = {
        "name": "Show.S01",
        "files": [{"name": "ep04.mkv", "length": 300}],
    }
    lib = await _library(db_session, shared_volume.media / "tv")
    await _rule(db_session, lib.id, TV_TEMPLATE)

    # tick 由独立会话写入；断言走新鲜会话，避免 db_session 的 MVCC 快照
    # 看不到 tick 提交的行。
    await _process_download_notifications()
    async with session_factory() as s:
        notification = (
            await s.execute(select(DownloadNotification))
        ).scalar_one()
        plan = (
            await s.execute(
                select(OrganizePlan).where(
                    OrganizePlan.notification_id == notification.id
                )
            )
        ).scalar_one()
        [op] = await _ops(s, plan.id)
        # 无卷绑定 → src 与快照路径一致（恒等）
        assert op.src == str(torrent_dir / "ep04.mkv")

        plan = await execute_plan(s, plan.id)
        assert plan.status == "done", plan.error_message
        assert Path(op.dst).exists()
        assert not torrent_dir.exists()
