"""Downloader client factory.

Picks the right async wrapper for a :class:`DownloaderInstance` based on
``type``. All wrappers expose the same async surface (``test_connection``,
``add_torrent``, ``list_torrents``, ``get_torrent``, ``pause_torrent``,
``resume_torrent``, ``remove_torrent``, ``free_space``) so callers can be
downloader-agnostic.
"""

from __future__ import annotations

from typing import Any


def get_downloader_client(downloader: Any):
    """Return an async wrapper matching ``downloader.type``.

    ``mock`` returns an in-process simulator (see
    :class:`MockDownloaderWrapper`); anything else falls through to the real
    Transmission RPC client. Imports are deferred so tests that patch
    ``app.clients.transmission.TransmissionWrapper`` still see the
    monkey-patched class (Python captures module attributes at attribute
    access, not at first import here).
    """
    dtype = (getattr(downloader, "type", "") or "").lower()
    if dtype == "mock":
        from app.clients import mock_downloader
        return mock_downloader.MockDownloaderWrapper(downloader=downloader)
    from app.clients import transmission
    return transmission.TransmissionWrapper(downloader=downloader)
