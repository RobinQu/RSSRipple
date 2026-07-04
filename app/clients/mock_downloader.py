"""Mock downloader wrapper for local testing.

Provides the same async surface as :class:`TransmissionWrapper` so the
Agent → download → progress-sync loop can be exercised end-to-end without a
real Transmission daemon.

Each ``add_torrent`` call registers an in-process ``_TorrentState`` and
schedules a background task that marks the torrent finished after a random
1-10 second delay. Progress is interpolated from wall-clock elapsed time so
callers polling :meth:`list_torrents` / :meth:`get_torrent` see the download
smoothly advance from 0% to 100%.

State lives in a module-level ``_STATE`` dict keyed by downloader id. It's
in-memory only — a process restart wipes every simulated torrent, which is
fine (and expected) for the dev/testing use case.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


MIN_DURATION_S = 1.0
MAX_DURATION_S = 10.0
FAKE_TOTAL_SIZE = 1_000_000_000  # 1 GB — enough that rate_download looks real
FAKE_FREE_SPACE = 1_000_000_000_000  # 1 TB


@dataclass
class _TorrentState:
    id: int
    name: str
    hash: str
    download_dir: str
    added_at: float
    duration: float
    paused: bool = False
    paused_at: float | None = None
    paused_elapsed: float = 0.0
    removed: bool = False

    # Cached total bytes so rate_download works out to something reasonable.
    total_size: int = FAKE_TOTAL_SIZE

    def elapsed(self, now: float | None = None) -> float:
        """Wall-clock time the torrent has spent 'downloading', excluding pauses."""
        now = now if now is not None else time.monotonic()
        if self.paused and self.paused_at is not None:
            return self.paused_elapsed
        return (now - self.added_at) - self.paused_elapsed

    def snapshot(self, now: float | None = None) -> dict[str, Any]:
        e = self.elapsed(now)
        pct = 0.0 if self.duration <= 0 else min(1.0, max(0.0, e / self.duration))
        is_finished = pct >= 1.0
        left = int(self.total_size * max(0.0, 1.0 - pct))
        rate = 0 if self.paused or is_finished else int(self.total_size / max(1.0, self.duration))
        eta = 0 if is_finished or self.paused else max(0, int(self.duration - e))
        if is_finished:
            status = "seeding"
        elif self.paused:
            status = "stopped"
        else:
            status = "downloading"
        return {
            "id": self.id,
            "name": self.name,
            "hash": self.hash,
            "status": status,
            "raw_status": status,
            "percent_done": pct,
            "rate_download": rate,
            "rate_upload": 0,
            "eta_seconds": eta,
            "total_size": self.total_size,
            "have_valid": self.total_size - left,
            "is_finished": is_finished,
            "left_until_done": left,
            "error": 0,
            "error_string": "",
            "added_date": None,
            "peers_connected": 0,
        }


@dataclass
class _DownloaderStore:
    torrents: dict[int, _TorrentState] = field(default_factory=dict)
    next_id: int = 1


_STATE: dict[str, _DownloaderStore] = {}


def _store(downloader_id: str) -> _DownloaderStore:
    return _STATE.setdefault(downloader_id, _DownloaderStore())


def _guess_name(torrent_url: str) -> str:
    """Extract a torrent-ish name from a magnet dn= param or file basename."""
    if torrent_url.startswith("magnet:"):
        for part in torrent_url.split("&"):
            if part.startswith("dn="):
                return part[3:] or "unnamed.torrent"
    return torrent_url.rsplit("/", 1)[-1] or "unnamed.torrent"


def _hash_for(torrent_url: str) -> str:
    return hashlib.sha1(torrent_url.encode("utf-8", errors="ignore")).hexdigest()


def reset_state() -> None:
    """Wipe all mock-downloader state. Used by tests."""
    _STATE.clear()


class MockDownloaderWrapper:
    """Drop-in stand-in for :class:`TransmissionWrapper` that never touches
    the network.

    The wrapper is stateless itself — all torrent state is kept in the module
    ``_STATE`` map, keyed by the ``DownloaderInstance.id``. That way every
    call to ``get_downloader_client`` for the same downloader sees the same
    simulated torrents even across API requests.
    """

    def __init__(
        self,
        url: str | None = None,
        username: str | None = None,
        password: str | None = None,
        downloader: Any | None = None,
    ) -> None:
        if downloader is not None:
            self._downloader_id = downloader.id
        else:
            # Anonymous instantiation is only used by tests. Derive a stable
            # key from the URL so repeated wrappers share state.
            self._downloader_id = url or "mock-anon"

    async def test_connection(self) -> tuple[bool, str | None]:
        return True, "Mock Downloader 1.0"

    async def add_torrent(
        self,
        torrent_url: str,
        download_dir: str | None = None,
        paused: bool = False,
    ) -> dict[str, Any]:
        store = _store(self._downloader_id)
        torrent_id = store.next_id
        store.next_id += 1
        duration = random.uniform(MIN_DURATION_S, MAX_DURATION_S)
        state = _TorrentState(
            id=torrent_id,
            name=_guess_name(torrent_url),
            hash=_hash_for(torrent_url),
            download_dir=download_dir or "",
            added_at=time.monotonic(),
            duration=duration,
        )
        if paused:
            state.paused = True
            state.paused_at = state.added_at
        store.torrents[torrent_id] = state
        logger.info(
            "[mock_downloader:%s] add torrent id=%d name=%r duration=%.2fs",
            self._downloader_id, torrent_id, state.name, duration,
        )
        return {"torrent_id": torrent_id, "name": state.name, "hash": state.hash}

    async def list_torrents(self) -> list[dict[str, Any]]:
        store = _store(self._downloader_id)
        now = time.monotonic()
        return [s.snapshot(now) for s in store.torrents.values() if not s.removed]

    async def get_torrent(self, torrent_id: int) -> dict[str, Any]:
        store = _store(self._downloader_id)
        state = store.torrents.get(torrent_id)
        if not state or state.removed:
            raise ValueError(f"Torrent {torrent_id} not found")
        return state.snapshot()

    async def pause_torrent(self, torrent_id: int) -> bool:
        store = _store(self._downloader_id)
        state = store.torrents.get(torrent_id)
        if not state or state.removed:
            return False
        if not state.paused:
            state.paused = True
            state.paused_at = time.monotonic()
        return True

    async def resume_torrent(self, torrent_id: int) -> bool:
        store = _store(self._downloader_id)
        state = store.torrents.get(torrent_id)
        if not state or state.removed:
            return False
        if state.paused and state.paused_at is not None:
            state.paused_elapsed += time.monotonic() - state.paused_at
            state.paused = False
            state.paused_at = None
        return True

    async def remove_torrent(self, torrent_id: int, delete_data: bool = False) -> bool:
        store = _store(self._downloader_id)
        state = store.torrents.pop(torrent_id, None)
        return state is not None

    async def free_space(self, path: str) -> int:
        return FAKE_FREE_SPACE
