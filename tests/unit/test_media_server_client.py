"""媒体服务器 adapter（media_server_client）单元测试。

httpx 在 ``app.services.media_server_client.httpx.AsyncClient`` 处打桩
（对齐 tests/unit/test_notify_service.py 的模块级打桩习惯），handler 按
(method, url, params) 返回造好的响应；覆盖三个 adapter 的解析、认证方式
与 refresh 路径（Plex partial 优先、失败退整库；Emby/Jellyfin 整库刷新）。
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.services import media_server_client as msc
from app.services.media_server_client import MediaServerError, get_client


def _server(type="plex", url="http://ms:32400", token="tok"):
    return SimpleNamespace(name="ms", type=type, url=url, token=token)


class _Resp:
    def __init__(self, payload=None, status=200):
        self._payload = payload
        self.status_code = status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://ms")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request,
                response=httpx.Response(self.status_code, request=request),
            )


class _FakeClient:
    """最小 httpx.AsyncClient 替身：记录请求，按 handler 分发响应。"""

    def __init__(self, handler, calls, **kwargs):
        self._handler = handler
        self._calls = calls
        calls.append(("init", kwargs))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, params=None, **kw):
        self._calls.append(("GET", url, params))
        return self._handler("GET", url, params)

    async def post(self, url, params=None, **kw):
        self._calls.append(("POST", url, params))
        return self._handler("POST", url, params)


def _patch_httpx(monkeypatch, handler):
    calls: list = []
    monkeypatch.setattr(
        msc.httpx, "AsyncClient",
        lambda **kw: _FakeClient(handler, calls, **kw),
    )
    return calls


# --------------------------------------------------------------------------- Plex

PLEX_SECTIONS = {
    "MediaContainer": {
        "Directory": [
            {"key": "1", "title": "Movies", "type": "movie",
             "Location": [{"id": 11, "path": "/data/Movies"}]},
            {"key": "2", "title": "Shows", "type": "show",
             "Location": [{"id": 21, "path": "/data/TV"},
                          {"id": 22, "path": "/data/TV2"}]},
            {"key": "3", "title": "Music", "type": "artist",
             "Location": [{"id": 31, "path": "/data/Music"}]},
        ]
    }
}


async def test_plex_list_libraries(monkeypatch):
    calls = _patch_httpx(
        monkeypatch, lambda m, u, p: _Resp(PLEX_SECTIONS)
    )
    client = get_client(_server())
    libs = await client.list_libraries()
    assert libs == [
        {"key": "1", "name": "Movies", "kind": "movie",
         "paths": ["/data/Movies"]},
        {"key": "2", "name": "Shows", "kind": "tv",
         "paths": ["/data/TV", "/data/TV2"]},  # artist 被跳过；多 Location 保留
    ]
    method, url, _ = calls[1]
    assert method == "GET" and url == "/library/sections"
    # X-Plex-Token 认证头
    assert calls[0][1]["headers"]["X-Plex-Token"] == "tok"


async def test_plex_test_connection(monkeypatch):
    _patch_httpx(
        monkeypatch,
        lambda m, u, p: _Resp({"MediaContainer": {"version": "1.40.2"}}),
    )
    ok, version = await get_client(_server()).test_connection()
    assert ok is True and version == "1.40.2"


async def test_plex_test_connection_unauthorized(monkeypatch):
    _patch_httpx(monkeypatch, lambda m, u, p: _Resp({}, status=401))
    ok, detail = await get_client(_server()).test_connection()
    assert ok is False and "401" in detail


async def test_plex_refresh_partial_then_fallback(monkeypatch):
    def handler(method, url, params):
        if params and params.get("path"):
            return _Resp({}, status=404)  # partial 失败 → 退整库
        return _Resp({})

    calls = _patch_httpx(monkeypatch, handler)
    await get_client(_server()).refresh("2", path="/data/TV/Show/Season 01")
    refresh_calls = [c for c in calls if c[0] == "GET"]
    assert refresh_calls[0][2] == {"path": "/data/TV/Show/Season 01"}
    assert refresh_calls[1][1] == "/library/sections/2/refresh"
    assert refresh_calls[1][2] is None  # 整库重试不带 path


async def test_plex_refresh_failure_raises(monkeypatch):
    _patch_httpx(monkeypatch, lambda m, u, p: _Resp({}, status=500))
    with pytest.raises(MediaServerError):
        await get_client(_server()).refresh("2", path="/data/TV/Show")


# ----------------------------------------------------------------- Emby/Jellyfin

EMBY_FOLDERS = [
    {"Name": "Movies", "CollectionType": "movies",
     "Locations": ["/data/Movies"]},
    {"Name": "Shows", "CollectionType": "tvshows", "ItemId": "abc",
     "Locations": ["/data/TV"]},
    {"Name": "Music", "CollectionType": "music", "Locations": ["/data/Music"]},
]


async def test_emby_list_libraries(monkeypatch):
    calls = _patch_httpx(monkeypatch, lambda m, u, p: _Resp(EMBY_FOLDERS))
    libs = await get_client(_server(type="emby", url="http://emby:8096")).list_libraries()
    assert libs == [
        {"key": "Movies", "name": "Movies", "kind": "movie",
         "paths": ["/data/Movies"]},
        # Jellyfin 风格 ItemId 优先于 Name
        {"key": "abc", "name": "Shows", "kind": "tv", "paths": ["/data/TV"]},
    ]
    # Emby 走 api_key 查询参数认证
    assert calls[1][2] == {"api_key": "tok"}


async def test_jellyfin_auth_header_and_refresh(monkeypatch):
    calls = _patch_httpx(
        monkeypatch, lambda m, u, p: _Resp({"Version": "10.9.0"})
    )
    client = get_client(_server(type="jellyfin", url="http://jellyfin:8920"))
    ok, version = await client.test_connection()
    assert ok is True and version == "10.9.0"
    # Jellyfin 走 X-Emby-Token 兼容头
    assert calls[0][1]["headers"]["X-Emby-Token"] == "tok"

    await client.refresh("abc", path="/data/TV/Show")
    post_calls = [c for c in calls if c[0] == "POST"]
    assert post_calls[0][1] == "/Library/Refresh"  # 整库刷新（无 partial 端点）


async def test_emby_refresh_failure_raises(monkeypatch):
    _patch_httpx(monkeypatch, lambda m, u, p: _Resp({}, status=502))
    with pytest.raises(MediaServerError):
        await get_client(_server(type="emby")).refresh("Movies")


async def test_unknown_server_type():
    with pytest.raises(MediaServerError, match="未知媒体服务器类型"):
        get_client(_server(type="kodi"))
