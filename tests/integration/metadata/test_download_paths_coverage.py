"""In-process integration coverage for app.utils.download_paths.

Targets the branches the integration run misses: Windows/UNC root styles,
root/subdir validation errors, and the commonpath escape translation in
resolve_download_dir.
"""

from __future__ import annotations

import ntpath
import posixpath

import pytest

from app.utils.download_paths import (
    DownloadPathError,
    resolve_download_dir,
    validate_download_root,
    validate_download_subdir,
)


def test_posix_root_normalized():
    assert validate_download_root("/volume1/downloads/../downloads/rss") == "/volume1/downloads/rss"


def test_windows_drive_root_normalized():
    # Redundant separators / dot segments collapse under ntpath rules.
    assert validate_download_root(r"D:\Downloads\.\RSS") == r"D:\Downloads\RSS"


def test_unc_root_normalized():
    assert validate_download_root(r"\\nas\downloads\rss") == r"\\nas\downloads\rss"


@pytest.mark.parametrize("value", ["", "   "])
def test_root_required(value):
    with pytest.raises(DownloadPathError, match="required"):
        validate_download_root(value)


def test_root_control_chars_rejected():
    with pytest.raises(DownloadPathError, match="control characters"):
        validate_download_root("/downloads/\x01bad")


def test_relative_root_rejected():
    with pytest.raises(DownloadPathError, match="absolute path"):
        validate_download_root("downloads/rss")


def test_subdir_none_and_blank_mean_no_subdir():
    assert validate_download_subdir(None) is None
    assert validate_download_subdir("   ") is None


def test_subdir_normalizes_separators():
    assert validate_download_subdir(r"Anime\2026\冬") == "Anime/2026/冬"


def test_subdir_control_chars_rejected():
    with pytest.raises(DownloadPathError, match="control characters"):
        validate_download_subdir("a\x7fb")


@pytest.mark.parametrize("value", ["/abs", r"\abs", "~user", r"C:\abs", r"\\nas\share"])
def test_subdir_absolute_forms_rejected(value):
    with pytest.raises(DownloadPathError, match="relative path"):
        validate_download_subdir(value)


def test_subdir_empty_segments_rejected():
    with pytest.raises(DownloadPathError, match="empty path segments"):
        validate_download_subdir("a//b")


@pytest.mark.parametrize("value", ["../escape", "a/../b", "a/./b"])
def test_subdir_dot_segments_rejected(value):
    with pytest.raises(DownloadPathError, match="path segments"):
        validate_download_subdir(value)


def test_resolve_posix_join():
    assert resolve_download_dir("/downloads/rss", "Anime/2026") == "/downloads/rss/Anime/2026"


def test_resolve_without_subdir_returns_root():
    assert resolve_download_dir("/downloads/rss", None) == "/downloads/rss"


def test_resolve_windows_root_uses_ntpath():
    assert resolve_download_dir(r"D:\Downloads\RSS", "Anime/2026") == r"D:\Downloads\RSS\Anime\2026"


def test_resolve_commonpath_error_translated(monkeypatch):
    # posixpath.commonpath raises ValueError when the candidate and root are
    # incomparable; resolve_download_dir must surface that as a path error.
    def _boom(paths):
        raise ValueError("can't mix absolute and relative paths")

    monkeypatch.setattr(posixpath, "commonpath", _boom)
    with pytest.raises(DownloadPathError, match="escapes downloader download_dir"):
        resolve_download_dir("/downloads/rss", "Anime/2026")


def test_resolve_commonpath_error_translated_windows(monkeypatch):
    def _boom(paths):
        raise ValueError("Paths don't have the same drive")

    monkeypatch.setattr(ntpath, "commonpath", _boom)
    with pytest.raises(DownloadPathError, match="escapes downloader download_dir"):
        resolve_download_dir(r"D:\Downloads\RSS", "Anime/2026")
