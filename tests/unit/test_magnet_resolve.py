"""Unit tests for app.services.magnet_resolve.

libtorrent is fully mocked: tests inject a fake ``lt`` module (SimpleNamespace)
so no real session / network is involved. The fake ``create_torrent`` /
``bencode`` pair produces real bencoded bytes (via bencodepy, as used by
torrent_inspect) so the success path can be validated end-to-end through
``parse_torrent_files``.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import bencodepy
import pytest

import app.services.magnet_resolve as mr
from app.services.torrent_inspect import parse_torrent_files
from app.utils.time import utcnow

_VALID_TORRENT = {
    b"info": {
        b"name": b"root",
        b"files": [{b"length": 100, b"path": [b"a.mkv"]}],
        b"piece length": 16384,
        b"pieces": b"x" * 20,
    }
}


def _fake_lt(*, has_metadata: bool = True, parse_error: bool = False):
    """Build a fake libtorrent module covering the API surface we use.

    ``added_params`` records every params object passed to add_torrent so
    tests can assert tracker injection.
    """
    added_params: list = []

    class _Params:
        def __init__(self):
            self.flags = 0
            self.trackers = ["udp://tracker.example:1337"]
            self.save_path = ""

    class _Status:
        errc = None

        @property
        def has_metadata(self):
            return has_metadata

    class _Handle:
        def status(self):
            return _Status()

        def torrent_file(self):
            return object()  # opaque torrent_info stand-in

    class _Session:
        def __init__(self, settings):
            self.settings = settings

        def add_torrent(self, params):
            added_params.append(params)
            return _Handle()

        def remove_torrent(self, handle):
            pass

    class _CreateTorrent:
        def __init__(self, ti):
            self.trackers = []

        def add_tracker(self, tracker):
            self.trackers.append(tracker)

        def generate(self):
            return _VALID_TORRENT

    def _parse(uri):
        if parse_error:
            raise RuntimeError("invalid magnet uri")
        return _Params()

    return SimpleNamespace(
        session=_Session,
        parse_magnet_uri=_parse,
        create_torrent=_CreateTorrent,
        bencode=bencodepy.encode,
        torrent_flags=SimpleNamespace(upload_mode=1),
        added_params=added_params,
    )


@pytest.fixture(autouse=True)
def _reset_magnet_state():
    mr._session = None
    mr._semaphore = None
    mr._inflight.clear()
    mr._background_tasks.clear()
    mr._unavailable_logged = False
    yield


async def _make_magnet_resource(db_session, channel_id, **overrides):
    from app.models.file_resource import FileResource

    defaults = dict(
        id=str(uuid.uuid4()),
        channel_id=channel_id,
        guid=str(uuid.uuid4()),
        title_raw="[Group] Show - 01 [1080p]",
        torrent_url="magnet:?xt=urn:btih:abc",
    )
    defaults.update(overrides)
    r = FileResource(**defaults)
    db_session.add(r)
    await db_session.commit()
    return r


async def _drain_background() -> None:
    tasks = list(mr._background_tasks)
    if tasks:
        await asyncio.gather(*tasks)


# =============================================================================
# resolve_magnet_to_cache
# =============================================================================

async def test_resolve_success_writes_valid_torrent(tmp_path, monkeypatch):
    monkeypatch.setattr(mr, "lt", _fake_lt())
    dest = tmp_path / "r.torrent"
    await mr.resolve_magnet_to_cache("magnet:?xt=urn:btih:abc", str(dest), 60)
    assert parse_torrent_files(str(dest)) == [{"name": "a.mkv", "size": 100}]


async def test_resolve_invalid_magnet_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(mr, "lt", _fake_lt(parse_error=True))
    with pytest.raises(mr.InvalidMagnetError):
        await mr.resolve_magnet_to_cache("magnet:?xt=bad", str(tmp_path / "x.torrent"), 60)


async def test_resolve_timeout_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(mr, "lt", _fake_lt(has_metadata=False))
    with pytest.raises(mr.MagnetTimeoutError):
        await mr.resolve_magnet_to_cache(
            "magnet:?xt=urn:btih:abc", str(tmp_path / "x.torrent"), 0.05
        )


async def test_resolve_unavailable_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(mr, "lt", None)
    with pytest.raises(mr.MagnetUnavailableError):
        await mr.resolve_magnet_to_cache(
            "magnet:?xt=urn:btih:abc", str(tmp_path / "x.torrent"), 60
        )


# =============================================================================
# launch_resolution / attempt loop
# =============================================================================

async def test_launch_claim_guard_rejects_pending(
    db_session, sample_channel, monkeypatch, tmp_path
):
    monkeypatch.setattr(mr, "lt", _fake_lt())
    monkeypatch.setattr(mr.settings, "torrent_cache_dir", str(tmp_path))

    async def _noop(resource_id):
        return None

    monkeypatch.setattr(mr, "_attempt_loop", _noop)

    r = await _make_magnet_resource(db_session, sample_channel.id)
    assert await mr.launch_resolution(r.id) is True
    await _drain_background()
    # The row is now "pending" — the atomic SQL claim rejects a second launch
    # (this or another process), independent of the in-process inflight set.
    assert await mr.launch_resolution(r.id) is False
    await db_session.refresh(r)
    assert r.magnet_resolve_status == "pending"


async def test_launch_skipped_when_libtorrent_missing(
    db_session, sample_channel, monkeypatch
):
    monkeypatch.setattr(mr, "lt", None)
    r = await _make_magnet_resource(db_session, sample_channel.id)
    assert await mr.launch_resolution(r.id) is False
    await db_session.refresh(r)
    # Statuses never transition without libtorrent.
    assert r.magnet_resolve_status is None


async def test_attempt_loop_success_marks_done_and_inspects(
    db_session, sample_channel, monkeypatch, tmp_path
):
    monkeypatch.setattr(mr, "lt", _fake_lt())
    monkeypatch.setattr(mr.settings, "torrent_cache_dir", str(tmp_path))
    inspect = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "app.services.torrent_inspect.maybe_inspect_torrent", inspect
    )

    r = await _make_magnet_resource(db_session, sample_channel.id)
    assert await mr.launch_resolution(r.id) is True
    await _drain_background()
    await db_session.refresh(r)
    assert r.magnet_resolve_status == "done"
    assert r.magnet_resolve_error is None
    assert r.torrent_file and Path(r.torrent_file).exists()
    assert parse_torrent_files(r.torrent_file) is not None
    inspect.assert_awaited_once()


async def test_attempt_loop_failure_exhausts_attempts(
    db_session, sample_channel, monkeypatch, tmp_path
):
    monkeypatch.setattr(mr, "lt", _fake_lt(parse_error=True))
    monkeypatch.setattr(mr.settings, "torrent_cache_dir", str(tmp_path))
    monkeypatch.setattr(mr.settings, "magnet_resolve_max_attempts", 1)

    r = await _make_magnet_resource(db_session, sample_channel.id)
    assert await mr.launch_resolution(r.id) is True
    await _drain_background()
    await db_session.refresh(r)
    # First attempt + one automatic retry (the 60s backoff is fast-patched).
    assert r.magnet_resolve_status == "failed"
    assert r.magnet_resolve_attempts == 2
    assert "invalid magnet" in (r.magnet_resolve_error or "")


# =============================================================================
# enqueue_resolution
# =============================================================================

async def test_enqueue_resolution_enqueues_for_magnet(
    db_session, sample_channel, monkeypatch
):
    monkeypatch.setattr(mr, "lt", _fake_lt())
    fake_queue = SimpleNamespace(enqueue=AsyncMock(return_value={"job_id": "j"}))
    monkeypatch.setattr("app.services.task_queue.task_queue", fake_queue)

    r = await _make_magnet_resource(db_session, sample_channel.id)
    await mr.enqueue_resolution(r.id)
    fake_queue.enqueue.assert_awaited_once_with(
        "resolve_magnet_torrent", f"magnet:{r.id}", {"resource_id": r.id}
    )


async def test_enqueue_resolution_skips_non_magnet_and_cached(
    db_session, sample_channel, monkeypatch, tmp_path
):
    monkeypatch.setattr(mr, "lt", _fake_lt())
    fake_queue = SimpleNamespace(enqueue=AsyncMock(return_value={"job_id": "j"}))
    monkeypatch.setattr("app.services.task_queue.task_queue", fake_queue)

    http_res = await _make_magnet_resource(
        db_session, sample_channel.id, torrent_url="https://x/r.torrent"
    )
    await mr.enqueue_resolution(http_res.id)

    # Usable cache: file exists and parses → no resolution needed.
    cached = tmp_path / "cached.torrent"
    cached.write_bytes(bencodepy.encode(_VALID_TORRENT))
    cached_res = await _make_magnet_resource(
        db_session, sample_channel.id, torrent_file=str(cached)
    )
    await mr.enqueue_resolution(cached_res.id)

    fake_queue.enqueue.assert_not_awaited()


# =============================================================================
# Hourly sweep: stuck-row reclaim (worker crash/restart leaves pending/running)
# =============================================================================

async def test_sweep_reclaims_stuck_running_row(
    db_session, sample_channel, monkeypatch
):
    from app.job_handlers import _handle_magnet_resolve_sweep

    monkeypatch.setattr(mr, "lt", _fake_lt())
    fake_queue = SimpleNamespace(enqueue=AsyncMock(return_value={"job_id": "j"}))
    monkeypatch.setattr("app.services.task_queue.task_queue", fake_queue)

    stuck = await _make_magnet_resource(
        db_session, sample_channel.id,
        magnet_resolve_status="running",
        magnet_resolve_attempts=2,
        magnet_resolve_error="no metadata received within 900s",
        magnet_resolve_updated_at=utcnow() - timedelta(days=1),
    )
    result = await _handle_magnet_resolve_sweep({})
    assert result["reclaimed"] == 1
    await db_session.refresh(stuck)
    # Reclaimed to NULL (then enqueued in the same sweep's NULL scan).
    assert stuck.magnet_resolve_status is None
    assert stuck.magnet_resolve_attempts == 0
    assert stuck.magnet_resolve_error == (
        "previous attempt interrupted (worker restarted)"
    )
    assert result["enqueued"] == 1
    fake_queue.enqueue.assert_awaited_once_with(
        "resolve_magnet_torrent", f"magnet:{stuck.id}", {"resource_id": stuck.id}
    )


async def test_sweep_keeps_fresh_running_row(
    db_session, sample_channel, monkeypatch
):
    from app.job_handlers import _handle_magnet_resolve_sweep

    monkeypatch.setattr(mr, "lt", _fake_lt())
    fake_queue = SimpleNamespace(enqueue=AsyncMock(return_value={"job_id": "j"}))
    monkeypatch.setattr("app.services.task_queue.task_queue", fake_queue)

    fresh = await _make_magnet_resource(
        db_session, sample_channel.id,
        magnet_resolve_status="running",
        magnet_resolve_attempts=1,
        magnet_resolve_updated_at=utcnow(),
    )
    result = await _handle_magnet_resolve_sweep({})
    assert result["reclaimed"] == 0
    await db_session.refresh(fresh)
    # A legitimately in-flight attempt is never reclaimed or re-enqueued.
    assert fresh.magnet_resolve_status == "running"
    assert fresh.magnet_resolve_attempts == 1
    assert result["enqueued"] == 0
    fake_queue.enqueue.assert_not_awaited()


# =============================================================================
# Tracker validation / merge / injection
# =============================================================================

def test_validate_tracker_urls_accepts_valid():
    out = mr.validate_tracker_urls([
        "  udp://tracker.opentrackr.org:1337/announce ",
        "http://tracker.example/announce",
        "https://tracker.example:443/announce",
    ])
    assert out == [
        "udp://tracker.opentrackr.org:1337/announce",
        "http://tracker.example/announce",
        "https://tracker.example:443/announce",
    ]


def test_validate_tracker_urls_rejects_bad_scheme():
    with pytest.raises(ValueError, match=r"ftp://bad"):
        mr.validate_tracker_urls(["ftp://bad/announce"])


def test_validate_tracker_urls_rejects_missing_host():
    with pytest.raises(ValueError, match="no host"):
        mr.validate_tracker_urls(["udp:///announce"])


def test_validate_tracker_urls_rejects_whitespace_and_control_chars():
    with pytest.raises(ValueError, match="whitespace"):
        mr.validate_tracker_urls(["udp://a b:1/announce"])
    with pytest.raises(ValueError, match="whitespace"):
        mr.validate_tracker_urls(["udp://a:1/announce\x00"])


def test_validate_tracker_urls_rejects_empty():
    with pytest.raises(ValueError, match="empty"):
        mr.validate_tracker_urls(["   "])


def test_validate_tracker_urls_rejects_too_many_and_too_long():
    with pytest.raises(ValueError, match="too many"):
        mr.validate_tracker_urls([f"udp://t{i}:1/announce" for i in range(21)])
    with pytest.raises(ValueError, match="too long"):
        mr.validate_tracker_urls(["udp://t:1/" + "a" * 200])


def test_merge_trackers_order_and_dedup():
    merged = mr.merge_trackers(
        ["udp://a:1/announce", "udp://b:1/announce"],
        ["udp://b:1/announce", "udp://c:1/announce"],
        ["udp://c:1/announce", "udp://d:1/announce"],
    )
    assert merged == [
        "udp://a:1/announce",
        "udp://b:1/announce",
        "udp://c:1/announce",
        "udp://d:1/announce",
    ]


async def test_resolve_injects_merged_trackers(tmp_path, monkeypatch):
    fake = _fake_lt()
    monkeypatch.setattr(mr, "lt", fake)
    monkeypatch.setattr(
        mr.settings, "magnet_resolve_default_trackers", ["udp://d1:1/announce"]
    )
    dest = tmp_path / "r.torrent"
    await mr.resolve_magnet_to_cache(
        "magnet:?xt=urn:btih:abc", str(dest), 60,
        extra_trackers=["udp://c1:1/announce", "udp://d1:1/announce"],
    )
    params = fake.added_params[-1]
    # Order: magnet's own → custom → defaults, exact-string dedup.
    assert params.trackers == [
        "udp://tracker.example:1337",
        "udp://c1:1/announce",
        "udp://d1:1/announce",
    ]
    assert params.tracker_tiers == [0, 1, 2]


async def test_attempt_loop_passes_stored_trackers_and_clears_on_done(
    db_session, sample_channel, monkeypatch, tmp_path
):
    fake = _fake_lt()
    monkeypatch.setattr(mr, "lt", fake)
    monkeypatch.setattr(mr.settings, "torrent_cache_dir", str(tmp_path))
    monkeypatch.setattr(
        mr.settings, "magnet_resolve_default_trackers", ["udp://d1:1/announce"]
    )
    monkeypatch.setattr(
        "app.services.torrent_inspect.maybe_inspect_torrent", AsyncMock(return_value=False)
    )

    r = await _make_magnet_resource(
        db_session, sample_channel.id,
        magnet_resolve_trackers=["udp://custom:1/announce"],
    )
    assert await mr.launch_resolution(r.id) is True
    await _drain_background()
    params = fake.added_params[-1]
    assert params.trackers == [
        "udp://tracker.example:1337",
        "udp://custom:1/announce",
        "udp://d1:1/announce",
    ]
    await db_session.refresh(r)
    assert r.magnet_resolve_status == "done"
    # Custom trackers are cleared on done.
    assert r.magnet_resolve_trackers is None


# =============================================================================
# Infohash-cache mirror fast path
# =============================================================================

def _mirror_payload(name: bytes = b"Movie.2024.1080p.mkv") -> tuple[bytes, str]:
    """Fabricated single-file torrent bytes + its true v1 infohash.

    Doubles as the canonical-bencode claim check: the magnet built from
    ``sha1(bencodepy.encode(info))`` is exactly what the fast path verifies
    against.
    """
    info = {
        b"name": name,
        b"length": 100,
        b"piece length": 16384,
        b"pieces": b"x" * 20,
    }
    raw = bencodepy.encode({b"info": info})
    return raw, hashlib.sha1(bencodepy.encode(info)).hexdigest()


def test_bencodepy_infohash_matches_libtorrent():
    """sha1(bencodepy.encode(info)) equals libtorrent's own v1 infohash."""
    lt_real = pytest.importorskip("libtorrent")
    raw, digest = _mirror_payload()
    assert str(lt_real.torrent_info(raw).info_hashes().v1) == digest


