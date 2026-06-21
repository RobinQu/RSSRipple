"""Transmission RPC client wrapper."""

import asyncio
from functools import partial
from typing import Any

from transmission_rpc import Client as TransmissionClient


class TransmissionWrapper:
    """Async wrapper around transmission-rpc synchronous client."""

    def __init__(self, url: str, username: str | None = None, password: str | None = None):
        self._client = TransmissionClient(
            host=url,
            username=username,
            password=password,
        )

    async def _run(self, func, *args, **kwargs) -> Any:
        """Run a synchronous Transmission method in executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(func, *args, **kwargs))

    async def test_connection(self) -> tuple[bool, str | None]:
        """Test connection to Transmission daemon.

        Returns:
            (success, version_string)
        """
        try:
            session = await self._run(self._client.get_session)
            return True, f"Transmission {session.version}"
        except Exception as e:
            return False, str(e)

    async def add_torrent(
        self,
        torrent_url: str,
        download_dir: str | None = None,
        paused: bool = False,
    ) -> dict:
        """Add a torrent by URL (.torrent file or magnet link).

        Returns:
            dict with torrent_id, name, hashString
        """
        kwargs: dict[str, Any] = {"paused": paused}
        if download_dir:
            kwargs["download_dir"] = download_dir

        torrent = await self._run(
            self._client.add_torrent,
            torrent_url,
            **kwargs,
        )
        return {
            "torrent_id": torrent.id,
            "name": torrent.name,
            "hash": torrent.hashString,
        }

    async def get_torrent(self, torrent_id: int) -> dict:
        """Get torrent status and progress.

        Returns:
            dict with status fields.
        """
        torrent = await self._run(self._client.get_torrent, torrent_id)
        return {
            "id": torrent.id,
            "name": torrent.name,
            "status": torrent.status,
            "progress": torrent.progress,
            "rate_download": torrent.rate_download,
            "rate_upload": torrent.rate_upload,
            "eta": torrent.eta.seconds if torrent.eta else None,
            "size_when_done": torrent.size_when_done,
            "have_valid": torrent.have_valid,
            "is_finished": torrent.is_finished,
        }

    async def pause_torrent(self, torrent_id: int) -> bool:
        try:
            await self._run(self._client.pause_torrent, torrent_id)
            return True
        except Exception:
            return False

    async def resume_torrent(self, torrent_id: int) -> bool:
        try:
            await self._run(self._client.start_torrent, torrent_id)
            return True
        except Exception:
            return False

    async def remove_torrent(self, torrent_id: int, delete_data: bool = False) -> bool:
        try:
            await self._run(self._client.remove_torrent, torrent_id, delete_data=delete_data)
            return True
        except Exception:
            return False
