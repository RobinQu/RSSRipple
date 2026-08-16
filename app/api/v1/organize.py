"""Built-in file organization (organize) API routes.

Three groups (docs/design/file-organization.md「API」):

- ``/libraries`` — media-server-derived organize targets (R2: read-only +
  partial update; derived root_path / bound status in responses).
- ``/organize-rules`` — global ordered first-match-wins routing rules
  (Filter DSL + naming template), plus a dry-run ``/organize-rules/preview``.
- ``/organize/plans`` + ``/organize/audit`` — two-phase plans: list/detail,
  execute (202 background), execute-batch, classify, cancel, audit paging.

Planning/execution semantics live in :mod:`app.services.organize_service`;
this module only does request validation and response shaping.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.download_notification import DownloadNotification
from app.models.download_task import DownloadTask
from app.models.downloader import DownloaderInstance
from app.models.file_resource import FileResource
from app.models.library import Library
from app.models.movie import Movie
from app.models.organize_audit import OrganizeAuditEntry
from app.models.organize_plan import OrganizePlan
from app.models.organize_rule import OrganizeRule
from app.models.series import TVSeries
from app.models.storage_volume import StorageVolume
from app.schemas.common import paginated_response, success_response
from app.schemas.notification import NotificationPayload
from app.schemas.organize import (
    PLAN_STATUSES,
    LibraryListItem,
    LibraryOut,
    LibraryUpdate,
    OrganizeAuditOut,
    OrganizeClassifyRequest,
    OrganizeExecuteBatchRequest,
    OrganizePlanDetail,
    OrganizePlanListItem,
    OrganizePlanOpOut,
    OrganizePreviewOp,
    OrganizePreviewRequest,
    OrganizePreviewResponse,
    OrganizeRuleCreate,
    OrganizeRuleOut,
    OrganizeRuleUpdate,
)
from app.services import organize_service
from app.services.filter_engine import validate_filter_config
from app.services.notify_service import build_payload
from app.services.organize_planner import PlanError
from app.services.organize_template import validate_template
from app.services.volume_service import resolve_library_root
from app.utils.download_paths import validate_download_subdir

router = APIRouter()

logger = logging.getLogger(__name__)


async def _replan_after_config_change(db: AsyncSession, reason: str) -> None:
    """规则/媒体库配置变更后刷新全部未执行计划（replan_open_plans）。

    附带动作：变更本身已提交，重建失败只记日志、不影响响应。
    """
    try:
        await organize_service.replan_open_plans(db, reason=reason)
    except Exception as e:  # noqa: BLE001
        logger.warning("[organize] %s 后重建计划失败：%s", reason, e)


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "data": None,
            "error": {"code": code, "message": message, "details": None},
            "meta": {},
        },
    )


def _validate_filter(filter_config) -> JSONResponse | None:
    """Save-time DSL validation (same entry point as Agent filter_config)."""
    if filter_config is None:
        return None
    errs = validate_filter_config(filter_config)
    if errs:
        return _error(422, "VALIDATION_ERROR", "; ".join(errs))
    return None


def _validate_template(template: str) -> JSONResponse | None:
    try:
        validate_template(template)
    except ValueError as e:
        return _error(422, "VALIDATION_ERROR", str(e))
    return None


# ---------------------------------------------------------------------------
# Libraries
# ---------------------------------------------------------------------------

_LIBRARY_LOAD_OPTIONS = (
    selectinload(Library.volume),
    selectinload(Library.media_server),
)


def _library_out(lib: Library) -> dict:
    """响应组装：root_path 为派生展示字段（卷引用解析结果），bound 为绑定状态。"""
    return LibraryOut(
        id=lib.id,
        name=lib.name,
        kind=lib.kind,
        media_server_id=lib.media_server_id,
        media_server_name=lib.media_server.name if lib.media_server else None,
        section_key=lib.section_key,
        server_path=lib.server_path,
        volume_id=lib.volume_id,
        volume_name=lib.volume.name if lib.volume else None,
        root_subpath=lib.root_subpath,
        root_path=resolve_library_root(lib),
        recycle_subpath=lib.recycle_subpath,
        bound=lib.volume_id is not None,
        subtitle_lang_map=lib.subtitle_lang_map,
        created_at=lib.created_at,
        updated_at=lib.updated_at,
    ).model_dump()


@router.get("/libraries")
async def list_libraries(
    unbound: bool = Query(False),
    db: AsyncSession = Depends(get_db),
):
    base_q = select(Library).options(*_LIBRARY_LOAD_OPTIONS)
    if unbound:
        base_q = base_q.where(Library.volume_id.is_(None))
    rows = (
        await db.execute(base_q.order_by(Library.created_at.asc()))
    ).scalars().all()
    pending_counts = dict(
        (
            await db.execute(
                select(OrganizePlan.library_id, func.count())
                .where(OrganizePlan.status == "pending")
                .group_by(OrganizePlan.library_id)
            )
        ).all()
    )
    items = []
    for lib in rows:
        data = LibraryListItem(
            **_library_out(lib),
            pending_plan_count=pending_counts.get(lib.id, 0),
        ).model_dump()
        items.append(data)
    return success_response(items)


async def _get_library_or_404(
    db: AsyncSession, library_id: str
) -> Library | JSONResponse:
    lib = await db.get(Library, library_id, options=_LIBRARY_LOAD_OPTIONS)
    if lib is None:
        return _error(404, "NOT_FOUND", "Library not found")
    return lib


@router.get("/libraries/{library_id}")
async def get_library(library_id: str, db: AsyncSession = Depends(get_db)):
    lib = await _get_library_or_404(db, library_id)
    if isinstance(lib, JSONResponse):
        return lib
    return success_response(_library_out(lib))


@router.put("/libraries/{library_id}")
async def update_library(
    library_id: str, body: LibraryUpdate, db: AsyncSession = Depends(get_db)
):
    """局部更新：subtitle_lang_map + volume_id/root_subpath（待绑定就地修复）。

    其余字段由扫描派生——schema extra="forbid" 使提交即 422。
    """
    lib = await _get_library_or_404(db, library_id)
    if isinstance(lib, JSONResponse):
        return lib
    update_data = body.model_dump(exclude_unset=True)
    if "volume_id" in update_data and update_data["volume_id"] is not None:
        if await db.get(StorageVolume, update_data["volume_id"]) is None:
            return _error(404, "NOT_FOUND", "Storage volume not found")
    if "root_subpath" in update_data:
        try:
            update_data["root_subpath"] = validate_download_subdir(
                update_data["root_subpath"]
            )
        except ValueError as e:
            return _error(422, "VALIDATION_ERROR", str(e))
    if "recycle_subpath" in update_data:
        try:
            update_data["recycle_subpath"] = validate_download_subdir(
                update_data["recycle_subpath"]
            )
        except ValueError as e:
            return _error(422, "VALIDATION_ERROR", str(e))
    for key, value in update_data.items():
        setattr(lib, key, value)
    await db.commit()
    # 强制重取（populate_existing）：volume_id 变更后原 selectinload 缓存的
    # volume=None 已过期，且要拿到服务端 onupdate 的 updated_at。
    lib = (
        await db.execute(
            select(Library)
            .where(Library.id == library_id)
            .options(*_LIBRARY_LOAD_OPTIONS)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    await _replan_after_config_change(db, f"媒体库「{lib.name}」更新")
    return success_response(_library_out(lib))


@router.delete("/libraries/{library_id}")
async def delete_library(library_id: str, db: AsyncSession = Depends(get_db)):
    lib = await _get_library_or_404(db, library_id)
    if isinstance(lib, JSONResponse):
        return lib
    plan_count = (
        await db.execute(
            select(func.count())
            .select_from(OrganizePlan)
            .where(OrganizePlan.library_id == library_id)
        )
    ).scalar_one()
    if plan_count > 0:
        return _error(
            409, "DELETE_BLOCKED",
            f"无法删除：{plan_count} 个整理计划仍引用该库，请先处理/取消计划",
        )
    rule_count = (
        await db.execute(
            select(func.count())
            .select_from(OrganizeRule)
            .where(OrganizeRule.library_id == library_id)
        )
    ).scalar_one()
    if rule_count > 0:
        return _error(
            409, "DELETE_BLOCKED",
            f"无法删除：{rule_count} 条整理规则仍指向该库，请先删除或改指规则",
        )
    await db.delete(lib)
    await db.commit()
    return success_response({"deleted": True})


# ---------------------------------------------------------------------------
# Organize rules
# ---------------------------------------------------------------------------


@router.get("/organize-rules")
async def list_organize_rules(db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(OrganizeRule).order_by(
                OrganizeRule.priority.asc(), OrganizeRule.created_at.asc()
            )
        )
    ).scalars().all()
    return success_response(
        [OrganizeRuleOut.model_validate(r).model_dump() for r in rows]
    )


async def _get_rule_or_404(
    db: AsyncSession, rule_id: str
) -> OrganizeRule | JSONResponse:
    rule = await db.get(OrganizeRule, rule_id)
    if rule is None:
        return _error(404, "NOT_FOUND", "Organize rule not found")
    return rule


def _validate_rule_payload(
    filter_config, path_template: str | None
) -> JSONResponse | None:
    err = _validate_filter(filter_config)
    if err is not None:
        return err
    if path_template is not None:
        return _validate_template(path_template)
    return None


@router.post("/organize-rules", status_code=201)
async def create_organize_rule(
    body: OrganizeRuleCreate, db: AsyncSession = Depends(get_db)
):
    err = _validate_rule_payload(body.filter, body.path_template)
    if err is not None:
        return err
    if await db.get(Library, body.library_id) is None:
        return _error(404, "NOT_FOUND", "Library not found")
    rule = OrganizeRule(
        name=body.name,
        priority=body.priority,
        enabled=body.enabled,
        filter=body.filter,
        library_id=body.library_id,
        path_template=body.path_template,
        file_op=body.file_op,
        auto_execute=body.auto_execute,
    )
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    await _replan_after_config_change(db, f"规则「{rule.name}」创建")
    return success_response(OrganizeRuleOut.model_validate(rule).model_dump())


@router.post("/organize-rules/preview")
async def preview_organize_rule(
    body: OrganizePreviewRequest, db: AsyncSession = Depends(get_db)
):
    """dry-run 预览：按规则草稿（缺省=当前规则列表 first-match）渲染逐文件
    src→dst，不落库、不动磁盘。与 ``/agents/rules-preview`` 同构。"""
    payload = await _load_preview_payload(db, body)
    if isinstance(payload, JSONResponse):
        return payload

    category = body.category
    if body.rule is not None:
        draft = body.rule
        err = _validate_rule_payload(draft.filter, draft.path_template)
        if err is not None:
            return err
        library = await db.get(
            Library, draft.library_id, options=[selectinload(Library.volume)]
        )
        if library is None:
            return _error(404, "NOT_FOUND", "Library not found")
        rules = [
            SimpleNamespace(
                id=None,
                name=draft.name,
                priority=draft.priority,
                enabled=draft.enabled,
                filter=draft.filter,
                library_id=draft.library_id,
                path_template=draft.path_template,
                file_op=draft.file_op,
                auto_execute=False,
            )
        ]
        libraries = {library.id: organize_service._library_ns(library)}
    else:
        rows = (
            await db.execute(
                select(OrganizeRule).order_by(
                    OrganizeRule.priority.asc(), OrganizeRule.created_at.asc()
                )
            )
        ).scalars().all()
        rules = [organize_service._rule_ns(r) for r in rows]
        libs = (
            await db.execute(
                select(Library).options(selectinload(Library.volume))
            )
        ).scalars().all()
        libraries = {lib.id: organize_service._library_ns(lib) for lib in libs}

    downloader = await organize_service._resolve_downloader(db, payload)
    try:
        result = await asyncio.to_thread(
            organize_service._collect_and_plan,
            payload, downloader, rules, libraries, category,
        )
    except PlanError as e:
        return _error(422, "VALIDATION_ERROR", str(e))

    return success_response(
        OrganizePreviewResponse(
            matched_rule=(
                {"id": result.rule.id, "name": result.rule.name}
                if result.rule is not None
                else None
            ),
            library=(
                {"id": result.library.id, "name": result.library.name}
                if result.library is not None
                else None
            ),
            category=result.category,
            needs_category=result.needs_category,
            uncategorized=result.uncategorized,
            ops=[
                OrganizePreviewOp(
                    op_type=op.op_type, src=op.src, dst=op.dst,
                    size=op.size, reason=op.reason,
                )
                for op in result.ops
            ],
        ).model_dump()
    )


async def _load_preview_payload(
    db: AsyncSession, body: OrganizePreviewRequest
) -> NotificationPayload | JSONResponse:
    """预览输入 → 通知快照：notification 直接取冻结 payload；resource 则按
    通知生成链路现构一份（不落库）。"""
    if body.notification_id is not None:
        n = await db.get(DownloadNotification, body.notification_id)
        if n is None:
            return _error(404, "NOT_FOUND", "Notification not found")
        return NotificationPayload.model_validate(n.payload)

    resource = (
        await db.execute(
            select(FileResource)
            .where(FileResource.id == body.resource_id)
            .options(
                selectinload(FileResource.series).selectinload(TVSeries.episodes),
                selectinload(FileResource.series).selectinload(TVSeries.collection),
                selectinload(FileResource.movie).selectinload(Movie.collection),
                # 资源级合集（franchise 包）：预览快照与触发链路同构。
                selectinload(FileResource.collection),
            )
        )
    ).scalar_one_or_none()
    if resource is None:
        return _error(404, "NOT_FOUND", "Resource not found")
    task = (
        await db.execute(
            select(DownloadTask)
            .where(DownloadTask.file_resource_id == resource.id)
            .order_by(DownloadTask.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if task is None:
        return _error(
            422, "VALIDATION_ERROR",
            "该资源尚无下载任务，无法定位磁盘文件做预览",
        )
    # best-effort 经下载器 RPC 取种子文件清单（预览只读，不停种）；拿不到
    # 则按无 files 清单处理，规划段会以清晰原因失败（与触发链路同语义）。
    torrent_info = None
    if task.transmission_torrent_id is not None and task.downloader_id:
        downloader = await db.get(DownloaderInstance, task.downloader_id)
        if downloader is not None:
            from app.clients.downloader import get_downloader_client

            try:
                torrent_info = await get_downloader_client(
                    downloader
                ).get_torrent_files(task.transmission_torrent_id)
            except Exception:  # noqa: BLE001 — best-effort
                torrent_info = None
    raw = build_payload("preview", None, task, resource, torrent_info)
    return NotificationPayload.model_validate(raw)


@router.get("/organize-rules/{rule_id}")
async def get_organize_rule(rule_id: str, db: AsyncSession = Depends(get_db)):
    rule = await _get_rule_or_404(db, rule_id)
    if isinstance(rule, JSONResponse):
        return rule
    return success_response(OrganizeRuleOut.model_validate(rule).model_dump())


@router.put("/organize-rules/{rule_id}")
async def update_organize_rule(
    rule_id: str, body: OrganizeRuleUpdate, db: AsyncSession = Depends(get_db)
):
    rule = await _get_rule_or_404(db, rule_id)
    if isinstance(rule, JSONResponse):
        return rule
    err = _validate_rule_payload(body.filter, body.path_template)
    if err is not None:
        return err
    if body.library_id is not None:
        if await db.get(Library, body.library_id) is None:
            return _error(404, "NOT_FOUND", "Library not found")
        rule.library_id = body.library_id
    if body.name is not None:
        rule.name = body.name
    if body.priority is not None:
        rule.priority = body.priority
    if body.enabled is not None:
        rule.enabled = body.enabled
    if body.filter is not None:
        rule.filter = body.filter
    if body.path_template is not None:
        rule.path_template = body.path_template
    if body.file_op is not None:
        rule.file_op = body.file_op
    if body.auto_execute is not None:
        rule.auto_execute = body.auto_execute
    await db.commit()
    await db.refresh(rule)
    await _replan_after_config_change(db, f"规则「{rule.name}」更新")
    return success_response(OrganizeRuleOut.model_validate(rule).model_dump())


@router.delete("/organize-rules/{rule_id}")
async def delete_organize_rule(rule_id: str, db: AsyncSession = Depends(get_db)):
    rule = await _get_rule_or_404(db, rule_id)
    if isinstance(rule, JSONResponse):
        return rule
    # 已有计划的 rule_id SET NULL 保留历史（与模型 ondelete 一致，显式执行
    # 以兼容未启用 FK 强制的部署）。
    rule_name = rule.name
    await db.execute(
        update(OrganizePlan)
        .where(OrganizePlan.rule_id == rule_id)
        .values(rule_id=None)
    )
    await db.delete(rule)
    await db.commit()
    await _replan_after_config_change(db, f"规则「{rule_name}」删除")
    return success_response({"deleted": True})


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def _pending_reason(plan: OrganizePlan) -> str | None:
    """pending 计划的派生原因：unclassified（library 未定或模板 {category}
    未定）/ unbound（目标库未绑定卷）；其余状态与可执行的 pending → None。"""
    if plan.status != "pending":
        return None
    if plan.library_id is None:
        return "unclassified"
    if plan.library is not None and plan.library.volume_id is None:
        return "unbound"
    if (
        plan.rule is not None
        and plan.category is None
        and "{category}" in plan.rule.path_template
    ):
        return "unclassified"
    return None


def _plan_list_item(plan: OrganizePlan) -> dict:
    summary = {"total": len(plan.ops), "move": 0, "keep": 0, "movedir": 0}
    for op in plan.ops:
        if op.op_type in summary:
            summary[op.op_type] += 1
    ops = sorted(plan.ops, key=lambda o: o.seq)
    item = OrganizePlanListItem(
        id=plan.id,
        notification_id=plan.notification_id,
        rule_id=plan.rule_id,
        rule_name=plan.rule.name if plan.rule else None,
        library_id=plan.library_id,
        library_name=plan.library.name if plan.library else None,
        category=plan.category,
        status=plan.status,
        error_message=plan.error_message,
        executed_at=plan.executed_at,
        created_at=plan.created_at,
        updated_at=plan.updated_at,
        ops_summary=summary,
        ops_preview=[OrganizePlanOpOut.model_validate(o) for o in ops[:3]],
        pending_reason=_pending_reason(plan),
    )
    return item.model_dump()


_PLAN_LOAD_OPTIONS = (
    selectinload(OrganizePlan.rule),
    selectinload(OrganizePlan.library),
)


@router.get("/organize/plans")
async def list_plans(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: str | None = Query(None),
    library_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    if status is not None and status not in PLAN_STATUSES:
        return _error(422, "VALIDATION_ERROR", f"未知状态: {status}")
    base_q = select(OrganizePlan)
    if status:
        base_q = base_q.where(OrganizePlan.status == status)
    if library_id:
        base_q = base_q.where(OrganizePlan.library_id == library_id)
    total = (
        await db.execute(select(func.count()).select_from(base_q.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            base_q.options(*_PLAN_LOAD_OPTIONS)
            .order_by(OrganizePlan.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return paginated_response(
        [_plan_list_item(p) for p in rows], total, page, page_size
    )


async def _get_plan_or_404(
    db: AsyncSession, plan_id: str
) -> OrganizePlan | JSONResponse:
    plan = await db.get(OrganizePlan, plan_id, options=_PLAN_LOAD_OPTIONS)
    if plan is None:
        return _error(404, "NOT_FOUND", "Organize plan not found")
    return plan


@router.get("/organize/plans/{plan_id}")
async def get_plan(plan_id: str, db: AsyncSession = Depends(get_db)):
    plan = await _get_plan_or_404(db, plan_id)
    if isinstance(plan, JSONResponse):
        return plan
    detail = OrganizePlanDetail(
        **_plan_list_item(plan),
        payload=plan.payload,
        ops=[OrganizePlanOpOut.model_validate(op) for op in plan.ops],
        audit_entries=[
            OrganizeAuditOut.model_validate(a) for a in plan.audit_entries
        ],
    )
    return success_response(detail.model_dump())


@router.post("/organize/plans/{plan_id}/execute", status_code=202)
async def execute_plan_endpoint(plan_id: str, db: AsyncSession = Depends(get_db)):
    """后台执行单个计划（202）。仅 pending/failed 可执行；其余状态 409。"""
    plan = await _get_plan_or_404(db, plan_id)
    if isinstance(plan, JSONResponse):
        return plan
    if plan.status not in ("pending", "failed"):
        return _error(
            409, "INVALID_STATE",
            f"计划当前状态（{plan.status}）不可执行，仅 pending/failed 可执行",
        )
    if plan.library_id is None:
        return _error(
            409, "INVALID_STATE", "待分类计划请先指定目标库（classify）再执行"
        )
    if plan.library is not None and plan.library.volume_id is None:
        return _error(
            409, "INVALID_STATE",
            "目标库未绑定存储卷（待绑定），请先补绑定再执行",
        )
    if (
        plan.rule is not None
        and plan.category is None
        and "{category}" in plan.rule.path_template
    ):
        return _error(
            409, "INVALID_STATE", "计划尚未指定影片类别，请先分类（classify）"
        )
    organize_service.schedule_auto_execute(plan.id)
    return success_response({"id": plan.id, "status": plan.status})


@router.post("/organize/plans/execute-batch")
async def execute_plans_batch(
    body: OrganizeExecuteBatchRequest, db: AsyncSession = Depends(get_db)
):
    """批量执行：锁内逐计划顺序执行，单个失败不影响其余。"""
    pairs = await organize_service.execute_plans(db, body.plan_ids)
    return success_response(
        {"results": [{"plan_id": pid, "status": status} for pid, status in pairs]}
    )


@router.post("/organize/plans/{plan_id}/classify")
async def classify_plan_endpoint(
    plan_id: str, body: OrganizeClassifyRequest, db: AsyncSession = Depends(get_db)
):
    """待分类计划人工指定 library（和/或 category）并重渲染全部 op 的 dst。"""
    plan = await _get_plan_or_404(db, plan_id)
    if isinstance(plan, JSONResponse):
        return plan
    if plan.status not in ("pending", "failed"):
        return _error(
            409, "INVALID_STATE",
            f"计划当前状态（{plan.status}）不可分类，仅 pending/failed 可分类",
        )
    if await db.get(Library, body.library_id) is None:
        return _error(404, "NOT_FOUND", "Library not found")
    try:
        plan = await organize_service.classify_plan(
            db, plan.id, body.library_id, body.category
        )
    except organize_service.OrganizeError as e:
        return _error(422, "VALIDATION_ERROR", str(e))
    # classify_plan 内部已 commit：updated_at（onupdate 列）随 UPDATE 过期，
    # 重建的 ops 按 plan_id 直接落行、library 关系仍指向旧值——全部刷新后
    # 再组装响应，避免访问过期属性触发异步上下文外的惰性 IO。
    await db.refresh(plan)
    await db.refresh(plan, attribute_names=["ops", "library"])
    return success_response(_plan_list_item(plan))


@router.post("/organize/plans/{plan_id}/cancel")
async def cancel_plan(plan_id: str, db: AsyncSession = Depends(get_db)):
    """取消 pending/failed 计划 → cancelled；done/running 409。"""
    plan = await _get_plan_or_404(db, plan_id)
    if isinstance(plan, JSONResponse):
        return plan
    if plan.status not in ("pending", "failed"):
        return _error(
            409, "INVALID_STATE",
            f"计划当前状态（{plan.status}）不可取消，仅 pending/failed 可取消",
        )
    plan.status = "cancelled"
    db.add(
        OrganizeAuditEntry(
            plan_id=plan.id, action="cancelled",
            detail={"from_status": "pending/failed"},
        )
    )
    await db.commit()
    return success_response({"id": plan.id, "status": plan.status})


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


@router.get("/organize/audit")
async def list_audit_entries(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    plan_id: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    base_q = select(OrganizeAuditEntry)
    if plan_id:
        base_q = base_q.where(OrganizeAuditEntry.plan_id == plan_id)
    total = (
        await db.execute(select(func.count()).select_from(base_q.subquery()))
    ).scalar_one()
    rows = (
        await db.execute(
            base_q.order_by(OrganizeAuditEntry.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).scalars().all()
    return paginated_response(
        [OrganizeAuditOut.model_validate(a).model_dump() for a in rows],
        total, page, page_size,
    )
