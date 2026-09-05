"""Unit tests for scripts/batch_sp_rescan.py.

Covers candidate selection (batch_seasons containing season 0), the pure
evaluation decision (season rewrite / multi_season refresh / never
downgrade / idempotent skip), cached-listing reading, and the apply path
(assignments + season_ranges rebuild, idempotent re-run).
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import bencodepy
import pytest

from app.services.torrent_inspect import TorrentReport
from scripts.batch_sp_rescan import (
    REWRITE,
    SKIP,
    apply_evaluation,
    evaluate_resource,
    read_cached_listing,
    select_candidates,
)

MB = 1024 * 1024


def _multi_torrent(entries: list[tuple[list[str], int]]) -> bytes:
    """Build a multi-file torrent from (path components, length) pairs."""
    return bencodepy.encode({
        b"info": {
            b"name": b"root",
            b"files": [
                {b"path": [c.encode() for c in comps], b"length": length}
                for comps, length in entries
            ],
            b"piece length": 16384,
            b"pieces": b"x" * 20,
        }
    })


def _shigatsu_listing() -> list[tuple[list[str], int]]:
    """22x S01 + 1x S00E01 — the pack mis-stored as multi_season [0, 1]."""
    entries = [
        (["Shigatsu S01", f"Shigatsu wa Kimi no Uso S01E{ep:02d}.mkv"], 500 * MB)
        for ep in range(1, 23)
    ]
    entries.append(
        (["Shigatsu S01", "Shigatsu wa Kimi no Uso S00E01.mkv"], 500 * MB)
    )
    return entries


def _resource(**overrides) -> SimpleNamespace:
    base = {
        "is_batch": True,
        "batch_scope": "multi_season",
        "season": None,
        "episode": None,
        "episode_start": None,
        "episode_end": None,
        "batch_seasons": [0, 1],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# =============================================================================
# select_candidates
# =============================================================================

def test_select_candidates_only_season_zero_coverage():
    hit = _resource(batch_seasons=[0, 1])
    assert select_candidates([
        hit,
        _resource(batch_seasons=[1, 2]),
        _resource(batch_seasons=None),
        _resource(batch_seasons=[0, 1], is_batch=False),
    ]) == [hit]


# =============================================================================
# evaluate_resource
# =============================================================================

def test_evaluate_season_rewrite():
    report = TorrentReport(
        scope="season", is_batch=True, episode_start=1, episode_end=22,
        seasons=[0, 1],
    )
    ev = evaluate_resource(_resource(), report)
    assert ev.action == REWRITE
    assert ev.changes == {
        "batch_scope": "season",
        "episode_start": 1,
        "episode_end": 22,
        "batch_seasons": None,
    }


def test_evaluate_multi_season_refreshes_batch_seasons():
    report = TorrentReport(scope="multi_season", is_batch=True, seasons=[0, 1, 2])
    ev = evaluate_resource(_resource(batch_seasons=[0, 1]), report)
    assert ev.action == REWRITE
    assert ev.changes == {"batch_seasons": [0, 1, 2]}


def test_evaluate_multi_season_unchanged_skips():
    report = TorrentReport(scope="multi_season", is_batch=True, seasons=[0, 1, 2])
    ev = evaluate_resource(_resource(batch_seasons=[0, 1, 2]), report)
    assert ev.action == SKIP


@pytest.mark.parametrize("scope", ["single", "unknown", "franchise"])
def test_evaluate_never_downgrades(scope):
    report = TorrentReport(scope=scope, is_batch=scope == "franchise")
    ev = evaluate_resource(_resource(), report)
    assert ev.action == SKIP
    assert "never downgraded" in ev.reason


# =============================================================================
# read_cached_listing
# =============================================================================

def test_read_cached_listing_without_cache(tmp_path):
    assert read_cached_listing(SimpleNamespace(torrent_file=None)) is None
    missing = str(tmp_path / "gone.torrent")
    assert read_cached_listing(SimpleNamespace(torrent_file=missing)) is None


def test_read_cached_listing_parses_cached_torrent(tmp_path):
    p = tmp_path / "cached.torrent"
    p.write_bytes(_multi_torrent(_shigatsu_listing()))
    files = read_cached_listing(SimpleNamespace(torrent_file=str(p)))
    assert files is not None and len(files) == 23


# =============================================================================
# apply path (DB): rewrite + rebuild, idempotent re-run
# =============================================================================

async def test_apply_season_rewrite_rebuilds_assignments_and_is_idempotent(
    db_session, sample_channel, tmp_path,
):
    from app.models.file_resource import FileResource
    from app.services.torrent_inspect import analyze_torrent_files

    p = tmp_path / "cached.torrent"
    p.write_bytes(_multi_torrent(_shigatsu_listing()))
    resource = FileResource(
        id=str(uuid.uuid4()), channel_id=sample_channel.id, guid=str(uuid.uuid4()),
        title_raw="[7³ACG] 四月是你的谎言 S01 | 01-22+SPx1",
        torrent_url="https://x/shigatsu.torrent", torrent_file=str(p),
        is_batch=True, batch_scope="multi_season", batch_seasons=[0, 1],
    )
    db_session.add(resource)
    await db_session.commit()

    report = analyze_torrent_files(read_cached_listing(resource))
    assert report.scope == "season"

    ev = evaluate_resource(resource, report)
    assert ev.action == REWRITE
    await db_session.refresh(resource, ["file_assignments"])
    apply_evaluation(resource, report, ev)
    await db_session.commit()

    assert resource.batch_scope == "season"
    assert resource.episode is None
    assert resource.episode_start == 1 and resource.episode_end == 22
    assert resource.batch_seasons is None
    # SP file keeps its season-0 placement for downstream assignments.
    assert {"season": 0, "episode_start": 1, "episode_end": 1} in resource.season_ranges
    assert {"season": 1, "episode_start": 1, "episode_end": 22} in resource.season_ranges
    assert len(resource.file_assignments) == 23
    sp = next(a for a in resource.file_assignments if "S00E01" in a.file_path)
    assert sp.season == 0 and sp.episode_start == 1

    # Re-running on the rewritten row changes nothing.
    ev2 = evaluate_resource(resource, report)
    assert ev2.action == SKIP
    apply_evaluation(resource, report, ev2)
    await db_session.commit()
    assert len(resource.file_assignments) == 23
    assert resource.batch_scope == "season"


# =============================================================================
# rescan() end-to-end (dry-run + apply) against the test database
# =============================================================================

async def test_rescan_apply_end_to_end(db_session, sample_channel, tmp_path, capsys):
    from app.models.file_resource import FileResource
    from scripts.batch_sp_rescan import rescan

    p = tmp_path / "cached.torrent"
    p.write_bytes(_multi_torrent(_shigatsu_listing()))
    hit = FileResource(
        id=str(uuid.uuid4()), channel_id=sample_channel.id, guid=str(uuid.uuid4()),
        title_raw="[7³ACG] 四月是你的谎言 S01 | 01-22+SPx1",
        torrent_url="https://x/shigatsu.torrent", torrent_file=str(p),
        is_batch=True, batch_scope="multi_season", batch_seasons=[0, 1],
    )
    no_cache = FileResource(
        id=str(uuid.uuid4()), channel_id=sample_channel.id, guid=str(uuid.uuid4()),
        title_raw="Show S01+SP no cache", torrent_url="https://x/gone.torrent",
        is_batch=True, batch_scope="multi_season", batch_seasons=[0, 1],
    )
    db_session.add_all([hit, no_cache])
    await db_session.commit()

    await rescan(apply=False, limit=None)
    out = capsys.readouterr().out
    assert "dry-run" in out and "rewrite=1" in out and "no_torrent_cache=1" in out
    await db_session.refresh(hit)
    assert hit.batch_scope == "multi_season"  # dry-run writes nothing

    await rescan(apply=True, limit=None)
    out = capsys.readouterr().out
    assert "rewrite=1" in out and "committed." in out
    await db_session.refresh(hit)
    assert hit.batch_scope == "season"
    assert hit.episode_start == 1 and hit.episode_end == 22
    assert hit.batch_seasons is None

    # Second apply pass: the rewritten row is no longer a candidate.
    await rescan(apply=True, limit=None)
    out = capsys.readouterr().out
    assert "rewrite=0" in out and "no_torrent_cache=1" in out
