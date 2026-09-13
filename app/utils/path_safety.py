"""Filesystem path syntax and containment checks, independent of consumers."""

from __future__ import annotations

import ntpath
from pathlib import Path
from typing import Any

from app.utils.download_paths import DownloadPathError, validate_download_subdir


class PathSafetyError(ValueError):
    """A path cannot safely be interpreted within the intended root."""


def relative_path(value: Any) -> str:
    """Normalize separators without silently trimming an external name."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise PathSafetyError(f"路径必须是非空相对路径且无首尾空白：{value!r}")
    if ntpath.splitdrive(value)[0]:
        raise PathSafetyError(f"路径不能包含驱动器：{value!r}")
    try:
        result = validate_download_subdir(value)
    except DownloadPathError as exc:
        raise PathSafetyError(f"不安全的路径：{value!r}（{exc}）") from exc
    if result is None:
        raise PathSafetyError("路径不能为空")
    return result


def require_contained(root: Path, path: Path) -> Path:
    if not root.is_absolute() or not path.is_absolute():
        raise PathSafetyError("本进程路径必须是绝对路径")
    try:
        resolved_root, resolved_path = root.resolve(), path.resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise PathSafetyError(f"路径无法安全解析：{path}") from exc
    if not resolved_path.is_relative_to(resolved_root):
        raise PathSafetyError(f"路径逃出根目录：{path}")
    return path


def resolve_subpath(root: str, subpath: str | None) -> str:
    base = Path(root)
    path = base / relative_path(subpath) if subpath not in (None, '') else base
    return str(require_contained(base, path))
