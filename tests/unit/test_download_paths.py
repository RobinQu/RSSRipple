"""Tests for Transmission download directory validation and resolution."""

import pytest

from app.utils.download_paths import DownloadPathError, resolve_download_dir, validate_download_root, validate_download_subdir


def test_validate_posix_root():
    assert validate_download_root("/volume1/downloads/../downloads/rss") == "/volume1/downloads/rss"


def test_validate_windows_root():
    assert validate_download_root(r"D:\Downloads\RSS") == r"D:\Downloads\RSS"


def test_validate_unc_root():
    assert validate_download_root(r"\\nas\downloads\rss") == r"\\nas\downloads\rss"


@pytest.mark.parametrize("value", ["downloads/rss", "", "../x", "\x01bad"])
def test_invalid_root(value):
    with pytest.raises(DownloadPathError):
        validate_download_root(value)


def test_validate_subdir_normalizes_separators():
    assert validate_download_subdir(r"Anime\2026") == "Anime/2026"


@pytest.mark.parametrize("value", ["/abs", r"C:\abs", r"\\nas\share", "../escape", "a/../b", "a//b", "~user"])
def test_invalid_subdir(value):
    with pytest.raises(DownloadPathError):
        validate_download_subdir(value)


def test_resolve_posix_subdir():
    assert resolve_download_dir("/downloads/rss", "Anime/2026") == "/downloads/rss/Anime/2026"


def test_resolve_windows_subdir():
    assert resolve_download_dir(r"D:\Downloads\RSS", "Anime/2026") == r"D:\Downloads\RSS\Anime\2026"