async def test_mirror_hit_short_circuits_p2p(tmp_path, monkeypatch):
    raw, digest = _mirror_payload()
    fake = _fake_lt()
    monkeypatch.setattr(mr, "lt", fake)
    fetch = Mock(return_value=raw)
    monkeypatch.setattr(mr, "_fetch_mirror_torrent", fetch)

    dest = tmp_path / "r.torrent"
    await mr.resolve_magnet_to_cache(f"magnet:?xt=urn:btih:{digest}", str(dest), 60)

    assert dest.read_bytes() == raw
    # The mirror URL carried the lowercase infohash.
    assert digest in fetch.call_args.args[0]
    # P2P never engaged: no session created, no torrent added.
    assert mr._session is None
    assert fake.added_params == []


async def test_mirror_wrong_torrent_falls_back_to_p2p(tmp_path, monkeypatch):
    """A valid but unrelated torrent (itorrents' unknown-hash behavior) is
    discarded on infohash mismatch and the P2P path completes instead."""
    _raw, digest = _mirror_payload()
    wrong_raw, _wrong_digest = _mirror_payload(name=b"Someone.Else.2001.mkv")
    fake = _fake_lt()
    monkeypatch.setattr(mr, "lt", fake)
    monkeypatch.setattr(mr, "_fetch_mirror_torrent", Mock(return_value=wrong_raw))

    dest = tmp_path / "r.torrent"
    await mr.resolve_magnet_to_cache(f"magnet:?xt=urn:btih:{digest}", str(dest), 60)

    assert fake.added_params, "P2P fallback must engage on infohash mismatch"
    assert parse_torrent_files(str(dest)) == [{"name": "a.mkv", "size": 100}]


