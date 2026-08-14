"""媒体服务器扫描 service（media_server_service）单元测试。

覆盖：扫描 upsert 幂等（created→updated）、bindings 最长前缀匹配、
未命中 → volume_id=NULL 待绑定、补绑定后重扫就地更新、refresh_library
寻址（停用/无 section_key 跳过）。adapter 在
``app.services.media_server_service.get_client`` 处打桩。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.models.library import Library
from app.models.media_server import MediaServerBinding, MediaServerInstance
from app.models.storage_volume import StorageVolume
from app.services import media_server_service
from app.services.media_server_service import (
    refresh_library,
    resolve_server_path,
    scan_server,
)


def _uuid() -> str:
    return str(uuid.uuid4())


async def _volume(db, name):
    volume = StorageVolume(id=_uuid(), name=name, mount_path=f"/mnt/{name}")
    db.add(volume)
    await db.flush()
    return volume


async def _server(db, bindings=()):
    server = MediaServerInstance(
        id=_uuid(), name="plex", type="plex",
        url="http://plex:32400", token="tok",
    )
    for prefix, volume_id, subpath in bindings:
        server.bindings.append(
            MediaServerBinding(
                server_path_prefix=prefix, volume_id=volume_id,
                subpath=subpath,
            )
        )
    db.add(server)
    await db.commit()
    # 重新带入 bindings 关系：scan_server 以 list(server.bindings) 消费，
    # 异步上下文下不允许触发惰性加载。
    from sqlalchemy.orm import selectinload

    return (
        await db.execute(
            select(MediaServerInstance)
            .where(MediaServerInstance.id == server.id)
            .options(selectinload(MediaServerInstance.bindings))
        )
    ).scalar_one()


def _fake_client(sections):
    return SimpleNamespace(list_libraries=AsyncMock(return_value=sections))


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(
        media_server_service, "get_client", lambda server: client
    )


async def _libraries(db):
    return (
        await db.execute(select(Library).order_by(Library.created_at))
    ).scalars().all()


SECTIONS = [
    {"key": "1", "name": "Movies", "kind": "movie",
     "paths": ["/data/Movies"]},
    {"key": "2", "name": "Shows", "kind": "tv",
     "paths": ["/data/TV", "/data/TV2"]},
]


# ---------------------------------------------------------------- 最长前缀匹配


async def test_resolve_server_path_longest_prefix(db_session):
    vol_a = await _volume(db_session, "a")
    vol_b = await _volume(db_session, "b")
    bindings = [
        SimpleNamespace(server_path_prefix="/data", volume_id=vol_a.id,
                        subpath="root"),
        SimpleNamespace(server_path_prefix="/data/Movies", volume_id=vol_b.id,
                        subpath="films"),
    ]
    # 更长的 /data/Movies 优先于 /data
    assert resolve_server_path(bindings, "/data/Movies") == (vol_b.id, "films")
    assert resolve_server_path(bindings, "/data/Movies/Sub") == (
        vol_b.id, "films/Sub"
    )
    assert resolve_server_path(bindings, "/data/TV") == (vol_a.id, "root/TV")
    # 边界：/database 不是 /data 的前缀命中；完全无命中 → None
    assert resolve_server_path(bindings, "/database/x") is None
    assert resolve_server_path(bindings, "/elsewhere") is None
    assert resolve_server_path([], "/data/TV") is None


# ---------------------------------------------------------------- 扫描 upsert


async def test_scan_creates_libraries_with_binding_resolution(
    db_session, monkeypatch
):
    vol_a = await _volume(db_session, "a")
    vol_b = await _volume(db_session, "b")
    server = await _server(db_session, bindings=[
        ("/data", vol_a.id, "root"),
        ("/data/Movies", vol_b.id, "films"),
    ])
    _patch_client(monkeypatch, _fake_client(SECTIONS))

    stats = await scan_server(db_session, server)
    assert stats == {"created": 3, "updated": 0, "unbound": 0}
    libs = {lib.server_path: lib for lib in await _libraries(db_session)}
    assert libs["/data/Movies"].volume_id == vol_b.id
    assert libs["/data/Movies"].root_subpath == "films"
    assert libs["/data/TV"].volume_id == vol_a.id
    assert libs["/data/TV"].root_subpath == "root/TV"
    assert libs["/data/TV"].kind == "tv" and libs["/data/TV"].section_key == "2"
    assert libs["/data/Movies"].media_server_id == server.id


async def test_scan_idempotent_rescan_updates(db_session, monkeypatch):
    vol = await _volume(db_session, "a")
    server = await _server(db_session, bindings=[("/data", vol.id, "")])
    client = _fake_client(SECTIONS)
    _patch_client(monkeypatch, client)

    stats = await scan_server(db_session, server)
    assert stats == {"created": 3, "updated": 0, "unbound": 0}
    # 重扫：同名同路径 → updated；显示名变化被刷新
    changed = [dict(s) for s in SECTIONS]
    changed[0]["name"] = "Movies HD"
    client.list_libraries = AsyncMock(return_value=changed)
    stats = await scan_server(db_session, server)
    assert stats == {"created": 0, "updated": 3, "unbound": 0}
    libs = await _libraries(db_session)
    assert len(libs) == 3  # 幂等：不重复建行
    movies = next(lib for lib in libs if lib.server_path == "/data/Movies")
    assert movies.name == "Movies HD"


async def test_scan_unbound_then_repair_on_rescan(db_session, monkeypatch):
    server = await _server(db_session)  # 无绑定
    _patch_client(monkeypatch, _fake_client(SECTIONS))

    stats = await scan_server(db_session, server)
    assert stats == {"created": 3, "updated": 0, "unbound": 3}
    libs = await _libraries(db_session)
    assert all(lib.volume_id is None for lib in libs)  # 待绑定

    # 补绑定后重扫：已有待绑定行就地解析
    vol = await _volume(db_session, "a")
    server.bindings.append(
        MediaServerBinding(
            server_path_prefix="/data", volume_id=vol.id, subpath="m"
        )
    )
    await db_session.commit()
    stats = await scan_server(db_session, server)
    assert stats == {"created": 0, "updated": 3, "unbound": 0}
    libs = await _libraries(db_session)
    assert len(libs) == 3
    movies = next(lib for lib in libs if lib.server_path == "/data/Movies")
    assert movies.volume_id == vol.id
    assert movies.root_subpath == "m/Movies"


async def test_scan_keeps_disappeared_libraries(db_session, monkeypatch):
    """服务器上已消失的库不删不标（可能挂载暂时离线）。"""
    vol = await _volume(db_session, "a")
    server = await _server(db_session, bindings=[("/data", vol.id, "")])
    client = _fake_client(SECTIONS)
    _patch_client(monkeypatch, client)
    await scan_server(db_session, server)

    client.list_libraries = AsyncMock(return_value=SECTIONS[:1])
    stats = await scan_server(db_session, server)
    assert stats["created"] == 0 and stats["updated"] == 1
    assert len(await _libraries(db_session)) == 3  # 不删


# ---------------------------------------------------------------- 刷新寻址


async def _load_library(db, library_id):
    """带 media_server 关系重取 Library（异步上下文禁惰性加载）。"""
    from sqlalchemy.orm import selectinload

    return (
        await db.execute(
            select(Library)
            .where(Library.id == library_id)
            .options(selectinload(Library.media_server))
        )
    ).scalar_one()


async def test_refresh_library_delegates_to_adapter(db_session, monkeypatch):
    server = await _server(db_session)
    lib = Library(
        id=_uuid(), name="TV", kind="tv", media_server_id=server.id,
        section_key="2",
    )
    db_session.add(lib)
    await db_session.commit()
    client = SimpleNamespace(refresh=AsyncMock())
    _patch_client(monkeypatch, client)

    # refresh_library 经 ORM 关系寻址 media_server
    loaded = await _load_library(db_session, lib.id)
    ok = await refresh_library(loaded, path="/data/TV/Show")
    assert ok is True
    client.refresh.assert_awaited_once_with("2", path="/data/TV/Show")


async def test_refresh_library_skips_disabled_or_unlinked(db_session, monkeypatch):
    server = await _server(db_session)
    server.enabled = False
    lib = Library(
        id=_uuid(), name="TV", kind="tv", media_server_id=server.id,
        section_key="2",
    )
    orphan = Library(id=_uuid(), name="Manual", kind="tv")  # 无服务器关联
    db_session.add_all([lib, orphan])
    await db_session.commit()
    client = SimpleNamespace(refresh=AsyncMock())
    _patch_client(monkeypatch, client)

    loaded = await _load_library(db_session, lib.id)
    orphan = await _load_library(db_session, orphan.id)
    assert await refresh_library(loaded, path=None) is False  # 停用跳过
    assert await refresh_library(orphan, path=None) is False  # 无关联跳过
    client.refresh.assert_not_awaited()
