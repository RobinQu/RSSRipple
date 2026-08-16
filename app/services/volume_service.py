"""逻辑存储卷（StorageVolume）的路径解析与挂载探测。

下载器卷绑定（``DownloaderInstance.volume_id`` / ``volume_subpath``）的
通用解析能力：daemon 视角路径 → 本进程视角路径。这是 downloader 级通用
能力（任何需要在本进程内触达下载文件的消费方共用），非 organize 私有
（docs/design/file-organization.md「统一路径解析：逻辑存储卷」）。

解析语义：``volume_id`` 为空 → 恒等（两视角一致，现状默认）；否则
``volume.mount_path + volume_subpath + (daemon_path 相对 download_dir
的部分)``。卷不存在 / 绑定不完整 / 路径不在下载根之下 →
:exc:`VolumeResolutionError` 明确异常（绝不静默恒等，避免写错位置）。
"""

from __future__ import annotations

import os
from typing import Any


class VolumeResolutionError(Exception):
    """卷绑定解析失败（卷不存在 / 绑定不完整 / 路径不在下载根之下）。"""


def resolve_downloader_path(downloader: Any, daemon_path: str) -> str:
    """daemon 视角路径 → 本进程视角路径（按下载器卷绑定解析）。

    ``downloader`` 为 DownloaderInstance（或同形对象，需带 ``download_dir``
    / ``volume_id`` / ``volume_subpath`` / ``volume`` 属性）；None 或
    ``volume_id`` 为空 → 恒等。
    """
    if downloader is None or not getattr(downloader, "volume_id", None):
        return daemon_path
    volume = getattr(downloader, "volume", None)
    if volume is None:
        raise VolumeResolutionError(
            f"下载器 {getattr(downloader, 'name', '?')!r} 绑定的存储卷不存在："
            f"{downloader.volume_id}"
        )
    root = (getattr(downloader, "download_dir", "") or "").rstrip("/")
    path = daemon_path or ""
    if not root or not path:
        raise VolumeResolutionError(
            f"下载器 {getattr(downloader, 'name', '?')!r} 已绑定存储卷但 "
            f"download_dir/daemon 路径为空，绑定不完整，无法解析"
        )
    base = volume.mount_path.rstrip("/")
    subpath = getattr(downloader, "volume_subpath", None)
    if subpath:
        base = base + "/" + subpath.strip("/")
    if path == root:
        return base
    if path.startswith(root + "/"):
        return base + path[len(root):]
    raise VolumeResolutionError(
        f"daemon 路径 {path!r} 不在下载根 {root!r} 之下，无法按卷绑定解析"
        f"（下载器 {getattr(downloader, 'name', '?')!r}）"
    )


def check_mount(mount_path: str) -> dict:
    """探测挂载点的存在性、可读性与写权限（均仅作展示提示，不拦截保存）。"""
    exists = os.path.isdir(mount_path)
    return {
        "exists": exists,
        "readable": os.access(mount_path, os.R_OK) if exists else False,
        "writable": os.access(mount_path, os.W_OK) if exists else False,
    }


def resolve_library_root(library: Any) -> str | None:
    """Library 库根动态解析 = ``volume.mount_path + root_subpath``。

    库根不再是静态列（R2）：一切配置面路径引用存 ``(volume_id,
    root_subpath)``，使用处动态解析。``volume_id`` 为空或卷已被删除 →
    None（待绑定，以其为目标的计划落「待绑定」pending）。
    """
    if library is None or not getattr(library, "volume_id", None):
        return None
    volume = getattr(library, "volume", None)
    if volume is None:
        return None
    base = volume.mount_path.rstrip("/")
    subpath = (getattr(library, "root_subpath", None) or "").strip("/")
    return f"{base}/{subpath}" if subpath else base


def resolve_library_recycle(library: Any) -> str | None:
    """Library 回收站目录动态解析 = ``volume.mount_path + recycle_subpath``。

    与库根同卷；``recycle_subpath`` 为空（默认）或卷未绑定 → None（合集
    move 计划的剩余文件原地保留，不产生 movedir op）。
    """
    subpath = (getattr(library, "recycle_subpath", None) or "").strip("/")
    if not subpath:
        return None
    if library is None or not getattr(library, "volume_id", None):
        return None
    volume = getattr(library, "volume", None)
    if volume is None:
        return None
    return f"{volume.mount_path.rstrip('/')}/{subpath}"