async def test_mirror_fetch_failure_falls_back_to_p2p(tmp_path, monkeypatch):
    _raw, digest = _mirror_payload()
    fake = _fake_lt()
    monkeypatch.setattr(mr, "lt", fake)
    # 404 / network error → None; and an unexpected exception → both degrade.
    monkeypatch.setattr(mr, "_fetch_mirror_torrent", Mock(return_value=None))

    dest = tmp_path / "r.torrent"
    await mr.resolve_magnet_to_cache(f"magnet:?xt=urn:btih:{digest}", str(dest), 60)
    assert fake.added_params
    assert parse_torrent_files(str(dest)) is not None


async def test_mirror_unexpected_error_degrades_to_p2p(tmp_path, monkeypatch):
    """The fast path must never raise — a crashing fetch helper degrades."""
    _raw, digest = _mirror_payload()
    fake = _fake_lt()
    monkeypatch.setattr(mr, "lt", fake)
    monkeypatch.setattr(
        mr, "_fetch_mirror_torrent", Mock(side_effect=RuntimeError("boom"))
    )

    dest = tmp_path / "r.torrent"
    await mr.resolve_magnet_to_cache(f"magnet:?xt=urn:btih:{digest}", str(dest), 60)
    assert fake.added_params
    assert parse_torrent_files(str(dest)) is not None


