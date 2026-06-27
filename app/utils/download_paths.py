"""Helpers for validating and resolving Transmission download directories."""

from __future__ import annotations

import ntpath
import posixpath
import re


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_DRIVE_ABS = re.compile(r"^[a-zA-Z]:[\\/]")


class DownloadPathError(ValueError):
    """Raised when a configured download path is invalid."""


def _style_for_root(root: str) -> str:
    if root.startswith("\\\\") or _WINDOWS_DRIVE_ABS.match(root):
        return "windows"
    return "posix"


def validate_download_root(download_dir: str) -> str:
    """Validate and normalize a downloader root directory.

    The path is interpreted by the Transmission daemon, not by RSSRipple's host.
    Therefore we accept POSIX, Windows drive, and UNC absolute paths.
    """
    value = (download_dir or "").strip()
    if not value:
        raise DownloadPathError("download_dir is required")
    if _CONTROL_CHARS.search(value):
        raise DownloadPathError("download_dir contains control characters")

    if value.startswith("/"):
        return posixpath.normpath(value)
    if _WINDOWS_DRIVE_ABS.match(value) or value.startswith("\\\\"):
        return ntpath.normpath(value)
    raise DownloadPathError("download_dir must be an absolute path on the Transmission server")


def validate_download_subdir(download_subdir: str | None) -> str | None:
    """Validate and normalize an optional Agent subdirectory."""
    if download_subdir is None:
        return None
    value = download_subdir.strip()
    if not value:
        return None
    if _CONTROL_CHARS.search(value):
        raise DownloadPathError("download_subdir contains control characters")
    if value.startswith(("/", "\\", "~")) or _WINDOWS_DRIVE_ABS.match(value) or value.startswith("\\\\"):
        raise DownloadPathError("download_subdir must be a relative path")
    if "//" in value or "\\\\" in value:
        raise DownloadPathError("download_subdir must not contain empty path segments")

    parts = re.split(r"[\\/]+", value)
    if any(part in ("", ".", "..") for part in parts):
        raise DownloadPathError("download_subdir must not contain empty, '.', or '..' path segments")
    return "/".join(parts)


def resolve_download_dir(download_dir: str, download_subdir: str | None = None) -> str:
    """Resolve a final per-task download directory under a downloader root."""
    root = validate_download_root(download_dir)
    subdir = validate_download_subdir(download_subdir)
    if not subdir:
        return root

    style = _style_for_root(root)
    pathmod = ntpath if style == "windows" else posixpath
    candidate = pathmod.normpath(pathmod.join(root, *subdir.split("/")))

    root_cmp = pathmod.normcase(root)
    candidate_cmp = pathmod.normcase(candidate)
    try:
        common = pathmod.commonpath([root_cmp, candidate_cmp])
    except ValueError as exc:
        raise DownloadPathError("download_subdir escapes downloader download_dir") from exc
    if common != root_cmp:
        raise DownloadPathError("download_subdir escapes downloader download_dir")
    return candidate
