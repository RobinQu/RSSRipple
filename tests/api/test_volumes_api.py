"""API tests for storage volume endpoints (/api/v1/volumes)."""

from __future__ import annotations


async def _create_volume(client, mount_path: str, name: str = "vol-a", **extra):
    return await client.post(
        "/api/v1/volumes",
        json={"name": name, "mount_path": mount_path, **extra},
    )


class TestVolumesCRUD:
    async def test_create_volume(self, client, tmp_path):
        res = await _create_volume(client, str(tmp_path), remark="flash")
        assert res.status_code == 201
        data = res.json()["data"]
        assert data["name"] == "vol-a"
        assert data["mount_path"] == str(tmp_path)
        assert data["remark"] == "flash"
        assert data["id"]

    async def test_create_rejects_relative_mount_path(self, client):
        res = await _create_volume(client, "relative/path")
        assert res.status_code == 422
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_create_rejects_nonexistent_mount_path(self, client, tmp_path):
        res = await _create_volume(client, str(tmp_path / "no-such-dir"))
        assert res.status_code == 422
        assert res.json()["error"]["code"] == "VALIDATION_ERROR"

    async def test_create_duplicate_name_conflict(self, client, tmp_path):
        res = await _create_volume(client, str(tmp_path))
        assert res.status_code == 201
        dup = await _create_volume(client, str(tmp_path))
        assert dup.status_code == 409
        assert dup.json()["error"]["code"] == "DUPLICATE_SUBMISSION"

    async def test_list_and_get(self, client, tmp_path):
        created = await _create_volume(client, str(tmp_path))
        volume_id = created.json()["data"]["id"]
        res = await client.get("/api/v1/volumes")
        assert res.status_code == 200
        assert res.json()["meta"]["total"] >= 1
        detail = await client.get(f"/api/v1/volumes/{volume_id}")
        assert detail.status_code == 200
        assert detail.json()["data"]["id"] == volume_id

    async def test_get_404(self, client):
        res = await client.get("/api/v1/volumes/nope")
        assert res.status_code == 404
        assert res.json()["error"]["code"] == "NOT_FOUND"

    async def test_update_volume(self, client, tmp_path):
        created = await _create_volume(client, str(tmp_path))
        volume_id = created.json()["data"]["id"]
        new_mount = tmp_path / "mnt"
        new_mount.mkdir()
        res = await client.put(
            f"/api/v1/volumes/{volume_id}",
            json={"name": "vol-b", "mount_path": str(new_mount)},
        )
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["name"] == "vol-b"
        assert data["mount_path"] == str(new_mount)

    async def test_update_rejects_nonexistent_mount_path(self, client, tmp_path):
        created = await _create_volume(client, str(tmp_path))
        volume_id = created.json()["data"]["id"]
        res = await client.put(
            f"/api/v1/volumes/{volume_id}",
            json={"mount_path": str(tmp_path / "no-such-dir")},
        )
        assert res.status_code == 422

    async def test_update_duplicate_name_conflict(self, client, tmp_path):
        a = await _create_volume(client, str(tmp_path), name="vol-a")
        b_dir = tmp_path / "b"
        b_dir.mkdir()
        b = await _create_volume(client, str(b_dir), name="vol-b")
        res = await client.put(
            f"/api/v1/volumes/{b.json()['data']['id']}", json={"name": "vol-a"}
        )
        assert res.status_code == 409
        assert a.status_code == 201

    async def test_delete_volume(self, client, tmp_path):
        created = await _create_volume(client, str(tmp_path))
        volume_id = created.json()["data"]["id"]
        res = await client.delete(f"/api/v1/volumes/{volume_id}")
        assert res.status_code == 200
        assert (await client.get(f"/api/v1/volumes/{volume_id}")).status_code == 404

    async def test_delete_blocked_by_downloader(self, client, tmp_path):
        created = await _create_volume(client, str(tmp_path))
        volume_id = created.json()["data"]["id"]
        dl = await client.post("/api/v1/downloaders", json={
            "name": "DL", "type": "transmission",
            "url": "http://127.0.0.1:9091/transmission/rpc",
            "download_dir": "/downloads/rssripple",
            "volume_id": volume_id,
        })
        assert dl.status_code == 201
        res = await client.delete(f"/api/v1/volumes/{volume_id}")
        assert res.status_code == 409
        body = res.json()
        assert body["error"]["code"] == "DELETE_BLOCKED"
        downloaders = body["error"]["details"]["downloaders"]
        assert downloaders[0]["id"] == dl.json()["data"]["id"]
        # 解绑后可删
        unbind = await client.put(
            f"/api/v1/downloaders/{dl.json()['data']['id']}",
            json={"volume_id": None},
        )
        assert unbind.status_code == 200
        assert (await client.delete(f"/api/v1/volumes/{volume_id}")).status_code == 200


class TestVolumeCheck:
    async def test_check_existing_mount(self, client, tmp_path):
        created = await _create_volume(client, str(tmp_path))
        volume_id = created.json()["data"]["id"]
        res = await client.post(f"/api/v1/volumes/{volume_id}/check")
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["exists"] is True
        assert data["writable"] is True

    async def test_check_missing_mount(self, client, tmp_path):
        mount = tmp_path / "mnt"
        mount.mkdir()
        created = await _create_volume(client, str(mount))
        volume_id = created.json()["data"]["id"]
        mount.rmdir()  # 挂载点消失（如远程存储掉线）
        res = await client.post(f"/api/v1/volumes/{volume_id}/check")
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["exists"] is False
        assert data["writable"] is False

    async def test_check_404(self, client):
        res = await client.post("/api/v1/volumes/nope/check")
        assert res.status_code == 404


class TestVolumeDirs:
    async def test_list_dirs(self, client, tmp_path):
        # Two real subdirs + a hidden one (filtered) + a file (filtered).
        (tmp_path / "alpha").mkdir()
        (tmp_path / "Beta").mkdir()
        (tmp_path / ".hidden").mkdir()
        (tmp_path / "file.txt").write_text("x")
        res = await client.get("/api/v1/volumes/dirs", params={"path": str(tmp_path)})
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["exists"] is True
        assert data["path"] == str(tmp_path)
        assert data["dirs"] == ["alpha", "Beta"]
        # parent is tmp_path's parent (the pytest temp root).
        assert data["parent"] != data["path"]

    async def test_list_dirs_nonexistent(self, client, tmp_path):
        res = await client.get(
            "/api/v1/volumes/dirs", params={"path": str(tmp_path / "nope")}
        )
        assert res.status_code == 422

    async def test_list_dirs_root(self, client):
        res = await client.get("/api/v1/volumes/dirs", params={"path": "/"})
        assert res.status_code == 200
        data = res.json()["data"]
        assert data["path"] == "/"
        assert data["parent"] == "/"