async def test_mirror_skipped_without_v1_hash(tmp_path, monkeypatch):
    fake = _fake_lt()
    monkeypatch.setattr(mr, "lt", fake)
    fetch = Mock(side_effect=AssertionError("mirror fetch must not run"))
    monkeypatch.setattr(mr, "_fetch_mirror_torrent", fetch)

    dest = tmp_path / "r.torrent"
    # base32 btih and a magnet without xt both lack a 40-hex v1 hash.
    for uri in ("magnet:?xt=urn:btih:MFRGGZDFMZTWQ2LK", "magnet:?dn=only"):
        await mr.resolve_magnet_to_cache(uri, str(dest), 60)
    assert fetch.call_count == 0
    assert len(fake.added_params) == 2


async def test_mirror_fast_path_disabled_by_empty_config(tmp_path, monkeypatch):
    _raw, digest = _mirror_payload()
    fake = _fake_lt()
    monkeypatch.setattr(mr, "lt", fake)
    monkeypatch.setattr(mr.settings, "magnet_resolve_cache_mirrors", [])
    fetch = Mock(side_effect=AssertionError("mirror fetch must not run"))
    monkeypatch.setattr(mr, "_fetch_mirror_torrent", fetch)

    dest = tmp_path / "r.torrent"
    await mr.resolve_magnet_to_cache(f"magnet:?xt=urn:btih:{digest}", str(dest), 60)
    assert fetch.call_count == 0
    assert fake.added_params
    assert parse_torrent_files(str(dest)) == [{"name": "a.mkv", "size": 100}]


