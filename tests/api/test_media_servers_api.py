"""API tests for media server endpoints (/api/v1/media-servers).

覆盖：CRUD + bindings 内嵌整体替换与校验（volume 404 / 空 prefix 422 /
非法 type 422）、test 连通性（mock adapter）、scan 扫描派生（计数 +
待绑定）、删除服务器派生 Library SET NULL 保留行、volumes 删除 409 的
bindings/libraries 引用检查。adapter 在
``app.services.media_server_service.get_client`` 处打桩。
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import select

from app.models.library import Library
from app.models.media_server import MediaServerBinding, MediaServerInstance


def _uuid() -> str:
    return str(uuid.uuid4())


def _fake_client(*, sections=None, test_result=(True, "1.40")):
    return SimpleNamespace(
        list_libraries=AsyncMock(return_value=sections or []),
        test_connection=AsyncMock(return_value=test_result),
    )


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(
        "app.services.media_server_service.get_client", lambda server: client
    )


async def _create_volume(client, tmp_path, name="vol-a"):
    resp = await client.post(
        "/api/v1/volumes", json={"name": name, "mount_path": str(tmp_path)}
    )
    assert resp.status_code == 201
    return resp.json()["data"]["id"]


async def _create_server(client, **overrides):
    body = {
        "name": "Plex", "type": "plex",
        "url": "http://plex:32400", "token": "tok",
    }
    body.update(overrides)
    return await client.post("/api/v1/media-servers", json=body)


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def test_server_crud_roundtrip(client, tmp_path):
    volume_id = await _create_volume(client, tmp_path)
    resp = await _create_server(
        client,
        bindings=[{"server_path_prefix": "/data/Movies",
                   "volume_id": volume_id, "subpath": "films"}],
    )
    assert resp.status_code == 201
    created = resp.json()["data"]
    assert created["name"] == "Plex" and created["type"] == "plex"
    assert created["enabled"] is True
    assert "token" not in created  # 凭证不回显
    [binding] = created["bindings"]
    assert binding["server_path_prefix"] == "/data/Movies"
    assert binding["subpath"] == "films"

    resp = await client.get("/api/v1/media-servers")
    rows = resp.json()["data"]
    assert len(rows) == 1
    assert rows[0]["library_count"] == 0 and rows[0]["unbound_library_count"] == 0

    resp = await client.get(f"/api/v1/media-servers/{created['id']}")
    assert resp.status_code == 200

    # bindings 整体替换
    resp = await client.put(
        f"/api/v1/media-servers/{created['id']}",
        json={"enabled": False,
              "bindings": [{"server_path_prefix": "/data",
                            "volume_id": volume_id}]},
    )
    assert resp.status_code == 200
    updated = resp.json()["data"]
    assert updated["enabled"] is False
    assert [b["server_path_prefix"] for b in updated["bindings"]] == ["/data"]
    assert updated["bindings"][0]["subpath"] == ""

    resp = await client.delete(f"/api/v1/media-servers/{created['id']}")
    assert resp.status_code == 200
    resp = await client.get("/api/v1/media-servers")
    assert resp.json()["data"] == []


async def test_server_create_validation(client, tmp_path):
    volume_id = await _create_volume(client, tmp_path)
    # 非法 type → 422
    resp = await _create_server(client, type="kodi")
    assert resp.status_code == 422
    # 空 prefix → 422
    resp = await _create_server(
        client, bindings=[{"server_path_prefix": "", "volume_id": volume_id}]
    )
    assert resp.status_code == 422
    # 绑定引用不存在的卷 → 404
    resp = await _create_server(
        client, bindings=[{"server_path_prefix": "/data", "volume_id": _uuid()}]
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "NOT_FOUND"


async def test_server_update_unknown_binding_volume_404(client, tmp_path):
    created = (await _create_server(client)).json()["data"]
    resp = await client.put(
        f"/api/v1/media-servers/{created['id']}",
        json={"bindings": [{"server_path_prefix": "/data",
                            "volume_id": _uuid()}]},
    )
    assert resp.status_code == 404


async def test_server_404s(client):
    resp = await client.get(f"/api/v1/media-servers/{_uuid()}")
    assert resp.status_code == 404
    resp = await client.put(f"/api/v1/media-servers/{_uuid()}",
                            json={"name": "x"})
    assert resp.status_code == 404
    resp = await client.delete(f"/api/v1/media-servers/{_uuid()}")
    assert resp.status_code == 404
    resp = await client.post(f"/api/v1/media-servers/{_uuid()}/test")
    assert resp.status_code == 404
    resp = await client.post(f"/api/v1/media-servers/{_uuid()}/scan")
    assert resp.status_code == 404


async def test_server_list_counts(client, db_session):
    from app.models.storage_volume import StorageVolume

    server = MediaServerInstance(
        id=_uuid(), name="Plex", type="plex",
        url="http://plex:32400", token="tok",
    )
    volume = StorageVolume(
        id=_uuid(), name=f"vol-{_uuid()[:8]}", mount_path="/mnt/a"
    )
    bound = Library(
        id=_uuid(), name="Movies", kind="movie", media_server_id=server.id,
        volume_id=volume.id,
    )
    unbound = Library(
        id=_uuid(), name="TV", kind="tv", media_server_id=server.id,
    )
    db_session.add_all([server, volume, bound, unbound])
    await db_session.commit()
    resp = await client.get("/api/v1/media-servers")
    [row] = resp.json()["data"]
    assert row["library_count"] == 2
    assert row["unbound_library_count"] == 1


# ---------------------------------------------------------------------------
# test / scan
# ---------------------------------------------------------------------------


async def test_server_test_endpoint(client, monkeypatch):
    created = (await _create_server(client)).json()["data"]
    _patch_client(monkeypatch, _fake_client(test_result=(True, "1.40.2")))
    resp = await client.post(f"/api/v1/media-servers/{created['id']}/test")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["ok"] is True and data["server_version"] == "1.40.2"

    _patch_client(monkeypatch, _fake_client(test_result=(False, "unauthorized")))
    resp = await client.post(f"/api/v1/media-servers/{created['id']}/test")
    data = resp.json()["data"]
    assert data["ok"] is False and data["message"] == "unauthorized"


async def test_server_test_stateless(client, monkeypatch):
    """创建表单按未保存值探测：无 id 的 POST /media-servers/test。"""
    _patch_client(monkeypatch, _fake_client(test_result=(True, "10.8.0")))
    resp = await client.post("/api/v1/media-servers/test", json={
        "type": "jellyfin", "url": "http://jelly:8096", "token": "k",
    })
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["ok"] is True and data["server_version"] == "10.8.0"


async def test_server_test_with_overrides(client, monkeypatch):
    """编辑表单覆盖值：url/token 用表单值探测，token 显式覆盖。"""
    created = (await _create_server(client)).json()["data"]  # token "tok"
    seen = {}
    fake = _fake_client(test_result=(True, "1.40.2"))

    def _capture(server):
        seen["url"] = server.url
        seen["token"] = server.token
        seen["type"] = server.type
        return fake

    monkeypatch.setattr(
        "app.services.media_server_service.get_client", _capture
    )
    resp = await client.post(
        f"/api/v1/media-servers/{created['id']}/test",
        json={"url": "http://newhost:32400", "token": "newtok"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["ok"] is True
    assert seen["url"] == "http://newhost:32400"
    assert seen["token"] == "newtok"


async def test_server_test_override_blank_token_falls_back(client, monkeypatch):
    """编辑表单不回显 token：空 token = 沿用已存凭证。"""
    created = (await _create_server(client)).json()["data"]  # token "tok"
    seen = {}
    fake = _fake_client(test_result=(True, "1.40.2"))

    def _capture(server):
        seen["token"] = server.token
        return fake

    monkeypatch.setattr(
        "app.services.media_server_service.get_client", _capture
    )
    resp = await client.post(
        f"/api/v1/media-servers/{created['id']}/test",
        json={"url": "http://newhost:32400"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["ok"] is True
    assert seen["token"] == "tok"


async def test_server_scan_endpoint(client, db_session, tmp_path, monkeypatch):
    volume_id = await _create_volume(client, tmp_path)
    resp = await _create_server(
        client,
        bindings=[{"server_path_prefix": "/data", "volume_id": volume_id}],
    )
    server = resp.json()["data"]
    sections = [
        {"key": "1", "name": "Movies", "kind": "movie",
         "paths": ["/data/Movies"]},
        {"key": "2", "name": "Shows", "kind": "tv",
         "paths": ["/elsewhere/TV"]},  # 无绑定命中 → 待绑定
    ]
    _patch_client(monkeypatch, _fake_client(sections=sections))

    resp = await client.post(f"/api/v1/media-servers/{server['id']}/scan")
    assert resp.status_code == 200
    assert resp.json()["data"] == {"created": 2, "updated": 0, "unbound": 1}

    libs = (await db_session.execute(select(Library))).scalars().all()
    by_path = {lib.server_path: lib for lib in libs}
    assert by_path["/data/Movies"].volume_id == volume_id
    assert by_path["/data/Movies"].root_subpath == "Movies"
    assert by_path["/elsewhere/TV"].volume_id is None

    # 幂等重扫
    resp = await client.post(f"/api/v1/media-servers/{server['id']}/scan")
    assert resp.json()["data"] == {"created": 0, "updated": 2, "unbound": 1}


async def test_server_scan_normalizes_paths(client, db_session_factory, monkeypatch):
    """尾部斜杠差异不应产生重复 Library（幂等）。"""
    created = (await _create_server(client)).json()["data"]
    sections = [{"key": "1", "name": "Anime", "kind": "tv",
                 "paths": ["/data/Anime/"]}]
    _patch_client(monkeypatch, _fake_client(sections=sections))
    resp = await client.post(f"/api/v1/media-servers/{created['id']}/scan")
    assert resp.json()["data"] == {"created": 1, "updated": 0, "unbound": 1}
    # 第二次扫描路径仍带尾斜杠 → 归一后同键，命中既有行而非新建。
    resp = await client.post(f"/api/v1/media-servers/{created['id']}/scan")
    assert resp.json()["data"] == {"created": 0, "updated": 1, "unbound": 1}
    async with db_session_factory() as s:
        libs = (await s.execute(select(Library))).scalars().all()
        assert len(libs) == 1
        assert libs[0].server_path == "/data/Anime"


async def test_server_scan_preserves_manual_binding(
    client, db_session_factory, tmp_path, monkeypatch
):
    """重扫未命中绑定时不应覆盖手工补绑定（volume_id/root_subpath）。"""
    volume_id = await _create_volume(client, tmp_path)
    created = (await _create_server(client)).json()["data"]
    sections = [{"key": "1", "name": "Anime", "kind": "tv",
                 "paths": ["/elsewhere/Anime"]}]
    _patch_client(monkeypatch, _fake_client(sections=sections))
    resp = await client.post(f"/api/v1/media-servers/{created['id']}/scan")
    assert resp.json()["data"]["unbound"] == 1
    async with db_session_factory() as s:
        lib_id = (await s.execute(select(Library.id))).scalar_one()
    # 手工补绑定（就地修复）
    resp = await client.put(f"/api/v1/libraries/{lib_id}", json={
        "volume_id": volume_id, "root_subpath": "anime",
    })
    assert resp.status_code == 200
    # 重扫：未命中绑定，保留手工绑定且不重复计数待绑定。
    resp = await client.post(f"/api/v1/media-servers/{created['id']}/scan")
    assert resp.json()["data"] == {"created": 0, "updated": 1, "unbound": 0}
    async with db_session_factory() as s:
        lib = (await s.execute(select(Library))).scalar_one()
        assert lib.volume_id == volume_id
        assert lib.root_subpath == "anime"


async def test_server_scan_disabled_409(client):
    created = (await _create_server(client, enabled=False)).json()["data"]
    resp = await client.post(f"/api/v1/media-servers/{created['id']}/scan")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "INVALID_STATE"


async def test_server_scan_connection_error_502(client, monkeypatch):
    from app.services.media_server_client import MediaServerError

    created = (await _create_server(client)).json()["data"]
    client_obj = SimpleNamespace(
        list_libraries=AsyncMock(side_effect=MediaServerError("connect timeout"))
    )
    _patch_client(monkeypatch, client_obj)
    resp = await client.post(f"/api/v1/media-servers/{created['id']}/scan")
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "MEDIA_SERVER_ERROR"


# ---------------------------------------------------------------------------
# 删除：派生 Library SET NULL
# ---------------------------------------------------------------------------


async def test_delete_server_nulls_derived_libraries(
    client, db_session, db_session_factory, tmp_path
):
    volume_id = await _create_volume(client, tmp_path)
    resp = await _create_server(
        client,
        bindings=[{"server_path_prefix": "/data", "volume_id": volume_id}],
    )
    server_id = resp.json()["data"]["id"]
    lib = Library(
        id=_uuid(), name="Movies", kind="movie", media_server_id=server_id,
        section_key="1", server_path="/data/Movies", volume_id=volume_id,
    )
    db_session.add(lib)
    await db_session.commit()

    resp = await client.delete(f"/api/v1/media-servers/{server_id}")
    assert resp.status_code == 200

    # 走新鲜会话断言：长生命周期 fixture 会话的 MVCC 快照看不到 API 会话
    # 提交的 UPDATE（tests/api/test_organize_api.py 注释的同款坑）。
    async with db_session_factory() as s:
        lib2 = await s.get(Library, lib.id)
        assert lib2.media_server_id is None  # 保留行
        # bindings 随服务器删除
        bindings = (
            await s.execute(select(MediaServerBinding))
        ).scalars().all()
        assert bindings == []


# ---------------------------------------------------------------------------
# volumes 删除 409：bindings / libraries 引用检查
# ---------------------------------------------------------------------------


async def test_volume_delete_blocked_by_binding(client, tmp_path):
    volume_id = await _create_volume(client, tmp_path)
    resp = await _create_server(
        client,
        bindings=[{"server_path_prefix": "/data", "volume_id": volume_id}],
    )
    assert resp.status_code == 201
    res = await client.delete(f"/api/v1/volumes/{volume_id}")
    assert res.status_code == 409
    body = res.json()
    assert body["error"]["code"] == "DELETE_BLOCKED"
    assert body["error"]["details"]["media_server_bindings"]


async def test_volume_delete_blocked_by_library(client, db_session, tmp_path):
    volume_id = await _create_volume(client, tmp_path)
    lib = Library(
        id=_uuid(), name="Movies", kind="movie", volume_id=volume_id,
    )
    db_session.add(lib)
    await db_session.commit()
    res = await client.delete(f"/api/v1/volumes/{volume_id}")
    assert res.status_code == 409
    body = res.json()
    assert body["error"]["code"] == "DELETE_BLOCKED"
    assert body["error"]["details"]["libraries"][0]["id"] == lib.id
