"""volume_service.resolve_downloader_path 单元测试。

语义（docs/design/file-organization.md「DownloaderInstance 卷绑定」）：
volume_id 为空 → 恒等；否则 volume.mount_path + volume_subpath +
（daemon_path 相对 download_dir 的部分）；卷不存在 / 绑定不完整 / 路径
不在下载根之下 → VolumeResolutionError。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.volume_service import (
    VolumeResolutionError,
    check_mount,
    resolve_downloader_path,
)


def _downloader(
    download_dir="/downloads",
    volume_id=None,
    volume_subpath=None,
    mount_path="/mnt/shared",
):
    volume = (
        SimpleNamespace(id=volume_id, mount_path=mount_path)
        if volume_id
        else None
    )
    return SimpleNamespace(
        id="d1", name="dl", download_dir=download_dir,
        volume_id=volume_id, volume_subpath=volume_subpath, volume=volume,
    )


class TestResolveDownloaderPath:
    def test_none_downloader_is_identity(self):
        assert resolve_downloader_path(None, "/downloads/x") == "/downloads/x"

    def test_no_volume_binding_is_identity(self):
        dl = _downloader(volume_id=None)
        assert resolve_downloader_path(dl, "/downloads/x") == "/downloads/x"

    def test_root_resolves_to_mount(self):
        dl = _downloader(volume_id="v1")
        assert resolve_downloader_path(dl, "/downloads") == "/mnt/shared"

    def test_relative_suffix_appended(self):
        dl = _downloader(volume_id="v1")
        assert (
            resolve_downloader_path(dl, "/downloads/complete/Show.S01/ep04.mkv")
            == "/mnt/shared/complete/Show.S01/ep04.mkv"
        )

    def test_volume_subpath_joined(self):
        dl = _downloader(
            download_dir="/downloads/complete",
            volume_id="v1", volume_subpath="dl/complete",
        )
        assert (
            resolve_downloader_path(dl, "/downloads/complete/ep04.mkv")
            == "/mnt/shared/dl/complete/ep04.mkv"
        )

    def test_trailing_slash_root_normalized(self):
        dl = _downloader(download_dir="/downloads/", volume_id="v1")
        assert resolve_downloader_path(dl, "/downloads/x") == "/mnt/shared/x"

    def test_missing_volume_raises(self):
        dl = _downloader(volume_id="v1")
        dl.volume = None  # volume_id 已绑但卷行不存在
        with pytest.raises(VolumeResolutionError, match="存储卷不存在"):
            resolve_downloader_path(dl, "/downloads/x")

    def test_path_outside_root_raises(self):
        dl = _downloader(volume_id="v1")
        with pytest.raises(VolumeResolutionError, match="不在下载根"):
            resolve_downloader_path(dl, "/elsewhere/x")
        # 前缀陷阱：/downloads2 不是 /downloads 的子路径
        with pytest.raises(VolumeResolutionError, match="不在下载根"):
            resolve_downloader_path(dl, "/downloads2/x")

    def test_empty_path_raises(self):
        dl = _downloader(volume_id="v1")
        with pytest.raises(VolumeResolutionError, match="绑定不完整"):
            resolve_downloader_path(dl, "")


class TestCheckMount:
    def test_existing_dir(self, tmp_path):
        result = check_mount(str(tmp_path))
        assert result == {"exists": True, "readable": True, "writable": True}

    def test_missing_dir(self, tmp_path):
        result = check_mount(str(tmp_path / "nope"))
        assert result == {"exists": False, "readable": False, "writable": False}