# =============================================================================
# _extract_v1_infohash — libtorrent info_hashes path vs regex fallback
# =============================================================================

def test_extract_v1_infohash_prefers_info_hashes():
    params = SimpleNamespace(
        info_hashes=SimpleNamespace(
            has_v1=lambda: True,
            v1="0123456789ABCDEF0123456789abcdef01234567",
        )
    )
    assert mr._extract_v1_infohash("magnet:?xt=urn:btih:whatever", params) == (
        "0123456789ABCDEF0123456789abcdef01234567"
    )


def test_extract_v1_infohash_no_v1_returns_none():
    params = SimpleNamespace(info_hashes=SimpleNamespace(has_v1=lambda: False))
    assert mr._extract_v1_infohash("magnet:?xt=urn:btih:MFRGGZDFMZTWQ2LK", params) is None


def test_extract_v1_infohash_exception_falls_back_to_regex():
    def _boom():
        raise RuntimeError("fake binding lacks v1 accessor")

    params = SimpleNamespace(info_hashes=SimpleNamespace(has_v1=_boom))
    assert mr._extract_v1_infohash(
        "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567", params
    ) == "0123456789abcdef0123456789abcdef01234567"
    # No 40-hex token in the URI -> regex gives up too.
    assert mr._extract_v1_infohash("magnet:?xt=urn:btih:abc", params) is None


