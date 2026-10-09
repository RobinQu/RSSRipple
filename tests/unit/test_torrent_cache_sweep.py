"""Unit tests for the torrent cache lifecycle sweep (``sweep_torrent_cache``).

A cache file ``<resource_id>-<suffix>.torrent`` under
``settings.torrent_cache_dir`` is deleted only when it is older than the
in-flight grace window AND is unreferenced: its resource id is gone from
``file_resources`` (orphan) or the resource's ``torrent_file`` now points at
a different path (superseded version). DB-referenced files, young files,
legacy ``<resource_id>.torrent`` names, ``.tmp`` files and foreign files are
never touched; a DB failure aborts the whole round with nothing deleted.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

import pytest

import app.services.torrent_inspect as ti
from app.models.file_resource import FileResource
from app.services.torrent_inspect import sweep_torrent_cache


def _uuid() -> str:
    return str(uuid.uuid4())


def _cache_file(cache_dir: Path, rid: str, suffix: str, *, age_seconds: float = 7200) -> Path:
    """Write a cache-shaped file with a backdated mtime (default: past grace)."""
    path = cache_dir / f"{rid}-{suffix}.torrent"
    path.write_bytes(b"x")
    old = time.time() - age_seconds
    os.utime(path, (old, old))
    return path


def _make_resource(channel_id: str, *, torrent_file: str | None = None) -> FileResource:
    return FileResource(
        id=_uuid(),
        channel_id=channel_id,
        guid=f"guid-{_uuid()}",
        title_raw="[Group] Show - 01 [1080p]",
        torrent_url="magnet:?xt=urn:btih:abc",
        torrent_file=torrent_file,
    )


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(ti.settings, "torrent_cache_dir", str(tmp_path))
    return tmp_path


@pytest.mark.asyncio
async def test_orphan_file_deleted(db_session, sample_channel, cache_dir):
    """resource_id absent from file_resources -> file removed."""
    orphan = _cache_file(cache_dir, _uuid(), "a" * 64)
    report = await sweep_torrent_cache(db_session)
    assert report["deleted"] == 1
    assert report["aborted"] is False
    assert not orphan.exists()


@pytest.mark.asyncio
async def test_db_referenced_file_kept(db_session, sample_channel, cache_dir):
    """The authoritative torrent_file reference is never deleted, any age."""
    referenced = _cache_file(cache_dir, _uuid(), "b" * 64, age_seconds=30 * 86400)
    resource = _make_resource(sample_channel.id, torrent_file=str(referenced))
    # The filename must carry the resource's real id.
    renamed = referenced.with_name(f"{resource.id}-{'b' * 64}.torrent")
    referenced.rename(renamed)
    resource.torrent_file = str(renamed)
    db_session.add(resource)
    await db_session.commit()

    report = await sweep_torrent_cache(db_session)
    assert report["deleted"] == 0
    assert renamed.exists()


@pytest.mark.asyncio
async def test_grace_age_protects_recent_orphan(db_session, sample_channel, cache_dir):
    """A young orphan (in-flight write/commit window) survives the sweep."""
    fresh = _cache_file(cache_dir, _uuid(), "c" * 64, age_seconds=60)
    report = await sweep_torrent_cache(db_session)
    assert report["scanned"] == 0
    assert report["deleted"] == 0
    assert fresh.exists()


@pytest.mark.asyncio
async def test_superseded_version_deleted_current_kept(
    db_session, sample_channel, cache_dir
):
    """Same resource, two cached versions: only the unreferenced one goes."""
    resource = _make_resource(sample_channel.id)
    old = _cache_file(cache_dir, resource.id, "d" * 64)
    current = _cache_file(cache_dir, resource.id, "e" * 64)
    resource.torrent_file = str(current)
    db_session.add(resource)
    await db_session.commit()

    report = await sweep_torrent_cache(db_session)
    assert report["deleted"] == 1
    assert not old.exists()
    assert current.exists()


@pytest.mark.asyncio
async def test_resource_without_reference_file_deleted(
    db_session, sample_channel, cache_dir
):
    """Resource alive but torrent_file NULL -> the cached file is unreferenced."""
    resource = _make_resource(sample_channel.id, torrent_file=None)
    db_session.add(resource)
    await db_session.commit()
    stale = _cache_file(cache_dir, resource.id, "f" * 64)

    report = await sweep_torrent_cache(db_session)
    assert report["deleted"] == 1
    assert not stale.exists()


@pytest.mark.asyncio
async def test_db_failure_aborts_round(db_session, sample_channel, cache_dir, monkeypatch):
    """Reference lookup failure -> nothing is deleted, round marked aborted."""
    orphan = _cache_file(cache_dir, _uuid(), "0" * 64)

    async def _boom(db, ids):
        raise RuntimeError("db down")

    monkeypatch.setattr(ti, "_sweep_referenced_paths", _boom)
    report = await sweep_torrent_cache(db_session)
    assert report["aborted"] is True
    assert report["deleted"] == 0
    assert orphan.exists()


@pytest.mark.asyncio
async def test_tmp_legacy_and_foreign_files_untouched(
    db_session, sample_channel, cache_dir
):
    """Non-cache shapes are never candidates, however old."""
    rid = _uuid()
    tmp_file = cache_dir / f".{uuid.uuid4().hex}.torrent.tmp"
    legacy = cache_dir / f"{rid}.torrent"
    foreign = cache_dir / "notes.txt"
    weird = cache_dir / "not-a-uuid-x.torrent"
    old = time.time() - 30 * 86400
    for path in (tmp_file, legacy, foreign, weird):
        path.write_bytes(b"x")
        os.utime(path, (old, old))

    report = await sweep_torrent_cache(db_session)
    assert report["scanned"] == 0
    assert report["deleted"] == 0
    for path in (tmp_file, legacy, foreign, weird):
        assert path.exists()


@pytest.mark.asyncio
async def test_per_file_delete_failure_continues(
    db_session, sample_channel, cache_dir, monkeypatch
):
    """One undeletable file is logged/counted; the other orphans still go."""
    victim = _cache_file(cache_dir, _uuid(), "1" * 64)
    gone = _cache_file(cache_dir, _uuid(), "2" * 64)

    real_unlink = Path.unlink

    def _flaky(self, *args, **kwargs):
        if self == victim:
            raise OSError("permission denied")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _flaky)
    report = await sweep_torrent_cache(db_session)
    assert report["errors"] == 1
    assert report["deleted"] == 1
    assert victim.exists()
    assert not gone.exists()


@pytest.mark.asyncio
async def test_reference_in_other_dir_does_not_protect_cache_file(
    db_session, sample_channel, cache_dir, tmp_path
):
    """A historical torrent_file outside the cache dir is left alone, and the
    same-resource cache file it replaced is still swept."""
    resource = _make_resource(sample_channel.id)
    external = tmp_path / "elsewhere" / f"{resource.id}.torrent"
    external.parent.mkdir()
    external.write_bytes(b"x")
    resource.torrent_file = str(external)
    db_session.add(resource)
    await db_session.commit()
    stale = _cache_file(cache_dir, resource.id, "3" * 64)

    report = await sweep_torrent_cache(db_session)
    assert report["deleted"] == 1
    assert not stale.exists()
    assert external.exists()
