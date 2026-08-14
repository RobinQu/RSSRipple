"""媒体服务器扫描派生 Library 与刷新寻址（organize R2）。

docs/design/file-organization.md「扫描派生 Library」：

- :func:`scan_server`（幂等）：``list_libraries()`` → 每 ``(section_key,
  path)`` 经该服务器 bindings **最长前缀匹配**解析 ``(volume_id,
  root_subpath)`` → upsert Library（唯一键 ``(media_server_id,
  section_key, server_path)``）；多 Location 的 section 拆成每 location
  一条；未命中绑定 → ``volume_id=NULL`` 的**待绑定**行（UI 引导补绑定后
  重扫，或经 ``PUT /libraries/{id}`` 就地修复）；服务器上已消失的库
  **不删不标**（保持简单：可能挂载暂时离线，删行会波及其整理计划）。
- :func:`refresh_library`：执行后刷新改址——经 ``Library →
  MediaServerInstance`` 寻址；服务器缺失/停用/无 section_key → 跳过；
  刷新失败向上抛（调用方 best-effort 捕获，不改写计划状态）。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.models.library import Library
from app.services.media_server_client import MediaServerError, get_client

logger = logging.getLogger(__name__)

__all__ = [
    "MediaServerError",
    "get_client",
    "refresh_library",
    "resolve_server_path",
    "scan_server",
    "test_server",
]


def resolve_server_path(
    bindings: list[Any], server_path: str
) -> tuple[str, str | None] | None:
    """服务器视角路径 → ``(volume_id, root_subpath)``，最长前缀匹配。

    ``bindings`` 为 MediaServerBinding（或同形对象，需带
    ``server_path_prefix`` / ``volume_id`` / ``subpath``）；无命中返回
    None（该路径待绑定）。语义同 P1 path_map 的最长前缀匹配，但目标是
    结构化卷引用而非字符串替换。
    """
    best = None
    best_len = -1
    for binding in bindings:
        prefix = (binding.server_path_prefix or "").rstrip("/")
        if not prefix:
            continue
        if server_path == prefix or server_path.startswith(prefix + "/"):
            if len(prefix) > best_len:
                best, best_len = binding, len(prefix)
    if best is None:
        return None
    suffix = server_path[best_len:].strip("/")
    parts = [p for p in ((best.subpath or "").strip("/"), suffix) if p]
    return best.volume_id, "/".join(parts) if parts else None


async def test_server(server: Any) -> tuple[bool, str | None]:
    """连通性 + 凭证校验：返回 (ok, server_version 或错误描述)。"""
    client = get_client(server)
    return await client.test_connection()


async def scan_server(db, server: Any) -> dict:
    """扫描服务器 sections/虚拟目录，幂等 upsert Library。

    返回 ``{created, updated, unbound}`` 计数。``server`` 须已加载
    ``bindings`` 关系。连接/接口失败抛 :exc:`MediaServerError`（路由层
    转 502）。
    """
    client = get_client(server)
    sections = await client.list_libraries()
    stats = {"created": 0, "updated": 0, "unbound": 0}
    for section in sections:
        for path in section["paths"]:
            resolved = resolve_server_path(list(server.bindings), path)
            volume_id, root_subpath = resolved if resolved else (None, None)
            existing = (
                await db.execute(
                    select(Library).where(
                        Library.media_server_id == server.id,
                        Library.section_key == section["key"],
                        Library.server_path == path,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                db.add(Library(
                    name=section["name"],
                    media_server_id=server.id,
                    section_key=section["key"],
                    server_path=path,
                    volume_id=volume_id,
                    root_subpath=root_subpath,
                    kind=section["kind"],
                ))
                stats["created"] += 1
            else:
                # 重扫更新：显示名/类型/绑定解析结果以服务器现状为准。
                existing.name = section["name"]
                existing.kind = section["kind"]
                existing.volume_id = volume_id
                existing.root_subpath = root_subpath
                stats["updated"] += 1
            if volume_id is None:
                stats["unbound"] += 1
    await db.commit()
    logger.info(
        "[media-server] 服务器 %s 扫描完成：%s", server.name, stats
    )
    return stats


async def refresh_library(library: Any, *, path: str | None = None) -> bool:
    """经 Library → MediaServerInstance 寻址刷新；跳过返回 False。

    服务器缺失/停用或无 section_key（手工/旧数据行）→ 跳过；刷新失败
    向上抛 :exc:`MediaServerError`，由调用方 best-effort 捕获。
    """
    server = getattr(library, "media_server", None)
    section_key = getattr(library, "section_key", None)
    if server is None or not server.enabled or not section_key:
        return False
    client = get_client(server)
    await client.refresh(section_key, path=path)
    logger.info(
        "[media-server] 已触发刷新：%s section %s（path=%s）",
        server.name, section_key, path,
    )
    return True