# =============================================================================
# _fetch_mirror_torrent — raw sync HTTPS GET
# =============================================================================

def _stub_mirror_client(monkeypatch, *, status=200, chunks=None, raise_exc=None):
    class _Resp:
        status_code = status

        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def iter_bytes(self_inner):
            yield from (chunks if chunks is not None else [])

    class _Client:
        def __init__(self_inner, *a, **kw):
            pass

        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def stream(self_inner, method, url):
            if raise_exc is not None:
                raise raise_exc
            return _Resp()

    monkeypatch.setattr(mr.httpx, "Client", _Client)


def test_fetch_mirror_torrent_success(monkeypatch):
    _stub_mirror_client(monkeypatch, chunks=[b"abc", b"def"])
    assert mr._fetch_mirror_torrent("https://mirror/aaa.torrent") == b"abcdef"


def test_fetch_mirror_torrent_non_200_returns_none(monkeypatch):
    _stub_mirror_client(monkeypatch, status=404)
    assert mr._fetch_mirror_torrent("https://mirror/missing.torrent") is None


def test_fetch_mirror_torrent_oversized_returns_none(monkeypatch):
    monkeypatch.setattr(mr, "_MAX_TORRENT_BYTES", 16)
    _stub_mirror_client(monkeypatch, chunks=[b"x" * 10, b"y" * 10])
    assert mr._fetch_mirror_torrent("https://mirror/big.torrent") is None


def test_fetch_mirror_torrent_exception_returns_none(monkeypatch):
    _stub_mirror_client(monkeypatch, raise_exc=RuntimeError("boom"))
    assert mr._fetch_mirror_torrent("https://mirror/broken.torrent") is None


# =============================================================================
# _verify_mirror_payload — structural + infohash recompute
# =============================================================================

def test_verify_mirror_payload_rejects_non_bencode():
    assert mr._verify_mirror_payload(b"not bencode", "a" * 40) is False


def test_verify_mirror_payload_digest_mismatch_is_false():
    raw, _digest = _mirror_payload()
    assert mr._verify_mirror_payload(raw, "f" * 40) is False


def test_verify_mirror_payload_encode_failure_is_false(monkeypatch):
    raw, _digest = _mirror_payload()

    def _boom(*a, **kw):
        raise RuntimeError("encode exploded")

    monkeypatch.setattr(mr.bencodepy, "encode", _boom)
    assert mr._verify_mirror_payload(raw, "a" * 40) is False


async def test_try_cache_mirrors_skips_url_without_placeholder(tmp_path, monkeypatch):
    """A mirror URL template lacking {infohash} is skipped, never fetched."""
    monkeypatch.setattr(mr.settings, "magnet_resolve_cache_mirrors", [
        "https://mirror/no-placeholder.torrent",
    ])
    fetch = Mock(side_effect=AssertionError("must not be fetched"))
    monkeypatch.setattr(mr, "_fetch_mirror_torrent", fetch)

    dest = tmp_path / "r.torrent"
    params = SimpleNamespace()
    uri = f"magnet:?xt=urn:btih:{'a' * 40}"
    assert await mr._try_cache_mirrors(uri, params, str(dest)) is False
    fetch.assert_not_called()
    assert not dest.exists()


# =============================================================================
# _get_session / _poll_metadata error branches
# =============================================================================

def test_get_session_raises_when_libtorrent_missing(monkeypatch):
    monkeypatch.setattr(mr, "lt", None)
    with pytest.raises(mr.MagnetUnavailableError):
        mr._get_session()


async def test_poll_metadata_raises_on_metadata_error():
    class _Status:
        @property
        def has_metadata(self):
            return False

        @property
        def errc(self):
            return SimpleNamespace(value=lambda: 1, message=lambda: "metadata fetch failed")

    class _Handle:
        def status(self):
            return _Status()

    with pytest.raises(mr.MagnetMetadataError, match="metadata fetch failed"):
        await mr._poll_metadata(_Handle())


# =============================================================================
# resolve_magnet_to_cache failure branches
# =============================================================================

