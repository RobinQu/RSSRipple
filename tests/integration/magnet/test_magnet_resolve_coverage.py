"""Branch coverage for app.services.magnet_resolve not reached by the E2E suite.

Covers: tracker validation errors, the infohash mirror fast path's failure
branches (non-200 / oversized / network error / wrong payload / no v1 hash),
libtorrent-unavailable degradation, poll/add/rebuild error mapping, the
attempt loop's retry/exhaustion/unexpected-error paths, launch_resolution
claim dedup, and enqueue_resolution's skip gates. All P2P work runs through a
fake libtorrent; all mirror HTTP through a fake httpx.Client.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import bencodepy
import pytest

import app.services.magnet_resolve as mr
from app.models.channel import Channel
from app.models.file_resource import FileResource

_FAKE_TORRENT = {
    b"info": {
        b"name": b"Some.Movie.2024.1080p.mkv",
        b"length": 1_000_000,
        b"piece length": 262144,
        b"pieces": b"x" * 20,
    }
}


@pytest.fixture(autouse=True)
def _reset_magnet_state():
    mr._session = None
    mr._semaphore = None
    mr._inflight.clear()
    mr._background_tasks.clear()
    mr._unavailable_logged = False
    yield


def _fake_lt(*, add_raises: bool = False, remove_raises: bool = False,
             bad_rebuild: bool = False, never_metadata: bool = False):
    """Fake libtorrent covering the API surface magnet_resolve uses."""
    added: list = []

    class _Params:
        def __init__(self):
            self.flags = 0
            self.trackers = []
            self.save_path = ""

    class _Handle:
        def status(self):
            if never_metadata:
                return SimpleNamespace(has_metadata=False, errc=None)
            return SimpleNamespace(has_metadata=True, errc=None)

        def torrent_file(self):
            return object()

    class _Session:
        def __init__(self, settings):
            self.settings = settings

        def add_torrent(self, params):
            if add_raises:
                raise RuntimeError("add_torrent boom")
            added.append(params)
            return _Handle()

        def remove_torrent(self, handle):
            if remove_raises:
                raise RuntimeError("remove boom")

    class _CreateTorrent:
        def __init__(self, ti):
            pass

        def add_tracker(self, tracker):
            pass

        def generate(self):
            return _FAKE_TORRENT

    def _bencode(payload):
        if bad_rebuild:
            return b"not-a-torrent"
        return bencodepy.encode(payload)

    return SimpleNamespace(
        session=_Session,
        parse_magnet_uri=lambda uri: _Params(),
        create_torrent=_CreateTorrent,
        bencode=_bencode,
        torrent_flags=SimpleNamespace(upload_mode=1),
        added_params=added,
    )


async def _make_magnet_resource(db_session, **overrides) -> FileResource:
    ch = Channel(
        id=str(uuid.uuid4()), name="ch", type="rss_feed",
        url="https://example.com/rss", fetch_interval=1800, status="active",
        field_mapping={}, metadata_agent_enabled=False,
    )
    db_session.add(ch)
    await db_session.flush()
    defaults = dict(
        id=str(uuid.uuid4()), channel_id=ch.id, guid=str(uuid.uuid4()),
        title_raw="[G] Some.Movie.2024.1080p",
        torrent_url="magnet:?xt=urn:btih:" + "ab" * 20,
    )
    defaults.update(overrides)
    res = FileResource(**defaults)
    db_session.add(res)
    await db_session.commit()
    return res


# ---------------------------------------------------------------------------
# validate_tracker_urls / merge_trackers
# ---------------------------------------------------------------------------


class TestValidateTrackerUrls:
    def test_too_many(self):
        with pytest.raises(ValueError, match="too many trackers"):
            mr.validate_tracker_urls(
                [f"http://t{i}.example.com/announce" for i in range(mr.TRACKER_MAX_COUNT + 1)]
            )

    def test_empty_entry(self):
        with pytest.raises(ValueError, match="empty"):
            mr.validate_tracker_urls(["   "])

    def test_too_long(self):
        with pytest.raises(ValueError, match="too long"):
            mr.validate_tracker_urls(["http://t.example.com/" + "a" * 200])

    def test_control_char(self):
        with pytest.raises(ValueError, match="whitespace/control"):
            mr.validate_tracker_urls(["http://t.example.com/an\tnounce"])

    def test_bad_scheme(self):
        with pytest.raises(ValueError, match="scheme"):
            mr.validate_tracker_urls(["ftp://t.example.com/announce"])

    def test_no_host(self):
        with pytest.raises(ValueError, match="no host"):
            mr.validate_tracker_urls(["udp:///announce"])

    def test_valid_cleaned_and_stripped(self):
        out = mr.validate_tracker_urls(
            [" udp://tracker.example.com:1337/announce ", "https://t2.example.com/x"]
        )
        assert out == [
            "udp://tracker.example.com:1337/announce",
            "https://t2.example.com/x",
        ]


def test_merge_trackers_dedup_preserves_order():
    merged = mr.merge_trackers(
        ["udp://a/announce", "udp://b/announce"],
        ["udp://b/announce", "udp://c/announce"],
        ["udp://a/announce", "udp://d/announce"],
    )
    assert merged == [
        "udp://a/announce", "udp://b/announce", "udp://c/announce", "udp://d/announce",
    ]


# ---------------------------------------------------------------------------
# _extract_v1_infohash
# ---------------------------------------------------------------------------


class TestExtractV1Infohash:
    def test_prefers_parsed_info_hashes(self):
        params = SimpleNamespace(
            info_hashes=SimpleNamespace(has_v1=lambda: True, v1="AB" * 20)
        )
        # libtorrent's own info_hashes value is returned verbatim (str()).
        assert mr._extract_v1_infohash("magnet:?xt=urn:btih:zz", params) == "AB" * 20

    def test_v2_only_params_yield_none(self):
        params = SimpleNamespace(info_hashes=SimpleNamespace(has_v1=lambda: False))
        uri = "magnet:?xt=urn:btih:" + "cd" * 20
        assert mr._extract_v1_infohash(uri, params) is None

    def test_regex_fallback_when_params_lack_info_hashes(self):
        uri = "magnet:?xt=urn:btih:" + "EF" * 20 + "&dn=x"
        assert mr._extract_v1_infohash(uri, SimpleNamespace()) == "ef" * 20

    def test_regex_fallback_when_info_hashes_raises(self):
        class _Bad:
            def has_v1(self):
                raise RuntimeError("broken binding")

        uri = "magnet:?xt=urn:btih:" + "12" * 20
        assert mr._extract_v1_infohash(uri, SimpleNamespace(info_hashes=_Bad())) == "12" * 20

    def test_base32_magnet_yields_none(self):
        uri = "magnet:?xt=urn:btih:MFRGGZDFMZTWQ2LK"
        assert mr._extract_v1_infohash(uri, SimpleNamespace()) is None


# ---------------------------------------------------------------------------
# Mirror fast path
# ---------------------------------------------------------------------------


class _FakeStreamResp:
    def __init__(self, status_code: int, chunks: list[bytes]):
        self.status_code = status_code
        self._chunks = chunks

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def iter_bytes(self):
        yield from self._chunks


class _FakeHttpxClient:
    """Stands in for httpx.Client (sync) inside _fetch_mirror_torrent."""

    def __init__(self, resp=None, raises: bool = False):
        self._resp = resp
        self._raises = raises

    def __call__(self, *a, **kw):
        return self

    def __enter__(self):
        if self._raises:
            raise RuntimeError("connect error")
        return self

    def __exit__(self, *a):
        return False

    def stream(self, method, url):
        return self._resp


class TestFetchMirrorTorrent:
    def test_success_returns_body(self, monkeypatch):
        client = _FakeHttpxClient(resp=_FakeStreamResp(200, [b"abc", b"def"]))
        monkeypatch.setattr(mr.httpx, "Client", client)
        assert mr._fetch_mirror_torrent("https://mirror.example/x.torrent") == b"abcdef"

    def test_non_200_returns_none(self, monkeypatch):
        client = _FakeHttpxClient(resp=_FakeStreamResp(404, [b""]))
        monkeypatch.setattr(mr.httpx, "Client", client)
        assert mr._fetch_mirror_torrent("https://mirror.example/x.torrent") is None

    def test_oversized_body_returns_none(self, monkeypatch):
        big = b"x" * (mr._MAX_TORRENT_BYTES + 1)
        client = _FakeHttpxClient(resp=_FakeStreamResp(200, [big]))
        monkeypatch.setattr(mr.httpx, "Client", client)
        assert mr._fetch_mirror_torrent("https://mirror.example/x.torrent") is None

    def test_network_error_returns_none(self, monkeypatch):
        monkeypatch.setattr(mr.httpx, "Client", _FakeHttpxClient(raises=True))
        assert mr._fetch_mirror_torrent("https://mirror.example/x.torrent") is None


class TestVerifyMirrorPayload:
    def _raw(self):
        return bencodepy.encode(_FAKE_TORRENT)

    def _digest(self):
        import hashlib

        return hashlib.sha1(bencodepy.encode(_FAKE_TORRENT[b"info"])).hexdigest()

    def test_valid_payload_matching_hash(self):
        assert mr._verify_mirror_payload(self._raw(), self._digest()) is True

    def test_garbage_payload_rejected(self):
        assert mr._verify_mirror_payload(b"junk", self._digest()) is False

    def test_wrong_infohash_rejected(self):
        # A structurally valid torrent for a DIFFERENT hash (mirror answered
        # 200 with an unrelated torrent) must be discarded.
        assert mr._verify_mirror_payload(self._raw(), "00" * 20) is False

    def test_encode_failure_returns_false(self, monkeypatch):
        digest = self._digest()  # computed before hashlib is sabotaged

        def _boom(*a, **kw):
            raise RuntimeError("encode boom")

        monkeypatch.setattr(mr.hashlib, "sha1", _boom)
        assert mr._verify_mirror_payload(self._raw(), digest) is False


class TestTryCacheMirrors:
    async def test_no_mirrors_configured(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mr.settings, "magnet_resolve_cache_mirrors", [])
        dest = tmp_path / "x.torrent"
        assert await mr._try_cache_mirrors("magnet:?xt=urn:btih:" + "ab" * 20, None, str(dest)) is False
        assert not dest.exists()

    async def test_no_v1_hash_skips(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            mr.settings, "magnet_resolve_cache_mirrors", ["https://m.example/{infohash}"]
        )
        dest = tmp_path / "x.torrent"
        ok = await mr._try_cache_mirrors(
            "magnet:?xt=urn:btih:MFRGGZDFMZTWQ2LK", SimpleNamespace(), str(dest)
        )
        assert ok is False

    async def test_mirror_without_placeholder_skipped(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mr.settings, "magnet_resolve_cache_mirrors", ["https://m.example/no-slot"])
        dest = tmp_path / "x.torrent"
        ok = await mr._try_cache_mirrors(
            "magnet:?xt=urn:btih:" + "ab" * 20, SimpleNamespace(), str(dest)
        )
        assert ok is False

    async def test_mirror_miss_falls_through(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            mr.settings, "magnet_resolve_cache_mirrors", ["https://m.example/{infohash}"]
        )
        monkeypatch.setattr(mr, "_fetch_mirror_torrent", lambda url: None)
        dest = tmp_path / "x.torrent"
        ok = await mr._try_cache_mirrors(
            "magnet:?xt=urn:btih:" + "ab" * 20, SimpleNamespace(), str(dest)
        )
        assert ok is False

    async def test_mirror_exception_never_raises(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            mr.settings, "magnet_resolve_cache_mirrors", ["https://m.example/{infohash}"]
        )

        def _boom(url):
            raise RuntimeError("mirror down")

        monkeypatch.setattr(mr, "_fetch_mirror_torrent", _boom)
        dest = tmp_path / "x.torrent"
        ok = await mr._try_cache_mirrors(
            "magnet:?xt=urn:btih:" + "ab" * 20, SimpleNamespace(), str(dest)
        )
        assert ok is False

    async def test_unverified_payload_discarded(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            mr.settings, "magnet_resolve_cache_mirrors", ["https://m.example/{infohash}"]
        )
        monkeypatch.setattr(
            mr, "_fetch_mirror_torrent", lambda url: bencodepy.encode(_FAKE_TORRENT)
        )
        dest = tmp_path / "x.torrent"
        ok = await mr._try_cache_mirrors(
            "magnet:?xt=urn:btih:" + "ab" * 20, SimpleNamespace(), str(dest)
        )
        assert ok is False
        assert not dest.exists()


# ---------------------------------------------------------------------------
# libtorrent session / resolve_magnet_to_cache
# ---------------------------------------------------------------------------


class TestUnavailableLibtorrent:
    def test_log_unavailable_once(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING, logger="app.services.magnet_resolve"):
            mr._log_unavailable_once()
            mr._log_unavailable_once()
        assert caplog.text.count("libtorrent is not installed") == 1
        assert mr._unavailable_logged is True

    async def test_get_session_raises_without_libtorrent(self, monkeypatch):
        monkeypatch.setattr(mr, "lt", None)
        with pytest.raises(mr.MagnetUnavailableError):
            mr._get_session()

    async def test_resolve_raises_without_libtorrent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mr, "lt", None)
        with pytest.raises(mr.MagnetUnavailableError):
            await mr.resolve_magnet_to_cache("magnet:?xt=urn:btih:x", str(tmp_path / "x"), 5)

    async def test_launch_resolution_false_without_libtorrent(self, monkeypatch):
        monkeypatch.setattr(mr, "lt", None)
        assert await mr.launch_resolution("rid") is False

    async def test_enqueue_resolution_noop_without_libtorrent(self, monkeypatch):
        monkeypatch.setattr(mr, "lt", None)
        await mr.enqueue_resolution("rid")  # must not raise


class TestPollMetadata:
    async def test_error_code_raises_metadata_error(self):
        errc = SimpleNamespace(value=lambda: 1, message=lambda: "connection timed out")
        handle = SimpleNamespace(
            status=lambda: SimpleNamespace(has_metadata=False, errc=errc)
        )
        with pytest.raises(mr.MagnetMetadataError, match="connection timed out"):
            await mr._poll_metadata(handle)

    async def test_polls_until_metadata_arrives(self):
        statuses = [
            SimpleNamespace(has_metadata=False, errc=None),
            SimpleNamespace(has_metadata=True, errc=None),
        ]
        handle = SimpleNamespace(status=lambda: statuses.pop(0))
        await mr._poll_metadata(handle)  # returns once the second status arrives
        assert statuses == []

    async def test_errc_without_value_method_is_ignored(self):
        # libtorrent <2 exposes errc as a plain int-like without .value();
        # that shape must not abort the poll.
        statuses = [
            SimpleNamespace(has_metadata=False, errc=0),
            SimpleNamespace(has_metadata=True, errc=None),
        ]
        handle = SimpleNamespace(status=lambda: statuses.pop(0))
        await mr._poll_metadata(handle)
        assert statuses == []


class TestResolveMagnetToCache:
    async def test_invalid_magnet_raises(self, monkeypatch, tmp_path):
        fake = _fake_lt()
        fake.parse_magnet_uri = lambda uri: (_ for _ in ()).throw(ValueError("parse error"))
        monkeypatch.setattr(mr, "lt", fake)
        monkeypatch.setattr(mr.settings, "magnet_resolve_cache_mirrors", [])
        with pytest.raises(mr.InvalidMagnetError, match="invalid magnet link"):
            await mr.resolve_magnet_to_cache("magnet:garbage", str(tmp_path / "x.torrent"), 5)

    async def test_success_writes_parseable_torrent(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mr, "lt", _fake_lt())
        monkeypatch.setattr(mr.settings, "magnet_resolve_cache_mirrors", [])
        dest = tmp_path / "ok.torrent"
        await mr.resolve_magnet_to_cache("magnet:?xt=urn:btih:x", str(dest), 5)
        assert dest.exists()
        from app.services.torrent_inspect import parse_torrent_files

        files = parse_torrent_files(str(dest))
        assert files and files[0]["name"].endswith(".mkv")

    async def test_unparseable_rebuild_raises_metadata_error(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mr, "lt", _fake_lt(bad_rebuild=True))
        monkeypatch.setattr(mr.settings, "magnet_resolve_cache_mirrors", [])
        with pytest.raises(mr.MagnetMetadataError, match="unusable"):
            await mr.resolve_magnet_to_cache("magnet:?xt=urn:btih:x", str(tmp_path / "x"), 5)

    async def test_add_torrent_failure_wrapped(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mr, "lt", _fake_lt(add_raises=True))
        monkeypatch.setattr(mr.settings, "magnet_resolve_cache_mirrors", [])
        with pytest.raises(mr.MagnetMetadataError, match="add_torrent boom"):
            await mr.resolve_magnet_to_cache("magnet:?xt=urn:btih:x", str(tmp_path / "x"), 5)

    async def test_remove_torrent_failure_is_swallowed(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mr, "lt", _fake_lt(remove_raises=True))
        monkeypatch.setattr(mr.settings, "magnet_resolve_cache_mirrors", [])
        dest = tmp_path / "ok.torrent"
        await mr.resolve_magnet_to_cache("magnet:?xt=urn:btih:x", str(dest), 5)
        assert dest.exists()

    async def test_timeout_raises_magnet_timeout(self, monkeypatch, tmp_path):
        monkeypatch.setattr(mr, "lt", _fake_lt(never_metadata=True))
        monkeypatch.setattr(mr.settings, "magnet_resolve_cache_mirrors", [])
        with pytest.raises(mr.MagnetTimeoutError, match="no metadata received"):
            await mr.resolve_magnet_to_cache("magnet:?xt=urn:btih:x", str(tmp_path / "x"), 1)

    async def test_extra_trackers_merged(self, monkeypatch, tmp_path):
        fake = _fake_lt()
        monkeypatch.setattr(mr, "lt", fake)
        monkeypatch.setattr(mr.settings, "magnet_resolve_cache_mirrors", [])
        monkeypatch.setattr(mr.settings, "magnet_resolve_default_trackers", ["udp://d/announce"])
        dest = tmp_path / "ok.torrent"
        await mr.resolve_magnet_to_cache(
            "magnet:?xt=urn:btih:x", str(dest), 5, extra_trackers=["udp://custom/announce"]
        )
        params = fake.added_params[0]
        assert params.trackers[:1] == ["udp://custom/announce"]
        assert "udp://d/announce" in params.trackers


# ---------------------------------------------------------------------------
# launch_resolution / _run_resolution
# ---------------------------------------------------------------------------


class TestLaunchResolution:
    async def test_disabled_feature_returns_false(self, db_session, monkeypatch):
        monkeypatch.setattr(mr, "lt", _fake_lt())
        monkeypatch.setattr(mr.settings, "magnet_resolve_enabled", False)
        assert await mr.launch_resolution("rid") is False

    async def test_inflight_dedup(self, db_session, monkeypatch):
        monkeypatch.setattr(mr, "lt", _fake_lt())
        monkeypatch.setattr(mr.settings, "magnet_resolve_enabled", True)
        mr._inflight.add("rid")
        assert await mr.launch_resolution("rid") is False

    async def test_pending_row_not_reclaimed(self, db_session, monkeypatch):
        monkeypatch.setattr(mr, "lt", _fake_lt())
        monkeypatch.setattr(mr.settings, "magnet_resolve_enabled", True)
        res = await _make_magnet_resource(db_session, magnet_resolve_status="pending")
        assert await mr.launch_resolution(res.id) is False

    async def test_claim_spawns_worker_and_resolves(self, db_session, monkeypatch, tmp_path):
        monkeypatch.setattr(mr, "lt", _fake_lt())
        monkeypatch.setattr(mr.settings, "magnet_resolve_enabled", True)
        monkeypatch.setattr(mr.settings, "magnet_resolve_cache_mirrors", [])
        monkeypatch.setattr(mr.settings, "torrent_cache_dir", str(tmp_path))
        monkeypatch.setattr(
            "app.services.torrent_inspect.maybe_inspect_torrent",
            lambda *a, **kw: asyncio.sleep(0),
        )
        res = await _make_magnet_resource(db_session)
        assert await mr.launch_resolution(res.id) is True
        assert mr._background_tasks
        await asyncio.gather(*mr._background_tasks)
        await db_session.refresh(res)
        assert res.magnet_resolve_status == "done"
        assert res.torrent_file is not None
        assert res.id not in mr._inflight

    async def test_run_resolution_crash_cleans_inflight(self, db_session, monkeypatch, caplog):
        import logging

        async def _boom(resource_id):
            raise RuntimeError("worker exploded")

        monkeypatch.setattr(mr, "_attempt_loop", _boom)
        mr._inflight.add("rid")
        with caplog.at_level(logging.WARNING, logger="app.services.magnet_resolve"):
            await mr._run_resolution("rid")
        assert "rid" not in mr._inflight
        assert "worker exploded" in caplog.text


# ---------------------------------------------------------------------------
# _attempt_loop failure paths
# ---------------------------------------------------------------------------


class TestAttemptLoop:
    async def test_non_magnet_resource_returns_immediately(self, db_session, monkeypatch):
        res = await _make_magnet_resource(db_session, torrent_url="https://x.example/a.torrent")
        called = False

        async def _resolve(*a, **kw):
            nonlocal called
            called = True

        monkeypatch.setattr(mr, "resolve_magnet_to_cache", _resolve)
        await mr._attempt_loop(res.id)
        await db_session.refresh(res)
        assert res.magnet_resolve_status == "running"
        assert called is False

    async def test_retry_then_exhausted_marks_failed(self, db_session, monkeypatch, tmp_path):
        monkeypatch.setattr(mr.settings, "magnet_resolve_max_attempts", 1)
        monkeypatch.setattr(mr.settings, "torrent_cache_dir", str(tmp_path))

        async def _fail(*a, **kw):
            raise mr.MagnetTimeoutError("no metadata received within 900s")

        monkeypatch.setattr(mr, "resolve_magnet_to_cache", _fail)
        res = await _make_magnet_resource(db_session)
        await mr._attempt_loop(res.id)
        await db_session.refresh(res)
        assert res.magnet_resolve_status == "failed"
        assert res.magnet_resolve_attempts == 2
        assert "no metadata received" in res.magnet_resolve_error

    async def test_first_failure_goes_back_to_pending(self, db_session, monkeypatch, tmp_path):
        monkeypatch.setattr(mr.settings, "magnet_resolve_max_attempts", 3)
        monkeypatch.setattr(mr.settings, "torrent_cache_dir", str(tmp_path))
        calls = 0

        async def _fail_once(*a, **kw):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise mr.MagnetTimeoutError("first attempt timed out")
            Path(a[1]).write_bytes(bencodepy.encode(_FAKE_TORRENT))

        monkeypatch.setattr(mr, "resolve_magnet_to_cache", _fail_once)
        monkeypatch.setattr(
            "app.services.torrent_inspect.maybe_inspect_torrent",
            lambda *a, **kw: asyncio.sleep(0),
        )
        res = await _make_magnet_resource(db_session)
        await mr._attempt_loop(res.id)
        await db_session.refresh(res)
        assert res.magnet_resolve_status == "done"
        assert res.magnet_resolve_attempts == 1
        assert res.torrent_file is not None

    async def test_unexpected_error_marks_failed(self, db_session, monkeypatch, tmp_path):
        monkeypatch.setattr(mr.settings, "torrent_cache_dir", str(tmp_path))

        async def _crash(*a, **kw):
            raise RuntimeError("segfault-ish")

        monkeypatch.setattr(mr, "resolve_magnet_to_cache", _crash)
        res = await _make_magnet_resource(db_session)
        await mr._attempt_loop(res.id)
        await db_session.refresh(res)
        assert res.magnet_resolve_status == "failed"
        assert "unexpected error: segfault-ish" in res.magnet_resolve_error

    async def test_post_resolve_inspection_failure_keeps_done(
        self, db_session, monkeypatch, tmp_path
    ):
        monkeypatch.setattr(mr.settings, "torrent_cache_dir", str(tmp_path))

        async def _ok(magnet_uri, dest, timeout, **kw):
            Path(dest).write_bytes(bencodepy.encode(_FAKE_TORRENT))

        async def _inspect_boom(*a, **kw):
            raise RuntimeError("inspection exploded")

        monkeypatch.setattr(mr, "resolve_magnet_to_cache", _ok)
        monkeypatch.setattr(
            "app.services.torrent_inspect.maybe_inspect_torrent", _inspect_boom
        )
        res = await _make_magnet_resource(db_session)
        await mr._attempt_loop(res.id)
        await db_session.refresh(res)
        assert res.magnet_resolve_status == "done"
        assert res.torrent_file is not None


# ---------------------------------------------------------------------------
# enqueue_resolution skip gates
# ---------------------------------------------------------------------------


class _StubQueue:
    def __init__(self):
        self.calls: list = []

    async def enqueue(self, job_type, key, payload):
        self.calls.append({"job_type": job_type, "key": key, "payload": payload})
        return {"key": key}


class TestEnqueueResolution:
    async def test_disabled_feature_skips(self, db_session, monkeypatch):
        monkeypatch.setattr(mr, "lt", _fake_lt())
        monkeypatch.setattr(mr.settings, "magnet_resolve_enabled", False)
        await mr.enqueue_resolution("rid")  # returns before touching the DB

    async def test_missing_resource_skips(self, db_session, monkeypatch):
        monkeypatch.setattr(mr, "lt", _fake_lt())
        monkeypatch.setattr(mr.settings, "magnet_resolve_enabled", True)
        stub = _StubQueue()
        monkeypatch.setattr("app.services.task_queue.task_queue", stub)
        await mr.enqueue_resolution(str(uuid.uuid4()))
        assert stub.calls == []

    async def test_non_magnet_resource_skips(self, db_session, monkeypatch):
        monkeypatch.setattr(mr, "lt", _fake_lt())
        monkeypatch.setattr(mr.settings, "magnet_resolve_enabled", True)
        stub = _StubQueue()
        monkeypatch.setattr("app.services.task_queue.task_queue", stub)
        res = await _make_magnet_resource(db_session, torrent_url="https://x.example/a.torrent")
        await mr.enqueue_resolution(res.id)
        assert stub.calls == []

    async def test_usable_cache_skips(self, db_session, monkeypatch, tmp_path):
        monkeypatch.setattr(mr, "lt", _fake_lt())
        monkeypatch.setattr(mr.settings, "magnet_resolve_enabled", True)
        cached = tmp_path / "cached.torrent"
        cached.write_bytes(bencodepy.encode(_FAKE_TORRENT))
        stub = _StubQueue()
        monkeypatch.setattr("app.services.task_queue.task_queue", stub)
        res = await _make_magnet_resource(db_session, torrent_file=str(cached))
        await mr.enqueue_resolution(res.id)
        assert stub.calls == []

    async def test_enqueue_magnet_without_cache(self, db_session, monkeypatch):
        monkeypatch.setattr(mr, "lt", _fake_lt())
        monkeypatch.setattr(mr.settings, "magnet_resolve_enabled", True)
        stub = _StubQueue()
        monkeypatch.setattr("app.services.task_queue.task_queue", stub)
        res = await _make_magnet_resource(db_session)
        await mr.enqueue_resolution(res.id)
        assert stub.calls == [{
            "job_type": "resolve_magnet_torrent",
            "key": f"magnet:{res.id}",
            "payload": {"resource_id": res.id},
        }]


class TestHasUsableCache:
    def test_missing_or_garbage_cache(self, tmp_path):
        res = SimpleNamespace(torrent_file=None)
        assert mr._has_usable_cache(res) is False
        res = SimpleNamespace(torrent_file=str(tmp_path / "nope.torrent"))
        assert mr._has_usable_cache(res) is False
        bad = tmp_path / "bad.torrent"
        bad.write_bytes(b"junk")
        assert mr._has_usable_cache(SimpleNamespace(torrent_file=str(bad))) is False

    def test_parseable_cache(self, tmp_path):
        good = tmp_path / "good.torrent"
        good.write_bytes(bencodepy.encode(_FAKE_TORRENT))
        assert mr._has_usable_cache(SimpleNamespace(torrent_file=str(good))) is True


# ---------------------------------------------------------------------------
# Row-deleted-mid-flight races (lines 501 / 523 / 565)
# ---------------------------------------------------------------------------


def _deleting_committed_session(rid: str, *, delete_on_call: int):
    """Wrap app.database.committed_session so the Nth transaction deletes the
    resource row before the handler code reads it — simulating a user deleting
    the resource while the detached resolution worker is in flight."""
    import app.database as db_mod

    real = db_mod.committed_session
    calls = {"n": 0}

    class _Ctx:
        def __init__(self, inner):
            self._inner = inner

        async def __aenter__(self):
            sess = await self._inner.__aenter__()
            obj = await sess.get(FileResource, rid)
            if obj is not None:
                await sess.delete(obj)
                await sess.flush()
            return sess

        async def __aexit__(self, *a):
            return await self._inner.__aexit__(*a)

    def _wrapper():
        calls["n"] += 1
        inner = real()
        if calls["n"] == delete_on_call:
            return _Ctx(inner)
        return inner

    return _wrapper


class TestRowDeletedMidFlight:
    async def test_status_write_on_deleted_row_returns(self, db_session, monkeypatch):
        # The row is gone before the first status write: _set_status reports
        # rowcount == 0 and the loop exits silently (line 501).
        await mr._attempt_loop(str(uuid.uuid4()))

    async def test_deleted_during_failure_handling(self, db_session, monkeypatch, tmp_path):
        # Call order in _attempt_loop: _set_status(running) [1], resource load
        # [2], then the failure branch's session [3] finds the row deleted.
        monkeypatch.setattr(mr.settings, "torrent_cache_dir", str(tmp_path))

        async def _fail(*a, **kw):
            raise mr.MagnetTimeoutError("no metadata")

        monkeypatch.setattr(mr, "resolve_magnet_to_cache", _fail)
        res = await _make_magnet_resource(db_session)
        monkeypatch.setattr(
            "app.database.committed_session",
            _deleting_committed_session(res.id, delete_on_call=3),
        )
        await mr._attempt_loop(res.id)  # returns without writing failure state
        db_session.expunge_all()  # drop identity-map copies; re-read the DB
        assert await db_session.get(FileResource, res.id) is None

    async def test_deleted_before_post_resolve_inspection(
        self, db_session, monkeypatch, tmp_path
    ):
        # Success path: done status committed at [3], then the inspection
        # session [4] finds the row deleted and returns (line 565).
        monkeypatch.setattr(mr.settings, "torrent_cache_dir", str(tmp_path))

        async def _ok(magnet_uri, dest, timeout, **kw):
            Path(dest).write_bytes(bencodepy.encode(_FAKE_TORRENT))

        monkeypatch.setattr(mr, "resolve_magnet_to_cache", _ok)
        res = await _make_magnet_resource(db_session)
        monkeypatch.setattr(
            "app.database.committed_session",
            _deleting_committed_session(res.id, delete_on_call=4),
        )
        await mr._attempt_loop(res.id)
        db_session.expunge_all()  # drop identity-map copies; re-read the DB
        assert await db_session.get(FileResource, res.id) is None
