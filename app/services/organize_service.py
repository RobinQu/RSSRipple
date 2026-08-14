"""内置整理子系统（organize）的持久化与触发服务。

职责（docs/design/file-organization.md「触发与执行链路」）：

- 规划（:func:`plan_for_notifications`）：消费 notify tick 新建/重建的通知，
  以 ``notification_id`` 唯一约束为幂等键落 OrganizePlan + ops。规划失败
  （文件定位不到 / PlanError）不落计划、记 error 日志，下一 tick 自然重试。
  无规则匹配落 ``library_id=null`` 的「待分类」pending 计划。规则
  ``auto_execute=true`` 时落库后调度后台执行（两阶段持久化边界不变）。
  ``ORGANIZE_ENABLED=false`` 时整步跳过（熔断语义对齐 NOTIFY_ENABLED）。
- 重建：通知 regenerate 后，pending/failed 计划随新快照重建（沿用已人工
  指定的 library/category，op 目标重渲染）；done/running 短路不重建。
- 执行（:func:`execute_plan` / :func:`execute_plans`）：状态门禁 +
  单 ``asyncio.Lock`` 进程内串行（对齐 vault-organizer），阻塞文件 IO 经
  ``asyncio.to_thread``；执行器本体在 :mod:`app.services.organize_executor`。
  完成后（done）按命中规则的 ``file_op`` 分流清理：``move`` = 内部任务
  清理（best-effort）；``hardlink``/``copy`` = 保种（不删任务，恢复做种，
  best-effort）；随后媒体服务器刷新（best-effort，经 Library →
  MediaServerInstance 寻址）。
- 人工分类（:func:`classify_plan`）：待分类计划指定 library/category 后
  重渲染全部 op 的 dst（保持 src/size 不变）。
- 待绑定（R2）：命中规则的 Library ``volume_id`` 为 NULL（卷未绑定）→
  规划照常落 pending 计划（无 ops），执行门禁拒绝；补绑定后重建/重执行。
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.config import settings
from app.models.download_notification import DownloadNotification
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.library import Library
from app.models.organize_audit import OrganizeAuditEntry
from app.models.organize_plan import OrganizePlan
from app.models.organize_plan_op import OrganizePlanOp
from app.models.organize_rule import OrganizeRule
from app.schemas.notification import NotificationPayload
from app.services.media_server_service import refresh_library
from app.services.organize_executor import ExecOp, run_execution
from app.services.organize_planner import (
    DiskFile,
    OrganizePlanResult,
    PlanError,
    build_plan,
)
from app.services.organize_template import PRESET_MOVIE, PRESET_TV
from app.services.task_cleanup import (
    delete_task_after_organize,
    resume_task_after_organize,
)
from app.services.volume_service import (
    VolumeResolutionError,
    resolve_downloader_path,
    resolve_library_root,
)
from app.utils.time import utcnow

logger = logging.getLogger(__name__)

# 单锁串行化规划外全部执行段（对齐 vault-organizer 的并发模型）。
_executor_lock = asyncio.Lock()
# 本进程正在执行的计划 id：区分「真正执行中」与「崩溃遗留的 running」
# （后者可重放，由执行器幂等状态表收敛）。
_executing_plan_ids: set[str] = set()


class OrganizeError(Exception):
    """整理服务层错误（计划不存在/状态不允许执行/待分类未指定目标等）。"""


# ---------------------------------------------------------------- 视图转换


def _rule_ns(rule: OrganizeRule) -> SimpleNamespace:
    """提取规则为 plain namespace：build_plan 在线程中运行，不能触碰会话。"""
    return SimpleNamespace(
        id=rule.id,
        name=rule.name,
        priority=rule.priority,
        enabled=rule.enabled,
        filter=rule.filter,
        library_id=rule.library_id,
        path_template=rule.path_template,
        file_op=rule.file_op,
        auto_execute=rule.auto_execute,
    )


def _library_ns(lib: Library) -> SimpleNamespace:
    """提取 Library 为 plain namespace；root_path 由卷引用动态解析
    （``volume.mount_path + root_subpath``），未绑定卷 → None（待绑定）。
    调用方须已 selectinload ``Library.volume``。"""
    return SimpleNamespace(
        id=lib.id,
        name=lib.name,
        root_path=resolve_library_root(lib),
        kind=lib.kind,
        subtitle_lang_map=lib.subtitle_lang_map,
    )


def _synthetic_rule(library_id: str, template: str) -> SimpleNamespace:
    """人工分类/重建时绕开规则匹配、直指目标库的合成规则（first-match 即中）。"""
    return SimpleNamespace(
        id=None,
        name="manual",
        priority=0,
        enabled=True,
        filter=None,
        library_id=library_id,
        path_template=template,
        file_op="move",
        auto_execute=False,
    )


def _preset_template(payload: NotificationPayload) -> str:
    """无命中规则时按作品类型选内置 Plex 兼容预设模板。"""
    work_type = payload.work.type if payload.work else None
    return PRESET_TV if work_type == "series" else PRESET_MOVIE


# ---------------------------------------------------------------- 磁盘文件收集


def _collect_files(
    payload: NotificationPayload, downloader: Any | None
) -> list[DiskFile]:
    """定位磁盘文件（同步，线程中运行）。优先 payload.files 清单，缺失回退
    只扫种子独立目录（download_dir/torrent_name）——绝不扫共享下载根。

    移植自 vault-organizer ``worker.collect_files``；返回路径均为本进程
    视角（已过下载器卷绑定解析），因此随后 build_plan 不再做翻译。
    """
    task = payload.task
    download_dir = (task.download_dir if task else None) or ""
    try:
        base = Path(resolve_downloader_path(downloader, download_dir))
    except VolumeResolutionError as e:
        raise PlanError(str(e)) from e
    tname = (task.torrent_name if task else None) or ""

    if tname and (base / tname).is_file():
        f = base / tname
        return [DiskFile(path=str(f), size=f.stat().st_size, rel=tname)]

    scoped_dir = bool(tname) and (base / tname).is_dir()
    root = base / tname if scoped_dir else base

    if payload.files:
        files: list[DiskFile] = []
        for entry in payload.files:
            name = entry.get("name") or ""
            if not name:
                continue
            for cand in (root / name, base / name):
                if cand.is_file():
                    files.append(
                        DiskFile(
                            path=str(cand), size=cand.stat().st_size, rel=name
                        )
                    )
                    break
        if files:
            return files
        logger.warning(
            "[organize] payload.files 在磁盘上均未命中（%s）",
            payload.notification_id,
        )

    if not scoped_dir:
        raise PlanError(
            "无法定位下载内容：payload 无可用 files 且种子独立目录不存在"
            f"（torrent_name={tname!r}，download_dir={download_dir}），"
            "拒绝扫描共享下载根以免误伤其他任务，请人工介入"
        )
    files = [
        DiskFile(path=str(p), size=p.stat().st_size, rel=str(p.relative_to(root)))
        for p in sorted(root.rglob("*"))
        if p.is_file()
    ]
    if not files:
        raise PlanError(f"下载目录无可整理文件：{root}")
    return files


def _cleanup_paths(
    payload: NotificationPayload, downloader: Any | None
) -> tuple[str | None, str | None]:
    """执行后空目录清理的 (范围, 保留边界)：种子独立目录与卷解析后的下载根。"""
    task = payload.task
    download_dir = (task.download_dir if task else None) or ""
    if not download_dir:
        return None, None
    base = resolve_downloader_path(downloader, download_dir)
    tname = (task.torrent_name if task else None) or ""
    return (os.path.join(base, tname) if tname else base), base


async def _resolve_downloader(db, payload: NotificationPayload) -> Any | None:
    """经 payload 的 download_task_id 找到 DownloaderInstance（含卷绑定）。

    返回 plain namespace 快照（含 volume 的 id/mount_path）：随后的文件
    收集在线程中运行，不能触碰会话。
    """
    task_id = payload.task.download_task_id if payload.task else None
    if not task_id:
        return None
    task = await db.get(DownloadTask, task_id)
    if task is None or not task.downloader_id:
        return None
    downloader = (
        await db.execute(
            select(DownloaderInstance)
            .where(DownloaderInstance.id == task.downloader_id)
            .options(selectinload(DownloaderInstance.volume))
        )
    ).scalar_one_or_none()
    if downloader is None:
        return None
    volume = downloader.volume
    return SimpleNamespace(
        id=downloader.id,
        name=downloader.name,
        download_dir=downloader.download_dir,
        volume_id=downloader.volume_id,
        volume_subpath=downloader.volume_subpath,
        volume=(
            SimpleNamespace(id=volume.id, mount_path=volume.mount_path)
            if volume is not None
            else None
        ),
    )


def _collect_and_plan(
    payload: NotificationPayload,
    downloader: Any | None,
    rules: list[Any],
    libraries: dict[str, Any],
    category: str | None,
) -> OrganizePlanResult:
    """收集磁盘文件 + 调 build_plan（同步段，线程中运行）。

    文件路径已在收集时过卷绑定解析，build_plan 不再重复翻译。
    """
    disk_files = _collect_files(payload, downloader)
    return build_plan(payload, disk_files, rules, libraries, category=category)


# ---------------------------------------------------------------- 审计


def _audit(db, plan_id: str, action: str, detail: dict | None = None) -> None:
    db.add(OrganizeAuditEntry(plan_id=plan_id, action=action, detail=detail))


# ---------------------------------------------------------------- 规划


async def plan_for_notifications(
    db, notifications: list[DownloadNotification]
) -> dict:
    """对一批通知做整理规划（幂等）。返回统计 dict。

    熔断：``ORGANIZE_ENABLED=false`` 或不存在 enabled 规则时整步跳过。
    已有计划：done/running 短路；pending/failed 且 payload 已 regenerate
    （与计划冻结快照不一致）→ 重建（沿用人工指定的 library/category）。
    """
    stats = {"planned": 0, "rebuilt": 0, "uncategorized": 0, "skipped": 0, "failed": 0}
    if not settings.organize_enabled or not notifications:
        return stats
    rules = (
        await db.execute(
            select(OrganizeRule)
            .where(OrganizeRule.enabled.is_(True))
            .order_by(OrganizeRule.priority, OrganizeRule.created_at)
        )
    ).scalars().all()
    if not rules:
        return stats
    libraries = (
        await db.execute(select(Library).options(selectinload(Library.volume)))
    ).scalars().all()
    rule_ns = [_rule_ns(r) for r in rules]
    lib_ns = {lib.id: _library_ns(lib) for lib in libraries}

    for notification in notifications:
        try:
            outcome = await _plan_one(db, notification, rule_ns, lib_ns)
        except Exception as e:  # noqa: BLE001 — 单条失败不影响本 tick 其余通知
            logger.error("[organize] 通知 %s 规划失败：%s", notification.id, e)
            stats["failed"] += 1
            continue
        stats[outcome] += 1
    return stats


async def _plan_one(
    db,
    notification: DownloadNotification,
    rules: list[Any],
    libraries: dict[str, Any],
) -> str:
    existing = (
        await db.execute(
            select(OrganizePlan).where(
                OrganizePlan.notification_id == notification.id
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.status in ("done", "running"):
            return "skipped"
        if existing.payload == notification.payload:
            return "skipped"  # 快照未变，无重建必要
        return await _rebuild_plan(db, existing, notification, rules, libraries)

    payload = NotificationPayload.model_validate(notification.payload)
    downloader = await _resolve_downloader(db, payload)
    try:
        result = await asyncio.to_thread(
            _collect_and_plan, payload, downloader, rules, libraries, None
        )
    except PlanError as e:
        # 不落计划：记 error 日志，下一 tick 自然重试。
        logger.error(
            "[organize] 通知 %s 规划失败（不落计划，下 tick 重试）：%s",
            notification.id, e,
        )
        return "failed"

    plan = OrganizePlan(
        notification_id=notification.id,
        rule_id=result.rule.id if result.rule else None,
        library_id=result.library.id if result.library else None,
        category=result.category,
        status="pending",
        payload=notification.payload,
    )
    for i, op in enumerate(result.ops):
        plan.ops.append(
            OrganizePlanOp(
                seq=i, op_type=op.op_type, src=op.src, dst=op.dst, size=op.size
            )
        )
    try:
        async with db.begin_nested():  # SAVEPOINT：吸收并发规划竞争
            db.add(plan)
            await db.flush()
    except IntegrityError:
        return "skipped"  # 输掉唯一约束竞争：计划已存在
    _audit(
        db, plan.id, "plan_created",
        {"notification_id": notification.id, "ops": len(result.ops),
         "uncategorized": result.uncategorized,
         "needs_category": result.needs_category},
    )
    await db.commit()
    _maybe_auto_execute(plan.id, result)
    return "uncategorized" if result.uncategorized else "planned"


async def _rebuild_plan(
    db,
    plan: OrganizePlan,
    notification: DownloadNotification,
    rules: list[Any],
    libraries: dict[str, Any],
) -> str:
    """通知 regenerate 后重建 pending/failed 计划。

    沿用已人工指定的 library/category：library_id 已人工指定（rule_id 为
    null）时按预设模板直指该库重渲染；否则按当前规则 first-match 重算，
    category 沿用计划现值。重建失败保留旧计划（下 tick 重试）。
    """
    payload = NotificationPayload.model_validate(notification.payload)
    downloader = await _resolve_downloader(db, payload)
    category = plan.category
    if plan.library_id and plan.rule_id is None and plan.library_id in libraries:
        use_rules = [_synthetic_rule(plan.library_id, _preset_template(payload))]
    else:
        use_rules = rules
    try:
        result = await asyncio.to_thread(
            _collect_and_plan, payload, downloader, use_rules, libraries, category
        )
    except PlanError as e:
        logger.error(
            "[organize] 计划 %s 重建失败（保留旧计划）：%s", plan.id, e
        )
        return "failed"

    old_ops = (
        await db.execute(
            select(OrganizePlanOp).where(OrganizePlanOp.plan_id == plan.id)
        )
    ).scalars().all()
    for op in old_ops:
        await db.delete(op)
    await db.flush()
    for i, op in enumerate(result.ops):
        db.add(
            OrganizePlanOp(
                plan_id=plan.id, seq=i, op_type=op.op_type,
                src=op.src, dst=op.dst, size=op.size,
            )
        )
    if not result.uncategorized:
        plan.rule_id = result.rule.id if result.rule and result.rule.id else plan.rule_id
        plan.library_id = result.library.id if result.library else plan.library_id
        plan.category = result.category
    plan.payload = notification.payload
    plan.status = "pending"
    plan.error_message = None
    _audit(
        db, plan.id, "plan_rebuilt",
        {"notification_id": notification.id, "ops": len(result.ops)},
    )
    await db.commit()
    _maybe_auto_execute(plan.id, result)
    return "rebuilt"


def _maybe_auto_execute(plan_id: str, result: OrganizePlanResult) -> None:
    """规则 auto_execute=true → 落库后调度后台执行（两阶段持久化不变）。"""
    rule = result.rule
    if (
        rule is not None
        and getattr(rule, "auto_execute", False)
        and result.ops
        and not result.needs_category
    ):
        schedule_auto_execute(plan_id)


def schedule_auto_execute(plan_id: str) -> None:
    """后台执行计划：独立会话，异常只记日志（计划状态由 execute_plan 落库）。

    与 notify tick 并发写库时可能撞 Turso 锁/写冲突：经 ``retry_on_lock``
    整体重放——execute_plan 幂等（执行器幂等状态表收敛），重放安全。
    """

    async def _run() -> None:
        from app.database import committed_session, retry_on_lock

        async def _attempt() -> None:
            async with committed_session() as session:
                await execute_plan(session, plan_id)

        try:
            await retry_on_lock(_attempt)
        except Exception as e:  # noqa: BLE001 — 后台任务，失败只记日志
            logger.error("[organize] 自动执行计划 %s 失败：%s", plan_id, e)

    asyncio.create_task(_run())


# ---------------------------------------------------------------- 执行


async def execute_plan(db, plan_id: str) -> OrganizePlan:
    """执行单个计划（幂等）。返回执行后的计划。

    状态门禁：done 幂等短路；running 且本进程正在执行 → 拒绝；崩溃遗留的
    running 可重放；cancelled / 待分类（library 未定或模板类别未定）→ 拒绝。
    执行失败（前置门禁/冲突/校验）计划落 failed + error_message，不抛异常
    ——failed 是可重试的正常终态。
    """
    async with _executor_lock:
        plan = await db.get(OrganizePlan, plan_id)
        if plan is None:
            raise OrganizeError(f"计划不存在：{plan_id}")
        if plan.status == "done":
            return plan
        if plan.status == "running" and plan_id in _executing_plan_ids:
            raise OrganizeError(f"计划 {plan_id} 正在执行中")
        if plan.status == "cancelled":
            raise OrganizeError(f"计划 {plan_id} 已取消")
        if plan.library_id is None:
            raise OrganizeError(f"计划 {plan_id} 为待分类计划，请先指定目标库")
        rule = await db.get(OrganizeRule, plan.rule_id) if plan.rule_id else None
        # 无规则（人工分类直指目标库）= 合成 move 语义
        file_op = rule.file_op if rule is not None else "move"
        if (
            rule is not None
            and plan.category is None
            and "{category}" in rule.path_template
        ):
            raise OrganizeError(f"计划 {plan_id} 尚未指定影片类别，请先分类")

        library = await db.get(
            Library, plan.library_id,
            options=[selectinload(Library.media_server)],
        )
        if library is None:
            raise OrganizeError(f"计划 {plan_id} 的目标库不存在：{plan.library_id}")
        if library.volume_id is None:
            raise OrganizeError(
                f"计划 {plan_id} 的目标库未绑定存储卷（待绑定），请先补绑定"
            )
        payload = NotificationPayload.model_validate(plan.payload)
        download_task_id = payload.task.download_task_id if payload.task else None

        ops = (
            await db.execute(
                select(OrganizePlanOp)
                .where(OrganizePlanOp.plan_id == plan.id)
                .order_by(OrganizePlanOp.seq)
            )
        ).scalars().all()
        exec_ops = [
            ExecOp(
                op_type=op.op_type, src=op.src, dst=op.dst, size=op.size,
                reason=op.error_message or "",
            )
            for op in ops
        ]
        downloader = await _resolve_downloader(db, payload)
        try:
            cleanup_root, preserve = _cleanup_paths(payload, downloader)
        except VolumeResolutionError as e:
            raise OrganizeError(str(e)) from e

        plan.status = "running"
        plan.error_message = None
        await db.commit()

        _executing_plan_ids.add(plan_id)
        try:
            outcome = await asyncio.to_thread(
                run_execution, exec_ops, file_op=file_op,
                cleanup_root=cleanup_root, preserve=preserve,
            )
        except Exception as e:
            # 执行段未预期异常（如非 EXDEV 的 OSError）：落 failed 可重试，
            # 绝不让计划卡在 running。
            plan.status = "failed"
            plan.error_message = f"内部错误：{e}"[:2000]
            _audit(db, plan.id, "execute",
                   {"status": "failed", "error": plan.error_message})
            await db.commit()
            raise OrganizeError(f"内部错误：{e}") from e
        finally:
            _executing_plan_ids.discard(plan_id)

        # 回写 op 结果与审计
        result_by_key = {
            (r.op.op_type, r.op.src): (r.status, r.error)
            for r in outcome.op_results
        }
        for op in ops:
            key = (op.op_type, op.src)
            if key in result_by_key:
                op.status, op.error_message = result_by_key[key]
        for entry in outcome.audits:
            _audit(db, plan.id, entry["action"], entry["detail"])

        if outcome.ok:
            plan.status = "done"
            plan.executed_at = utcnow()
            plan.error_message = None
            _audit(db, plan.id, "execute", {"status": "done"})
        else:
            plan.status = "failed"
            plan.error_message = (outcome.error or "")[:2000]
            _audit(db, plan.id, "execute",
                   {"status": "failed", "error": outcome.error})
        await db.commit()

    # 锁外：任务清理/恢复做种与媒体服务器刷新均为 best-effort，失败只记日志
    # 不改写计划状态
    if outcome.ok:
        if download_task_id:
            try:
                if file_op == "move":
                    ok = await delete_task_after_organize(db, download_task_id)
                    action = "任务清理"
                else:
                    # hardlink/copy 保种：不删任务、不清源目录，恢复快照时停
                    # 过的做种（RPC 幂等）
                    ok = await resume_task_after_organize(db, download_task_id)
                    action = "恢复做种"
                await db.commit()
                if not ok:
                    logger.error(
                        "[organize] 计划 %s 执行成功但%s失败：%s",
                        plan_id, action, download_task_id,
                    )
            except Exception as e:  # noqa: BLE001
                logger.error(
                    "[organize] 计划 %s 执行成功但任务清理异常：%s", plan_id, e
                )
        # 刷新改址（R2）：Library → MediaServerInstance → adapter；无服务器
        # 关联（手工/旧数据行）或服务器停用 → refresh_library 内部跳过。
        if library.media_server_id and library.section_key:
            try:
                await refresh_library(library, path=_touched_path(exec_ops))
            except Exception as e:  # noqa: BLE001
                logger.warning("[organize] 计划 %s 媒体服务器刷新异常：%s", plan_id, e)
    return plan


def _touched_path(ops: list[ExecOp]) -> str | None:
    """本次执行触及的公共目录（move op 目标父目录的公共前缀），供 Plex
    partial refresh；无 move 目标或路径不可比 → None（退整库刷新）。"""
    dirs = sorted(
        {os.path.dirname(op.dst) for op in ops if op.op_type == "move" and op.dst}
    )
    if not dirs:
        return None
    try:
        return os.path.commonpath(dirs)
    except ValueError:
        return None


async def execute_plans(db, plan_ids: list[str]) -> list[tuple[str, str]]:
    """批量执行：锁内逐计划顺序执行，单个失败不影响其余。

    返回 ``(plan_id, "done" | 错误描述)`` 列表。
    """
    results: list[tuple[str, str]] = []
    for plan_id in plan_ids:
        try:
            plan = await execute_plan(db, plan_id)
            results.append((plan_id, plan.status))
        except OrganizeError as e:
            results.append((plan_id, str(e)))
    return results


# ---------------------------------------------------------------- 人工分类


async def classify_plan(
    db, plan_id: str, library_id: str, category: str | None = None
) -> OrganizePlan:
    """待分类计划人工指定 library（和/或 category）后重渲染全部 op 的 dst。

    保持 op 的 src/size 不变：以现有 op 为磁盘文件清单重调 build_plan。
    模板：命中规则的 ``path_template``；无规则（待分类）按作品类型用内置
    预设。仅 pending/failed 计划可分类。
    """
    plan = await db.get(OrganizePlan, plan_id)
    if plan is None:
        raise OrganizeError(f"计划不存在：{plan_id}")
    if plan.status not in ("pending", "failed"):
        raise OrganizeError(f"计划 {plan_id} 当前状态（{plan.status}）不可分类")
    library = await db.get(
        Library, library_id, options=[selectinload(Library.volume)]
    )
    if library is None:
        raise OrganizeError(f"目标库不存在：{library_id}")

    rule = await db.get(OrganizeRule, plan.rule_id) if plan.rule_id else None
    payload = NotificationPayload.model_validate(plan.payload)
    template = rule.path_template if rule is not None else _preset_template(payload)

    ops = (
        await db.execute(
            select(OrganizePlanOp)
            .where(OrganizePlanOp.plan_id == plan.id)
            .order_by(OrganizePlanOp.seq)
        )
    ).scalars().all()
    # 保持 src/size 不变：以现有文件 op 为磁盘清单重渲染 dst。待分类计划
    # （无规则匹配）从未生成过 op，回退重新收集磁盘文件（唯一渲染途径）。
    disk_files = [
        DiskFile(path=op.src, size=op.size, rel=os.path.basename(op.src))
        for op in ops
        if op.op_type in ("move", "keep")
    ]
    if not disk_files:
        downloader = await _resolve_downloader(db, payload)
        try:
            disk_files = await asyncio.to_thread(
                _collect_files, payload, downloader
            )
        except PlanError as e:
            raise OrganizeError(f"无法定位磁盘文件：{e}") from e
    lib_ns = _library_ns(library)
    syn_rule = _synthetic_rule(library.id, template)
    try:
        result = await asyncio.to_thread(
            build_plan, payload, disk_files, [syn_rule],
            {library.id: lib_ns},
            category=category,
        )
    except PlanError as e:
        raise OrganizeError(f"重渲染失败：{e}") from e
    if result.needs_category:
        raise OrganizeError("模板包含 {category}，请同时指定影片类别")

    for op in ops:
        await db.delete(op)
    await db.flush()
    for i, op in enumerate(result.ops):
        db.add(
            OrganizePlanOp(
                plan_id=plan.id, seq=i, op_type=op.op_type,
                src=op.src, dst=op.dst, size=op.size,
            )
        )
    plan.library_id = library.id
    plan.category = category
    plan.status = "pending"
    plan.error_message = None
    _audit(
        db, plan.id, "classify",
        {"library_id": library.id, "category": category, "ops": len(result.ops)},
    )
    await db.commit()
    return plan
