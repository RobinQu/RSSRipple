"""Minimal BitTorrent HTTP tracker (BEP 3).

Implements announce and scrape endpoints for testing.
Tracks peers per info_hash and returns peer lists on announce.
"""

from __future__ import annotations

import struct
import time
from dataclasses import dataclass, field

import bencodepy


@dataclass
class Peer:
    """A tracked peer."""
    peer_id: bytes
    ip: str
    port: int
    uploaded: int = 0
    downloaded: int = 0
    left: int = 0
    last_seen: float = field(default_factory=time.time)


class Tracker:
    """In-memory BitTorrent tracker."""

    def __init__(self, interval: int = 30):
        self.interval = interval
        # info_hash (20 bytes hex) -> dict of peer_id -> Peer
        self.swarms: dict[str, dict[bytes, Peer]] = {}
        # info_hash -> completed count
        self.completed: dict[str, int] = {}

    def announce(
        self,
        info_hash: str,
        peer_id: bytes,
        ip: str,
        port: int,
        uploaded: int = 0,
        downloaded: int = 0,
        left: int = 0,
        event: str = "",
        numwant: int = 50,
    ) -> bytes:
        """Handle an announce request.

        Returns bencoded response.
        """
        if info_hash not in self.swarms:
            self.swarms[info_hash] = {}

        swarm = self.swarms[info_hash]

        if event == "stopped":
            swarm.pop(peer_id, None)
            return bencodepy.encode({
                b"interval": self.interval,
                b"peers": b"",
            })

        if event == "completed":
            self.completed[info_hash] = self.completed.get(info_hash, 0) + 1

        # Update or add peer
        swarm[peer_id] = Peer(
            peer_id=peer_id,
            ip=ip,
            port=port,
            uploaded=uploaded,
            downloaded=downloaded,
            left=left,
        )

        # Remove stale peers (not seen in 2x interval)
        now = time.time()
        stale = [pid for pid, p in swarm.items() if now - p.last_seen > self.interval * 2]
        for pid in stale:
            del swarm[pid]

        # Build peer list (compact format: 6 bytes per peer: 4 IP + 2 port)
        peers_compact = b""
        count = 0
        for pid, peer in swarm.items():
            if pid == peer_id:
                continue  # Don't include requesting peer
            if count >= numwant:
                break
            try:
                ip_parts = [int(x) for x in peer.ip.split(".")]
                peers_compact += struct.pack("!BBBBH", *ip_parts, peer.port)
                count += 1
            except (ValueError, struct.error):
                continue

        # Count seeders (left == 0) and leechers
        seeders = sum(1 for p in swarm.values() if p.left == 0)
        leechers = len(swarm) - seeders

        return bencodepy.encode({
            b"interval": self.interval,
            b"min interval": max(self.interval // 2, 10),
            b"complete": seeders,
            b"incomplete": leechers,
            b"peers": peers_compact,
        })

    def scrape(self, info_hashes: list[str] | None = None) -> bytes:
        """Handle a scrape request.

        Returns bencoded response.
        """
        files = {}
        hashes = info_hashes or list(self.swarms.keys())

        for ih in hashes:
            swarm = self.swarms.get(ih, {})
            seeders = sum(1 for p in swarm.values() if p.left == 0)
            leechers = len(swarm) - seeders
            files[ih.encode() if isinstance(ih, str) else ih] = {
                b"complete": seeders,
                b"incomplete": leechers,
                b"downloaded": self.completed.get(ih, 0),
            }

        return bencodepy.encode({b"files": files})
