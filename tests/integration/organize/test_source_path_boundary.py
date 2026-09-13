"""Captured reviewed filenames with explicit hostile-path fault injection.

Real notification creation and DB planning/execution are used. Only the RPC
edge is replaced. Faults are applied to the frozen snapshot, not corpus gold.
"""

import copy
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.download_task import DownloadTask
from app.models.organize_plan import OrganizePlan
from app.models.series import TVSeries
from app.models.work_collection import WorkCollection
from app.services.notify_service import create_notification_for_task
from app.services.organize_service import OrganizeError, execute_plan, plan_for_notifications
from tests.integration.organize.test_organize_pipeline import (
    TV_TEMPLATE,
    _library,
    _ops,
    _rule,
    _seed_chain,
)
from tests.metadata_corpus.dataset import ROOT, asset, digest, load_corpus, read_json


@pytest.mark.parametrize(
    "scenario",
    [
        "valid_nested",
        "legacy_source",
        "legacy_destination",
        "library_parent",
        "library_symlink",
        "recycle_parent",
        "unrelated_bad_library",
        "parent_swap",
        "parent",
        "absolute",
        "windows_absolute",
        "drive_relative",
        "control",
        "empty",
        "non_string",
        "fallback_mixed",
        "torrent_parent_file",
        "torrent_parent_directory",
        "file_symlink",
        "directory_symlink",
        "scan_symlink",
    ],
)
async def test_snapshot_source_boundary(
    db_session,
    session_factory,
    shared_volume,
    rpc_mocks,
    refresh_mock,
    scenario,
    record_testsuite_property,
):
    manifest, corpus, reviews = load_corpus(ROOT)
    case_id = "f79ef2eb-02d5-42d3-80dc-70dd3c1d733b"
    case = next(c for c in corpus["cases"] if c["id"] == case_id)
    review = reviews[case_id]
    assert review["status"] == "confirmed"
    torrent = asset(ROOT, case["evidence"]["torrent"])
    assert digest(torrent.read_bytes()) == torrent.stem
    listing_file = asset(ROOT, manifest["file_list_file"])
    assert digest(listing_file.read_bytes()) == manifest["file_list_sha256"]
    [entry] = read_json(listing_file)[case["evidence"]["torrent"]]
    [assignment] = review["expected"]["assignments"]
    assert entry["name"] == assignment["file_path"]
    record_testsuite_property("case_id", case_id)
    record_testsuite_property("torrent_sha256", torrent.stem)
    record_testsuite_property("path_fault", scenario)
    record_testsuite_property("media_bytes", "synthetic; reviewed filenames retained")

    collection = WorkCollection(id=str(uuid.uuid4()), title_cn="猫与龙")
    db_session.add(collection)
    work = TVSeries(
        id=str(uuid.uuid4()), title_cn="猫与龙", season_number=1, collection_id=collection.id, number_of_episodes=12
    )
    chain = await _seed_chain(
        db_session,
        work=work,
        resource_kw={
            "title_raw": case["input"]["title_raw"],
            "season": assignment["season"],
            "episode": assignment["episode_start"],
            "is_batch": False,
        },
        download_dir=shared_volume.complete_daemon,
        volume=shared_volume.volume,
        downloader_dir=shared_volume.daemon,
    )
    root = shared_volume.complete_process
    src = root / "nested" / entry["name"]
    src.parent.mkdir()
    src.write_bytes(b"synthetic captured media")
    outside = root.parent / "outside.mkv"
    outside.write_bytes(b"outside sentinel must never move")
    outside_dir = root.parent / "outside-dir"
    outside_dir.mkdir()
    other = outside_dir / entry["name"]
    other.write_bytes(b"outside directory sentinel")
    rpc_mocks.get_files.return_value = {
        "name": None,
        "files": [{"name": "nested/" + entry["name"], "size": entry["size"]}],
    }
    notification, _created = await create_notification_for_task(db_session, chain.task)
    assert notification is not None
    payload = copy.deepcopy(notification.payload)
    bad_names = {
        "parent": "../outside.mkv",
        "absolute": str(outside),
        "windows_absolute": "C:\\outside.mkv",
        "drive_relative": "C:outside.mkv",
        "control": "bad\x00.mkv",
        "empty": "",
        "non_string": 17,
    }
    if scenario in bad_names:
        payload["files"].append({"name": bad_names[scenario], "size": outside.stat().st_size})
    elif scenario == "fallback_mixed":
        payload["files"] = None
        rpc_mocks.get_files.return_value["files"].append({"name": "../outside.mkv", "size": outside.stat().st_size})
    elif scenario == "torrent_parent_file":
        payload["task"]["torrent_name"] = "../outside.mkv"
    elif scenario == "torrent_parent_directory":
        payload["task"]["torrent_name"] = "../outside-dir"
    elif scenario == "file_symlink":
        (root / "linked.mkv").symlink_to(outside)
        payload["files"].append({"name": "linked.mkv", "size": outside.stat().st_size})
    elif scenario == "directory_symlink":
        (root / "linked").symlink_to(outside_dir, target_is_directory=True)
        payload["files"].append({"name": "linked/" + entry["name"], "size": other.stat().st_size})
    elif scenario == "scan_symlink":
        (src.parent / "linked").symlink_to(outside_dir, target_is_directory=True)
        payload["task"]["torrent_name"] = "nested"
        payload["files"] = [{"name": "missing.mkv", "size": 1}]
    notification.payload = payload
    await db_session.commit()
    library = await _library(db_session, shared_volume.media / "tv")
    if scenario == "library_parent":
        library.root_subpath = "../outside-library"
    elif scenario == "library_symlink":
        library_root = shared_volume.media / "tv"
        library_root.mkdir(parents=True)
        (library_root / "linked").symlink_to(outside_dir, target_is_directory=True)
        library.root_subpath = "linked"
    elif scenario == "recycle_parent":
        library.recycle_subpath = "../outside-library"
    elif scenario == "unrelated_bad_library":
        bad = await _library(db_session, shared_volume.media / "bad", name="bad")
        bad.root_subpath = "../outside-library"
    await db_session.commit()
    await _rule(db_session, library.id, TV_TEMPLATE)
    await plan_for_notifications(db_session, [notification])

    async with session_factory() as session:
        plan = (await session.execute(select(OrganizePlan))).scalar_one()
        ops = await _ops(session, plan.id)
        if scenario in {"legacy_source", "legacy_destination", "parent_swap"}:
            assert plan.status == "pending"
            [op] = ops
            retained_source = src
            if scenario == "legacy_source":
                op.src = str(outside)
                await session.commit()
            elif scenario == "legacy_destination":
                op.dst = str(outside_dir / "new-target.mkv")
                await session.commit()
            else:
                original_parent = root / "retained"
                src.parent.rename(original_parent)
                src.parent.symlink_to(outside_dir, target_is_directory=True)
                retained_source = original_parent / src.name
            with pytest.raises(OrganizeError, match="路径"):
                await execute_plan(session, plan.id)
            assert not Path(op.dst).exists()
            assert retained_source.read_bytes() == b"synthetic captured media"
            assert (await session.get(DownloadTask, chain.task.id)).status == "completed"
            rpc_mocks.remove.assert_not_awaited()
            refresh_mock.assert_not_awaited()
        elif scenario in {"valid_nested", "unrelated_bad_library"}:
            assert plan.status == "pending", plan.error_message
            [op] = ops
            assert Path(op.src) == src
            assert "s01e10" in Path(op.dst).name.lower()
            result = await execute_plan(session, plan.id)
            assert result.status == "done", result.error_message
            assert Path(op.dst).read_bytes() == b"synthetic captured media"
            assert not src.exists()
        else:
            assert plan.status == "failed", (plan.status, [(op.src, op.dst) for op in ops])
            assert plan.error_message and "路径" in plan.error_message
            assert not ops
            assert src.read_bytes() == b"synthetic captured media"
            assert (await session.get(DownloadTask, chain.task.id)).status == "completed"
            rpc_mocks.remove.assert_not_awaited()
            refresh_mock.assert_not_awaited()
        assert outside.read_bytes() == b"outside sentinel must never move"
        assert other.read_bytes() == b"outside directory sentinel"
