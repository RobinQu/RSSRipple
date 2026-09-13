"""Validate and locate a notification's files within its download directory.

Both manifest and directory fallback use this boundary. Path checks do not
provide descriptor-based protection against concurrent directory replacement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.schemas.notification import NotificationPayload
from app.services.organize_file_safety import FileOp
from app.services.organize_planner import DiskFile, PlanError
from app.services.volume_service import VolumeResolutionError, resolve_downloader_path
from app.utils.path_safety import PathSafetyError, relative_path, require_contained

logger = logging.getLogger(__name__)


def relative_name(value: Any) -> str:
    try:
        return relative_path(value)
    except PathSafetyError as exc:
        raise PlanError(str(exc)) from exc


def validate_manifest(entries: list[dict]) -> list[dict]:
    """Reject the entire manifest instead of executing a safe-looking subset."""
    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise PlanError("文件清单路径条目必须是对象")
        out.append({"name": relative_name(entry.get("name")), "size": entry.get("size") or entry.get("length") or 0})
    return out


def require_within(root: Path, path: Path) -> Path:
    try:
        return require_contained(root, path)
    except PathSafetyError as exc:
        raise PlanError(str(exc)) from exc


@dataclass(frozen=True)
class DownloadScope:
    root: Path
    torrent: Path | None
    names: tuple[str, ...]

    @classmethod
    def from_payload(cls, payload: NotificationPayload, downloader: Any) -> DownloadScope:
        task = payload.task
        download_dir = task.download_dir if task else None
        if not download_dir:
            raise PlanError("下载根路径不能为空")
        try:
            root = Path(resolve_downloader_path(downloader, download_dir))
        except VolumeResolutionError as exc:
            raise PlanError(str(exc)) from exc
        if not root.is_absolute():
            raise PlanError(f"本进程下载根路径必须为绝对路径：{root}")
        if downloader is not None and getattr(downloader, "volume_id", None):
            require_within(Path(downloader.volume.mount_path), root)
        # Validate every name before any single-file fast path or disk selection.
        names = tuple(entry["name"] for entry in validate_manifest(payload.files or []))
        tname = task.torrent_name if task else None
        torrent = require_within(root, root / relative_name(tname)) if tname else None
        if torrent is not None and torrent.resolve() == root.resolve():
            raise PlanError("种子目录路径不能指向共享下载根")
        for name in names:
            require_within(root, root / name)
            if torrent is not None:
                require_within(root, torrent / name)
        return cls(root, torrent, names)


def collect_files(payload: NotificationPayload, downloader: Any) -> list[DiskFile]:
    scope = DownloadScope.from_payload(payload, downloader)
    if scope.torrent is not None and scope.torrent.is_file():
        return [
            DiskFile(
                path=str(scope.torrent),
                size=scope.torrent.stat().st_size,
                rel=str(scope.torrent.relative_to(scope.root)),
            )
        ]
    scoped = scope.torrent is not None and scope.torrent.is_dir()
    root = scope.torrent if scoped else scope.root
    files = []
    for name in scope.names:
        for candidate in (root / name, scope.root / name):
            require_within(scope.root, candidate)
            if candidate.is_file():
                files.append(DiskFile(path=str(candidate), size=candidate.stat().st_size, rel=name))
                break
    if files:
        return files
    if scope.names:
        logger.warning("[organize] payload.files 在磁盘上均未命中（%s）", payload.notification_id)
    if not scoped:
        raise PlanError("无法定位下载内容路径：清单缺失或均未命中，且种子独立目录不存在；拒绝扫描共享下载根")
    for path in sorted(root.rglob("*")):
        require_within(scope.root, path)  # includes directory symlinks skipped by rglob
        if path.is_file():
            files.append(DiskFile(path=str(path), size=path.stat().st_size, rel=str(path.relative_to(root))))
    if not files:
        raise PlanError(f"下载目录无可整理文件：{root}")
    return files


def cleanup_paths(payload: NotificationPayload, downloader: Any) -> tuple[str | None, str | None]:
    if payload.task is None or not payload.task.download_dir:
        return None, None
    scope = DownloadScope.from_payload(payload, downloader)
    if scope.torrent is None:
        return None, None
    return str(scope.torrent), str(scope.root)


def scoped_source_dir(payload: NotificationPayload, downloader: Any) -> str | None:
    scope = DownloadScope.from_payload(payload, downloader)
    return str(scope.torrent) if scope.torrent is not None and scope.torrent.is_dir() else None


def validate_execution_sources(payload: NotificationPayload, downloader: Any, ops: list[FileOp]) -> None:
    """Recheck persisted sources after planning, including existing old plans."""
    scope = DownloadScope.from_payload(payload, downloader)
    for op in ops:
        source = Path(op.src)
        if not source.is_absolute():
            raise PlanError(f"计划源路径不是绝对路径：{source}")
        require_within(scope.root, source)
        if source.resolve() == scope.root.resolve():
            raise PlanError("计划源路径不能是共享下载根")



def validate_execution_destinations(library: Any, ops: list[FileOp]) -> None:
    for op in ops:
        if op.dst is None:
            continue
        root = library.recycle_path if op.op_type == "movedir" else library.root_path
        if root is None:
            raise PlanError("计划目标缺少有效媒体库路径")
        destination = Path(op.dst)
        try:
            require_contained(Path(root), destination)
            if destination.resolve() == Path(root).resolve():
                raise PathSafetyError("计划目标路径不能是媒体库或回收站根目录")
        except PathSafetyError as exc:
            raise PlanError(str(exc)) from exc
