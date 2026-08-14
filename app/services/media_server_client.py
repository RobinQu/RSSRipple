"""媒体服务器 adapter 层（Plex / Emby / Jellyfin）。

内置整理子系统 R2（docs/design/file-organization.md「MediaServerInstance /
MediaServerBinding」）：扫描派生 Library 与执行后刷新都经此统一接口——

- ``test_connection()``：连通性 + 凭证校验，返回 ``(ok, 版本或错误描述)``。
- ``list_libraries()``：``[{key, name, kind, paths}]``——Plex 取
  ``GET /library/sections`` 的 movie/show section（Location 列表）；Emby/
  Jellyfin 取 ``GET /Library/VirtualFolders``（CollectionType movies/
  tvshows，Locations）。kind: ``tv | movie``。
- ``refresh(library_key, *, path=None)``：Plex 优先按触及目录 partial
  refresh（``?path=``），失败退整库刷新；Emby/Jellyfin 无稳定公开的按库
  刷新端点（按 item 刷新需要先解析 item id），选型为整库刷新
  ``POST /Library/Refresh``，``library_key``/``path`` 仅留档不使用。

认证选型：Plex 用 ``X-Plex-Token`` 头；Emby 用 ``api_key`` 查询参数；
Jellyfin 用 ``X-Emby-Token`` 头（其兼容头，避免拼 Authorization
MediaBrowser 串）。httpx async、短超时；网络/HTTP 错误统一抛
:exc:`MediaServerError`（``test_connection`` 例外：吞掉错误返回
``(False, 描述)``，对齐 TransmissionWrapper.test_connection 惯例）。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MEDIA_SERVER_TIMEOUT_SECONDS = 10.0


class MediaServerError(Exception):
    """媒体服务器连接/接口错误（网络失败、非 2xx、响应结构不符）。"""


def _client(server: Any, *, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=(server.url or "").rstrip("/"),
        headers=headers or {},
        timeout=MEDIA_SERVER_TIMEOUT_SECONDS,
    )


async def _get_json(client: httpx.AsyncClient, url: str, **kw: Any) -> Any:
    try:
        resp = await client.get(url, **kw)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPError as e:
        raise MediaServerError(f"GET {url} 失败：{e}") from e


# --------------------------------------------------------------------------- Plex


class PlexClient:
    """Plex adapter：X-Plex-Token 认证，JSON 响应（Accept: application/json）。"""

    def __init__(self, server: Any) -> None:
        self._server = server

    def _new_client(self) -> httpx.AsyncClient:
        return _client(
            self._server,
            headers={
                "X-Plex-Token": self._server.token or "",
                "Accept": "application/json",
            },
        )

    async def test_connection(self) -> tuple[bool, str | None]:
        """GET /identity 校验连通性与凭证；返回 (ok, server_version 或错误)。"""
        try:
            async with self._new_client() as client:
                data = await _get_json(client, "/identity")
            version = (data.get("MediaContainer") or {}).get("version")
            return True, version or "Plex"
        except Exception as e:  # noqa: BLE001 — 连通性探测不抛出
            return False, str(e)

    async def list_libraries(self) -> list[dict]:
        """GET /library/sections → movie/show section 的 key/title/Location。"""
        async with self._new_client() as client:
            data = await _get_json(client, "/library/sections")
        container = data.get("MediaContainer") or {}
        sections = container.get("Directory") or []
        libraries: list[dict] = []
        for section in sections:
            kind = {"show": "tv", "movie": "movie"}.get(section.get("type"))
            if kind is None:
                continue  # artist/photo 等非整理目标
            paths = [
                loc["path"]
                for loc in (section.get("Location") or [])
                if loc.get("path")
            ]
            libraries.append({
                "key": str(section.get("key")),
                "name": section.get("title") or "",
                "kind": kind,
                "paths": paths,
            })
        return libraries

    async def refresh(self, library_key: str, *, path: str | None = None) -> None:
        """优先按触及目录 partial refresh（?path=），失败退整库刷新。"""
        async with self._new_client() as client:
            if path:
                try:
                    resp = await client.get(
                        f"/library/sections/{library_key}/refresh",
                        params={"path": path},
                    )
                    resp.raise_for_status()
                    return
                except httpx.HTTPError as e:
                    logger.info(
                        "[media-server] Plex partial refresh 失败（section %s, "
                        "path %s）：%s；退整库刷新", library_key, path, e,
                    )
            try:
                resp = await client.get(f"/library/sections/{library_key}/refresh")
                resp.raise_for_status()
            except httpx.HTTPError as e:
                raise MediaServerError(
                    f"Plex 刷新失败（section {library_key}）：{e}"
                ) from e


# ----------------------------------------------------------------- Emby/Jellyfin


class _EmbyLikeClient:
    """Emby/Jellyfin 共有 adapter：VirtualFolders 扫描 + 整库刷新。

    两者 API 同构，差异仅在认证方式（``_auth_kw``）与少量路径前缀——
    Jellyfin 兼容 ``X-Emby-Token`` 头，Emby 原生 ``api_key`` 查询参数。
    按库 partial refresh 无稳定公开端点（需先解析 item id），选型为
    ``POST /Library/Refresh`` 整库刷新。
    """

    def __init__(self, server: Any) -> None:
        self._server = server

    def _auth_kw(self) -> dict[str, Any]:
        raise NotImplementedError

    def _new_client(self) -> httpx.AsyncClient:
        return _client(self._server, headers=self._auth_kw().get("headers", {}))

    async def _get(self, client: httpx.AsyncClient, url: str) -> Any:
        return await _get_json(client, url, params=self._auth_kw().get("params"))

    async def test_connection(self) -> tuple[bool, str | None]:
        """GET /System/Info 校验连通性与凭证；返回 (ok, Version 或错误)。"""
        try:
            async with self._new_client() as client:
                data = await self._get(client, "/System/Info")
            return True, data.get("Version") or self._server.type
        except Exception as e:  # noqa: BLE001 — 连通性探测不抛出
            return False, str(e)

    async def list_libraries(self) -> list[dict]:
        """GET /Library/VirtualFolders → CollectionType movies/tvshows。"""
        async with self._new_client() as client:
            folders = await self._get(client, "/Library/VirtualFolders")
        libraries: list[dict] = []
        for folder in folders or []:
            kind = {"movies": "movie", "tvshows": "tv"}.get(
                folder.get("CollectionType")
            )
            if kind is None:
                continue  # mixedcontent/music 等非整理目标
            libraries.append({
                # Jellyfin 带 ItemId；Emby 只有 Name——虚拟目录标识兼作
                # 刷新寻址留档（refresh 实际走整库端点）。
                "key": str(folder.get("ItemId") or folder.get("Name") or ""),
                "name": folder.get("Name") or "",
                "kind": kind,
                "paths": [p for p in (folder.get("Locations") or []) if p],
            })
        return libraries

    async def refresh(self, library_key: str, *, path: str | None = None) -> None:
        """整库刷新（library_key/path 留档不使用，见模块 docstring 选型）。"""
        async with self._new_client() as client:
            try:
                resp = await client.post(
                    "/Library/Refresh", params=self._auth_kw().get("params")
                )
                resp.raise_for_status()
            except httpx.HTTPError as e:
                raise MediaServerError(
                    f"{self._server.type} 刷新失败：{e}"
                ) from e


class EmbyClient(_EmbyLikeClient):
    """Emby：``api_key`` 查询参数认证。"""

    def _auth_kw(self) -> dict[str, Any]:
        return {"params": {"api_key": self._server.token or ""}}


class JellyfinClient(_EmbyLikeClient):
    """Jellyfin：``X-Emby-Token`` 兼容头认证。"""

    def _auth_kw(self) -> dict[str, Any]:
        return {"headers": {"X-Emby-Token": self._server.token or ""}}


# ---------------------------------------------------------------------- 工厂

_CLIENT_TYPES = {
    "plex": PlexClient,
    "emby": EmbyClient,
    "jellyfin": JellyfinClient,
}


def get_client(server: Any):
    """按 ``server.type`` 构造 adapter；未知类型 → MediaServerError。"""
    cls = _CLIENT_TYPES.get(getattr(server, "type", None))
    if cls is None:
        raise MediaServerError(f"未知媒体服务器类型：{getattr(server, 'type', None)!r}")
    return cls(server)
