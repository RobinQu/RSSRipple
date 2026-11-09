from types import SimpleNamespace

import pytest

from app.models.resource_file_assignment import ResourceFileAssignment
from app.services.batch_content_analysis import (
    apply_auto_assignments,
    compute_season_ranges,
)
from app.services.file_roles import assignment_file_role, classify_file
from app.services.torrent_inspect import analyze_torrent_files

MB = 1024 * 1024


def _f(name: str, size: int = 500 * MB) -> dict:
    return {"name": name, "size": size}


@pytest.mark.parametrize("path", [
    "[VCB-Studio] Show [NCOP_01][BDRip 1080p].mkv",
    "[VCB-Studio] Show [NCED_01][BDRip 1080p].mkv",
    "Show/OP/Show OP.mkv",
    "Show/ED/Show ED2.mkv",
    "Show/PV/PV01.mp4",
    "Show/CM/CM_01.webm",
    "Show/MENU/Menu_01.mkv",
    "Show/特典/Interview 01.mkv",
])
def test_real_extra_video_names_and_directories(path):
    assert classify_file(path) == "extra"
    assert assignment_file_role(path) == "extra"


def test_shared_classifier_distinguishes_all_manifest_kinds():
    assert classify_file("Show/S01/Show S01E01.mkv") == "video"
    assert classify_file("Show/S01/Show S01E01.chs.ass") == "subtitle"
    assert classify_file("Show/Scans/cover.jpg") == "extra"
    assert classify_file("Show/fonts.zip") == "other"
    assert assignment_file_role("Show/fonts.zip") == "excluded"


def test_auto_analysis_builds_role_aware_manifest_and_unique_subtitle_links():
    report = analyze_torrent_files([
        _f("Show/S01/Show.S01E01.1080p.mkv"),
        _f("Show/S01/Show.S01E02.1080p.mkv"),
        _f("Show/S01/Show.S01E01.chs.ass", 100_000),
        _f("Show/S01/Show.S01E02.cht.srt", 100_000),
        _f("Show/NCOP/[VCB-Studio] Show [NCOP_01].mkv"),
        _f("Show/Scans/cover.jpg", 2 * MB),
        _f("Show/fonts.zip", 5 * MB),
    ])
    resource = SimpleNamespace(
        id="r1", file_assignments=[], subtitle_group="VCB-Studio",
        subtitle_groups=["VCB-Studio"], subtitle_groups_source="manual",
    )

    apply_auto_assignments(resource, report)

    rows = {row.file_path: row for row in resource.file_assignments}
    assert rows["Show/S01/Show.S01E01.1080p.mkv"].file_role == "episode"
    assert rows["Show/S01/Show.S01E01.chs.ass"].file_role == "subtitle"
    assert rows["Show/S01/Show.S01E01.chs.ass"].attached_to_path == (
        "Show/S01/Show.S01E01.1080p.mkv"
    )
    assert rows["Show/S01/Show.S01E02.cht.srt"].attached_to_path == (
        "Show/S01/Show.S01E02.1080p.mkv"
    )
    assert rows["Show/NCOP/[VCB-Studio] Show [NCOP_01].mkv"].file_role == "extra"
    assert rows["Show/Scans/cover.jpg"].file_role == "extra"
    assert rows["Show/fonts.zip"].file_role == "excluded"
    assert rows["Show/fonts.zip"].series_id is None
    assert compute_season_ranges(resource) == [
        {"season": 1, "episode_start": 1, "episode_end": 2}
    ]


def test_subtitle_matching_uses_unique_normalized_main_filename():
    report = analyze_torrent_files([
        _f("Show/Omake.mkv"),
        _f("Show/Omake.chs.ass", 100_000),
    ])
    resource = SimpleNamespace(
        id="r2", file_assignments=[], subtitle_group="G", subtitle_groups=["G"],
    )
    apply_auto_assignments(resource, report)
    subtitle = next(
        row for row in resource.file_assignments if row.file_role == "subtitle"
    )
    assert subtitle.attached_to_path == "Show/Omake.mkv"


def test_ambiguous_subtitle_episode_and_filename_never_guess():
    report = analyze_torrent_files([
        _f("A/Show.S01E01.mkv"),
        _f("B/Show.S01E01.mkv"),
        _f("Show.S01E01.chs.ass", 100_000),
        _f("A/Track.mkv"),
        _f("B/Track.mkv"),
        _f("Track.chs.ass", 100_000),
    ])
    resource = SimpleNamespace(
        id="r3", file_assignments=[], subtitle_group="G", subtitle_groups=["G"],
    )
    apply_auto_assignments(resource, report)
    subtitles = [
        row for row in resource.file_assignments if row.file_role == "subtitle"
    ]
    assert len(subtitles) == 2
    assert {row.attached_to_path for row in subtitles} == {None}


def test_manual_subtitle_attachment_is_not_overwritten():
    manual = ResourceFileAssignment(
        resource_id="r4", file_path="Show/S01E01.chs.ass",
        file_role="subtitle", attached_to_path="manual-choice.mkv",
        file_size=123, source="manual",
    )
    resource = SimpleNamespace(
        id="r4", file_assignments=[manual], subtitle_group="G", subtitle_groups=["G"],
    )
    report = analyze_torrent_files([
        _f("Show/S01E01.mkv"),
        _f("Show/S01E01.chs.ass", 100_000),
    ])
    apply_auto_assignments(resource, report)
    assert manual.attached_to_path == "manual-choice.mkv"
    assert manual.file_size == 123
    assert manual.source == "manual"