async def test_resolve_rebuilt_torrent_unusable_raises(tmp_path, monkeypatch):
    """The rebuilt .torrent failing re-validation is a MagnetMetadataError."""
    monkeypatch.setattr(mr, "lt", _fake_lt())
    monkeypatch.setattr(mr, "parse_torrent_payload", Mock(return_value=None))
    with pytest.raises(mr.MagnetMetadataError, match="rebuilt torrent metadata is unusable"):
        await mr.resolve_magnet_to_cache(
            "magnet:?xt=urn:btih:abc", str(tmp_path / "x.torrent"), 60
        )


async def test_resolve_unexpected_lt_error_wrapped_and_cleanup_silent(tmp_path, monkeypatch):
    """A raw libtorrent crash is wrapped in MagnetMetadataError and a throwing
    remove_torrent during cleanup never escapes."""
    class _Handle:
        def status(self):
            return SimpleNamespace(has_metadata=True, errc=None)

        def torrent_file(self):
            return object()

    class _Session:
        def __init__(self, settings):
            pass

        def add_torrent(self, params):
            return _Handle()

        def remove_torrent(self, handle):
            raise RuntimeError("cleanup boom")

    class _CreateTorrent:
        def __init__(self, ti):
            self.trackers = []

        def add_tracker(self, tracker):
            self.trackers.append(tracker)

        def generate(self):
            raise RuntimeError("rebuild boom")

    def _parse(uri):
        return SimpleNamespace(flags=0, trackers=[], save_path="")

    fake = SimpleNamespace(
        session=_Session,
        parse_magnet_uri=_parse,
        create_torrent=_CreateTorrent,
        bencode=bencodepy.encode,
        torrent_flags=SimpleNamespace(upload_mode=1),
    )
    monkeypatch.setattr(mr, "lt", fake)
    monkeypatch.setattr(mr.settings, "magnet_resolve_cache_mirrors", [])
    with pytest.raises(mr.MagnetMetadataError, match="metadata resolution failed"):
        await mr.resolve_magnet_to_cache(
            "magnet:?xt=urn:btih:abc", str(tmp_path / "x.torrent"), 60
        )


# =============================================================================
# launch_resolution guard branches
# =============================================================================

async def test_launch_disabled_returns_false(db_session, sample_channel, monkeypatch):
    monkeypatch.setattr(mr, "lt", _fake_lt())
    monkeypatch.setattr(mr.settings, "magnet_resolve_enabled", False)
    r = await _make_magnet_resource(db_session, sample_channel.id)
    assert await mr.launch_resolution(r.id) is False
    await db_session.refresh(r)
    assert r.magnet_resolve_status is None


async def test_launch_skips_already_inflight_resource(monkeypatch):
    monkeypatch.setattr(mr, "lt", _fake_lt())
    mr._inflight.add("already-running")
    try:
        assert await mr.launch_resolution("already-running") is False
    finally:
        mr._inflight.discard("already-running")


async def test_run_resolution_swallows_crashed_attempt(monkeypatch):
    async def _boom(resource_id):
        raise RuntimeError("crash")

    monkeypatch.setattr(mr, "_attempt_loop", _boom)
    mr._inflight.add("crashed")
    try:
        await mr._run_resolution("crashed")
    finally:
        # The guard discards the inflight marker even after a crash.
        assert "crashed" not in mr._inflight


# =============================================================================
# _attempt_loop edge branches
# =============================================================================

async def test_attempt_loop_returns_when_row_disappeared(db_session, monkeypatch):
    """set_status(running) failing (row gone) exits the loop immediately."""
    monkeypatch.setattr(mr, "lt", _fake_lt())
    await mr._attempt_loop("nonexistent-id")


async def test_attempt_loop_returns_for_non_magnet(
    db_session, sample_channel, monkeypatch, tmp_path
):
    monkeypatch.setattr(mr, "lt", _fake_lt())
    monkeypatch.setattr(mr.settings, "torrent_cache_dir", str(tmp_path))
    r = await _make_magnet_resource(
        db_session, sample_channel.id, torrent_url="https://x/plain.torrent"
    )
    await mr._attempt_loop(r.id)
    await db_session.refresh(r)
    assert r.magnet_resolve_status == "running"


