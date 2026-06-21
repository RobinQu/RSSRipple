"""Torrent service using libtorrent for create/seed/download/assert operations.

Provides a REST-manageable torrent client that:
- Creates .torrent files from test files
- Seeds torrents via libtorrent
- Downloads torrents from the test tracker
- Provides assertion endpoints for download completion
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Lazy import libtorrent — may not be available outside Docker
_lt = None


def _get_lt():
    global _lt
    if _lt is None:
        import libtorrent as lt
        _lt = lt
    return _lt


@dataclass
class TorrentJob:
    """Tracks a torrent being seeded or downloaded."""
    info_hash: str
    name: str
    save_path: str
    torrent_path: str | None = None
    status: str = "pending"  # pending, seeding, downloading, complete, error
    progress: float = 0.0
    download_rate: int = 0
    upload_rate: int = 0
    error: str | None = None
    started_at: float = field(default_factory=time.time)


class TorrentService:
    """Manages torrent creation, seeding, and downloading."""

    def __init__(self, base_dir: str = "/tmp/torrent-test"):
        self.base_dir = Path(base_dir)
        self.files_dir = self.base_dir / "files"
        self.torrents_dir = self.base_dir / "torrents"
        self.downloads_dir = self.base_dir / "downloads"

        # Ensure directories exist
        self.files_dir.mkdir(parents=True, exist_ok=True)
        self.torrents_dir.mkdir(parents=True, exist_ok=True)
        self.downloads_dir.mkdir(parents=True, exist_ok=True)

        # State
        self.jobs: dict[str, TorrentJob] = {}  # info_hash -> job
        self._sessions: dict[str, object] = {}  # info_hash -> lt.session
        self._tracker_url: str = ""

    def set_tracker_url(self, url: str):
        """Set the tracker URL for new torrents."""
        self._tracker_url = url

    def save_test_file(self, name: str, content: bytes) -> Path:
        """Save a test file to the files directory."""
        path = self.files_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def create_torrent(self, file_name: str) -> tuple[str, str]:
        """Create a .torrent file from a test file.

        Returns:
            (info_hash_hex, torrent_file_path)
        """
        lt = _get_lt()
        file_path = self.files_dir / file_name

        if not file_path.exists():
            raise FileNotFoundError(f"Test file not found: {file_name}")

        # Create file storage
        fs = lt.file_storage()
        fs.add_file(str(file_path.name), file_path.stat().st_size)

        # Create torrent
        t = lt.create_torrent(fs, 0, 16384)  # 16KB piece size for small test files
        if self._tracker_url:
            t.add_tracker(self._tracker_url)
        t.set_creator("RSSRipple Test Server")

        # Compute piece hashes
        parent = str(file_path.parent)
        lt.set_piece_hashes(t, parent)

        # Generate .torrent
        torrent_data = lt.bencode(t.generate())
        info_hash = hashlib.sha1(lt.bencode(t.generate()["info"])).hexdigest().lower()

        torrent_path = self.torrents_dir / f"{info_hash}.torrent"
        torrent_path.write_bytes(torrent_data)

        logger.info("Created torrent: %s -> %s (hash: %s)", file_name, torrent_path, info_hash)
        return info_hash, str(torrent_path)

    async def seed(self, info_hash: str) -> TorrentJob:
        """Start seeding a torrent."""
        lt = _get_lt()
        torrent_path = self.torrents_dir / f"{info_hash}.torrent"

        if not torrent_path.exists():
            raise FileNotFoundError(f"Torrent not found: {info_hash}")

        # Create a dedicated session for seeding
        ses = lt.session()
        ses.apply_settings({
            "listen_interfaces": "0.0.0.0:0",  # random port
            "enable_dht": False,
            "enable_lsd": False,
            "enable_natpmp": False,
            "enable_upnp": False,
        })

        ti = lt.torrent_info(str(torrent_path))
        h = ses.add_torrent({
            "ti": ti,
            "save_path": str(self.files_dir),
            "flags": lt.torrent_flags.seed_mode,
        })

        job = TorrentJob(
            info_hash=info_hash,
            name=ti.name(),
            save_path=str(self.files_dir),
            torrent_path=str(torrent_path),
            status="seeding",
        )
        self.jobs[info_hash] = job
        self._sessions[info_hash] = ses

        logger.info("Seeding: %s (hash: %s)", ti.name(), info_hash)
        return job

    async def download(self, info_hash: str) -> TorrentJob:
        """Start downloading a torrent."""
        lt = _get_lt()
        torrent_path = self.torrents_dir / f"{info_hash}.torrent"

        if not torrent_path.exists():
            raise FileNotFoundError(f"Torrent not found: {info_hash}")

        dl_dir = self.downloads_dir / info_hash
        dl_dir.mkdir(parents=True, exist_ok=True)

        ses = lt.session()
        ses.apply_settings({
            "listen_interfaces": "0.0.0.0:0",
            "enable_dht": False,
            "enable_lsd": False,
            "enable_natpmp": False,
            "enable_upnp": False,
        })

        ti = lt.torrent_info(str(torrent_path))
        h = ses.add_torrent({
            "ti": ti,
            "save_path": str(dl_dir),
        })

        job = TorrentJob(
            info_hash=info_hash,
            name=ti.name(),
            save_path=str(dl_dir),
            torrent_path=str(torrent_path),
            status="downloading",
        )
        self.jobs[info_hash] = job
        self._sessions[info_hash] = ses

        # Start background monitor
        asyncio.create_task(self._monitor_download(info_hash, h, ses))

        logger.info("Downloading: %s (hash: %s) to %s", ti.name(), info_hash, dl_dir)
        return job

    async def _monitor_download(self, info_hash: str, handle, session):
        """Background task to monitor download progress."""
        while True:
            await asyncio.sleep(0.5)
            job = self.jobs.get(info_hash)
            if not job or job.status in ("complete", "error"):
                break

            try:
                s = handle.status()
                job.progress = s.progress
                job.download_rate = s.download_rate
                job.upload_rate = s.upload_rate

                if s.is_seeding:
                    job.status = "complete"
                    job.progress = 1.0
                    logger.info("Download complete: %s", info_hash)
                    break
            except Exception as e:
                job.error = str(e)
                job.status = "error"
                break

    def get_status(self, info_hash: str) -> TorrentJob | None:
        """Get the current status of a torrent job."""
        job = self.jobs.get(info_hash)
        if job:
            # Update from live handle if session exists
            ses = self._sessions.get(info_hash)
            if ses:
                try:
                    for h in ses.get_torrents():
                        s = h.status()
                        job.progress = s.progress
                        job.download_rate = s.download_rate
                        job.upload_rate = s.upload_rate
                        if s.is_seeding and job.status == "downloading":
                            job.status = "complete"
                            job.progress = 1.0
                except Exception:
                    pass
        return job

    def assert_complete(self, info_hash: str) -> tuple[bool, str]:
        """Assert that a torrent has been fully downloaded.

        Returns:
            (success, message)
        """
        job = self.jobs.get(info_hash)
        if not job:
            return False, f"No job found for hash {info_hash}"

        if job.status != "complete":
            return False, f"Torrent not complete: status={job.status}, progress={job.progress:.1%}"

        # Verify file exists in download directory
        dl_dir = Path(job.save_path)
        if not dl_dir.exists():
            return False, f"Download directory missing: {dl_dir}"

        # Check that the file has content
        for f in dl_dir.rglob("*"):
            if f.is_file() and f.stat().st_size > 0:
                return True, f"Download verified: {f.name} ({f.stat().st_size} bytes)"

        return False, "Download directory exists but no files with content found"

    def get_torrent_bytes(self, info_hash: str) -> bytes | None:
        """Get raw .torrent file bytes."""
        path = self.torrents_dir / f"{info_hash}.torrent"
        if path.exists():
            return path.read_bytes()
        return None

    def cleanup(self):
        """Clean up all sessions and files."""
        for ses in self._sessions.values():
            try:
                ses.pause()
            except Exception:
                pass
        self._sessions.clear()
        self.jobs.clear()
