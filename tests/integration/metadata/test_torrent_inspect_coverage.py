"""In-process integration coverage for app.services.torrent_inspect.

Targets the branches the unit suite does not exercise under the integration
coverage run: fetch_torrent_file failure/abort paths, ensure_torrent_cached
poison-cache handling, parse_torrent_payload validation errors,
read_torrent_root_name fallbacks, analyze_torrent_files scope verdicts, and
maybe_inspect_torrent write-back branches (verified-season backfill,
enrichment pass, gated LLM refinement, franchise linking) — the latter with
real DB rows where relationships are touched.

All network access is stubbed (httpx.Client / fetch_torrent_file); the LLM
refinement layer and franchise linker are monkeypatched.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import bencodepy

import app.services.batch_content_analysis as bca
import app.services.franchise_service as fs_mod
import app.services.torrent_inspect as ti
from app.services.torrent_inspect import (
    analyze_torrent_files,
    ensure_torrent_cached,
    fetch_torrent_file,
    maybe_inspect_torrent,
    parse_torrent_files,
    parse_torrent_payload,
    read_torrent_root_name,
)

MB = 1024 * 1024


def _torrent_bytes(payload: dict) -> bytes:
    return bencodepy.encode(payload)


def _multi_torrent(entries: list[tuple[list, int]], root: bytes = b"root") -> bytes:
    """Build a multi-file torrent; path components may be bytes or int."""
    return _torrent_bytes({
        b"info": {
            b"name": root,
            b"files": [
                {
                    b"length": length,
                    b"path": [p if isinstance(p, bytes) else p for p in parts],
                }
                for parts, length in entries
            ],
            b"piece length": 16384,
            b"pieces": b"x" * 20,
        }
    })


def _single_torrent(name: str, length: int) -> bytes:
    return _torrent_bytes({
        b"info": {
            b"name": name.encode(),
            b"length": length,
            b"piece length": 16384,
            b"pieces": b"x" * 20,
        }
    })


def _f(name: str, size: int = 500 * MB) -> dict:
    return {"name": name, "size": size}


def _resource(**over):
    base = dict(
        id="rid-cov",
        is_batch=False,
        torrent_url="https://x/pack.torrent",
        torrent_file=None,
        batch_scope=None,
        batch_seasons=None,
        season=None,
        episode=None,
        episode_start=None,
        episode_end=None,
        series_id=None,
        movie_id=None,
        collection_id=None,
        episode_confidence=None,
        season_ranges=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


class _FakeDb:
    """Minimal db double for paths that only touch get/refresh."""

    def __init__(self, series=None, *, get_raises=False, refresh_raises=False):
        self._series = series
        self._get_raises = get_raises
        self._refresh_raises = refresh_raises

    async def get(self, model, pk):
        if self._get_raises:
            raise RuntimeError("db down")
        return self._series

    async def refresh(self, obj, attribute_names=None):
        if self._refresh_raises:
            raise RuntimeError("refresh failed")


# =============================================================================
# fetch_torrent_file — guard clauses and failure branches
# =============================================================================

def _stub_httpx_counting(monkeypatch, behaviors):
    """Stub httpx.Client; ``behaviors`` = per-attempt ("raise", exc) or
    ("respond", status, chunks). Returns the attempt counter list."""
    calls = [0]

    class _Resp:
        def __init__(self_inner, status, chunks):
            self_inner.status_code = status
            self_inner._chunks = chunks

        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def iter_bytes(self_inner):
            yield from self_inner._chunks

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def stream(self_inner, method, url):
            idx = min(calls[0], len(behaviors) - 1)
            calls[0] += 1
            action = behaviors[idx]
            if action[0] == "raise":
                raise action[1]
            _, status, chunks = action
            return _Resp(status, chunks)

    async def _fake_to_thread(fn, *a, **kw):
        return fn()

    async def _no_sleep(*a, **kw):
        return None

    monkeypatch.setattr(ti.httpx, "Client", _Client)
    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    return calls


async def test_fetch_rejects_non_http_urls(tmp_path, monkeypatch):
    monkeypatch.setattr(ti.settings, "torrent_cache_dir", str(tmp_path))
    assert await fetch_torrent_file("", "rid-x") is None
    assert await fetch_torrent_file("magnet:?xt=urn:btih:abc", "rid-x") is None
    assert await fetch_torrent_file("ftp://x/a.torrent", "rid-x") is None


async def test_fetch_requires_cache_dir(monkeypatch):
    monkeypatch.setattr(ti.settings, "torrent_cache_dir", "")
    assert await fetch_torrent_file("https://x/a.torrent", "rid-x") is None


async def test_fetch_cache_dir_not_writable(tmp_path, monkeypatch):
    monkeypatch.setattr(ti.settings, "torrent_cache_dir", str(tmp_path / "sub"))

    def _boom(self, *a, **kw):
        raise OSError("read-only fs")

    monkeypatch.setattr(Path, "mkdir", _boom)
    assert await fetch_torrent_file("https://x/a.torrent", "rid-x") is None


async def test_fetch_success_persists_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(ti.settings, "torrent_cache_dir", str(tmp_path))
    payload = _single_torrent("Show.S01E01.mkv", 500 * MB)
    _stub_httpx_counting(monkeypatch, [("respond", 200, [payload])])
    out = await fetch_torrent_file("https://x/abc.torrent", "rid-ok")
    assert out == str(tmp_path / "rid-ok.torrent")
    assert parse_torrent_files(out) == [{"name": "Show.S01E01.mkv", "size": 500 * MB}]


async def test_fetch_oversize_aborts_without_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(ti.settings, "torrent_cache_dir", str(tmp_path))
    monkeypatch.setattr(ti, "_MAX_TORRENT_BYTES", 16)
    calls = _stub_httpx_counting(monkeypatch, [
        ("respond", 200, [b"x" * 10, b"y" * 10]),
        ("respond", 200, [b"x" * 10, b"y" * 10]),
    ])
    assert await fetch_torrent_file("https://x/big.torrent", "rid-big") is None
    assert calls[0] == 1  # the abort sentinel short-circuits the retry loop
    assert list(tmp_path.iterdir()) == []


async def test_fetch_download_error_uses_retry_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(ti.settings, "torrent_cache_dir", str(tmp_path))
    calls = _stub_httpx_counting(monkeypatch, [
        ("raise", ti.httpx.ConnectError("connection refused")),
        ("raise", ti.httpx.ReadTimeout("timed out")),
    ])
    assert await fetch_torrent_file("https://x/down.torrent", "rid-err") is None
    assert calls[0] == 2


async def test_fetch_write_failure_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ti.settings, "torrent_cache_dir", str(tmp_path))
    payload = _single_torrent("Show.S01E01.mkv", 500 * MB)
    _stub_httpx_counting(monkeypatch, [("respond", 200, [payload])])

    def _boom(self, *a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "write_bytes", _boom)
    assert await fetch_torrent_file("https://x/a.torrent", "rid-w") is None


# =============================================================================
# ensure_torrent_cached — poison cache removal and silent failures
# =============================================================================

async def test_ensure_removes_poison_cache_and_refetches(tmp_path, monkeypatch):
    poison = tmp_path / "rid-p.torrent"
    poison.write_bytes(b"<html>404 not found</html>")

    async def _fake_fetch(url, rid):
        return str(tmp_path / "fresh.torrent")

    monkeypatch.setattr(ti, "fetch_torrent_file", _fake_fetch)
    r = _resource(torrent_file=str(poison))
    out = await ensure_torrent_cached(r)
    assert out == str(tmp_path / "fresh.torrent")
    assert r.torrent_file == str(tmp_path / "fresh.torrent")
    assert not poison.exists()  # poison entry was unlinked


async def test_ensure_poison_cache_unlink_failure_tolerated(tmp_path, monkeypatch):
    poison = tmp_path / "rid-p2.torrent"
    poison.write_bytes(b"<html>error page</html>")

    def _boom(self, *a, **kw):
        raise OSError("locked")

    monkeypatch.setattr(Path, "unlink", _boom)

    async def _fake_fetch(url, rid):
        return str(tmp_path / "fresh2.torrent")

    monkeypatch.setattr(ti, "fetch_torrent_file", _fake_fetch)
    r = _resource(torrent_file=str(poison))
    assert await ensure_torrent_cached(r) == str(tmp_path / "fresh2.torrent")
    assert r.torrent_file == str(tmp_path / "fresh2.torrent")


async def test_ensure_fetch_exception_is_silent(monkeypatch):
    async def _boom(url, rid):
        raise RuntimeError("unexpected fetch crash")

    monkeypatch.setattr(ti, "fetch_torrent_file", _boom)
    r = _resource()
    assert await ensure_torrent_cached(r) is None
    assert r.torrent_file is None


# =============================================================================
# parse_torrent_payload / parse_torrent_files — validation errors
# =============================================================================

def test_payload_undecodable_bytes_returns_none():
    assert parse_torrent_payload(b"this is not bencode at all") is None


def test_payload_non_dict_returns_none():
    assert parse_torrent_payload(bencodepy.encode([1, 2, 3])) is None


def test_payload_info_not_dict_returns_none():
    assert parse_torrent_payload(_torrent_bytes({b"info": b"opaque"})) is None


def test_payload_file_entry_not_dict_returns_none():
    raw = _torrent_bytes({
        b"info": {b"name": b"root", b"files": [5], b"piece length": 16384, b"pieces": b"x" * 20}
    })
    assert parse_torrent_payload(raw) is None


def test_payload_file_entry_invalid_length_or_path_returns_none():
    raw = _torrent_bytes({
        b"info": {
            b"name": b"root",
            b"files": [
                {b"length": b"not-an-int", b"path": [b"a.mkv"]},
            ],
            b"piece length": 16384,
            b"pieces": b"x" * 20,
        }
    })
    assert parse_torrent_payload(raw) is None

    raw_empty_path = _torrent_bytes({
        b"info": {
            b"name": b"root",
            b"files": [{b"length": 10, b"path": []}],
            b"piece length": 16384,
            b"pieces": b"x" * 20,
        }
    })
    assert parse_torrent_payload(raw_empty_path) is None


def test_payload_non_bytes_path_component_decoded_via_str():
    raw = _multi_torrent([([b"dir", 5], 500 * MB)])
    files = parse_torrent_payload(raw)
    assert files == [{"name": "dir/5", "size": 500 * MB}]


def test_payload_single_file_missing_name_or_length_returns_none():
    no_length = _torrent_bytes({
        b"info": {b"name": b"movie.mkv", b"piece length": 16384, b"pieces": b"x" * 20}
    })
    assert parse_torrent_payload(no_length) is None


def test_parse_files_unreadable_path_returns_none(tmp_path):
    assert parse_torrent_files(str(tmp_path / "missing.torrent")) is None


# =============================================================================
# read_torrent_root_name
# =============================================================================

def test_root_name_success(tmp_path):
    p = tmp_path / "multi.torrent"
    p.write_bytes(_multi_torrent([([b"ep01.mkv"], 500 * MB)], root="作品A 第一季".encode()))
    assert read_torrent_root_name(str(p)) == "作品A 第一季"


def test_root_name_read_failure_returns_none(tmp_path):
    assert read_torrent_root_name(str(tmp_path / "nope.torrent")) is None
    bad = tmp_path / "bad.torrent"
    bad.write_bytes(b"garbage")
    assert read_torrent_root_name(str(bad)) is None


def test_root_name_non_dict_payload_returns_none(tmp_path):
    p = tmp_path / "list.torrent"
    p.write_bytes(bencodepy.encode([1, 2]))
    assert read_torrent_root_name(str(p)) is None


def test_root_name_single_file_torrent_returns_none(tmp_path):
    p = tmp_path / "single.torrent"
    p.write_bytes(_single_torrent("Movie.2024.mkv", 2 * 1024 * MB))
    # info/name IS the file for single-file torrents — no root directory.
    assert read_torrent_root_name(str(p)) is None


# =============================================================================
# analyze_torrent_files — scope verdicts and filters
# =============================================================================

def test_analyze_empty_listing_is_unknown():
    report = analyze_torrent_files([])
    assert report.scope == "unknown"
    assert report.is_batch is False
    assert report.video_file_count == 0


def test_analyze_empty_filename_not_a_video():
    report = analyze_torrent_files([{"name": "", "size": 500 * MB}])
    assert report.scope == "unknown"
    assert report.video_file_count == 0


def test_analyze_extras_dir_excluded_from_main_videos():
    files = [_f(f"Show S01/Show S01E{ep:02d}.mkv") for ep in range(1, 4)]
    files.append(_f("Show S01/SP/Show SP01.mkv", 600 * MB))  # extras dir, full size
    report = analyze_torrent_files(files)
    assert report.scope == "season"
    assert report.video_file_count == 3  # the SP-dir file never counts


def test_analyze_franchise_two_work_clusters():
    files = [_f(f"作品X TV/作品X S01E{ep:02d}.mkv") for ep in range(1, 4)]
    files.append(_f("作品X 剧场版/作品X Movie.mkv", 2 * 1024 * MB))
    report = analyze_torrent_files(files)
    assert report.scope == "franchise"
    assert report.is_batch is True
    assert report.work_titles == ["作品X TV", "作品X 剧场版"]
    assert report.seasons == [1]
    # Cluster membership is exposed for the edit wizard / LLM refinement.
    by_title = {c.title: c.files for c in report.clusters}
    assert by_title["作品X TV"] == [f"作品X TV/作品X S01E{ep:02d}.mkv" for ep in range(1, 4)]
    assert by_title["作品X 剧场版"] == ["作品X 剧场版/作品X Movie.mkv"]


def test_analyze_tech_only_dirs_never_form_franchise():
    # "1080p" / "720p" normalize to nothing credible -> no clusters; with the
    # same episode number in both encodes there is no episode run either.
    report = analyze_torrent_files([_f("1080p/ep01.mkv"), _f("720p/ep01.mkv")])
    assert report.scope == "unknown"
    assert report.is_batch is False
    assert report.clusters == []


def test_analyze_multi_season_span():
    files = [_f(f"Show/S01/Show S01E{ep:02d}.mkv") for ep in range(1, 4)]
    files += [_f(f"Show/S02/Show S02E{ep:02d}.mkv") for ep in range(1, 4)]
    report = analyze_torrent_files(files)
    assert report.scope == "multi_season"
    assert report.is_batch is True
    assert report.episode_start is None and report.episode_end is None
    assert report.seasons == [1, 2]
    assert report.season_ranges == [
        {"season": 1, "episode_start": 1, "episode_end": 3},
        {"season": 2, "episode_start": 1, "episode_end": 3},
    ]


def test_analyze_sp_only_pack_is_single_season():
    files = [_f(f"Show/Show S00E{ep:02d}.mkv") for ep in range(1, 4)]
    report = analyze_torrent_files(files)
    assert report.scope == "season"
    assert report.is_batch is True
    assert report.episode_start == 1 and report.episode_end == 3
    assert report.seasons == [0]


def test_analyze_duplicate_episode_numbers_stays_unknown():
    # Same episode in two encodes: no >= 2 distinct episode numbers.
    report = analyze_torrent_files([
        _f("Show S01/Show S01E01.1080p.mkv"),
        _f("Show S01/Show S01E01.720p.mkv"),
    ])
    assert report.scope == "unknown"
    assert report.is_batch is False
    assert report.video_file_count == 2


# =============================================================================
# maybe_inspect_torrent — preconditions on complete batch verdicts
# =============================================================================

async def test_inspect_complete_batch_refresh_failure_returns_false():
    db = _FakeDb(refresh_raises=True)
    r = _resource(is_batch=True, batch_scope="multi_season", file_assignments=[])
    assert await maybe_inspect_torrent(db, r) is False


async def test_inspect_complete_batch_with_assignments_skipped(monkeypatch):
    async def _boom(url, rid):  # pragma: no cover - must not be called
        raise AssertionError("fetch_torrent_file called for a complete batch")

    monkeypatch.setattr(ti, "fetch_torrent_file", _boom)
    db = _FakeDb()
    r = _resource(
        is_batch=True,
        batch_scope="season",
        episode_start=1,
        episode_end=12,
        file_assignments=[SimpleNamespace(id="a-1")],
    )
    assert await maybe_inspect_torrent(db, r) is False
    assert r.batch_scope == "season"


# =============================================================================
# maybe_inspect_torrent — poison cache defense
# =============================================================================

async def test_inspect_removes_poison_cache_then_skips_magnet(tmp_path):
    poison = tmp_path / "rid-cov.torrent"
    poison.write_bytes(b"<html>error</html>")
    r = _resource(torrent_file=str(poison), torrent_url="magnet:?xt=urn:btih:abc")
    assert await maybe_inspect_torrent(None, r) is False
    assert r.torrent_file is None
    assert not poison.exists()


async def test_inspect_poison_cache_unlink_failure_tolerated(tmp_path, monkeypatch):
    poison = tmp_path / "rid-cov2.torrent"
    poison.write_bytes(b"<html>error</html>")

    def _boom(self, *a, **kw):
        raise OSError("locked")

    monkeypatch.setattr(Path, "unlink", _boom)
    r = _resource(torrent_file=str(poison), torrent_url="magnet:?xt=urn:btih:abc")
    assert await maybe_inspect_torrent(None, r) is False
    assert r.torrent_file is None


# =============================================================================
# maybe_inspect_torrent — analysis write-back branches
# =============================================================================

def _stub_pipeline(monkeypatch, files, path="/tmp/rid-cov.torrent"):
    async def _fake_fetch(url, rid):
        return path

    monkeypatch.setattr(ti, "fetch_torrent_file", _fake_fetch)
    monkeypatch.setattr(ti, "parse_torrent_files", lambda p: files)


async def test_inspect_season_pack_writeback(monkeypatch):
    _stub_pipeline(monkeypatch, [
        _f("Show.S01E01.1080p.mkv"),
        _f("Show.S01E02.1080p.mkv"),
        _f("Show.S01E03.1080p.mkv"),
    ])
    r = _resource(episode=5, season=1)
    assert await maybe_inspect_torrent(None, r) is True
    assert r.is_batch is True
    assert r.batch_scope == "season"
    assert r.episode is None
    assert (r.episode_start, r.episode_end) == (1, 3)
    assert r.torrent_file == "/tmp/rid-cov.torrent"


async def test_inspect_parse_failure_keeps_cache_path(monkeypatch):
    _stub_pipeline(monkeypatch, None)
    r = _resource()
    assert await maybe_inspect_torrent(None, r) is False
    assert r.is_batch is False
    assert r.torrent_file == "/tmp/rid-cov.torrent"


async def test_inspect_multi_season_clears_episode_and_persists_coverage(monkeypatch):
    _stub_pipeline(monkeypatch, [
        _f("Show.S01E01.1080p.mkv"),
        _f("Show.S01E02.1080p.mkv"),
        _f("Show.S02E01.1080p.mkv"),
        _f("Show.S02E02.1080p.mkv"),
    ])
    r = _resource(season=1, episode=5, episode_start=1, episode_end=2)
    assert await maybe_inspect_torrent(None, r) is True
    assert r.batch_scope == "multi_season"
    assert r.season is None and r.episode is None
    assert r.episode_start is None and r.episode_end is None
    assert r.batch_seasons == [1, 2]


async def test_inspect_season_backfills_verified_season_from_linked_work(
    monkeypatch, db_session
):
    """无季标记 season 包：链接作品 season_number=2 → 资源季号补 2。"""
    from app.models.series import TVSeries

    series = TVSeries(id=str(uuid.uuid4()), title_cn="第二季作品", season_number=2)
    db_session.add(series)
    await db_session.commit()

    _stub_pipeline(monkeypatch, [
        _f("Show - 01 [1080p].mkv"),
        _f("Show - 02 [1080p].mkv"),
    ])
    r = _resource(series_id=series.id, season=None, episode=None)
    assert await maybe_inspect_torrent(db_session, r) is True
    assert r.batch_scope == "season"
    assert r.season == 2


async def test_inspect_season_unknown_series_keeps_season_none(monkeypatch, db_session):
    """series_id 指向不存在的作品：verified=None，季号保持 None。"""
    _stub_pipeline(monkeypatch, [
        _f("Show - 01 [1080p].mkv"),
        _f("Show - 02 [1080p].mkv"),
    ])
    r = _resource(series_id=str(uuid.uuid4()), season=None, episode=None)
    assert await maybe_inspect_torrent(db_session, r) is True
    assert r.batch_scope == "season"
    assert r.season is None


async def test_inspect_season_db_failure_does_not_block_verdict(monkeypatch):
    _stub_pipeline(monkeypatch, [
        _f("Show - 01 [1080p].mkv"),
        _f("Show - 02 [1080p].mkv"),
    ])
    db = _FakeDb(get_raises=True)
    r = _resource(series_id="s-1", season=None, episode=None)
    assert await maybe_inspect_torrent(db, r) is True
    assert r.batch_scope == "season"
    assert r.season is None


async def test_inspect_franchise_writeback_and_linking(monkeypatch):
    linked = []

    async def _fake_link(db, resource, report, channel):
        linked.append((resource.id, sorted(report.work_titles), channel))

    monkeypatch.setattr(fs_mod, "link_franchise_pack", _fake_link)
    _stub_pipeline(monkeypatch, [
        _f("作品X TV/作品X S01E01.mkv"),
        _f("作品X TV/作品X S01E02.mkv"),
        _f("作品X 剧场版/作品X Movie.mkv"),
    ])
    channel = SimpleNamespace(id="ch-1", metadata_source="wikipedia")
    r = _resource(season=1, episode=3)
    assert await maybe_inspect_torrent(None, r, channel) is True
    assert r.batch_scope == "franchise"
    assert r.episode is None
    assert r.batch_seasons == [1]
    assert linked == [("rid-cov", ["作品X TV", "作品X 剧场版"], channel)]


async def test_inspect_franchise_link_failure_keeps_verdict(monkeypatch):
    async def _boom(db, resource, report, channel):
        raise RuntimeError("llm down")

    monkeypatch.setattr(fs_mod, "link_franchise_pack", _boom)
    _stub_pipeline(monkeypatch, [
        _f("作品X TV/作品X S01E01.mkv"),
        _f("作品X TV/作品X S01E02.mkv"),
        _f("作品X 剧场版/作品X Movie.mkv"),
    ])
    r = _resource()
    assert await maybe_inspect_torrent(None, r) is True
    assert r.is_batch is True
    assert r.batch_scope == "franchise"


async def test_inspect_single_backfills_episode_and_confidence(monkeypatch):
    _stub_pipeline(monkeypatch, [_f("Show S01/Show [01a].mkv")])
    r = _resource(series_id="series-1", season=None, episode=None, episode_confidence="ambiguous")
    assert await maybe_inspect_torrent(None, r) is False
    assert r.season == 1
    assert r.episode == 1
    assert r.episode_confidence == "raw"


# =============================================================================
# maybe_inspect_torrent — enrichment pass (real DB rows)
# =============================================================================

async def _make_resource(db_session, **over):
    from app.models.channel import Channel
    from app.models.file_resource import FileResource

    channel = Channel(
        id=str(uuid.uuid4()),
        name="Coverage Channel",
        type="rss_feed",
        url="https://example.com/rss",
        fetch_interval=1800,
        status="active",
        field_mapping={"list_locator": {"source": "entries"}},
        metadata_agent_enabled=False,
    )
    resource = FileResource(
        id=str(uuid.uuid4()),
        channel_id=channel.id,
        guid=str(uuid.uuid4()),
        title_raw="[Group] Show - 01 [1080p]",
        torrent_url="https://x/pack.torrent",
        **over,
    )
    db_session.add_all([channel, resource])
    await db_session.commit()
    await db_session.refresh(resource, ["file_assignments"])
    return resource


async def test_enrichment_writes_auto_assignments_and_ranges(monkeypatch, db_session):
    monkeypatch.setattr(bca, "llm_refinement_needed", lambda report, scope: False)
    _stub_pipeline(monkeypatch, [
        _f("Show.S01E01.1080p.mkv"),
        _f("Show.S01E02.1080p.mkv"),
    ])
    resource = await _make_resource(db_session, season=None, episode=None)
    assert await maybe_inspect_torrent(db_session, resource) is True
    assert resource.batch_scope == "season"
    paths = sorted(a.file_path for a in resource.file_assignments)
    assert paths == ["Show.S01E01.1080p.mkv", "Show.S01E02.1080p.mkv"]
    assert all(a.source == "auto" for a in resource.file_assignments)
    assert resource.season_ranges == [{"season": 1, "episode_start": 1, "episode_end": 2}]


async def test_enrichment_tolerates_assignment_refresh_failure(monkeypatch, db_session):
    monkeypatch.setattr(bca, "llm_refinement_needed", lambda report, scope: False)
    _stub_pipeline(monkeypatch, [
        _f("Show.S01E01.1080p.mkv"),
        _f("Show.S01E02.1080p.mkv"),
    ])
    resource = await _make_resource(
        db_session, is_batch=True, batch_scope=None, season=None, episode=None
    )

    async def _refresh_boom(obj, attribute_names=None):
        raise RuntimeError("refresh hiccup")

    monkeypatch.setattr(db_session, "refresh", _refresh_boom)
    # The refresh failure is swallowed; assignments still land on the
    # (already loaded) relationship collection.
    assert await maybe_inspect_torrent(db_session, resource) is True
    assert resource.batch_scope == "season"
    assert len(resource.file_assignments) == 2


async def test_llm_refinement_success_recomputes_season_ranges(monkeypatch, db_session):
    calls = []

    monkeypatch.setattr(bca, "llm_refinement_needed", lambda report, scope: True)

    async def _fake_refine(db, resource, report, channel):
        calls.append((resource.id, sorted(report.work_titles), channel))
        return True

    monkeypatch.setattr(bca, "refine_batch_content", _fake_refine)
    _stub_pipeline(monkeypatch, [
        _f("Show.S01E01.1080p.mkv"),
        _f("Show.S01E02.1080p.mkv"),
    ])
    channel = SimpleNamespace(id="ch-1", metadata_source="wikipedia")
    resource = await _make_resource(db_session, season=1, episode=None)
    assert await maybe_inspect_torrent(db_session, resource, channel) is True
    assert calls == [(resource.id, [], channel)]
    # llm_bound_movies=True triggers the second compute_season_ranges pass.
    assert resource.season_ranges == [{"season": 1, "episode_start": 1, "episode_end": 2}]


async def test_llm_refinement_failure_degrades_silently(monkeypatch, db_session):
    monkeypatch.setattr(bca, "llm_refinement_needed", lambda report, scope: True)

    async def _boom(db, resource, report, channel):
        raise RuntimeError("llm timeout")

    monkeypatch.setattr(bca, "refine_batch_content", _boom)
    _stub_pipeline(monkeypatch, [
        _f("Show.S01E01.1080p.mkv"),
        _f("Show.S01E02.1080p.mkv"),
    ])
    resource = await _make_resource(db_session, season=1, episode=None)
    assert await maybe_inspect_torrent(db_session, resource) is True
    assert resource.batch_scope == "season"
    assert resource.season_ranges == [{"season": 1, "episode_start": 1, "episode_end": 2}]