async def test_attempt_loop_resource_deleted_during_failure_returns(
    db_session, sample_channel, monkeypatch, tmp_path
):
    from sqlalchemy import delete

    from app.database import committed_session
    from app.models.file_resource import FileResource

    monkeypatch.setattr(mr, "lt", _fake_lt())
    monkeypatch.setattr(mr.settings, "torrent_cache_dir", str(tmp_path))
    r = await _make_magnet_resource(db_session, sample_channel.id)

    async def _delete_then_fail(*a, **kw):
        async with committed_session() as db:
            await db.execute(delete(FileResource).where(FileResource.id == r.id))
        raise mr.MagnetResolveError("gone mid-flight")

    monkeypatch.setattr(mr, "resolve_magnet_to_cache", _delete_then_fail)
    await mr._attempt_loop(r.id)


async def test_attempt_loop_unexpected_exception_marks_failed(
    db_session, sample_channel, monkeypatch, tmp_path
):
    monkeypatch.setattr(mr, "lt", _fake_lt())
    monkeypatch.setattr(mr.settings, "torrent_cache_dir", str(tmp_path))

    async def _boom(*a, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(mr, "resolve_magnet_to_cache", _boom)
    r = await _make_magnet_resource(db_session, sample_channel.id)
    await mr._attempt_loop(r.id)
    await db_session.refresh(r)
    assert r.magnet_resolve_status == "failed"
    assert r.magnet_resolve_error == "unexpected error: kaboom"


async def test_attempt_loop_resource_deleted_after_done_returns(
    db_session, sample_channel, monkeypatch, tmp_path
):
    from sqlalchemy import delete

    from app.database import committed_session
    from app.models.file_resource import FileResource

    monkeypatch.setattr(mr, "lt", _fake_lt())
    monkeypatch.setattr(mr.settings, "torrent_cache_dir", str(tmp_path))
    monkeypatch.setattr(
        "app.services.torrent_inspect.maybe_inspect_torrent", AsyncMock(return_value=False)
    )
    r = await _make_magnet_resource(db_session, sample_channel.id)

    real_set_status = mr._set_status

    async def _set_status_and_delete(resource_id, **values):
        if values.get("magnet_resolve_status") == "done":
            async with committed_session() as db:
                await db.execute(delete(FileResource).where(FileResource.id == resource_id))
        return await real_set_status(resource_id, **values)

    monkeypatch.setattr(mr, "_set_status", _set_status_and_delete)
    await mr._attempt_loop(r.id)


async def test_attempt_loop_post_inspect_crash_keeps_done(
    db_session, sample_channel, monkeypatch, tmp_path
):
    monkeypatch.setattr(mr, "lt", _fake_lt())
    monkeypatch.setattr(mr.settings, "torrent_cache_dir", str(tmp_path))

    async def _boom(db, resource, channel=None):
        raise RuntimeError("inspect down")

    monkeypatch.setattr("app.services.torrent_inspect.maybe_inspect_torrent", _boom)
    r = await _make_magnet_resource(db_session, sample_channel.id)
    assert await mr.launch_resolution(r.id) is True
    await _drain_background()
    await db_session.refresh(r)
    # Inspection failure must not flip a successful resolution away from done.
    assert r.magnet_resolve_status == "done"


# =============================================================================
# enqueue_resolution guard branches
# =============================================================================

async def test_enqueue_disabled_returns(db_session, sample_channel, monkeypatch):
    monkeypatch.setattr(mr, "lt", _fake_lt())
    monkeypatch.setattr(mr.settings, "magnet_resolve_enabled", False)
    fake_queue = SimpleNamespace(enqueue=AsyncMock())
    monkeypatch.setattr("app.services.task_queue.task_queue", fake_queue)
    r = await _make_magnet_resource(db_session, sample_channel.id)
    await mr.enqueue_resolution(r.id)
    fake_queue.enqueue.assert_not_awaited()


async def test_enqueue_missing_libtorrent_returns(db_session, sample_channel, monkeypatch):
    monkeypatch.setattr(mr, "lt", None)
    fake_queue = SimpleNamespace(enqueue=AsyncMock())
    monkeypatch.setattr("app.services.task_queue.task_queue", fake_queue)
    r = await _make_magnet_resource(db_session, sample_channel.id)
    await mr.enqueue_resolution(r.id)
    fake_queue.enqueue.assert_not_awaited()


async def test_enqueue_missing_resource_returns(db_session, monkeypatch):
    monkeypatch.setattr(mr, "lt", _fake_lt())
    fake_queue = SimpleNamespace(enqueue=AsyncMock())
    monkeypatch.setattr("app.services.task_queue.task_queue", fake_queue)
    await mr.enqueue_resolution("nonexistent")
    fake_queue.enqueue.assert_not_awaited()
