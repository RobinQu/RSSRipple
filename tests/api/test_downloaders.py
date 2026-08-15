"""API tests for downloader endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

TEST_FIELD_MAPPING = {
    "list_locator": {"source": "entries"},
    "field_mappings": {"torrent_url": {"source": "link"}},
}


class TestDownloadersCRUD:
    async def test_create_downloader(self, client):
        res = await client.post("/api/v1/downloaders", json={
            "name": "DL", "type": "transmission",
            "url": "http://127.0.0.1:9091/transmission/rpc",
            "download_dir": "/downloads/rssripple",
        })
        assert res.status_code == 201
        assert res.json()["data"]["name"] == "DL"
        assert res.json()["data"]["download_dir"] == "/downloads/rssripple"

    async def test_create_downloader_rejects_relative_download_dir(self, client):
        res = await client.post("/api/v1/downloaders", json={
            "name": "DL", "type": "transmission",
            "url": "http://127.0.0.1:9091/transmission/rpc",
            "download_dir": "downloads/rssripple",
        })
        assert res.status_code == 422

    async def test_list_downloaders(self, client, sample_downloader):
        res = await client.get("/api/v1/downloaders")
        assert res.status_code == 200
        assert res.json()["meta"]["total"] >= 1

    async def test_get_downloader(self, client, sample_downloader):
        res = await client.get(f"/api/v1/downloaders/{sample_downloader.id}")
        assert res.status_code == 200
        assert res.json()["data"]["id"] == sample_downloader.id

    async def test_update_downloader(self, client, sample_downloader):
        res = await client.put(
            f"/api/v1/downloaders/{sample_downloader.id}",
            json={"name": "Renamed"},
        )
        assert res.status_code == 200
        assert res.json()["data"]["name"] == "Renamed"

    async def test_delete_downloader(self, client, sample_downloader):
        res = await client.delete(f"/api/v1/downloaders/{sample_downloader.id}")
        assert res.status_code == 200
        res2 = await client.get(f"/api/v1/downloaders/{sample_downloader.id}")
        assert res2.status_code == 404

    async def test_get_404(self, client):
        res = await client.get("/api/v1/downloaders/nope")
        assert res.status_code == 404


class TestDownloaderVolumeBinding:
    """R1 卷绑定：volume_id + volume_subpath 取代 P1 的 path_map。"""

    async def _create_volume(self, client, mount_path: str, name: str = "vol"):
        res = await client.post(
            "/api/v1/volumes", json={"name": name, "mount_path": mount_path}
        )
        assert res.status_code == 201
        return res.json()["data"]["id"]

    async def test_create_with_volume_binding(self, client, tmp_path):
        volume_id = await self._create_volume(client, str(tmp_path))
        res = await client.post("/api/v1/downloaders", json={
            "name": "DL", "type": "transmission",
            "url": "http://127.0.0.1:9091/transmission/rpc",
            "download_dir": "/downloads/rssripple",
            "volume_id": volume_id, "volume_subpath": "rss/complete",
        })
        assert res.status_code == 201
        data = res.json()["data"]
        assert data["volume_id"] == volume_id
        assert data["volume_subpath"] == "rss/complete"

    async def test_create_without_binding_is_identity(self, client):
        res = await client.post("/api/v1/downloaders", json={
            "name": "DL", "type": "transmission",
            "url": "http://127.0.0.1:9091/transmission/rpc",
            "download_dir": "/downloads/rssripple",
        })
        assert res.status_code == 201
        data = res.json()["data"]
        assert data["volume_id"] is None
        assert data["volume_subpath"] is None

    async def test_create_volume_not_found(self, client):
        res = await client.post("/api/v1/downloaders", json={
            "name": "DL", "type": "transmission",
            "url": "http://127.0.0.1:9091/transmission/rpc",
            "download_dir": "/downloads/rssripple",
            "volume_id": "no-such-volume",
        })
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "NOT_FOUND"

    async def test_create_rejects_absolute_volume_subpath(self, client, tmp_path):
        volume_id = await self._create_volume(client, str(tmp_path))
        res = await client.post("/api/v1/downloaders", json={
            "name": "DL", "type": "transmission",
            "url": "http://127.0.0.1:9091/transmission/rpc",
            "download_dir": "/downloads/rssripple",
            "volume_id": volume_id, "volume_subpath": "/abs/path",
        })
        assert res.status_code == 422

    async def test_create_rejects_subpath_without_volume(self, client):
        res = await client.post("/api/v1/downloaders", json={
            "name": "DL", "type": "transmission",
            "url": "http://127.0.0.1:9091/transmission/rpc",
            "download_dir": "/downloads/rssripple",
            "volume_subpath": "rss",
        })
        assert res.status_code == 422

    async def test_create_volume_subpath_empty_string_normalizes_to_null(
        self, client, tmp_path
    ):
        volume_id = await self._create_volume(client, str(tmp_path))
        res = await client.post("/api/v1/downloaders", json={
            "name": "DL", "type": "transmission",
            "url": "http://127.0.0.1:9091/transmission/rpc",
            "download_dir": "/downloads/rssripple",
            "volume_id": volume_id, "volume_subpath": "",
        })
        assert res.status_code == 201
        assert res.json()["data"]["volume_subpath"] is None

    async def test_update_bind_and_unbind(self, client, sample_downloader, tmp_path):
        volume_id = await self._create_volume(client, str(tmp_path))
        res = await client.put(
            f"/api/v1/downloaders/{sample_downloader.id}",
            json={"volume_id": volume_id, "volume_subpath": "complete"},
        )
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["volume_id"] == volume_id
        assert data["volume_subpath"] == "complete"
        # 只更新子路径（沿用已存卷）
        res = await client.put(
            f"/api/v1/downloaders/{sample_downloader.id}",
            json={"volume_subpath": "other"},
        )
        assert res.status_code == 200
        assert res.json()["data"]["volume_subpath"] == "other"
        # 解绑 → 恒等
        res = await client.put(
            f"/api/v1/downloaders/{sample_downloader.id}",
            json={"volume_id": None, "volume_subpath": None},
        )
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["volume_id"] is None
        assert data["volume_subpath"] is None

    async def test_update_volume_not_found(self, client, sample_downloader):
        res = await client.put(
            f"/api/v1/downloaders/{sample_downloader.id}",
            json={"volume_id": "no-such-volume"},
        )
        assert res.status_code == 404


class TestDownloaderActions:
    async def test_test_endpoint(self, client, sample_downloader, mock_transmission):
        res = await client.post(f"/api/v1/downloaders/{sample_downloader.id}/test")
        assert res.status_code == 200
        assert res.json()["data"]["success"] is True
        assert res.json()["data"]["free_space"] is not None

    async def test_torrents_live(self, client, sample_downloader, mock_transmission):
        res = await client.get(f"/api/v1/downloaders/{sample_downloader.id}/torrents")
        assert res.status_code == 200

    async def test_tasks_list(self, client, sample_downloader):
        res = await client.get(f"/api/v1/downloaders/{sample_downloader.id}/tasks")
        assert res.status_code == 200

    async def test_test_endpoint_failure(self, client, sample_downloader, mock_transmission):
        mock_transmission.test_connection.return_value = (False, "connection refused")
        res = await client.post(f"/api/v1/downloaders/{sample_downloader.id}/test")
        assert res.status_code == 200
        assert res.json()["data"]["success"] is False

    async def test_test_endpoint_free_space_failure(self, client, sample_downloader, mock_transmission):
        mock_transmission.free_space.side_effect = RuntimeError("no such directory")
        res = await client.post(f"/api/v1/downloaders/{sample_downloader.id}/test")
        assert res.status_code == 200
        assert res.json()["data"]["success"] is False
        assert "download_dir check failed" in res.json()["data"]["message"]

    async def test_test_endpoint_with_form_overrides(
        self, client, sample_downloader, mock_transmission, monkeypatch, db_session
    ):
        """Edit-form probe: unsaved form values override the stored config for
        the probe only, and the stored health status is left untouched."""
        from app.clients import transmission as tx_mod

        seen = {}
        orig_init = tx_mod.TransmissionWrapper.__init__

        def spy_init(self, *a, **kw):
            downloader = kw.get("downloader") if "downloader" in kw else (a[0] if a else None)
            seen["url"] = downloader.url if downloader is not None else kw.get("url")
            orig_init(self, *a, **kw)

        monkeypatch.setattr(tx_mod.TransmissionWrapper, "__init__", spy_init)

        res = await client.post(
            f"/api/v1/downloaders/{sample_downloader.id}/test",
            json={"url": "http://10.0.0.2:9091/transmission/rpc", "download_dir": "/downloads/complete"},
        )
        assert res.status_code == 200
        assert res.json()["data"]["success"] is True
        # The probe used the form's URL and download_dir, not the stored ones.
        assert seen["url"] == "http://10.0.0.2:9091/transmission/rpc"
        mock_transmission.free_space.assert_awaited_with("/downloads/complete")
        # Status is persisted only for stored-config probes.
        await db_session.refresh(sample_downloader)
        assert sample_downloader.status == "disconnected"

    async def test_torrents_live_error(self, client, sample_downloader, mock_transmission):
        mock_transmission.list_torrents.side_effect = Exception("conn err")
        res = await client.get(f"/api/v1/downloaders/{sample_downloader.id}/torrents")
        assert res.status_code == 502

    async def test_delete_rejects_linked_agents(self, client, sample_downloader):
        # Create an agent pointing at this downloader first
        with patch("app.api.v1.channels.validate_rss_url", AsyncMock(return_value=(True, "ok", 5, 5))):
            ch = await client.post("/api/v1/channels", json={
                "name": "DCh", "type": "rss_feed", "url": "https://x/rss",
                "field_mapping": TEST_FIELD_MAPPING,
            })
        ch_id = ch.json()["data"]["id"]
        agent_res = await client.post("/api/v1/agents", json={
            "name": "DA", "channel_id": ch_id, "downloader_id": sample_downloader.id,
            "scope_channel_wide": True,
        })
        agent_id = agent_res.json()["data"]["id"]
        # Delete downloader
        res = await client.delete(f"/api/v1/downloaders/{sample_downloader.id}")
        assert res.status_code == 409
        body = res.json()
        assert body["success"] is False
        # F1: response must include the specific agents blocking the delete
        # so the UI can offer a "jump to agent" link. Verify both id and
        # human-readable name appear.
        details = body["error"].get("details") or {}
        agents = details.get("agents") or []
        assert len(agents) == 1
        assert agents[0]["id"] == agent_id
        assert agents[0]["name"] == "DA"
        assert "DA" in body["error"]["message"]

    async def test_test_404(self, client):
        res = await client.post("/api/v1/downloaders/nope/test")
        assert res.status_code == 404

    async def test_torrents_404(self, client):
        res = await client.get("/api/v1/downloaders/nope/torrents")
        assert res.status_code == 404

    async def test_tasks_404(self, client):
        res = await client.get("/api/v1/downloaders/nope/tasks")
        assert res.status_code == 404


class TestDownloaderTestVolumeCheck:
    """「测试连接」同时校验卷配置有效性（存在且可读可写）。"""

    async def _create_volume(self, client, mount_path, name="vol"):
        res = await client.post(
            "/api/v1/volumes", json={"name": name, "mount_path": mount_path}
        )
        assert res.status_code == 201
        return res.json()["data"]["id"]

    async def _create_bound_downloader(self, client, volume_id, volume_subpath=None):
        res = await client.post("/api/v1/downloaders", json={
            "name": "BoundDL", "type": "transmission",
            "url": "http://127.0.0.1:9091/transmission/rpc",
            "download_dir": "/downloads/rssripple",
            "volume_id": volume_id,
            "volume_subpath": volume_subpath,
        })
        assert res.status_code == 201
        return res.json()["data"]["id"]

    async def test_bound_volume_valid(self, client, tmp_path, mock_transmission):
        volume_id = await self._create_volume(client, str(tmp_path))
        (tmp_path / "sub").mkdir()
        dl_id = await self._create_bound_downloader(client, volume_id, "sub")
        res = await client.post(f"/api/v1/downloaders/{dl_id}/test")
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["success"] is True
        assert data["volume_check"] == {
            "exists": True, "readable": True, "writable": True,
        }

    async def test_bound_volume_missing_subpath(self, client, tmp_path, mock_transmission):
        volume_id = await self._create_volume(client, str(tmp_path))
        dl_id = await self._create_bound_downloader(client, volume_id, "missing")
        res = await client.post(f"/api/v1/downloaders/{dl_id}/test")
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["success"] is False
        assert data["volume_check"]["exists"] is False
        assert "volume path does not exist" in data["message"]

    async def test_override_volume_binding(self, client, sample_downloader, tmp_path, mock_transmission):
        volume_id = await self._create_volume(client, str(tmp_path))
        (tmp_path / "complete").mkdir()
        res = await client.post(
            f"/api/v1/downloaders/{sample_downloader.id}/test",
            json={"volume_id": volume_id, "volume_subpath": "complete"},
        )
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["success"] is True
        assert data["volume_check"]["exists"] is True

    async def test_override_null_unbinds(self, client, sample_downloader, mock_transmission):
        res = await client.post(
            f"/api/v1/downloaders/{sample_downloader.id}/test",
            json={"volume_id": None, "volume_subpath": None},
        )
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["success"] is True
        assert data["volume_check"] is None
