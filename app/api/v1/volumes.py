"""StorageVolume（逻辑存储卷）API 路由。

docs/design/file-organization.md「API」：逻辑卷 CRUD + 挂载探测。一切配置
面路径引用存 ``(volume_id, subpath)``，使用处动态解析
``volume.mount_path + subpath``——改挂载点一处修改全局生效。
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.downloader import DownloaderInstance
from app.models.library import Library
from app.models.media_server import MediaServerBinding
from app.models.storage_volume import StorageVolume
from app.schemas.common import paginated_response, success_response
from app.schemas.storage_volume import (
    StorageVolumeCreate,
    StorageVolumeResponse,
    StorageVolumeUpdate,
)
from app.services.volume_service import check_mount

router = APIRouter()


def _error(status_code: int, code: str, message: str, details=None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "success": False,
            "data": None,
            "error": {"code": code, "message": message, "details": details},
            "meta": {},
        },
    )


def _mount_path_must_exist(mount_path: str) -> JSONResponse | None:
    """保存时探测存在性：不存在 422（写权限仅 check 时探测，不拦截保存）。"""
    if not os.path.isdir(mount_path):
        return _error(
            422, "VALIDATION_ERROR",
            f"mount_path 不存在或不是目录：{mount_path}",
        )
    return None


async def _name_must_be_unique(
    db: AsyncSession, name: str, exclude_id: str | None = None
) -> JSONResponse | None:
    q = select(func.count()).select_from(StorageVolume).where(
        StorageVolume.name == name
    )
    if exclude_id is not None:
        q = q.where(StorageVolume.id != exclude_id)
    if (await db.execute(q)).scalar_one() > 0:
        return _error(
            409, "DUPLICATE_SUBMISSION", f"存储卷名称已存在：{name}"
        )
    return None


@router.get("/volumes")
async def list_volumes(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    offset = (page - 1) * page_size
    total = (
        await db.execute(select(func.count()).select_from(StorageVolume))
    ).scalar_one()
    rows = (
        await db.execute(
            select(StorageVolume)
            .order_by(StorageVolume.created_at.desc())
            .offset(offset)
            .limit(page_size)
        )
    ).scalars().all()
    return paginated_response(
        [StorageVolumeResponse.model_validate(v).model_dump() for v in rows],
        total=total, page=page, page_size=page_size,
    )


@router.post("/volumes", status_code=201)
async def create_volume(
    body: StorageVolumeCreate, db: AsyncSession = Depends(get_db)
):
    if err := _mount_path_must_exist(body.mount_path):
        return err
    if err := await _name_must_be_unique(db, body.name):
        return err
    volume = StorageVolume(
        name=body.name, mount_path=body.mount_path, remark=body.remark
    )
    db.add(volume)
    await db.flush()
    await db.refresh(volume)
    return success_response(
        StorageVolumeResponse.model_validate(volume).model_dump()
    )


@router.get("/volumes/{volume_id}")
async def get_volume(volume_id: str, db: AsyncSession = Depends(get_db)):
    volume = await db.get(StorageVolume, volume_id)
    if volume is None:
        return _error(404, "NOT_FOUND", "Storage volume not found")
    return success_response(
        StorageVolumeResponse.model_validate(volume).model_dump()
    )


@router.put("/volumes/{volume_id}")
async def update_volume(
    volume_id: str,
    body: StorageVolumeUpdate,
    db: AsyncSession = Depends(get_db),
):
    volume = await db.get(StorageVolume, volume_id)
    if volume is None:
        return _error(404, "NOT_FOUND", "Storage volume not found")
    update_data = body.model_dump(exclude_unset=True)
    # 改 mount_path 全局生效：所有卷引用在使用处动态解析。
    if "mount_path" in update_data:
        if err := _mount_path_must_exist(update_data["mount_path"]):
            return err
    if "name" in update_data:
        if err := await _name_must_be_unique(
            db, update_data["name"], exclude_id=volume_id
        ):
            return err
    for key, value in update_data.items():
        setattr(volume, key, value)
    await db.flush()
    await db.refresh(volume)
    return success_response(
        StorageVolumeResponse.model_validate(volume).model_dump()
    )


@router.delete("/volumes/{volume_id}")
async def delete_volume(volume_id: str, db: AsyncSession = Depends(get_db)):
    volume = await db.get(StorageVolume, volume_id)
    if volume is None:
        return _error(404, "NOT_FOUND", "Storage volume not found")
    # 被下载器卷绑定 / 媒体服务器绑定 / Library 库根引用 → 409。
    bound = (
        await db.execute(
            select(DownloaderInstance.id, DownloaderInstance.name).where(
                DownloaderInstance.volume_id == volume_id
            )
        )
    ).all()
    if bound:
        payload = [{"id": did, "name": name} for did, name in bound]
        names = ", ".join(d["name"] for d in payload)
        return _error(
            409, "DELETE_BLOCKED",
            f"存储卷仍被 {len(payload)} 个下载器绑定：{names}",
            details={"downloaders": payload},
        )
    bindings = (
        await db.execute(
            select(MediaServerBinding.id, MediaServerBinding.server_path_prefix)
            .where(MediaServerBinding.volume_id == volume_id)
        )
    ).all()
    if bindings:
        payload = [
            {"id": bid, "server_path_prefix": prefix} for bid, prefix in bindings
        ]
        prefixes = ", ".join(d["server_path_prefix"] for d in payload)
        return _error(
            409, "DELETE_BLOCKED",
            f"存储卷仍被 {len(payload)} 条媒体服务器绑定引用：{prefixes}",
            details={"media_server_bindings": payload},
        )
    libraries = (
        await db.execute(
            select(Library.id, Library.name).where(Library.volume_id == volume_id)
        )
    ).all()
    if libraries:
        payload = [{"id": lid, "name": name} for lid, name in libraries]
        names = ", ".join(d["name"] for d in payload)
        return _error(
            409, "DELETE_BLOCKED",
            f"存储卷仍被 {len(payload)} 个媒体库引用：{names}",
            details={"libraries": payload},
        )
    await db.delete(volume)
    await db.commit()
    return success_response({"deleted": True})


@router.post("/volumes/{volume_id}/check")
async def check_volume(volume_id: str, db: AsyncSession = Depends(get_db)):
    """探测挂载点存在性与写权限；writable 仅作展示提示。"""
    volume = await db.get(StorageVolume, volume_id)
    if volume is None:
        return _error(404, "NOT_FOUND", "Storage volume not found")
    return success_response(check_mount(volume.mount_path))
