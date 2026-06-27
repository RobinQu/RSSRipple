"""Transmission RPC client wrapper."""

import asyncio
from typing import Any
from urllib.parse import urlparse

from transmission_rpc import Client as TransmissionClient

# Fields requested from Transmission for the torrent list view.
_TORRENT_FIELDS = [
    "id",
    "name",
    "hashString",
    "status",
    "percentDone",
    "rateDownload",
    "rateUpload",
    "eta",
    "totalSize",
    "haveValid",
    "isFinished",
    "leftUntilDone",
    "error",
    "errorString",
    "addedDate",
    "peersConnected",
]


def _parse_url(url: str) -> dict[str, Any]:
    """Parse a full Transmission URL into Client keyword args."""
    p = urlparse(url)
    return {
        "protocol": p.scheme or "http",
        "host": p.hostname or "127.0.0.1",
        "port": p.port or 9091,
        "path": p.path or "/transmission/rpc",
    }


def _torrent_to_dict(t) -> dict[str, Any]:
    """Normalize a transmission_rpc Torrent object to a plain dict."""
    eta_seconds: int | None = None
    try:
        if t.eta is not None:
            total = getattr(t.eta, "total_seconds", None)
            if callable(total):
                val = total()
            else:
                val = t.eta
            if val is not None and val > 0:
                eta_seconds = int(val)
    except (AttributeError, TypeError):
        pass

    added_date: str | None = None
    try:
        added_date = t.added_date.isoformat()
    except (AttributeError, TypeError):
        pass

    left_until_done = 0
    try:
        left_until_done = int(getattr(t, "leftUntilDone", 0) or 0)
    except Exception:
        left_until_done = 0

    status_str = str(t.status)
    # Normalize common transmission_rpc status strings
    status_l = status_str.lower()
    if "stop" in status_l:
        norm_status = "stopped"
    elif "check" in status_l:
        norm_status = "checking"
    elif "seed" in status_l:
        norm_status = "seeding"
    elif "download" in status_l or t.rateDownload > 0:
        norm_status = "downloading"
    else:
        norm_status = "queued"

    return {
        "id": t.id,
        "name": t.name,
        "hash": t.hashString,
        "status": norm_status,
        "raw_status": status_str,
        "percent_done": float(getattr(t, "percentDone", 0) or 0),
        "rate_download": int(getattr(t, "rateDownload", 0) or 0),
        "rate_upload": int(getattr(t, "rateUpload", 0) or 0),
        "eta_seconds": eta_seconds,
        "total_size": int(getattr(t, "totalSize", 0) or 0),
        "have_valid": int(getattr(t, "haveValid", 0) or 0),
        "is_finished": bool(getattr(t, "isFinished", False)),
        "left_until_done": left_until_done,
        "error": int(getattr(t, "error", 0) or 0),
        "error_string": t.fields.get("errorString", "") if hasattr(t, "fields") else "",
        "added_date": added_date,
        "peers_connected": int(t.fields.get("peersConnected", 0)) if hasattr(t, "fields") else 0,
    }


class TransmissionWrapper:
    """Async wrapper around transmission-rpc synchronous client.

    Can be constructed either with a URL+credentials, or by passing a
    ``DownloaderInstance`` model directly.
    """

    def __init__(
        self,
        url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        downloader: Any | None = None,
    ):
        if downloader is not None:
            url = downloader.url
            username = downloader.username
            password = downloader.password
        if not url:
            raise ValueError("url or downloader is required")
        self._conn = _parse_url(url)
        self._username = username
        self._password = password

    def _client(self) -> TransmissionClient:
        return TransmissionClient(
            **self._conn,
            username=self._username,
            password=self._password,
        )

    async def test_connection(self) -> tuple[bool, str | None]:
        """Connect to Transmission and return (success, version_string | error_message)."""
        def _run():
            c = self._client()
            session = c.get_session()
            return f"Transmission {session.version}"
        try:
            version = await asyncio.to_thread(_run)
            return True, version
        except Exception as e:
            return False, str(e)

    async def list_torrents(self) -> list[dict[str, Any]]:
        """Return all torrents with live stats."""
        def _run():
            c = self._client()
            torrents = c.get_torrents(arguments=_TORRENT_FIELDS)
            return [_torrent_to_dict(t) for t in torrents]
        return await asyncio.to_thread(_run)

    async def add_torrent(
        self,
        torrent_url: str,
        download_dir: str | None = None,
        paused: bool = False,
    ) -> dict:
        """Add a torrent by URL (.torrent file or magnet link)."""
        def _run():
            c = self._client()
            kwargs: dict[str, Any] = {"paused": paused}
            if download_dir:
                kwargs["download_dir"] = download_dir
            torrent = c.add_torrent(torrent_url, **kwargs)
            return {
                "torrent_id": torrent.id,
                "name": torrent.name,
                "hash": torrent.hashString,
            }
        return await asyncio.to_thread(_run)

    async def get_torrent(self, torrent_id: int) -> dict:
        """Get a single torrent's live stats."""
        def _run():
            c = self._client()
            torrents = c.get_torrents(arguments=_TORRENT_FIELDS, ids=[torrent_id])
            if not torrents:
                raise ValueError(f"Torrent {torrent_id} not found")
            return _torrent_to_dict(torrents[0])
        return await asyncio.to_thread(_run)

    async def pause_torrent(self, torrent_id: int) -> bool:
        def _run():
            self._client().stop_torrent(torrent_id)
        try:
            await asyncio.to_thread(_run)
            return True
        except Exception:
            return False

    async def resume_torrent(self, torrent_id: int) -> bool:
        def _run():
            self._client().start_torrent(torrent_id)
        try:
            await asyncio.to_thread(_run)
            return True
        except Exception:
            return False

    async def remove_torrent(self, torrent_id: int, delete_data: bool = False) -> bool:
        def _run():
            self._client().remove_torrent(torrent_id, delete_data=delete_data)
        try:
            await asyncio.to_thread(_run)
            return True
        except Exception:
            return False
