"""End-to-end integration: RSS fetch → magnet metadata resolution → file listing.

Fixtures are REAL data harvested from the production ThePirateBay channel
("PriateBay-4K-Movies"): ``tests/fixtures/magnet_feed.xml`` carries three
verbatim magnet entries and ``tests/fixtures/magnet_links.json`` the full
harvest set (source of the live-test magnet).

Two layers:

- **Hermetic E2E** (CI-safe, no network): a channel points at the fixture
  feed file (feedparser accepts a filesystem path), the fetch pipeline
  creates magnet FileResources and enqueues ``resolve_magnet_torrent`` jobs,
  the queue handler claims each row and the worker pool rebuilds a .torrent
  through a fake libtorrent — then Channel A inspection and the files
  resolution chain (``_resolve_resource_files``) run for real against the
  rebuilt cache. Re-fetch proves dedup + usable-cache skip.

- **Live E2E** (opt-in, real libtorrent + DHT + swarm): set
  ``RSSRIPPLE_LIVE_MAGNET=1`` to enable. Uses the harvested Mufasa 2024
  magnet (healthy swarm at harvest time). Requires **UDP outbound** (DHT
  bootstrap + peer traffic) — networks that block UDP make resolution time
  out by design. Tunable via ``RSSRIPPLE_LIVE_MAGNET_TIMEOUT`` (default
  300s). Run:

      RSSRIPPLE_LIVE_MAGNET=1 uv run pytest tests/integration/magnet -q -k live
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import bencodepy
import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

import app.services.magnet_resolve as mr
import app.services.task_queue as task_queue_mod
from app.job_handlers import _handle_resolve_magnet_torrent
from app.models.channel import Channel
from app.models.file_resource import FileResource
from app.services.fetch_service import fetch_channel_resources
from app.services.torrent_inspect import parse_torrent_files

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
FEED_FILE = FIXTURES / "magnet_feed.xml"
LINKS_FILE = FIXTURES / "magnet_links.json"

# The fake libtorrent rebuilds this payload — a realistic single-file movie
# release (the harvested magnets are single-file 4K movie releases).
_FAKE_NAME = b"Mufasa.The.Lion.King.2024.2160p.DSNP.WEB-DL.MULTi.Atmos-SomniWare.mkv"
_FAKE_TORRENT = {
    b"info": {
        b"name": _FAKE_NAME,
        b"length": 42_000_000_000,
        b"piece length": 4 * 1024 * 1024,
        b"pieces": b"x" * 20,
    }
}


def _fake_lt():
    """Fake libtorrent covering the API surface magnet_resolve uses.

    ``added_params`` records every params object passed to add_torrent and
    ``sessions_created`` every constructed session, so tests can assert
    tracker injection and that the mirror fast path skipped P2P entirely.
    """
    added_params: list = []
    sessions_created: list = []

    class _Params:
        def __init__(self):
            self.flags = 0
            self.trackers = []
            self.save_path = ""

    class _Status:
        errc = None
        has_metadata = True

    class _Handle:
        def status(self):
            return _Status()

        def torrent_file(self):
            return object()

    class _Session:
        def __init__(self, settings):
            self.settings = settings
            sessions_created.append(self)

        def add_torrent(self, params):
            added_params.append(params)
            return _Handle()

        def remove_torrent(self, handle):
            pass

    class _CreateTorrent:
        def __init__(self, ti):
            pass

        def add_tracker(self, tracker):
            pass

        def generate(self):
            return _FAKE_TORRENT

    return SimpleNamespace(
        session=_Session,
        parse_magnet_uri=lambda uri: _Params(),
        create_torrent=_CreateTorrent,
        bencode=bencodepy.encode,
        torrent_flags=SimpleNamespace(upload_mode=1),
        added_params=added_params,
        sessions_created=sessions_created,
    )


class _StubQueue:
    """Records enqueued jobs; never executes them (tests drive handlers)."""

    def __init__(self):
        self.calls: list[dict] = []

    async def enqueue(self, job_type, key, payload):
        self.calls.append({"job_type": job_type, "key": key, "payload": payload})
        return {"key": key, "status": "queued"}


@pytest.fixture(autouse=True)
def _reset_magnet_state():
    mr._session = None
    mr._semaphore = None
    mr._inflight.clear()
    mr._background_tasks.clear()
    mr._unavailable_logged = False
    yield


async def _make_feed_channel(db_session, url: str) -> Channel:
    ch = Channel(
        id=str(uuid.uuid4()),
        name="PriateBay fixture",
        type="rss_feed",
        url=url,
        fetch_interval=1800,
        status="active",
        field_mapping={},
        metadata_agent_enabled=False,
    )
    db_session.add(ch)
    await db_session.commit()
    return await _reload_channel(db_session, ch.id)


async def _reload_channel(db_session, channel_id: str) -> Channel:
    cur = await db_session.execute(
        select(Channel)
        .where(Channel.id == channel_id)
        .options(
            selectinload(Channel.agents),
            selectinload(Channel.file_resources),
            selectinload(Channel.raw_title_mappings),
        )
    )
    return cur.scalar_one()


async def _channel_resources(db_session, channel_id: str) -> list[FileResource]:
    cur = await db_session.execute(
        select(FileResource)
        .where(FileResource.channel_id == channel_id)
        .order_by(FileResource.title_raw)
    )
    return list(cur.scalars().all())


async def _drain_and_wait(db_session, resource_ids: list[str], deadline_s: float = 30.0) -> None:
    """Drain the detached worker tasks, then poll until all rows are done."""
    tasks = list(mr._background_tasks)
    if tasks:
        await asyncio.gather(*tasks)
    deadline = asyncio.get_running_loop().time() + deadline_s
    while asyncio.get_running_loop().time() < deadline:
        await db_session.rollback()  # drop snapshot reads
        cur = await db_session.execute(
            select(FileResource.magnet_resolve_status).where(FileResource.id.in_(resource_ids))
        )
        statuses = {row[0] for row in cur.all()}
        if statuses == {"done"}:
            return
        await asyncio.sleep(0.2)
    raise AssertionError(f"magnet resolution did not finish; statuses={statuses}")


# =============================================================================
# Hermetic E2E (CI-safe)
# =============================================================================


async def test_fetch_to_magnet_resolve_end_to_end(db_session, monkeypatch, tmp_path):
    """Fixture feed → fetch → enqueue → claim → rebuild .torrent → Channel A
    → files chain, all in-process with a fake libtorrent."""
    from app.api.v1.resources import _resolve_resource_files

    fake = _fake_lt()
    monkeypatch.setattr(mr, "lt", fake)
    monkeypatch.setattr(mr.settings, "torrent_cache_dir", str(tmp_path))
    # Hermetic: no real HTTPS calls to infohash-cache mirrors.
    monkeypatch.setattr(mr.settings, "magnet_resolve_cache_mirrors", [])
    stub_queue = _StubQueue()
    monkeypatch.setattr(task_queue_mod, "task_queue", stub_queue)

    # 1. Fetch: the fixture feed's magnet entries become FileResources.
    # Capture the id up front — fetch commits expire ORM objects, and lazy
    # attribute access on an expired object raises MissingGreenlet.
    channel = await _make_feed_channel(db_session, str(FEED_FILE))
    channel_id = channel.id
    result = await fetch_channel_resources(channel, db_session)
    assert result["new_count"] == 3

    resources = await _channel_resources(db_session, channel_id)
    assert len(resources) == 3
    for r in resources:
        assert r.torrent_url.startswith("magnet:")
        assert r.torrent_file is None  # nothing resolvable synchronously
    resource_ids = [r.id for r in resources]

    # 2. Fetch enqueued one resolution job per magnet resource.
    resolve_jobs = [c for c in stub_queue.calls if c["job_type"] == "resolve_magnet_torrent"]
    assert {c["payload"]["resource_id"] for c in resolve_jobs} == set(resource_ids)

    # 3. Queue handler claims each row; detached pool resolves and rebuilds
    #    the .torrent into the cache.
    for rid in resource_ids:
        outcome = await _handle_resolve_magnet_torrent({"resource_id": rid})
        assert outcome == {"accepted": True}
    await _drain_and_wait(db_session, resource_ids)

    # 3b. The harvested TPB magnets carry no tr= params — every resolution
    #     attempt must have received the configured default public trackers
    #     (with parallel tiers) on top of the magnet's own (none here).
    assert len(fake.added_params) == 3
    for params in fake.added_params:
        assert params.trackers == mr.settings.magnet_resolve_default_trackers
        assert params.tracker_tiers == list(range(len(params.trackers)))

    # 4. Terminal state: done + cached .torrent that parses, and the files
    #    chain serves it from torrent_cache.
    for r in resources:
        await db_session.refresh(r)
        assert r.magnet_resolve_status == "done"
        assert r.magnet_resolve_error is None
        assert r.torrent_file is not None
        files = parse_torrent_files(r.torrent_file)
        assert files is not None and len(files) == 1
        assert files[0]["name"].endswith(".mkv")

        listing, source = await _resolve_resource_files(db_session, r)
        assert source == "torrent_cache"
        assert listing == files

    # 5. Re-fetch: guid dedup creates nothing, and the usable cache suppresses
    #    re-enqueueing resolution.
    calls_before = len(stub_queue.calls)
    channel = await _reload_channel(db_session, channel_id)
    result2 = await fetch_channel_resources(channel, db_session)
    assert result2["new_count"] == 0
    assert not [
        c for c in stub_queue.calls[calls_before:]
        if c["job_type"] == "resolve_magnet_torrent"
    ]


async def test_fetch_to_resolve_via_cache_mirror(db_session, monkeypatch, tmp_path):
    """Mirror fast path E2E: fetch → enqueue → claim → mirror hit, with P2P
    never engaged (fake lt session never constructed).

    The fixture magnets carry real hashes whose torrents we cannot fabricate
    (preimage), so ``_extract_v1_infohash`` is stubbed to the fabricated
    torrent's digest — everything downstream (mirror fetch → structural parse
    → infohash re-verification → cache write → status/Channel A) runs for real.
    """
    import hashlib as _hashlib

    fake = _fake_lt()
    monkeypatch.setattr(mr, "lt", fake)
    monkeypatch.setattr(mr.settings, "torrent_cache_dir", str(tmp_path))
    stub_queue = _StubQueue()
    monkeypatch.setattr(task_queue_mod, "task_queue", stub_queue)

    mirror_raw = bencodepy.encode({b"info": _FAKE_TORRENT[b"info"]})
    mirror_hash = _hashlib.sha1(
        bencodepy.encode(_FAKE_TORRENT[b"info"])
    ).hexdigest()
    monkeypatch.setattr(mr, "_extract_v1_infohash", lambda uri, params: mirror_hash)
    monkeypatch.setattr(mr, "_fetch_mirror_torrent", lambda url: mirror_raw)

    channel = await _make_feed_channel(db_session, str(FEED_FILE))
    result = await fetch_channel_resources(channel, db_session)
    assert result["new_count"] == 3
    resources = await _channel_resources(db_session, channel.id)
    resource_ids = [r.id for r in resources]

    for rid in resource_ids:
        outcome = await _handle_resolve_magnet_torrent({"resource_id": rid})
        assert outcome == {"accepted": True}
    await _drain_and_wait(db_session, resource_ids)

    # Every resolution completed through the mirror — P2P never constructed.
    assert fake.sessions_created == []
    for r in resources:
        await db_session.refresh(r)
        assert r.magnet_resolve_status == "done"
        assert r.torrent_file is not None
        files = parse_torrent_files(r.torrent_file)
        assert files is not None and files[0]["name"].endswith(".mkv")


# =============================================================================
# Live E2E (opt-in: real libtorrent + DHT + swarm)
# =============================================================================

_LIVE = os.environ.get("RSSRIPPLE_LIVE_MAGNET") == "1"
_LIVE_TIMEOUT = int(os.environ.get("RSSRIPPLE_LIVE_MAGNET_TIMEOUT", "300"))
_live_skip = pytest.mark.skipif(
    not _LIVE or mr.lt is None,
    reason="live magnet test: set RSSRIPPLE_LIVE_MAGNET=1 (needs network/DHT + libtorrent)",
)


def _live_magnet() -> str:
    data = json.loads(LINKS_FILE.read_text())
    default_title = data["live_default"]
    return next(e["magnet"] for e in data["entries"] if e["title"] == default_title)


@_live_skip
@pytest.mark.timeout(_LIVE_TIMEOUT + 120)
async def test_live_magnet_resolution(tmp_path):
    """Real libtorrent/DHT: resolve the harvested magnet and parse the listing."""
    dest = tmp_path / "live.torrent"
    await mr.resolve_magnet_to_cache(_live_magnet(), str(dest), _LIVE_TIMEOUT)
    files = parse_torrent_files(str(dest))
    assert files, "resolved .torrent must carry a file listing"
    assert any(f["name"].lower().endswith((".mkv", ".mp4")) for f in files)


@_live_skip
@pytest.mark.timeout(_LIVE_TIMEOUT + 120)
async def test_live_fetch_to_resolve_end_to_end(db_session, monkeypatch, tmp_path):
    """Full live pipeline: fixture feed fetch → real resolution → done state
    → cached .torrent serving the files chain."""
    from app.api.v1.resources import _resolve_resource_files

    monkeypatch.setattr(mr.settings, "torrent_cache_dir", str(tmp_path))
    monkeypatch.setattr(mr.settings, "magnet_resolve_timeout_seconds", _LIVE_TIMEOUT)
    monkeypatch.setattr(mr.settings, "magnet_resolve_max_attempts", 0)
    # Live means live P2P only — never hit real mirrors from tests.
    monkeypatch.setattr(mr.settings, "magnet_resolve_cache_mirrors", [])

    channel = await _make_feed_channel(db_session, str(FEED_FILE))
    result = await fetch_channel_resources(channel, db_session)
    assert result["new_count"] == 3
    resources = await _channel_resources(db_session, channel.id)

    # Resolve the healthy-swarm entry only; the other two stay queued (their
    # fetch-time enqueue is harmless — no consumer runs in-process).
    target = next(r for r in resources if "Mufasa" in r.title_raw)
    outcome = await _handle_resolve_magnet_torrent({"resource_id": target.id})
    assert outcome == {"accepted": True}

    await _drain_and_wait(db_session, [target.id], deadline_s=_LIVE_TIMEOUT + 60)
    await db_session.refresh(target)
    assert target.magnet_resolve_status == "done", (
        f"live resolution ended as {target.magnet_resolve_status}: {target.magnet_resolve_error}"
    )
    files = parse_torrent_files(target.torrent_file)
    assert files and any(f["name"].lower().endswith((".mkv", ".mp4")) for f in files)
    listing, source = await _resolve_resource_files(db_session, target)
    assert source == "torrent_cache" and listing == files
