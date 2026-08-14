"""媒体服务器（MediaServerInstance）API 路由。

docs/design/file-organization.md「API · Media Servers」（R2）：服务器
CRUD + test 连通性 + scan 扫描派生 Library。bindings 内嵌在
create/update 中整体替换；删除服务器时 bindings 随 FK CASCADE，派生
Library 的 ``media_server_id`` SET NULL 保留行。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.library import Library
from app.models.media_server import MediaServerBinding, MediaServerInstance
from app.models.storage_volume import StorageVolume
from app.schemas.common import success_response
from app.schemas.media_server import (
    MediaServerCreate,
    MediaServerListItem,
    MediaServerOut,
    MediaServerScanResult,
    MediaServerTestResult,
    MediaServerUpdate,
)
from app.services import media_server_service
from app.services.media_server_client import MediaServerError

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


def _server_out(server: MediaServerInstance) -> dict:
    return MediaServerOut(
        id=server.id,
        name=server.name,
        type=server.type,
        url=server.url,
        enabled=server.enabled,
        bindings=[
            {
                "id": b.id,
                "server_path_prefix": b.server_path_prefix,
                "volume_id": b.volume_id,
                "subpath": b.subpath,
            }
            for b in server.bindings
        ],
        created_at=server.created_at,
        updated_at=server.updated_at,
    ).model_dump()


async def _get_server_or_404(
    db: AsyncSession, server_id: str
) -> MediaServerInstance | JSONResponse:
    server = await db.get(
        MediaServerInstance, server_id,
        options=[selectinload(MediaServerInstance.bindings)],
    )
    if server is None:
        return _error(404, "NOT_FOUND", "Media server not found")
    return server


async def _reload_server(
    db: AsyncSession, server_id: str
) -> MediaServerInstance:
    """flush 后带 bindings 关系强制重取（populate_existing）：避免 refresh
    过期关系后在异步上下文触发惰性 IO，同时拿到服务端默认时间戳。"""
    return (
        await db.execute(
            select(MediaServerInstance)
            .where(MediaServerInstance.id == server_id)
            .options(selectinload(MediaServerInstance.bindings))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _validate_bindings(
    db: AsyncSession, bindings
) -> JSONResponse | None:
    """校验绑定引用的卷存在（404）；prefix 非空与子路径格式由 schema 保证。"""
    for binding in bindings or []:
        if await db.get(StorageVolume, binding.volume_id) is None:
            return _error(
                404, "NOT_FOUND",
                f"绑定引用的存储卷不存在：{binding.volume_id}",
            )
    return None


def _replace_bindings(server: MediaServerInstance, bindings) -> None:
    """整体替换 bindings（先删后插；关系 cascade delete-orphan 落删除）。"""
    server.bindings.clear()
    for b in bindings:
        server.bindings.append(
            MediaServerBinding(
                server_path_prefix=b.server_path_prefix,
                volume_id=b.volume_id,
                subpath=b.subpath,
            )
        )


@router.get("/media-servers")
async def list_media_servers(db: AsyncSession = Depends(get_db)):
    rows = (
        await db.execute(
            select(MediaServerInstance)
            .options(selectinload(MediaServerInstance.bindings))
            .order_by(MediaServerInstance.created_at.asc())
        )
    ).scalars().all()
    counts = dict(
        (
            await db.execute(
                select(Library.media_server_id, func.count())
                .where(Library.media_server_id.is_not(None))
                .group_by(Library.media_server_id)
            )
        ).all()
    )
    unbound_counts = dict(
        (
            await db.execute(
                select(Library.media_server_id, func.count())
                .where(
                    Library.media_server_id.is_not(None),
                    Library.volume_id.is_(None),
                )
                .group_by(Library.media_server_id)
            )
        ).all()
    )
    items = []
    for server in rows:
        item = MediaServerListItem(
            **_server_out(server),
            library_count=counts.get(server.id, 0),
            unbound_library_count=unbound_counts.get(server.id, 0),
        )
        items.append(item.model_dump())
    return success_response(items)


@router.post("/media-servers", status_code=201)
async def create_media_server(
    body: MediaServerCreate, db: AsyncSession = Depends(get_db)
):
    if err := await _validate_bindings(db, body.bindings):
        return err
    server = MediaServerInstance(
        name=body.name, type=body.type, url=body.url,
        token=body.token, enabled=body.enabled,
    )
    _replace_bindings(server, body.bindings)
    db.add(server)
    await db.flush()
    server = await _reload_server(db, server.id)
    return success_response(_server_out(server))


@router.get("/media-servers/{server_id}")
async def get_media_server(server_id: str, db: AsyncSession = Depends(get_db)):
    server = await _get_server_or_404(db, server_id)
    if isinstance(server, JSONResponse):
        return server
    return success_response(_server_out(server))


@router.put("/media-servers/{server_id}")
async def update_media_server(
    server_id: str,
    body: MediaServerUpdate,
    db: AsyncSession = Depends(get_db),
):
    server = await _get_server_or_404(db, server_id)
    if isinstance(server, JSONResponse):
        return server
    if err := await _validate_bindings(db, body.bindings):
        return err
    if body.name is not None:
        server.name = body.name
    if body.type is not None:
        server.type = body.type
    if body.url is not None:
        server.url = body.url
    if body.token is not None:
        server.token = body.token
    if body.enabled is not None:
        server.enabled = body.enabled
    if body.bindings is not None:
        _replace_bindings(server, body.bindings)
    await db.flush()
    server = await _reload_server(db, server.id)
    return success_response(_server_out(server))


@router.delete("/media-servers/{server_id}")
async def delete_media_server(server_id: str, db: AsyncSession = Depends(get_db)):
    server = await _get_server_or_404(db, server_id)
    if isinstance(server, JSONResponse):
        return server
    # 派生 Library 的 media_server_id SET NULL 保留行（显式执行，兼容未启用
    # FK 强制的部署；bindings 随 ORM cascade delete-orphan 删除）。
    await db.execute(
        update(Library)
        .where(Library.media_server_id == server_id)
        .values(media_server_id=None)
    )
    await db.delete(server)
    await db.commit()
    return success_response({"deleted": True})


@router.post("/media-servers/{server_id}/test")
async def test_media_server(server_id: str, db: AsyncSession = Depends(get_db)):
    """连通性 + 凭证校验 → {ok, server_version?}。"""
    server = await _get_server_or_404(db, server_id)
    if isinstance(server, JSONResponse):
        return server
    try:
        ok, detail = await media_server_service.test_server(server)
    except MediaServerError as e:
        ok, detail = False, str(e)
    return success_response(
        MediaServerTestResult(
            ok=ok,
            server_version=detail if ok else None,
            message=None if ok else detail,
        ).model_dump()
    )


@router.post("/media-servers/{server_id}/scan")
async def scan_media_server(server_id: str, db: AsyncSession = Depends(get_db)):
    """扫描 sections/虚拟目录，幂等 upsert Library → {created, updated, unbound}。"""
    server = await _get_server_or_404(db, server_id)
    if isinstance(server, JSONResponse):
        return server
    if not server.enabled:
        return _error(
            409, "INVALID_STATE", "服务器已停用，启用后才能扫描"
        )
    try:
        stats = await media_server_service.scan_server(db, server)
    except MediaServerError as e:
        return _error(502, "MEDIA_SERVER_ERROR", str(e))
    return success_response(MediaServerScanResult(**stats).model_dump())
