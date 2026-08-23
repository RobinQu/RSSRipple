"""Unit tests for app.services.torrent_inspect.

Covers parse_torrent_files (multi-file / single-file / corrupt bytes),
analyze_torrent_files scope classification (single / season / multi_season /
franchise / unknown, sample filtering), extract_season_episode_from_path,
and fetch_torrent_file (200 / 404 / oversize / non-http) with httpx stubbed,
plus maybe_inspect_torrent (channel A write-back, preconditions, failures).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import bencodepy
import pytest

import app.services.torrent_inspect as ti
from app.services.resource_parser import extract_season_episode_from_path
from app.services.torrent_inspect import (
    analyze_torrent_files,
    ensure_torrent_cached,
    fetch_torrent_file,
    maybe_inspect_torrent,
    parse_torrent_files,
)

MB = 1024 * 1024


def _torrent_bytes(payload: dict) -> bytes:
    return bencodepy.encode(payload)


def _multi_torrent(entries: list[tuple[list[str], int]]) -> bytes:
    """Build a multi-file torrent from (path components, length) pairs."""
    return _torrent_bytes({
        b"info": {
            b"name": b"root",
            b"files": [
                {b"length": length, b"path": [p.encode() for p in parts]}
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


# =============================================================================
# parse_torrent_files
# =============================================================================

def test_parse_multi_file_torrent(tmp_path):
    p = tmp_path / "multi.torrent"
    p.write_bytes(_multi_torrent([
        (["Show S01", "Show S01E01.mkv"], 500 * MB),
        (["Show S01", "Show S01E02.mkv"], 600 * MB),
        (["特典", "bonus.mkv"], 10 * MB),
    ]))
    files = parse_torrent_files(str(p))
    assert files == [
        {"name": "Show S01/Show S01E01.mkv", "size": 500 * MB},
        {"name": "Show S01/Show S01E02.mkv", "size": 600 * MB},
        {"name": "特典/bonus.mkv", "size": 10 * MB},
    ]


def test_parse_single_file_torrent(tmp_path):
    p = tmp_path / "single.torrent"
    p.write_bytes(_single_torrent("Movie.2024.1080p.mkv", 2 * 1024 * MB))
    files = parse_torrent_files(str(p))
    assert files == [{"name": "Movie.2024.1080p.mkv", "size": 2 * 1024 * MB}]


def test_parse_path_utf8_preferred(tmp_path):
    payload = {
        b"info": {
            b"name": b"root",
            b"files": [{
                b"length": 500 * MB,
                b"path": [b"garbled\xffdir", b"ep01.mkv"],
                b"path.utf-8": ["作品A".encode(), b"ep01.mkv"],
            }],
            b"piece length": 16384,
            b"pieces": b"x" * 20,
        }
    }
    p = tmp_path / "utf8.torrent"
    p.write_bytes(_torrent_bytes(payload))
    files = parse_torrent_files(str(p))
    assert files == [{"name": "作品A/ep01.mkv", "size": 500 * MB}]


def test_parse_corrupt_bytes_returns_none(tmp_path):
    p = tmp_path / "bad.torrent"
    p.write_bytes(b"this is not bencode at all")
    assert parse_torrent_files(str(p)) is None


def test_parse_missing_file_returns_none(tmp_path):
    assert parse_torrent_files(str(tmp_path / "nope.torrent")) is None


def test_parse_no_info_dict_returns_none(tmp_path):
    p = tmp_path / "noinfo.torrent"
    p.write_bytes(_torrent_bytes({b"announce": b"http://tracker/announce"}))
    assert parse_torrent_files(str(p)) is None


# =============================================================================
# extract_season_episode_from_path
# =============================================================================

@pytest.mark.parametrize("path,expected", [
    ("Show S01/Show S01E01.mkv", (1, 1)),
    ("Show/S02/Show - 13.mkv", (2, 13)),
    ("Show/Season 3/Show - 01 [1080p].mkv", (3, 1)),
    ("[Group] 作品A 01-12/[Group] 作品A - 05.mkv", (None, 5)),
    ("作品X/第2季/作品X 第03話.mkv", (2, 3)),
    ("Movie.2024.1080p.WEB-DL.mkv", (None, None)),
    ("Show S01E12v2.mkv", (1, 12)),
    # Batch range in the directory must NOT be read as episode 12.
    ("Show S01 01-12/Show S01E07.mkv", (1, 7)),
])
def test_extract_season_episode_from_path(path, expected):
    assert extract_season_episode_from_path(path) == expected


# =============================================================================
# analyze_torrent_files
# =============================================================================

def test_analyze_single_movie():
    report = analyze_torrent_files([_f("Movie.2024.1080p.WEB-DL.mkv", 3 * 1024 * MB)])
    assert report.scope == "single"
    assert report.is_batch is False
    assert report.video_file_count == 1
    assert report.episode_start is None and report.episode_end is None


def test_analyze_season_sxxexx():
    files = [_f(f"Show S01/Show S01E{ep:02d}.mkv") for ep in range(1, 13)]
    report = analyze_torrent_files(files)
    assert report.scope == "season"
    assert report.is_batch is True
    assert report.episode_start == 1 and report.episode_end == 12
    assert report.seasons == [1]
    assert report.video_file_count == 12
    assert report.unparsed_ratio == 0.0


def test_analyze_multi_season_dirs():
    files = [_f(f"Show/S01/Show S01E{ep:02d}.mkv") for ep in range(1, 13)]
    files += [_f(f"Show/S02/Show S02E{ep:02d}.mkv") for ep in range(1, 13)]
    report = analyze_torrent_files(files)
    assert report.scope == "multi_season"
    assert report.is_batch is True
    assert report.episode_start is None and report.episode_end is None
    assert report.seasons == [1, 2]


def test_analyze_flat_same_season():
    files = [_f(f"[Group] 作品A 01-12/[Group] 作品A - {ep:02d}.mkv") for ep in range(1, 13)]
    report = analyze_torrent_files(files)
    assert report.scope == "season"
    assert report.is_batch is True
    assert report.episode_start == 1 and report.episode_end == 12
    assert report.seasons == []


def test_analyze_franchise_clusters():
    files = [_f(f"作品X TV/作品X - {ep:02d}.mkv") for ep in range(1, 13)]
    files += [_f("作品X 剧场版/作品X 剧场版.mkv", 2 * 1024 * MB)]
    report = analyze_torrent_files(files)
    assert report.scope == "franchise"
    assert report.is_batch is True
    assert report.work_titles == ["作品X TV", "作品X 剧场版"]


def test_analyze_franchise_not_forced_on_uncredible_dirs():
    # Top-level dirs normalize to nothing credible (pure tech tags / numbers)
    # -> must NOT be judged franchise; falls through conservatively.
    files = [_f("1080p/ep01.mkv"), _f("720p/ep01.mkv")]
    report = analyze_torrent_files(files)
    assert report.scope == "unknown"
    assert report.is_batch is False
    assert report.work_titles == []


def test_analyze_all_subtitles_unknown():
    files = [
        _f("Show/ep01.chs.ass", 200 * 1024),
        _f("Show/ep02.chs.ass", 200 * 1024),
        _f("Show/font.zip", 5 * MB),
    ]
    report = analyze_torrent_files(files)
    assert report.scope == "unknown"
    assert report.is_batch is False
    assert report.video_file_count == 0


def test_analyze_empty_listing_unknown():
    report = analyze_torrent_files([])
    assert report.scope == "unknown"
    assert report.is_batch is False
    assert report.video_file_count == 0


def test_analyze_sample_mixed_in_still_season():
    files = [_f(f"Show S01/Show S01E{ep:02d}.mkv") for ep in range(1, 13)]
    files.append(_f("Show S01/Sample/Show S01 sample.mkv", 80 * MB))  # sample dir
    files.append(_f("Show S01/Show S01E01.preview.mkv", 10 * MB))  # too small
    files.append(_f("Show S01/特典/Show SP01.mkv", 300 * MB))  # extras dir
    report = analyze_torrent_files(files)
    assert report.scope == "season"
    assert report.is_batch is True
    assert report.episode_start == 1 and report.episode_end == 12
    assert report.video_file_count == 12


def test_analyze_unparseable_videos_unknown():
    files = [_f(f"Show/track{n}.mkv") for n in range(1, 5)]
    report = analyze_torrent_files(files)
    assert report.scope == "unknown"
    assert report.is_batch is False
    assert report.unparsed_ratio == 1.0


# =============================================================================
# fetch_torrent_file
# =============================================================================

def _stub_httpx(monkeypatch, *, status: int = 200, chunks: list[bytes] | None = None):
    class _Resp:
        status_code = status

        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def iter_bytes(self_inner):
            yield from (chunks if chunks is not None else [b"d8:announce0:e"])

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def stream(self_inner, method, url):
            return _Resp()

    async def _fake_to_thread(fn, *a, **kw):
        return fn()

    monkeypatch.setattr(ti.httpx, "Client", _Client)
    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)


async def test_fetch_success(tmp_path, monkeypatch):
    monkeypatch.setattr(ti.settings, "torrent_cache_dir", str(tmp_path))
    _stub_httpx(monkeypatch, chunks=[b"torrent-bytes"])
    out = await fetch_torrent_file("https://x/abc.torrent", "rid-1")
    assert out == str(tmp_path / "rid-1.torrent")
    assert (tmp_path / "rid-1.torrent").read_bytes() == b"torrent-bytes"


async def test_fetch_404_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ti.settings, "torrent_cache_dir", str(tmp_path))
    _stub_httpx(monkeypatch, status=404)
    assert await fetch_torrent_file("https://x/missing.torrent", "rid-2") is None
    assert list(tmp_path.iterdir()) == []


async def test_fetch_oversize_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ti.settings, "torrent_cache_dir", str(tmp_path))
    monkeypatch.setattr(ti, "_MAX_TORRENT_BYTES", 16)
    _stub_httpx(monkeypatch, chunks=[b"x" * 10, b"y" * 10])
    assert await fetch_torrent_file("https://x/big.torrent", "rid-3") is None
    assert list(tmp_path.iterdir()) == []


async def test_fetch_non_http_scheme_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(ti.settings, "torrent_cache_dir", str(tmp_path))
    assert await fetch_torrent_file("magnet:?xt=urn:btih:abc", "rid-4") is None
    assert await fetch_torrent_file("ftp://x/a.torrent", "rid-4") is None


async def test_fetch_network_error_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(ti.settings, "torrent_cache_dir", str(tmp_path))

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self_inner):
            return self_inner

        def __exit__(self_inner, *a):
            return False

        def stream(self_inner, method, url):
            raise ti.httpx.ConnectError("connection refused")

    async def _fake_to_thread(fn, *a, **kw):
        return fn()

    monkeypatch.setattr(ti.httpx, "Client", _Client)
    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
    assert await fetch_torrent_file("https://x/a.torrent", "rid-5") is None


# =============================================================================
# maybe_inspect_torrent (channel A)
# =============================================================================

def _resource(**over):
    base = dict(
        id="rid-a",
        is_batch=False,
        torrent_url="https://x/pack.torrent",
        torrent_file=None,
        batch_scope=None,
        season=1,
        episode=5,
        episode_start=None,
        episode_end=None,
        series_id=None,
        movie_id=None,
        collection_id=None,
    )
    base.update(over)
    return SimpleNamespace(**base)


def _stub_pipeline(monkeypatch, files, path="/tmp/rid-a.torrent"):
    async def _fake_fetch(url, rid):
        return path

    monkeypatch.setattr(ti, "fetch_torrent_file", _fake_fetch)
    monkeypatch.setattr(ti, "parse_torrent_files", lambda p: files)


async def test_inspect_season_pack_full_flow(monkeypatch):
    _stub_pipeline(monkeypatch, [
        _f("Show.S01E01.1080p.mkv"),
        _f("Show.S01E02.1080p.mkv"),
        _f("Show.S01E03.1080p.mkv"),
    ])
    r = _resource()
    assert await maybe_inspect_torrent(None, r) is True
    assert r.is_batch is True
    assert r.batch_scope == "season"
    assert r.episode is None
    assert r.episode_start == 1
    assert r.episode_end == 3
    assert r.torrent_file == "/tmp/rid-a.torrent"


async def test_inspect_multi_season_clears_episode_fields(monkeypatch):
    _stub_pipeline(monkeypatch, [
        _f("Show.S01E01.1080p.mkv"),
        _f("Show.S02E01.1080p.mkv"),
    ])
    r = _resource()
    assert await maybe_inspect_torrent(None, r) is True
    assert r.is_batch is True
    assert r.batch_scope == "multi_season"
    assert r.season is None
    assert r.episode is None
    assert r.episode_start is None
    assert r.episode_end is None


async def test_inspect_franchise_keeps_work_fks(monkeypatch):
    import app.services.franchise_service as fs_mod

    linked = []

    async def _fake_link(db, resource, report, channel):
        linked.append((resource, sorted(report.work_titles), channel))

    monkeypatch.setattr(fs_mod, "link_franchise_pack", _fake_link)
    _stub_pipeline(monkeypatch, [
        _f("作品X TV/作品X S01E01.mkv"),
        _f("作品X TV/作品X S01E02.mkv"),
        _f("作品X 剧场版/作品X Movie.mkv"),
    ])
    r = _resource(series_id="s-1", collection_id=None)
    channel = SimpleNamespace(id="ch-1", metadata_source="wikipedia")
    assert await maybe_inspect_torrent(None, r, channel) is True
    assert r.is_batch is True
    assert r.batch_scope == "franchise"
    assert r.episode is None
    # The pack verdict does not itself touch work FKs / collection_id —
    # link_franchise_pack (mocked away here) owns those writes.
    assert r.series_id == "s-1"
    assert r.collection_id is None
    # Wiring: the linker ran with the report and the channel.
    assert linked == [(r, ["作品X TV", "作品X 剧场版"], channel)]


async def test_inspect_franchise_link_failure_keeps_verdict(monkeypatch):
    import app.services.franchise_service as fs_mod

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


async def test_inspect_single_keeps_verdict_and_cache(monkeypatch):
    _stub_pipeline(monkeypatch, [_f("Show.S01E05.1080p.mkv")])
    r = _resource()
    assert await maybe_inspect_torrent(None, r) is False
    assert r.is_batch is False
    assert r.batch_scope is None
    assert r.episode == 5
    assert r.torrent_file == "/tmp/rid-a.torrent"


async def test_inspect_magnet_skipped(monkeypatch):
    async def _boom(url, rid):  # pragma: no cover - must not be called
        raise AssertionError("fetch_torrent_file called for magnet")

    monkeypatch.setattr(ti, "fetch_torrent_file", _boom)
    r = _resource(torrent_url="magnet:?xt=urn:btih:abc")
    assert await maybe_inspect_torrent(None, r) is False
    assert r.torrent_file is None
    assert r.is_batch is False


async def test_inspect_already_batch_skipped(monkeypatch):
    async def _boom(url, rid):  # pragma: no cover - must not be called
        raise AssertionError("fetch_torrent_file called for batch resource")

    monkeypatch.setattr(ti, "fetch_torrent_file", _boom)
    # 信息完整（scope 已细分、season 包集数范围齐全）的合集不重跑。
    r = _resource(is_batch=True, batch_scope="season", episode_start=1, episode_end=3)
    assert await maybe_inspect_torrent(None, r) is False
    assert r.batch_scope == "season"


async def test_inspect_enriches_unscoped_batch(monkeypatch):
    """标题正则判出的合集（batch_scope NULL）用 torrent 清单补齐 scope
    与集数范围。"""
    _stub_pipeline(monkeypatch, [
        _f("[ANi] 勇者之渣 - 01 [1080P][Baha].mkv"),
        _f("[ANi] 勇者之渣 - 02 [1080P][Baha].mkv"),
        _f("[ANi] 勇者之渣 - 03 [1080P][Baha].mkv"),
    ])
    r = _resource(is_batch=True, batch_scope=None, episode=None, season=None)
    assert await maybe_inspect_torrent(None, r) is True
    assert r.is_batch is True
    assert r.batch_scope == "season"
    assert r.episode_start == 1
    assert r.episode_end == 3


async def test_inspect_enriches_season_batch_missing_range(monkeypatch):
    """scope=season 但缺集数范围的合集同样补齐范围。"""
    _stub_pipeline(monkeypatch, [
        _f("Show.S01E01.1080p.mkv"),
        _f("Show.S01E02.1080p.mkv"),
    ])
    r = _resource(is_batch=True, batch_scope="season", episode=None)
    assert await maybe_inspect_torrent(None, r) is True
    assert r.episode_start == 1
    assert r.episode_end == 2


async def test_inspect_batch_single_listing_keeps_verdict(monkeypatch):
    """已判合集但清单只见到单文件（single/unknown）→ 不降级既有判定。"""
    _stub_pipeline(monkeypatch, [_f("Show.S01E05.1080p.mkv")])
    r = _resource(is_batch=True, batch_scope=None, episode=None)
    assert await maybe_inspect_torrent(None, r) is False
    assert r.is_batch is True
    assert r.batch_scope is None
    assert r.torrent_file == "/tmp/rid-a.torrent"


async def test_inspect_download_failure_silent(monkeypatch):
    async def _none(url, rid):
        return None

    monkeypatch.setattr(ti, "fetch_torrent_file", _none)
    r = _resource()
    assert await maybe_inspect_torrent(None, r) is False
    assert r.is_batch is False
    assert r.torrent_file is None


async def test_inspect_parse_failure_silent(monkeypatch):
    _stub_pipeline(monkeypatch, None)
    r = _resource()
    assert await maybe_inspect_torrent(None, r) is False
    assert r.is_batch is False
    # The cached file path survives a parse failure.
    assert r.torrent_file == "/tmp/rid-a.torrent"


# =============================================================================
# ensure_torrent_cached + cache reuse in maybe_inspect_torrent
# =============================================================================

async def test_ensure_caches_when_missing(monkeypatch):
    async def _fake_fetch(url, rid):
        return "/tmp/rid-a.torrent"

    monkeypatch.setattr(ti, "fetch_torrent_file", _fake_fetch)
    r = _resource()
    assert await ensure_torrent_cached(r) == "/tmp/rid-a.torrent"
    assert r.torrent_file == "/tmp/rid-a.torrent"


async def test_ensure_skips_fetch_when_cache_exists(tmp_path, monkeypatch):
    cached = tmp_path / "rid-a.torrent"
    cached.write_bytes(b"d4:infod4:name4:spam6:lengthi1eee")

    async def _boom(url, rid):  # pragma: no cover - must not be called
        raise AssertionError("fetch_torrent_file called with a live cache")

    monkeypatch.setattr(ti, "fetch_torrent_file", _boom)
    r = _resource(torrent_file=str(cached))
    assert await ensure_torrent_cached(r) == str(cached)
    assert r.torrent_file == str(cached)


async def test_ensure_refetches_when_cache_file_deleted(tmp_path, monkeypatch):
    missing = str(tmp_path / "gone.torrent")

    async def _fake_fetch(url, rid):
        return "/tmp/rid-a.torrent"

    monkeypatch.setattr(ti, "fetch_torrent_file", _fake_fetch)
    r = _resource(torrent_file=missing)
    assert await ensure_torrent_cached(r) == "/tmp/rid-a.torrent"
    assert r.torrent_file == "/tmp/rid-a.torrent"


async def test_ensure_skips_magnet(monkeypatch):
    async def _boom(url, rid):  # pragma: no cover - must not be called
        raise AssertionError("fetch_torrent_file called for magnet")

    monkeypatch.setattr(ti, "fetch_torrent_file", _boom)
    r = _resource(torrent_url="magnet:?xt=urn:btih:abc")
    assert await ensure_torrent_cached(r) is None
    assert r.torrent_file is None


async def test_ensure_fetch_failure_silent(monkeypatch):
    async def _none(url, rid):
        return None

    monkeypatch.setattr(ti, "fetch_torrent_file", _none)
    r = _resource()
    assert await ensure_torrent_cached(r) is None
    assert r.torrent_file is None


async def test_inspect_reuses_existing_cache(tmp_path, monkeypatch):
    """maybe_inspect_torrent must not re-download when the fetch pipeline's
    ensure_torrent_cached already cached the .torrent."""
    cached = tmp_path / "rid-a.torrent"
    cached.write_bytes(b"not-really-parsed")

    async def _boom(url, rid):  # pragma: no cover - must not be called
        raise AssertionError("fetch_torrent_file called with a live cache")

    monkeypatch.setattr(ti, "fetch_torrent_file", _boom)
    monkeypatch.setattr(ti, "parse_torrent_files", lambda p: [
        _f("Show.S01E01.1080p.mkv"),
        _f("Show.S01E02.1080p.mkv"),
    ])
    r = _resource(torrent_file=str(cached))
    assert await maybe_inspect_torrent(None, r) is True
    assert r.is_batch is True
    assert r.batch_scope == "season"
    assert r.torrent_file == str(cached)


# ---------------------------------------------------------------------------
# Frieren BD-box regression: "S01 + S02" pack whose listing uses season
# directories with bare-number filenames ("Frieren S01/01 [VOSTFR].mkv").
# ---------------------------------------------------------------------------


def _frieren_listing() -> list[dict]:
    mb = 500 * 1024 * 1024
    files = [
        {"name": f"Frieren S01/{ep:02d} [VOSTFR] [BDRip 1080p].mkv", "size": mb}
        for ep in range(1, 29)
    ]
    files += [
        {"name": f"Frieren S02/{ep:02d} [VOSTFR] [BDRip 1080p].mkv", "size": mb}
        for ep in range(1, 13)
    ]
    return files


def test_frieren_bd_pack_multi_season_with_ranges():
    report = ti.analyze_torrent_files(_frieren_listing())
    assert report.scope == "multi_season"
    assert report.is_batch is True
    assert report.seasons == [1, 2]
    # Per-season ranges come free from the same parses — no LLM needed.
    assert {"season": 1, "episode_start": 1, "episode_end": 28} in report.season_ranges
    assert {"season": 2, "episode_start": 1, "episode_end": 12} in report.season_ranges
    # Both season dirs normalize to ONE work cluster (same title).
    assert len(report.clusters) == 1
    assert len(report.file_parses) == 40
    sample = report.file_parses[0]
    assert sample["season"] == 1 and sample["episode"] == 1


def test_extract_path_season_from_dir_and_bare_number_filename():
    from app.services.resource_parser import extract_season_episode_from_path

    assert extract_season_episode_from_path(
        "Frieren S01/01 [VOSTFR] [BDRip 1080p].mkv"
    ) == (1, 1)
    assert extract_season_episode_from_path("Frieren S02/12.mkv") == (2, 12)
    # Tech numbers deeper in the name never become episodes.
    assert extract_season_episode_from_path("BDRip 1080p.mkv") == (None, None)
    assert extract_season_episode_from_path("Movie 2001.mkv") == (None, None)


def test_normalize_fields_subtitle_group_and_bare_resolution_fallbacks():
    from app.services.resource_parser import normalize_parsed_fields

    out = normalize_parsed_fields(
        "[Xspitfire911] 葬送的芙莉莲/Sousou No Frieren S01 + S02 BDRIP 1080p X265 10bit VOSTFR",
        {},
    )
    assert out["subtitle_group"] == "Xspitfire911"
    assert out["resolution"] == "1080p"
    assert out["source"] == "BDRip" or out["source"] == "BDRIP"
    # Tech values preserve the casing found in the title ("X265").
    assert (out["video_codec"] or "").lower() == "x265"
    # Pure-number leading brackets are years, not groups.
    year_out = normalize_parsed_fields("[2020] Some Movie.mkv", {})
    assert year_out.get("subtitle_group") != "2020"


def test_apply_auto_assignments_and_season_ranges():
    from types import SimpleNamespace

    import app.services.batch_content_analysis as bca

    class FakeCollection(list):
        pass

    resource = SimpleNamespace(
        id="r1", file_assignments=FakeCollection(), season_ranges=None,
        batch_scope=None,
    )
    report = ti.analyze_torrent_files(_frieren_listing())
    bca.apply_auto_assignments(resource, report)

    assignments = list(resource.file_assignments)
    assert len(assignments) == 40
    first = assignments[0]
    assert first.file_path.startswith("Frieren S01/")
    assert first.season == 1 and first.episode_start == 1
    assert first.source == "auto"
    assert first.work_title_hint == "Frieren"

    ranges = bca.compute_season_ranges(resource)
    assert {"season": 1, "episode_start": 1, "episode_end": 28} in ranges
    assert {"season": 2, "episode_start": 1, "episode_end": 12} in ranges
